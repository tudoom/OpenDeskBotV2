"""PB camera controls for continuous debug streaming and one-shot capture."""

from __future__ import annotations

import uuid
from typing import Any

from deskbot_server.pb.servo_pcm import parse_pb_cam_fps
from deskbot_server.pb.shapes import PB_ACTION_DEFAULT, PB_LEVEL_TASK


def build_cam_fps_signal_pb(
    *,
    cam_fps: int,
    req: str | None = None,
    one_shot: bool = False,
) -> dict[str, Any]:
    """Build a media-free camera control PB.

    ``one_shot`` asks the device for exactly one fresh frame and does not
    change its normal camera cadence. Without it, ``cam_fps`` retains the
    explicit continuous-stream behavior used by the debug live view.
    """

    try:
        raw_fps = int(cam_fps)
    except (TypeError, ValueError) as exc:
        raise ValueError("cam_fps must be a non-negative integer") from exc
    if raw_fps == 0:
        if one_shot:
            raise ValueError("one-shot camera capture requires a positive cam_fps")
        fps = 0
    else:
        fps = parse_pb_cam_fps(raw_fps)
        if fps is None:
            raise ValueError("cam_fps must be a non-negative integer")
    message: dict[str, Any] = {
        "type": "pb_single",
        "req": req or uuid.uuid4().hex[:16],
        "idx": 0,
        "chunk_ms": 1,
        "pb_ver": 2,
        "action": PB_ACTION_DEFAULT,
        "level": PB_LEVEL_TASK,
        "cam_fps": fps,
    }
    if one_shot:
        message["camera_once"] = True
    return message
