"""Connection generations for camera frames and derived vision state.

A detector task can outlive the WebSocket that created it (for example while
an inference call is running in a worker thread).  A monotonically increasing
generation lets caches reject those late writes after a reconnect.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

# 相机侧缓存共用的新鲜度 TTL：设备最近一帧 JPEG（device_camera_frame_store）、
# 人脸快照（face_snapshot_cache）与 /camera_view broker 快照（camera_broker）
# 统一由这一个常量 + 环境变量控制。
CAMERA_STATE_TTL_DEFAULT_S = 5.0


def camera_state_ttl_seconds() -> float:
    """Shared freshness TTL for cached camera frames and face snapshots."""

    try:
        return max(
            0.5,
            float(
                os.environ.get(
                    "CAMERA_STATE_TTL_SEC",
                    str(CAMERA_STATE_TTL_DEFAULT_S),
                )
            ),
        )
    except ValueError:
        return CAMERA_STATE_TTL_DEFAULT_S


_lock = threading.Lock()
_next_generation = 1
_active_by_device: dict[str, int] = {}
_active_source_by_device: dict[str, str] = {}

_SOURCE_PRIORITY = {
    "legacy": 10,
    "asr_chat": 10,
    "camera_uplink": 20,
}
_TOMBSTONE_SOURCE_PREFIX = "__tombstone__:"


@dataclass(frozen=True, slots=True)
class VisionTombstone:
    """Proof that one connection generation was replaced by a tombstone."""

    device_id: str
    retired_generation: int | None
    tombstone_generation: int


def _device_id(value: str) -> str:
    return str(value or "").strip()


def _tombstone_source(retired_generation: int | None) -> str:
    retired = "none" if retired_generation is None else str(retired_generation)
    return f"{_TOMBSTONE_SOURCE_PREFIX}{retired}"


def _is_tombstone_source(source: str | None) -> bool:
    return bool(source and source.startswith(_TOMBSTONE_SOURCE_PREFIX))


def begin_vision_generation(
    device_id: str,
    *,
    source: str = "legacy",
) -> int | None:
    """Activate a camera connection generation for ``device_id``.

    The dedicated ``camera_uplink`` channel is authoritative while it is
    connected.  A lower-priority ``asr_chat`` camera stream therefore cannot
    invalidate or overwrite its frames.  Returning ``None`` tells the caller
    to ignore that lower-priority frame and retry after the authoritative
    source disconnects.
    """

    global _next_generation
    dev = _device_id(device_id)
    if not dev:
        raise ValueError("device_id is required")
    normalized_source = str(source or "legacy").strip() or "legacy"
    with _lock:
        active_source = _active_source_by_device.get(dev)
        active_priority = (
            0
            if active_source is None or _is_tombstone_source(active_source)
            else _SOURCE_PRIORITY.get(active_source, 10)
        )
        requested_priority = _SOURCE_PRIORITY.get(normalized_source, 10)
        if active_priority > requested_priority:
            return None
        generation = _next_generation
        _next_generation += 1
        _active_by_device[dev] = generation
        _active_source_by_device[dev] = normalized_source
        return generation


def current_vision_generation(device_id: str) -> int | None:
    dev = _device_id(device_id)
    if not dev:
        return None
    with _lock:
        return _active_by_device.get(dev)


def current_vision_source(device_id: str) -> str | None:
    dev = _device_id(device_id)
    if not dev:
        return None
    with _lock:
        source = _active_source_by_device.get(dev)
        return None if _is_tombstone_source(source) else source


def is_current_vision_generation(device_id: str, generation: int | None) -> bool:
    """Whether a write belongs to the active connection generation.

    ``None`` remains accepted for legacy/internal callers.  Such callers are
    stamped with the current generation by the cache itself.
    """

    if generation is None:
        return True
    dev = _device_id(device_id)
    if not dev:
        return False
    try:
        candidate = int(generation)
    except (TypeError, ValueError):
        return False
    with _lock:
        active = _active_by_device.get(dev)
        return candidate == (active if active is not None else 0)


def invalidate_vision_generation(
    device_id: str,
    *,
    expected_generation: int | None = None,
) -> tuple[bool, int | None, int | None]:
    """Retire a generation and install a tombstone generation.

    The tombstone prevents an in-flight task from repopulating caches after
    disconnect cleanup.  Returns ``(invalidated, retired, tombstone)``.
    """

    global _next_generation
    dev = _device_id(device_id)
    if not dev:
        return False, None, None
    with _lock:
        active = _active_by_device.get(dev)
        if expected_generation is not None:
            try:
                expected = int(expected_generation)
            except (TypeError, ValueError):
                return False, active, active
            if active != expected:
                return False, active, active
        tombstone = _next_generation
        _next_generation += 1
        _active_by_device[dev] = tombstone
        _active_source_by_device[dev] = _tombstone_source(active)
        return True, active, tombstone


def forget_vision_tombstone(tombstone: VisionTombstone) -> bool:
    """Atomically reclaim one retired connection's tombstone.

    Callers must first cancel that connection's producer tasks and clear all
    generation-scoped caches and broker snapshots.  The exact tombstone value
    is globally unique, so a reconnect or a later disconnect changes the
    active generation and makes this cleanup a no-op instead of deleting the
    replacement state.
    """

    dev = _device_id(tombstone.device_id)
    if not dev:
        return False
    try:
        candidate = int(tombstone.tombstone_generation)
        retired = (
            int(tombstone.retired_generation)
            if tombstone.retired_generation is not None
            else None
        )
    except (TypeError, ValueError):
        return False
    if candidate <= 0 or (retired is not None and candidate <= retired):
        return False
    with _lock:
        if (
            _active_by_device.get(dev) != candidate
            or _active_source_by_device.get(dev) != _tombstone_source(retired)
        ):
            return False
        _active_by_device.pop(dev, None)
        _active_source_by_device.pop(dev, None)
        return True
