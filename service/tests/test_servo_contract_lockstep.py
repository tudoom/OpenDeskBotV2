"""Numeric lock-step between the firmware servo contract and the service.

``/api/servo_contract`` is the single source the web front-ends render from,
and ``servo_protocol.py`` is the Python copy of the firmware ``servo[]`` PB
contract.  This test parses the firmware sources (read-only) and asserts the
numbers are identical, so any drift fails CI instead of silently producing a
front-end that disagrees with the device.  Parsing precedent:
``test_firmware_pb_terminal.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from deskbot_server import servo_protocol

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def _read(name: str) -> str:
    return (FW / name).read_text(encoding="utf-8")


def _int_match(pattern: str, source: str, what: str) -> int:
    match = re.search(pattern, source)
    assert match, f"firmware constant not found: {what} ({pattern!r})"
    return int(match.group(1))


def test_hardware_envelope_matches_head_h_limits():
    head = _read("head.h")
    limits = {
        "xMin": _int_match(r"#define\s+X_MIN_LIMIT\s+(\d+)", head, "X_MIN_LIMIT"),
        "xMax": _int_match(r"#define\s+X_MAX_LIMIT\s+(\d+)", head, "X_MAX_LIMIT"),
        "yMin": _int_match(r"#define\s+Y_MIN_LIMIT\s+(\d+)", head, "Y_MIN_LIMIT"),
        "yMax": _int_match(r"#define\s+Y_MAX_LIMIT\s+(\d+)", head, "Y_MAX_LIMIT"),
    }
    assert limits == dict(servo_protocol.SERVO_HARDWARE_ENVELOPE)


def test_pb_segment_count_matches_asr_chat_client_h():
    header = _read("asr_chat_client.h")
    segs = _int_match(
        r"kPbMaxServoSegsPerChunk\s*=\s*(\d+)\s*;",
        header,
        "kPbMaxServoSegsPerChunk",
    )
    assert segs == servo_protocol.SERVO_MAX_SEGMENTS_PER_PB
    # One explicit semantic plan is deliberately capped at one firmware batch.
    assert servo_protocol.SERVO_MAX_PLAN_STEPS == segs


def test_modality_duration_budget_matches_firmware():
    asr = _read("asr_chat_client.cpp")
    max_ms = _int_match(
        r"kMaxModalityDurationMs\s*=\s*(\d+)u?\s*;",
        asr,
        "kMaxModalityDurationMs",
    )
    assert max_ms == servo_protocol.SERVO_MAX_BATCH_DURATION_MS
    assert servo_protocol.SERVO_MAX_PLAN_DURATION_MS == max_ms


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_min_segment_duration_matches_firmware_wire_validation():
    asr = _read("asr_chat_client.cpp")
    # Scope to the servo[] lane: the anim lane above it uses a 0 lower bound.
    servo_block = _between(
        asr,
        "servo.size() > kPbMaxServoSegsPerChunk",
        "servo_total_ms += duration_ms",
    )
    # The firmware validates servo segment["ms"] in [<min>, kMaxModalityDurationMs].
    min_ms = _int_match(
        r'segment\["ms"\],\s*(\d+)\s*,\s*\n?\s*kMaxModalityDurationMs',
        servo_block,
        'servo segment["ms"] lower bound',
    )
    assert min_ms == servo_protocol.SERVO_MIN_SEGMENT_DURATION_MS


def test_motor_tick_and_step_rate_match_firmware():
    head_h = _read("head.h")
    asr = _read("asr_chat_client.cpp")

    tick_ms = _int_match(
        r"SERVO_TICK_MS\s*=\s*(\d+)\s*;", head_h, "SERVO_TICK_MS"
    )
    assert tick_ms == servo_protocol.SERVO_TICK_MS
    # 2026-09-09 运动学常量：梯形曲线的步速/加速拍数/USB 双轴降速/边缘余量/死区。
    pairs = {
        "SERVO_MAX_STEP_DEG_PER_TICK": servo_protocol.SERVO_MAX_DEGREES_PER_TICK,
        "SERVO_ACCEL_TICKS": servo_protocol.SERVO_ACCEL_TICKS,
        "SERVO_USB_BOTH_AXES_STEP_DEG_PER_TICK": servo_protocol.SERVO_SLOW_DEGREES_PER_TICK,
        "SERVO_EDGE_MARGIN_DEG": servo_protocol.SERVO_EDGE_MARGIN_DEG,
        "SERVO_DEADBAND_DEG": servo_protocol.SERVO_DEADBAND_DEG,
    }
    for name, expected in pairs.items():
        assert _int_match(rf"{name}\s*=\s*(\d+)\s*;", head_h, name) == expected
    # 客户端的时间线预算不再自己算除法，直接用固件与 PC 逐位一致的最短时长函数。
    assert "head_servo_travel_min_ms(travel)" in asr
    assert "/ 3u) * SERVO_TICK_MS" not in asr

def test_axis_mode_domain_matches_firmware_hold_contract():
    head_h = _read("head.h")
    modes = {
        "abs": _int_match(
            r"HEAD_SERVO_ABS\s*=\s*(\d+)\s*;", head_h, "HEAD_SERVO_ABS"
        ),
        "rel": _int_match(
            r"HEAD_SERVO_REL\s*=\s*(\d+)\s*;", head_h, "HEAD_SERVO_REL"
        ),
        "hold": _int_match(
            r"HEAD_SERVO_HOLD\s*=\s*(\d+)\s*;", head_h, "HEAD_SERVO_HOLD"
        ),
    }
    assert modes == {"abs": 0, "rel": 1, "hold": 2}
    # validate_servo_steps accepts exactly this domain (HOLD included).
    steps, total = servo_protocol.validate_servo_steps(
        [{"xm": 2, "ym": 2, "x": 0, "y": 0, "ms": 100}]
    )
    assert total == 100 and steps[0]["xm"] == 2
