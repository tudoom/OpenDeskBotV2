"""摄像头人脸场景描述（位置、坐标），供 LLM system prompt。"""
from __future__ import annotations

import math
from typing import Any

from deskbot_server.camera_face_tune import get_horizontal_fov_deg
from deskbot_server.face_snapshot_cache import list_device_faces


def _nose_xy(face: dict[str, Any]) -> tuple[float, float, int, int] | None:
    w = int(face.get("image_w") or 0) or 320
    h = int(face.get("image_h") or 0) or 240
    for src in (face.get("landmarks"), face.get("points")):
        if not isinstance(src, list):
            continue
        for p in src:
            if not isinstance(p, dict) or p.get("name") != "nose":
                continue
            try:
                return float(p["x"]), float(p["y"]), w, h
            except (TypeError, ValueError, KeyError):
                continue
    return None


def describe_face_screen_position(face: dict[str, Any]) -> dict[str, Any]:
    """鼻尖在画面中的方位与归一化坐标。"""
    nose = _nose_xy(face)
    if nose is None:
        return {"label": "位置未知", "nx_pct": None, "ny_pct": None, "screen_yaw": None, "screen_pitch": None}
    nx, ny, w, h = nose
    nx_pct = round(nx / w * 100, 1)
    ny_pct = round(ny / h * 100, 1)
    x_ratio = nx / w
    y_ratio = ny / h

    if x_ratio < 0.35:
        h_label = "左"
    elif x_ratio > 0.65:
        h_label = "右"
    else:
        h_label = "中"

    if y_ratio < 0.35:
        v_label = "上"
    elif y_ratio > 0.65:
        v_label = "下"
    else:
        v_label = "中"

    if h_label == "中" and v_label == "中":
        label = "画面中央"
    elif h_label == "中":
        label = f"画面{v_label}方"
    elif v_label == "中":
        label = f"画面{h_label}侧"
    else:
        label = f"画面{h_label}{v_label}"

    hfov_rad = math.radians(get_horizontal_fov_deg())
    vfov_rad = 2 * math.atan(math.tan(hfov_rad / 2) * (h / w))
    dx = nx - w / 2
    dy = ny - h / 2
    r2d = 180 / math.pi
    screen_yaw = round(math.atan((2 * dx * math.tan(hfov_rad / 2)) / w) * r2d, 1)
    screen_pitch = round(math.atan((2 * dy * math.tan(vfov_rad / 2)) / h) * r2d, 1)

    return {
        "label": label,
        "nx_pct": nx_pct,
        "ny_pct": ny_pct,
        "screen_yaw": screen_yaw,
        "screen_pitch": screen_pitch,
    }


def list_faces_for_prompt(device_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    device_id = str(device_id or "").strip()
    if not device_id:
        return []
    cap = max(1, min(int(limit), 10))
    faces = list_device_faces(device_id)
    rows: list[dict[str, Any]] = []
    for fid in sorted(faces.keys()):
        face = faces[fid]
        if not isinstance(face, dict):
            continue
        pos = describe_face_screen_position(face)
        name = str(face.get("person_name") or "").strip()
        score = face.get("identity_score")
        try:
            conf = round(float(score), 3) if score is not None else None
        except (TypeError, ValueError):
            conf = None
        pid = face.get("person_id")
        rows.append(
            {
                "face_id": int(fid),
                "person_id": int(pid) if pid is not None else None,
                "person_name": name or None,
                "identity_score": conf,
                **pos,
            }
        )
    rows.sort(key=lambda r: (-(r.get("identity_score") or 0.0), r["face_id"]))
    return rows[:cap]
