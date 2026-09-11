"""各设备最近一帧人脸快照（进程内，供 LLM 识别上下文）。

注册人名由调试页提交 ``jpeg_base64`` + ``points``，不再写盘 ``face_live_snapshot.json``。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from deskbot_server.face_identity import (
    descriptor_from_jpeg_base64,
    is_embedding_vector,
)
from deskbot_server.vision.generation import (
    camera_state_ttl_seconds,
    current_vision_generation,
    is_current_vision_generation,
)

_lock = threading.Lock()
_snapshots: dict[str, dict[int, dict[str, Any]]] = {}
_snapshot_updated_at: dict[str, float] = {}
_snapshot_captured_at: dict[str, float] = {}
_snapshot_generation: dict[str, int] = {}


def _ttl_seconds() -> float:
    return camera_state_ttl_seconds()


def _coerce_captured_at(value: float | None) -> float:
    try:
        captured_at = float(value) if value is not None else time.time()
    except (TypeError, ValueError):
        captured_at = time.time()
    return captured_at if captured_at > 0 else time.time()


def update_device_faces(
    device_id: str,
    faces: list[dict[str, Any]],
    *,
    captured_at: float | None = None,
    generation: int | None = None,
) -> bool:
    device_id = str(device_id or "").strip()
    if not device_id:
        return False
    if generation is not None and not is_current_vision_generation(
        device_id, generation
    ):
        return False
    active_generation = current_vision_generation(device_id)
    effective_generation = int(
        generation if generation is not None else active_generation or 0
    )
    effective_captured_at = _coerce_captured_at(captured_at)
    if time.time() - effective_captured_at > _ttl_seconds():
        return False
    by_id: dict[int, dict[str, Any]] = {}
    for face in faces or []:
        if not isinstance(face, dict):
            continue
        fid = face.get("face_id")
        if fid is None:
            continue
        row = dict(face)
        row["captured_at"] = effective_captured_at
        row["generation"] = effective_generation
        by_id[int(fid)] = row
    with _lock:
        if generation is not None and not is_current_vision_generation(
            device_id, generation
        ):
            return False
        _snapshots[device_id] = by_id
        _snapshot_updated_at[device_id] = time.monotonic()
        _snapshot_captured_at[device_id] = effective_captured_at
        _snapshot_generation[device_id] = effective_generation
    return True


def clear_device_faces(
    device_id: str,
    *,
    generation: int | None = None,
) -> bool:
    device_id = str(device_id or "").strip()
    if not device_id:
        return False
    cutoff_generation = current_vision_generation(device_id)
    with _lock:
        stored_generation = _snapshot_generation.get(device_id)
        if generation is not None and stored_generation not in (None, int(generation)):
            return False
        if (
            generation is None
            and cutoff_generation is not None
            and stored_generation is not None
            and stored_generation > cutoff_generation
        ):
            return False
        _snapshots.pop(device_id, None)
        _snapshot_updated_at.pop(device_id, None)
        _snapshot_captured_at.pop(device_id, None)
        _snapshot_generation.pop(device_id, None)
    return True


def list_device_faces(device_id: str) -> dict[int, dict[str, Any]]:
    """返回设备最近一帧各 ``face_id`` 的快照（进程内缓存）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return {}
    with _lock:
        mem = _snapshots.get(device_id)
        updated_at = _snapshot_updated_at.get(device_id, 0.0)
        captured_at = _snapshot_captured_at.get(device_id, 0.0)
        generation = _snapshot_generation.get(device_id)
        generation_is_current = is_current_vision_generation(
            device_id, generation
        )
        if mem is not None and (
            time.monotonic() - updated_at > _ttl_seconds()
            or time.time() - captured_at > _ttl_seconds()
            or not generation_is_current
        ):
            _snapshots.pop(device_id, None)
            _snapshot_updated_at.pop(device_id, None)
            _snapshot_captured_at.pop(device_id, None)
            _snapshot_generation.pop(device_id, None)
            mem = None
    if not mem:
        return {}
    return {int(k): dict(v) for k, v in mem.items()}


def get_device_face_snapshot_state(device_id: str) -> Optional[dict[str, Any]]:
    """Return the latest completed face analysis, including no-face frames.

    An empty ``faces`` mapping means inference completed and found no face.
    ``None`` means that no current analysis result exists.
    """

    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    with _lock:
        if device_id not in _snapshot_updated_at:
            return None
        updated_at = _snapshot_updated_at.get(device_id, 0.0)
        captured_at = _snapshot_captured_at.get(device_id, 0.0)
        generation = _snapshot_generation.get(device_id)
        if (
            time.monotonic() - updated_at > _ttl_seconds()
            or time.time() - captured_at > _ttl_seconds()
            or not is_current_vision_generation(device_id, generation)
        ):
            _snapshots.pop(device_id, None)
            _snapshot_updated_at.pop(device_id, None)
            _snapshot_captured_at.pop(device_id, None)
            _snapshot_generation.pop(device_id, None)
            return None
        faces = _snapshots.get(device_id) or {}
        return {
            "captured_at": float(captured_at),
            "generation": generation,
            "faces": {int(key): dict(value) for key, value in faces.items()},
        }




def resolve_descriptor_from_payload(payload: dict[str, Any]) -> Optional[list[float]]:
    """注册 API：仅接受 InsightFace embedding（``jpeg_base64`` + ``points`` 现算，
    或 payload 已带 embedding 向量）。

    无 embedding 时返回 ``None``：注册路径必须向用户返回明确错误，而不是
    静默存 9 维几何特征——几何 descriptor 已整体移除，不再参与身份判定。
    """

    points = payload.get("points") if isinstance(payload.get("points"), list) else None
    landmarks = payload.get("landmarks") if isinstance(payload.get("landmarks"), list) else []

    jpeg_b64 = payload.get("jpeg_base64") or payload.get("frame_jpeg_base64")
    if points and isinstance(jpeg_b64, str) and jpeg_b64.strip():
        emb = descriptor_from_jpeg_base64(jpeg_b64, points, landmarks=landmarks)
        if emb is not None:
            return emb

    raw_desc = payload.get("face_descriptor")
    if isinstance(raw_desc, list):
        try:
            vec = [float(x) for x in raw_desc]
        except (TypeError, ValueError):
            return None
        if is_embedding_vector(vec):
            return vec
    return None
