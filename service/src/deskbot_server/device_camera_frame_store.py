"""设备最近一帧相机 JPEG 缓存（供 ``capture_camera`` 工具）。"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Optional

from deskbot_server.llm.vision_input import (
    TRANSIENT_VISION_IMAGE_KEY,
    ValidatedJpeg,
    VisionImageValidationError,
    make_transient_vision_image,
)
from deskbot_server.vision.generation import (
    VisionTombstone,
    camera_state_ttl_seconds,
    current_vision_generation,
    invalidate_vision_generation,
    is_current_vision_generation,
)

logger = logging.getLogger("deskbot-server")

_lock = threading.Lock()
_frame_ready = threading.Condition(_lock)
_by_device: dict[str, dict[str, Any]] = {}
_frame_reaper_started = False

_DEFAULT_CAPTURE_FPS = 5
_DEFAULT_MAX_AGE_S = 1.5
_DEFAULT_WAIT_TIMEOUT_S = 6.0
# A healthy on-demand firmware answers almost immediately.  Do not spend most
# of the vision-tool budget waiting before enabling the bounded compatibility
# stream: older or temporarily starved runtimes recover much more reliably
# when that second capture trigger is issued right away.
_ONE_SHOT_PRIMARY_WAIT_S = 0.15


def _frame_bytes_ttl_s() -> float:
    return camera_state_ttl_seconds()

# Serialize concurrent captures per device: two overlapping captures would
# otherwise interleave their boost/stop commands and kill each other's bounded
# fallback stream.  Entries only live while a capture is in flight (the last
# user removes the lock again), so a disconnected device leaves no residue and
# a fresh event loop never sees a lock bound to an old loop.
_capture_locks: dict[str, asyncio.Lock] = {}
_capture_lock_users: dict[str, int] = {}


@asynccontextmanager
async def _device_capture_lock(device_id: str) -> AsyncIterator[None]:
    lock = _capture_locks.setdefault(device_id, asyncio.Lock())
    _capture_lock_users[device_id] = _capture_lock_users.get(device_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        users = _capture_lock_users.get(device_id, 1) - 1
        if users > 0:
            _capture_lock_users[device_id] = users
        else:
            _capture_lock_users.pop(device_id, None)
            if _capture_locks.get(device_id) is lock:
                _capture_locks.pop(device_id, None)


def _purge_expired_frames_locked(now_monotonic: float) -> int:
    expired = [
        dev
        for dev, row in _by_device.items()
        if float(row.get("expires_monotonic") or 0) <= now_monotonic
    ]
    for dev in expired:
        _by_device.pop(dev, None)
    return len(expired)


def _frame_reaper_loop() -> None:
    while True:
        with _frame_ready:
            now_monotonic = time.monotonic()
            removed = _purge_expired_frames_locked(now_monotonic)
            if removed:
                _frame_ready.notify_all()
            deadlines = [
                float(row.get("expires_monotonic") or now_monotonic)
                for row in _by_device.values()
            ]
            timeout = (
                max(0.05, min(deadlines) - now_monotonic)
                if deadlines
                else _frame_bytes_ttl_s()
            )
            _frame_ready.wait(timeout=timeout)


def _ensure_frame_reaper_locked() -> None:
    global _frame_reaper_started
    if _frame_reaper_started:
        return
    _frame_reaper_started = True
    threading.Thread(
        target=_frame_reaper_loop,
        name="deskbot-camera-frame-ttl",
        daemon=True,
    ).start()


def clear_device_camera_frame(
    device_id: str,
    *,
    generation: int | None = None,
) -> bool:
    return retire_device_camera_frame(device_id, generation=generation) is not None


def retire_device_camera_frame(
    device_id: str,
    *,
    generation: int | None = None,
) -> VisionTombstone | None:
    """Clear one frame generation and return its conditional cleanup proof."""

    dev = str(device_id or "").strip()
    if not dev:
        return None
    invalidated, retired, tombstone = invalidate_vision_generation(
        dev,
        expected_generation=generation,
    )
    if not invalidated or tombstone is None:
        return None
    with _frame_ready:
        row = _by_device.get(dev)
        row_generation = int((row or {}).get("generation") or 0)
        if row is not None and (
            (
                generation is None
                and tombstone is not None
                and row_generation < tombstone
            )
            or (
                generation is not None
                and row_generation == int(generation)
            )
        ):
            _by_device.pop(dev, None)
        _frame_ready.notify_all()
    return VisionTombstone(
        device_id=dev,
        retired_generation=retired,
        tombstone_generation=tombstone,
    )


def update_device_camera_frame(
    device_id: str,
    validated_jpeg: ValidatedJpeg,
    *,
    width: int = 0,
    height: int = 0,
    source: str = "uplink",
    captured_at: float | None = None,
    generation: int | None = None,
) -> bool:
    """Store a JPEG proof produced by the camera ingress validation boundary.

    仅接受 :class:`ValidatedJpeg`：原先接收裸 bytes 的门面已删除，调用方
    （含测试）必须先经 ``validate_jpeg`` / ``validate_jpeg_with_rgb`` 校验。
    """

    if not isinstance(validated_jpeg, ValidatedJpeg):
        raise TypeError("validated_jpeg must be a ValidatedJpeg")
    dev = str(device_id or "").strip()
    if not dev:
        return False
    if generation is not None and not is_current_vision_generation(dev, generation):
        return False
    active_generation = current_vision_generation(dev)
    effective_generation = int(
        generation if generation is not None else active_generation or 0
    )
    try:
        frame_captured_at = (
            float(captured_at) if captured_at is not None else time.time()
        )
    except (TypeError, ValueError):
        frame_captured_at = time.time()
    if frame_captured_at <= 0:
        frame_captured_at = time.time()
    with _frame_ready:
        if generation is not None and not is_current_vision_generation(
            dev, generation
        ):
            return False
        _by_device[dev] = {
            "jpeg": validated_jpeg.data,
            "ts": frame_captured_at,
            "captured_at": frame_captured_at,
            "generation": effective_generation,
            "width": int(width or validated_jpeg.width),
            "height": int(height or validated_jpeg.height),
            "source": str(source or "uplink"),
            "expires_monotonic": time.monotonic() + _frame_bytes_ttl_s(),
        }
        _ensure_frame_reaper_locked()
        _frame_ready.notify_all()
    return True


# 兼容别名：ws/asr_chat.py（语音智能体所有）仍按旧私有名导入，
# 待其同步改为 ``update_device_camera_frame`` 后删除本行。


def _copy_current_frame(
    device_id: str,
    row: dict[str, Any] | None,
) -> Optional[dict[str, Any]]:
    if not row:
        return None
    if float(row.get("expires_monotonic") or 0) <= time.monotonic():
        return None
    generation = int(row.get("generation") or 0)
    if not is_current_vision_generation(device_id, generation):
        return None
    captured_at = float(row.get("captured_at") or row.get("ts") or 0)
    return {
        "jpeg": bytes(row["jpeg"]),
        "ts": captured_at,
        "captured_at": captured_at,
        "generation": generation,
        "width": int(row.get("width") or 0),
        "height": int(row.get("height") or 0),
        "source": str(row.get("source") or ""),
    }


def get_device_camera_frame(device_id: str) -> Optional[dict[str, Any]]:
    dev = str(device_id or "").strip()
    if not dev:
        return None
    with _lock:
        row = _by_device.get(dev)
        copied = _copy_current_frame(dev, row)
        if copied is None and row is not None:
            _by_device.pop(dev, None)
        if copied is None:
            return None
        return copied


def wait_for_device_camera_frame(
    device_id: str,
    *,
    after_ts: float = 0.0,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    timeout: float = _DEFAULT_WAIT_TIMEOUT_S,
) -> Optional[dict[str, Any]]:
    """阻塞等待可用帧：``after_ts`` 之后的新帧，或足够新的缓存帧。"""
    dev = str(device_id or "").strip()
    if not dev:
        return None
    deadline = time.time() + max(0.1, float(timeout))
    with _frame_ready:
        while True:
            row = _by_device.get(dev)
            now = time.time()
            if row:
                copied = _copy_current_frame(dev, row)
                if copied is None:
                    _by_device.pop(dev, None)
                    row = None
                captured_at = float((copied or {}).get("captured_at") or 0)
                fresh_enough = (now - captured_at) <= max_age_s
                new_enough = captured_at > float(after_ts) + 1e-6
                if fresh_enough and (after_ts <= 0 or new_enough):
                    return copied
            remaining = deadline - now
            if remaining <= 0:
                break
            _frame_ready.wait(timeout=min(0.1, remaining))
    # A caller asking for a frame after ``after_ts`` must never receive an
    # arbitrary cached frame on timeout.  It can inspect stale metadata via
    # ``get_device_camera_frame`` and return an explicit stale/error result.
    return None


def _frame_to_capture_result(
    row: dict[str, Any],
    *,
    display: bool = False,
) -> dict[str, Any]:
    jpeg = row["jpeg"]
    try:
        transient_image = make_transient_vision_image(jpeg)
    except VisionImageValidationError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "captured_at": row.get("captured_at"),
            "generation": row.get("generation"),
        }
    image_display: dict[str, Any] | None = None
    if display:
        try:
            from deskbot_server.pb.llm_display import decode_llm_image_item

            # This base64 exists only inside the display decoder call.  It is
            # never returned in a tool result or serialized to text/storage.
            b64 = base64.standard_b64encode(jpeg).decode("ascii")
            image_display = decode_llm_image_item({"b64": b64, "x": 0, "y": 0})
        except Exception:
            image_display = None
    out: dict[str, Any] = {
        "ok": True,
        "ts": row["ts"],
        "captured_at": row["captured_at"],
        "generation": row["generation"],
        "width": row["width"],
        "height": row["height"],
        "source": row["source"],
        "jpeg_bytes": len(jpeg),
        "display_requested": bool(display),
        TRANSIENT_VISION_IMAGE_KEY: transient_image,
    }
    if image_display:
        out["image_display"] = image_display
    return out


def capture_camera_for_device(
    device_id: str,
    *,
    display: bool = False,
) -> dict[str, Any]:
    """工具 ``capture_camera``：返回一帧瞬态 JPEG；默认不在设备屏幕回显。"""
    row = wait_for_device_camera_frame(
        device_id,
        after_ts=0.0,
        max_age_s=_DEFAULT_MAX_AGE_S,
        timeout=0.0,
    )
    if row is None:
        row = get_device_camera_frame(device_id)
    if not row:
        return {
            "ok": False,
            "error": "暂无相机帧；请确认设备已连接且相机上行已开启（收音期间可能暂停上传）",
        }
    age = time.time() - float(row.get("captured_at") or row.get("ts") or 0)
    if age > _DEFAULT_MAX_AGE_S:
        return {
            "ok": False,
            "error": (
                f"相机帧过旧（{age:.1f}s 前）；收音或播报期间设备可能暂停上传，请稍后再试"
            ),
            "ts": row["ts"],
            "captured_at": row.get("captured_at"),
            "generation": row.get("generation"),
            "stale": True,
        }
    return _frame_to_capture_result(row, display=display)


async def request_camera_uplink_boost(
    device_id: str,
    hub: Any,
    *,
    cam_fps: int = _DEFAULT_CAPTURE_FPS,
    one_shot: bool = False,
) -> None:
    """Request a camera frame or explicitly raise the continuous cadence."""
    dev = str(device_id or "").strip()
    if not dev or hub is None:
        return
    try:
        from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb

        payload = build_cam_fps_signal_pb(
            cam_fps=cam_fps,
            one_shot=one_shot,
        )
        n = await hub.send(dev, payload)
        logger.info(
            "[capture_camera] cam_fps=%d one_shot=%s device_id=%s delivered=%s",
            cam_fps,
            one_shot,
            dev,
            n,
        )
    except Exception as exc:
        logger.warning("[capture_camera] cam_fps boost failed device_id=%s: %s", dev, exc)


async def capture_camera_for_device_async(
    device_id: str,
    *,
    hub: Any = None,
    cam_fps: int = _DEFAULT_CAPTURE_FPS,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    wait_timeout_s: float = _DEFAULT_WAIT_TIMEOUT_S,
    display: bool = False,
    require_fresh: bool = False,
) -> dict[str, Any]:
    """异步拍照：必要时下发 ``cam_fps`` 并等待新帧。

    同设备并发调用按 per-device 锁串行化：并行的 boost/停止命令会互相掐流。
    """
    dev = str(device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "缺少 device_id"}
    async with _device_capture_lock(dev):
        return await _capture_camera_for_device_locked(
            dev,
            hub=hub,
            cam_fps=cam_fps,
            max_age_s=max_age_s,
            wait_timeout_s=wait_timeout_s,
            display=display,
            require_fresh=require_fresh,
        )


async def _capture_camera_for_device_locked(
    dev: str,
    *,
    hub: Any,
    cam_fps: int,
    max_age_s: float,
    wait_timeout_s: float,
    display: bool,
    require_fresh: bool,
) -> dict[str, Any]:
    row = get_device_camera_frame(dev)
    now = time.time()
    if not require_fresh and row and (
        now - float(row.get("captured_at") or row.get("ts") or 0)
    ) <= max_age_s:
        return _frame_to_capture_result(row, display=display)

    after_ts = now
    await request_camera_uplink_boost(
        dev,
        hub,
        cam_fps=cam_fps,
        one_shot=True,
    )
    total_wait_s = max(0.1, float(wait_timeout_s))
    # Without a hub there is no second capture trigger to issue, so the short
    # one-shot window must not truncate the wait: spend the full budget on the
    # primary wait instead.
    primary_wait_s = (
        min(total_wait_s, _ONE_SHOT_PRIMARY_WAIT_S)
        if hub is not None
        else total_wait_s
    )
    row = await asyncio.to_thread(
        wait_for_device_camera_frame,
        dev,
        after_ts=after_ts,
        max_age_s=max_age_s,
        timeout=primary_wait_s,
    )

    # Firmware normally satisfies camera_once well inside the primary window.
    # Keep a bounded compatibility path for an older/cold camera runtime: start
    # a temporary low-rate stream, consume one genuinely new frame, then always
    # drop back to the cadence the preview lease system expects.  This avoids
    # turning image Q&A into a permanent camera stream.
    fallback_wait_s = max(0.0, total_wait_s - primary_wait_s)
    if row is None and hub is not None and fallback_wait_s > 0.0:
        from deskbot_server.application.camera_cadence import (
            camera_cadence_controller,
        )

        logger.warning(
            "[capture_camera] one-shot pending after %.2fs; "
            "using bounded stream fallback device_id=%s fps=%d",
            primary_wait_s,
            dev,
            cam_fps,
        )
        # The single-writer cadence controller merges this bounded boost with
        # any preview lease and restores the lease cadence on exit, so the
        # fallback can never kill an active /camera_view stream and never
        # leaves a temporary capture stream running.
        async with camera_cadence_controller().boost(
            dev,
            cam_fps,
            hub=hub,
            reason="capture_fallback",
        ):
            row = await asyncio.to_thread(
                wait_for_device_camera_frame,
                dev,
                after_ts=after_ts,
                max_age_s=max_age_s,
                timeout=fallback_wait_s,
            )
    if not row:
        stale = get_device_camera_frame(dev)
        if stale:
            age = time.time() - float(
                stale.get("captured_at") or stale.get("ts") or 0
            )
            return {
                "ok": False,
                "error": (
                    f"等待新相机帧超时（{wait_timeout_s:.1f}s）；"
                    f"最近一帧 {age:.1f}s 前（收音/播报期间设备可能暂停上传）"
                ),
                "ts": stale.get("ts"),
                "captured_at": stale.get("captured_at"),
                "generation": stale.get("generation"),
                "stale": True,
            }
        return {
            "ok": False,
            "error": "暂无相机帧；请确认设备已连接且相机上行已开启",
        }
    return _frame_to_capture_result(row, display=display)
