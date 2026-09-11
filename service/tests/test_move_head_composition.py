from __future__ import annotations

import asyncio

import pytest


class _Hub:
    pass


@pytest.fixture()
def captured(monkeypatch):
    """Intercept the servo dispatch so nothing reaches a device."""
    from deskbot_server.application import rtc_tool_service as rts

    box: dict = {}

    async def fake_send(hub, device_id, moves, **kwargs):
        box["moves"] = moves
        box["source"] = kwargs.get("source")

        class Delivery:
            ok = True
            status = "played"
            request_id = "req"
            delivered = 1
            accepted = 1
            played = 1

        return Delivery()

    monkeypatch.setattr(rts, "send_servo_moves_and_wait", fake_send)
    return box


def _move(**arguments):
    from deskbot_server.application import rtc_tool_service as rts

    return asyncio.run(
        rts._move_head(
            device_id="dev",
            arguments=arguments,
            asr_chat_hub=_Hub(),
            call_id="call-1",
        )
    )


def test_composed_steps_run_when_no_preset_fits(captured):
    """Layer two: the model builds a motion the catalog does not cover."""
    result = _move(
        steps=[
            {"x": 90, "y": 80, "xm": 0, "ym": 0, "ms": 300},
            {"y": 15, "ym": 1, "xm": 2, "ms": 300},
        ]
    )

    assert result["ok"] is True
    assert result["move"] == "composed"
    assert result["steps"] == 2
    assert result["duration_ms"] == 600
    # 走协议层给显式步骤留的通道，限位与分片照旧生效。
    assert [m["move"] for m in captured["moves"]] == ["__custom__", "__custom__"]
    assert captured["source"] == "rtc_tool_move_composed"


def test_a_preset_still_wins_over_composition(captured, monkeypatch):
    """Layer one stays the default: presets are tuned, composition is not."""
    from deskbot_server.application import rtc_tool_service as rts

    monkeypatch.setattr(
        rts,
        "load_servo_cfg_file",
        lambda: {
            "presets": [
                {
                    "id": "nod_head",
                    "exposeToModel": True,
                    "steps": [{"x": 0, "y": 15, "xm": 1, "ym": 1, "ms": 300}],
                }
            ]
        },
    )

    result = _move(move="nod_head")
    assert result["ok"] is True
    assert result["move"] == "nod_head"
    assert captured["source"] == "rtc_tool_move"


def test_composition_is_capped_so_it_cannot_become_a_program(captured):
    result = _move(steps=[{"ms": 100}] * 6)
    assert result["ok"] is False
    assert result["status"] == "invalid_move"
    assert "5" in result["error"]
    assert "moves" not in captured, "越界的编排不能下发到设备"


def test_total_duration_is_bounded(captured):
    result = _move(steps=[{"ms": 8000}, {"ms": 8000}])
    assert result["ok"] is False
    assert result["status"] == "invalid_move"
    assert "moves" not in captured


def test_a_malformed_step_is_refused(captured):
    result = _move(steps=["not-an-object"])
    assert result["ok"] is False
    assert result["status"] == "invalid_move"
    assert "moves" not in captured


def test_neither_preset_nor_steps_is_an_error(captured):
    result = _move()
    assert result["ok"] is False
    assert "preset" in result["error"] or "steps" in result["error"]


def test_schema_offers_both_layers_without_forcing_either():
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    schema = next(s for s in build_rtc_tool_schemas() if s["name"] == "move_head")
    props = schema["parameters"]["properties"]
    assert "move" in props and "steps" in props
    assert props["steps"]["maxItems"] == 5
    # move 不再是必填，否则自编排那条路走不通。
    assert "move" not in schema["parameters"].get("required", [])
