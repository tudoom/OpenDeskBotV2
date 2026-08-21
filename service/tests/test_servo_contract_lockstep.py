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
    head_cpp = _read("head.cpp")
    asr = _read("asr_chat_client.cpp")

    tick_ms = _int_match(
        r"SERVO_TICK_MS\s*=\s*(\d+)\s*;", head_h, "SERVO_TICK_MS"
    )
    assert tick_ms == servo_protocol.SERVO_TICK_MS

    step_deg = _int_match(
        r"kPbMaxStepDegPerTick\s*=\s*(\d+)\s*;",
        head_cpp,
        "kPbMaxStepDegPerTick",
    )
    assert step_deg == servo_protocol.SERVO_MAX_DEGREES_PER_TICK

    # The client-side completion estimate uses ceil(travel / N) ticks written
    # as ``(travel + N-1) / N``; keep that N in lock-step too.
    round_up = _int_match(
        r"\(\(travel\s*\+\s*(\d+)u?\)\s*/\s*(?:\d+)u?\)\s*\*\s*SERVO_TICK_MS",
        asr,
        "pb physical_min_ms rounding",
    )
    divisor = _int_match(
        r"\(\(travel\s*\+\s*\d+u?\)\s*/\s*(\d+)u?\)\s*\*\s*SERVO_TICK_MS",
        asr,
        "pb physical_min_ms divisor",
    )
    assert divisor == servo_protocol.SERVO_MAX_DEGREES_PER_TICK
    assert round_up == divisor - 1


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
