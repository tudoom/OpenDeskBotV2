from __future__ import annotations

from tools.usb_device_smoke import _hold_servo_segment


def test_hold_servo_segment_is_schema_complete_and_motionless() -> None:
    assert _hold_servo_segment(250) == {
        "x": 0,
        "y": 0,
        "xm": 2,
        "ym": 2,
        "ms": 250,
    }
