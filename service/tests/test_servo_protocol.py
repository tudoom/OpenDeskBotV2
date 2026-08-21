from __future__ import annotations

import pytest

from deskbot_server.pb.llm_plan import (
    _scale_ms_values,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
)
from deskbot_server.pb.servo_pcm import (
    PB_CHUNK_MS_MAX,
    make_anim_item,
    merge_pb_subchunks,
    pb_json_messages,
)
from deskbot_server.servo_config_store import (
    logical_step_to_protocol,
    normalize_servo_document,
)
from deskbot_server.servo_protocol import (
    ServoProtocolError,
    pb_sequence_completion_budget_ms,
    servo_sequence_completion_budget_ms,
    validate_servo_steps,
)


def _preset(preset_id: str, count: int = 1) -> dict:
    return {
        "id": preset_id,
        "label": preset_id,
        "steps": [
            {"xm": 1, "ym": 1, "x": 1, "y": 0, "ms": 50}
            for _ in range(count)
        ],
    }


def _catalog(*presets: dict) -> dict:
    return {
        "xMin": 10,
        "xMax": 170,
        "yMin": 70,
        "yMax": 110,
        "xReverse": 0,
        "yReverse": 0,
        "perspective": "robot",
        "presets": list(presets),
    }


def _elements(marker: int = 0) -> dict:
    return {
        "mouth": [],
        "eye_l": [],
        "eye_r": [],
        "nose": [],
        "extra": [{"shape": "circle", "x": marker, "y": 1, "r": 1}],
    }


def test_duration_scaler_rejects_unrepresentable_targets():
    with pytest.raises(ValueError, match="below"):
        _scale_ms_values([400, 400], 79, minimum_ms=40)
    with pytest.raises(ValueError, match="exceeds"):
        _scale_ms_values([400], 30_001)
    assert _scale_ms_values(
        [400, 400, 400],
        151,
        minimum_ms=50,
        maximum_ms=None,
    ) == [51, 50, 50]


def test_expand_motion_plan_loads_one_snapshot_and_rejects_over_32(monkeypatch):
    import deskbot_server.pb.llm_plan as plan

    calls = 0
    catalog = _catalog(_preset("a", 17), _preset("b", 17))

    def _load():
        nonlocal calls
        calls += 1
        return catalog

    monkeypatch.setattr(plan, "load_servo_cfg_file", _load)
    with pytest.raises(ServoProtocolError, match="exceeds 32"):
        expand_llm_moves(
            [
                {"move": "a", "ms": 850},
                {"move": "b", "ms": 850},
            ]
        )
    assert calls == 1


def test_expand_motion_plan_rejects_target_below_step_minimum(monkeypatch):
    import deskbot_server.pb.llm_plan as plan

    monkeypatch.setattr(
        plan,
        "load_servo_cfg_file",
        lambda: _catalog(_preset("two", 2)),
    )
    with pytest.raises(ServoProtocolError, match="cannot fit"):
        expand_llm_moves([{"move": "two", "ms": 99}])


def test_three_lane_union_keeps_json_only_tail_and_atomic_servo_batch():
    audio = [{"phoneme": "a", "ms": 1000, "pcm": b"\x01\x00" * 24_000}]
    moves = [
        {"xm": 1, "ym": 1, "x": 1, "y": 0, "ms": 500},
        {"xm": 1, "ym": 1, "x": 2, "y": 0, "ms": 1000},
        {"xm": 1, "ym": 1, "x": 3, "y": 0, "ms": 500},
    ]
    anims = [{"ms": 3000, "elements": _elements(7)}]

    segs, parallel_servo, parallel_anim = interleave_tts_segs_with_llm_plan(
        audio,
        moves,
        anims,
        24_000,
    )

    assert sum(segment["ms"] for segment in segs) == 3000
    assert [segment["ms"] for segment in segs] == [1000, 1000, 1000]
    assert segs[0]["pcm"]
    assert segs[1]["pcm"] == segs[2]["pcm"] == b""
    assert parallel_servo[0] == moves
    assert parallel_servo[1:] == [None, None]
    assert all(frame is not None for frame in parallel_anim)


def test_long_plain_pcm_row_is_really_split_without_losing_samples():
    row_ms = PB_CHUNK_MS_MAX * 2 + 1234
    pcm = b"\x01\x00" * (row_ms * 24)
    rows, parts = merge_pb_subchunks(
        [
            {
                "chunk_ms": row_ms,
                "anim": [make_anim_item(_elements(), row_ms, phoneme="a")],
            }
        ],
        [pcm],
        sample_rate=24_000,
    )
    assert [row["chunk_ms"] for row in rows] == [
        PB_CHUNK_MS_MAX,
        PB_CHUNK_MS_MAX,
        1234,
    ]
    assert all(row["chunk_ms"] <= PB_CHUNK_MS_MAX for row in rows)
    assert b"".join(parts) == pcm


