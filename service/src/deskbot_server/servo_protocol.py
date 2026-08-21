"""Shared server-side limits for the firmware ``servo[]`` PB contract.

Keep these values in lock-step with ``AsrChatClient`` and the motor actor.  A
semantic motion plan may be re-sliced across several PB chunks, but every
individual wire chunk must satisfy the limits below.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SERVO_MAX_SEGMENTS_PER_PB = 32
SERVO_MAX_BATCH_DURATION_MS = 300_000
SERVO_MIN_SEGMENT_DURATION_MS = 50

# Keep this conservative completion estimate in lock-step with the firmware
# motor actor (``head.cpp``): both axes move concurrently by at most three
# degrees every 20 ms.
SERVO_MAX_DEGREES_PER_TICK = 3
SERVO_TICK_MS = 20

# Before wire-time slicing, one explicit semantic plan is intentionally kept
# to one firmware batch worth of source steps.  Slicing a long step at media
# boundaries may create more wire segments, distributed across several PBs.
SERVO_MAX_PLAN_STEPS = SERVO_MAX_SEGMENTS_PER_PB
SERVO_MAX_PLAN_DURATION_MS = SERVO_MAX_BATCH_DURATION_MS

SERVO_HARDWARE_ENVELOPE: dict[str, int] = {
    "xMin": 10,
    "xMax": 170,
    "yMin": 70,
    "yMax": 110,
}


class ServoProtocolError(ValueError):
    """A motion cannot be represented safely by the firmware PB contract."""


def _wire_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServoProtocolError(f"{field} must be an integer")
    return value


def servo_steps_duration_ms(steps: Iterable[Mapping[str, Any]]) -> int:
    """Return the checked duration of a sequence of protocol servo steps."""

    total = 0
    for index, step in enumerate(steps):
        try:
            duration = _wire_int(step.get("ms", 0), f"servo step {index} ms")
        except (TypeError, ValueError) as exc:
            raise ServoProtocolError(
                f"servo step {index} has an invalid duration"
            ) from exc
        if (
            duration < SERVO_MIN_SEGMENT_DURATION_MS
            or duration > SERVO_MAX_BATCH_DURATION_MS
        ):
            raise ServoProtocolError(
                f"servo step {index} duration must be between "
                f"{SERVO_MIN_SEGMENT_DURATION_MS} and "
                f"{SERVO_MAX_BATCH_DURATION_MS} ms"
            )
        if total > SERVO_MAX_BATCH_DURATION_MS - duration:
            raise ServoProtocolError(
                f"servo sequence exceeds {SERVO_MAX_BATCH_DURATION_MS} ms"
            )
        total += duration
    return total


def validate_servo_steps(
    steps: Iterable[Mapping[str, Any]],
    *,
    context: str = "servo sequence",
    max_segments: int = SERVO_MAX_SEGMENTS_PER_PB,
    max_duration_ms: int = SERVO_MAX_BATCH_DURATION_MS,
) -> tuple[list[Mapping[str, Any]], int]:
    """Materialize and validate one atomic ``servo[]`` sequence.

    No prefix is returned on failure.  Callers can therefore reject or split
    the complete transaction without accidentally executing a partial plan.
    """

    materialized = list(steps)
    if not materialized:
        raise ServoProtocolError(f"{context} requires at least one step")
    if len(materialized) > max_segments:
        raise ServoProtocolError(
            f"{context} exceeds {max_segments} servo steps"
        )
    total = 0
    for index, step in enumerate(materialized):
        if not isinstance(step, Mapping):
            raise ServoProtocolError(f"{context} step {index} must be an object")
        try:
            xm = _wire_int(step.get("xm", 0), f"{context} step {index} xm")
            ym = _wire_int(step.get("ym", 0), f"{context} step {index} ym")
            x = _wire_int(step.get("x", 0), f"{context} step {index} x")
            y = _wire_int(step.get("y", 0), f"{context} step {index} y")
        except (TypeError, ValueError) as exc:
            raise ServoProtocolError(
                f"{context} step {index} has invalid axis values"
            ) from exc
        if xm not in (0, 1, 2) or ym not in (0, 1, 2):
            raise ServoProtocolError(
                f"{context} step {index} axis modes must be 0, 1 or 2"
            )
        try:
            duration = _wire_int(step.get("ms", 0), f"{context} step {index} ms")
        except (TypeError, ValueError) as exc:
            raise ServoProtocolError(
                f"{context} step {index} has an invalid duration"
            ) from exc
        if duration < SERVO_MIN_SEGMENT_DURATION_MS or duration > max_duration_ms:
            raise ServoProtocolError(
                f"{context} step {index} duration must be between "
                f"{SERVO_MIN_SEGMENT_DURATION_MS} and "
                f"{max_duration_ms} ms"
            )
        if total > max_duration_ms - duration:
            raise ServoProtocolError(
                f"{context} exceeds {max_duration_ms} ms"
            )
        total += duration
        bound_keys = ("x_min", "x_max", "y_min", "y_max")
        present = [key in step for key in bound_keys]
        if any(present) and not all(present):
            raise ServoProtocolError(
                f"{context} step {index} must provide all protocol bounds"
            )
        if all(present):
            bounds = {
                key: _wire_int(step[key], f"{context} step {index} {key}")
                for key in bound_keys
            }
            for axis in ("x", "y"):
                lo = bounds[f"{axis}_min"]
                hi = bounds[f"{axis}_max"]
                hard_lo = SERVO_HARDWARE_ENVELOPE[f"{axis}Min"]
                hard_hi = SERVO_HARDWARE_ENVELOPE[f"{axis}Max"]
                if lo > hi or lo < hard_lo or hi > hard_hi:
                    raise ServoProtocolError(
                        f"{context} step {index} {axis} bounds must stay within "
                        f"[{hard_lo}, {hard_hi}]"
                    )
        else:
            bounds = {
                "x_min": SERVO_HARDWARE_ENVELOPE["xMin"],
                "x_max": SERVO_HARDWARE_ENVELOPE["xMax"],
                "y_min": SERVO_HARDWARE_ENVELOPE["yMin"],
                "y_max": SERVO_HARDWARE_ENVELOPE["yMax"],
            }
        for axis, mode, value in (("x", xm, x), ("y", ym, y)):
            lo = bounds[f"{axis}_min"]
            hi = bounds[f"{axis}_max"]
            if mode == 0 and not lo <= value <= hi:
                raise ServoProtocolError(
                    f"{context} step {index} absolute {axis}={value} outside "
                    f"[{lo}, {hi}]"
                )
            if mode == 1 and abs(value) > hi - lo:
                raise ServoProtocolError(
                    f"{context} step {index} relative {axis}={value} exceeds "
                    f"span {hi - lo}"
                )
    return materialized, total


def _axis_worst_travel_degrees(
    mode: int,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    lo, hi = sorted((int(minimum), int(maximum)))
    if mode == 2:  # HEAD_SERVO_HOLD
        return 0
    if mode == 1:  # HEAD_SERVO_REL
        return min(abs(int(value)), hi - lo)
    if mode != 0:
        raise ServoProtocolError(f"unsupported servo axis mode: {mode}")
    target = max(lo, min(hi, int(value)))
    return max(target - lo, hi - target)


def servo_step_physical_min_ms(
    step: Mapping[str, Any],
    *,
    envelope: Mapping[str, int] | None = None,
) -> int:
    """Return the worst-case safe wall time for one protocol step.

    Relative travel is bounded by the axis span.  An absolute target may
    begin at either end of the hardware envelope, so the farther endpoint is
    used.  X and Y move concurrently; consequently the slower axis wins.
    """

    # Soft bounds constrain this command's target but do not prove the current
    # pose is already inside them: it may have been set under an older, wider
    # configuration.  Without a trusted current pose, completion estimates
    # must therefore start from the hardware envelope.
    if envelope is None:
        envelope = SERVO_HARDWARE_ENVELOPE
    try:
        x_travel = _axis_worst_travel_degrees(
            int(step.get("xm", 0)),
            int(step.get("x", 0)),
            minimum=int(envelope["xMin"]),
            maximum=int(envelope["xMax"]),
        )
        y_travel = _axis_worst_travel_degrees(
            int(step.get("ym", 0)),
            int(step.get("y", 0)),
            minimum=int(envelope["yMin"]),
            maximum=int(envelope["yMax"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ServoProtocolError("invalid servo step or hardware envelope") from exc
    travel = max(x_travel, y_travel)
    ticks = (travel + SERVO_MAX_DEGREES_PER_TICK - 1) // SERVO_MAX_DEGREES_PER_TICK
    return ticks * SERVO_TICK_MS


def servo_sequence_completion_budget_ms(
    steps: Iterable[Mapping[str, Any]],
    *,
    context: str = "servo sequence",
    envelope: Mapping[str, int] | None = None,
) -> int:
    """Conservatively budget sequential motor completion for ``played``.

    This validates the complete transaction before calculating a value, so a
    malformed suffix can never cause a valid prefix to be executed.
    """

    materialized, _declared_ms = validate_servo_steps(steps, context=context)
    total = 0
    for step in materialized:
        requested = int(step["ms"])
        total += max(requested, servo_step_physical_min_ms(step, envelope=envelope))
    return total


def pb_sequence_completion_budget_ms(
    messages: Iterable[Mapping[str, Any]],
) -> int:
    """Return a conservative shared-timeline completion budget for a PB chain."""

    timeline_ms = 0
    motor_cursor_ms = 0
    completion_ms = 0
    for index, message in enumerate(messages):
        try:
            chunk_ms = max(0, int(message.get("chunk_ms", 0)))
        except (TypeError, ValueError) as exc:
            raise ServoProtocolError(f"PB message {index} has invalid chunk_ms") from exc
        completion_ms = max(completion_ms, timeline_ms + chunk_ms)
        servo = message.get("servo")
        if servo is not None:
            if not isinstance(servo, list):
                raise ServoProtocolError(
                    f"PB message {index} servo must be an array"
                )
            if servo:
                motor_ms = servo_sequence_completion_budget_ms(
                    servo,
                    context=f"PB message {index} servo",
                )
                motor_cursor_ms = max(timeline_ms, motor_cursor_ms) + motor_ms
                completion_ms = max(completion_ms, motor_cursor_ms)
        timeline_ms += chunk_ms
    return completion_ms


__all__ = [
    "SERVO_HARDWARE_ENVELOPE",
    "SERVO_MAX_BATCH_DURATION_MS",
    "SERVO_MAX_DEGREES_PER_TICK",
    "SERVO_MAX_PLAN_DURATION_MS",
    "SERVO_MAX_PLAN_STEPS",
    "SERVO_MAX_SEGMENTS_PER_PB",
    "SERVO_MIN_SEGMENT_DURATION_MS",
    "SERVO_TICK_MS",
    "ServoProtocolError",
    "pb_sequence_completion_budget_ms",
    "servo_sequence_completion_budget_ms",
    "servo_step_physical_min_ms",
    "servo_steps_duration_ms",
    "validate_servo_steps",
]
