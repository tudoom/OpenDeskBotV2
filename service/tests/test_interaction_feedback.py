from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_build_servo_only_pb_payload_no_audio():
    from deskbot_server.application import interaction_feedback as feedback

    built = feedback.build_servo_only_pb_payload(
        [
            {
                "move": "__custom__",
                "xm": 0,
                "ym": 0,
                "x": 120,
                "y": 90,
                "ms": 2800,
            }
        ],
        request_id="abc123",
    )

    assert built is not None
    payload, request_id = built
    assert request_id == "abc123"
    assert payload["type"] == "pb_single"
    assert payload.get("audio") is None
    assert payload["chunk_ms"] == 2800
    assert payload["action"] == "replace"
    assert payload["level"] == 1
    assert len(payload["servo"]) == 1


def test_empty_servo_move_list_is_rejected():
    from deskbot_server.application import interaction_feedback as feedback

    assert (
        feedback.build_servo_only_pb_payload([])
        is None
    )


def test_servo_send_waits_for_terminal_played_ack(monkeypatch):
    from deskbot_server.application import interaction_feedback as feedback
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run() -> None:
        published: list[dict] = []

        class Hub:
            pipeline_broker = SimpleNamespace()

            async def send(self, device_id: str, payload: dict) -> int:
                await pb_ack_gate.notify(
                    device_id,
                    {"req": payload["req"], "idx": 0, "phase": "accepted"},
                )
                await pb_ack_gate.notify(
                    device_id,
                    {"req": payload["req"], "idx": 0, "phase": "played"},
                )
                return 1

        async def fake_publish(_broker, **kwargs):
            published.append(kwargs)

        monkeypatch.setattr(
            "deskbot_server.ws.device_pipeline.publish_auto_dispatch_event",
            fake_publish,
        )
        result = await feedback.send_servo_moves_and_wait(
            Hub(),
            "played-dev",
            [{"move": "center", "ms": 200}],
            source="rtc_tool_move",
            summary="center",
            request_id="played-req",
            accepted_timeout=0.1,
            played_timeout=0.1,
        )

        assert result.ok is True
        assert result.status == "played"
        assert result.accepted is result.played is True
        assert result.request_id == "played-req"
        assert published[0]["status"] == "ok"
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


@pytest.mark.parametrize("terminal_phase", ["failed", "cancelled"])
def test_servo_send_surfaces_terminal_failure(monkeypatch, terminal_phase):
    from deskbot_server.application import interaction_feedback as feedback
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run() -> None:
        published: list[dict] = []

        class Hub:
            pipeline_broker = SimpleNamespace()

            async def send(self, device_id: str, payload: dict) -> int:
                await pb_ack_gate.notify(
                    device_id,
                    {"req": payload["req"], "idx": 0, "phase": "accepted"},
                )
                await pb_ack_gate.notify(
                    device_id,
                    {"req": payload["req"], "idx": 0, "phase": terminal_phase},
                )
                return 1

        async def fake_publish(_broker, **kwargs):
            published.append(kwargs)

        monkeypatch.setattr(
            "deskbot_server.ws.device_pipeline.publish_auto_dispatch_event",
            fake_publish,
        )
        result = await feedback.send_servo_moves_and_wait(
            Hub(),
            "terminal-dev",
            [{"move": "center", "ms": 200}],
            source="rtc_tool_move",
            summary="center",
            request_id=f"{terminal_phase}-req",
            accepted_timeout=0.1,
            played_timeout=0.1,
        )

        assert result.ok is False
        assert result.accepted is True
        assert result.played is False
        assert result.status == terminal_phase
        assert published[0]["status"] == "error"
        assert published[0]["error"] == terminal_phase
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_servo_send_surfaces_disconnect(monkeypatch):
    from deskbot_server.application import interaction_feedback as feedback
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run() -> None:
        class Hub:
            pipeline_broker = SimpleNamespace()

            async def send(self, device_id: str, _payload: dict) -> int:
                await pb_ack_gate.cancel_device(device_id)
                return 1

        async def fake_publish(_broker, **_kwargs):
            return None

        monkeypatch.setattr(
            "deskbot_server.ws.device_pipeline.publish_auto_dispatch_event",
            fake_publish,
        )
        result = await feedback.send_servo_moves_and_wait(
            Hub(),
            "disconnect-dev",
            [{"move": "center", "ms": 200}],
            source="rtc_tool_move",
            summary="center",
            accepted_timeout=1.0,
            played_timeout=1.0,
        )

        assert result.ok is False
        assert result.status == "disconnected"
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_removed_automatic_feedback_entrypoints_stay_absent():
    from deskbot_server.application import interaction_feedback as feedback

    for removed in (
        "schedule_listen_feedback",
        "start_llm_wait_nod_feedback",
        "stop_llm_wait_nod_feedback",
        "llm_wait_nod_feedback_loop",
        "maybe_send_listen_feedback",
        "note_face_analysis",
        "clear_face_analysis",
    ):
        assert not hasattr(feedback, removed)