def test_merge_splits_before_servo_count_budget():
    command = {"xm": 1, "ym": 1, "x": 0, "y": 0, "ms": 50}
    row = {
        "chunk_ms": 1000,
        "anim": [make_anim_item(_elements(), 1000)],
        "servo": [dict(command) for _ in range(20)],
    }
    rows, _parts = merge_pb_subchunks(
        [row, row],
        [b"", b""],
        sample_rate=24_000,
    )
    assert [len(item.get("servo") or []) for item in rows] == [20, 20]


def test_wire_rejects_oversized_final_json():
    huge = _elements()
    huge["extra"] = [
        {"shape": "rect", "x": i, "y": i, "w": 1, "h": 1}
        for i in range(1000)
    ]
    with pytest.raises(ServoProtocolError, match="JSON bytes"):
        pb_json_messages(
            pb_req="too-large",
            sample_rate=24_000,
            fmt="s16le",
            channels=1,
            anim_rows=[
                {"chunk_ms": 50, "anim": [make_anim_item(huge, 50)]}
            ],
            pcm_per_idx=[b""],
        )


def test_physical_completion_budget_covers_short_full_range_batches():
    steps = [
        {"xm": 0, "ym": 0, "x": 10, "y": 70, "ms": 50}
        for _ in range(32)
    ]
    assert servo_sequence_completion_budget_ms(steps) == 34_560


def test_physical_budget_does_not_assume_pose_is_inside_new_soft_bounds():
    step = {
        "xm": 0,
        "ym": 0,
        "x": 90,
        "y": 90,
        "ms": 50,
        "x_min": 80,
        "x_max": 100,
        "y_min": 80,
        "y_max": 100,
    }
    # X may still be at hardware endpoint 170 from an older configuration:
    # ceil(80 / 3) ticks * 20 ms = 540 ms.
    assert servo_sequence_completion_budget_ms([step]) == 540


def test_pb_completion_budget_serializes_motor_batches_across_chunks():
    first = {"xm": 1, "ym": 1, "x": 0, "y": 0, "ms": 30_000}
    second = {"xm": 1, "ym": 1, "x": 0, "y": 0, "ms": 10_000}
    assert pb_sequence_completion_budget_ms(
        [
            {"chunk_ms": 10_000, "servo": [first]},
            {"chunk_ms": 10_000, "servo": [second]},
        ]
    ) == 40_000


def test_protocol_bounds_reject_outside_absolute_and_relative_values():
    base = {
        "xm": 1,
        "ym": 1,
        "x": 81,
        "y": 0,
        "ms": 50,
        "x_min": 20,
        "x_max": 100,
        "y_min": 80,
        "y_max": 100,
    }
    with pytest.raises(ServoProtocolError, match="relative x"):
        validate_servo_steps([base])
    absolute = dict(base, xm=0, x=19)
    with pytest.raises(ServoProtocolError, match="absolute x"):
        validate_servo_steps([absolute])


def test_reverse_mapping_carries_protocol_soft_bounds():
    limits = {
        "xMin": 20,
        "xMax": 160,
        "yMin": 80,
        "yMax": 100,
        "xReverse": 1,
        "yReverse": 1,
    }
    step = logical_step_to_protocol(
        {"xm": 0, "ym": 0, "x": 30, "y": 85, "ms": 50},
        limits,
    )
    assert step == {
        "xm": 0,
        "ym": 0,
        "x": 150,
        "y": 95,
        "ms": 50,
        "x_min": 20,
        "x_max": 160,
        "y_min": 80,
        "y_max": 100,
    }


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda doc: doc.update(yMin=60), "yMin must be between 70 and 110"),
        (
            lambda doc: doc["presets"][0].update(exposeToModel="false"),
            "exposeToModel must be a boolean",
        ),
        (lambda doc: doc["presets"][0]["steps"][0].update(ms=True), "invalid step ms"),
        (lambda doc: doc["presets"][0]["steps"][0].update(x=90.5), "invalid step x"),
    ],
)
def test_servo_config_rejects_hardware_and_json_type_drift(mutate, match):
    doc = _catalog(_preset("one"))
    mutate(doc)
    with pytest.raises(ValueError, match=match):
        normalize_servo_document(doc, require_presets=True)


def test_server_source_has_no_visual_face_follow_mode_residue():
    import inspect

    from deskbot_server.llm import face_scene
    from deskbot_server.vision import geometry

    assert "跟随人脸" not in inspect.getsource(face_scene)
    assert "跟随正脸" not in inspect.getsource(face_scene)
    assert "用于「跟随正脸」" not in inspect.getsource(geometry)
