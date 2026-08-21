from __future__ import annotations

import pytest

from deskbot_server.llm.utils import llm_pb_moves_prompt_appendix
from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas
from deskbot_server.servo_config_store import (
    normalize_servo_document,
    servo_model_preset_ids,
)


def _document(presets: list[dict]) -> dict:
    return {
        "xMin": 10,
        "xMax": 170,
        "yMin": 70,
        "yMax": 110,
        "xReverse": 0,
        "yReverse": 0,
        "perspective": "viewer",
        "presets": presets,
    }


def _preset(preset_id: str, *, y: int = 90) -> dict:
    return {
        "id": preset_id,
        "label": preset_id,
        "steps": [{"x": 90, "y": y, "xm": 0, "ym": 0, "ms": 400}],
    }


def test_catalog_rejects_case_insensitive_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate preset id"):
        normalize_servo_document(
            _document([_preset("nod_head"), _preset("NOD_HEAD")]),
            require_presets=True,
        )


def test_catalog_rejects_absolute_steps_outside_configured_limits():
    with pytest.raises(ValueError, match=r"absolute step y=135 outside \[70, 110\]"):
        normalize_servo_document(
            _document([_preset("look_up", y=135)]),
            require_presets=True,
        )


def test_internal_pose_presets_are_not_model_visible():
    visible = servo_model_preset_ids()
    assert "nod_head" in visible
    assert visible
    assert all(not preset_id.startswith("pose_") for preset_id in visible)


def test_llm_and_rtc_use_the_same_model_visible_catalog():
    visible = servo_model_preset_ids()
    appendix = llm_pb_moves_prompt_appendix()
    move_schema = next(
        schema for schema in build_rtc_tool_schemas() if schema["name"] == "move_head"
    )
    enum = tuple(move_schema["parameters"]["properties"]["move"]["enum"])
    assert enum == visible
    assert "pose_x" not in appendix
    for preset_id in visible:
        assert preset_id in appendix


def test_rtc_omits_move_tool_when_catalog_has_no_model_visible_presets(monkeypatch):
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    monkeypatch.setattr(
        "deskbot_server.servo_config_store.load_servo_cfg_file",
        lambda: {"presets": []},
    )
    assert all(
        schema.get("name") != "move_head"
        for schema in build_rtc_tool_schemas()
    )


def test_rtc_move_schema_uses_representable_step_duration_minimum(monkeypatch):
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    monkeypatch.setattr(
        "deskbot_server.servo_config_store.load_servo_cfg_file",
        lambda: {
            "xMin": 10,
            "xMax": 170,
            "yMin": 70,
            "yMax": 110,
            "xReverse": 0,
            "yReverse": 0,
            "presets": [
                {
                    "id": "full_range",
                    "label": "full range",
                    "exposeToModel": True,
                    "steps": [
                        {"xm": 0, "ym": 0, "x": 10, "y": 70, "ms": 50}
                    ],
                }
            ],
        },
    )
    schema = next(
        item for item in build_rtc_tool_schemas() if item["name"] == "move_head"
    )
    duration = schema["parameters"]["properties"]["duration_ms"]
    assert duration["minimum"] == 200
