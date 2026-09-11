"""Reference-counted camera streaming leases for browser previews."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from deskbot_server.application.camera_cadence import camera_cadence_controller
from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb

logger = logging.getLogger("deskbot-server")

DEFAULT_CAMERA_PREVIEW_FPS = 2
# 浏览器预览允许要的最高帧率：QVGA JPEG 约 10 KB 一帧，5 fps 也只有 50 KB/s，但固件相机任务与音频共
# 用一颗核，再高就该先看真机 [CAMERA] stat 了。
CAMERA_PREVIEW_MAX_FPS = 5
DEFAULT_CAMERA_PREVIEW_REFRESH_SECONDS = 10.0


def parse_preview_fps(raw: Any, default: int = DEFAULT_CAMERA_PREVIEW_FPS) -> int:
    """订阅时的 ``fps`` 查询参数：1..CAMERA_PREVIEW_MAX_FPS，缺失 / 坏值用默认。"""
    try:
        value = int(str(raw if raw is not None else "").strip())
    except (TypeError, ValueError):
        return int(default)
    return max(1, min(CAMERA_PREVIEW_MAX_FPS, value))
CAMERA_PREVIEW_STOP_RETRY_DELAYS = (0.05, 0.15)

# The lease manager tracks who is entitled to a continuous preview stream;
# the actual cadence a device receives is always computed and sent by the
# single-writer ``CameraCadenceController`` (max of preview lease fps and any
# bounded capture boost), so no camera user can stomp another's stream.
_active_manager: "CameraPreviewLeaseManager | None" = None


def preview_target_fps(device_id: str) -> int:
    """Return the fps the preview lease system currently wants for a device.

    Read-only: 0 when no manager is active, the manager is closed, or the
    device holds no preview lease.
    """

    manager = _active_manager
    if manager is None:
        return 0
    return manager.target_fps(device_id)


class CameraPreviewLeaseManager:
    """Keep low-rate camera uplink enabled while a device has preview clients.

    Leases are counted per device so closing one browser tab cannot stop the
    stream used by another.  An online listener restores the stream after a
    normal USB reconnect.  The low-frequency refresh also covers the narrow
    replacement race where a new USB session attaches before the stale one has
    fully detached and therefore does not create an offline-to-online edge.
    """

    def __init__(
        self,
        hub: Any,
        *,
        fps: int = DEFAULT_CAMERA_PREVIEW_FPS,
        refresh_seconds: float = DEFAULT_CAMERA_PREVIEW_REFRESH_SECONDS,
    ) -> None:
        # Validate once through the same builder used for every wire command.
        build_cam_fps_signal_pb(cam_fps=fps)
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self._hub = hub
        self._fps = int(fps)
        self._refresh_seconds = float(refresh_seconds)
        # 每台设备当前所有租约各自要的 fps；设备节拍 = 最大值（首页 1 fps 和调试页 2 fps 互不影响）
        self._leases: dict[str, list[int]] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._command_locks: dict[str, asyncio.Lock] = {}
        self._command_users: dict[str, int] = {}
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._hub.add_online_listener(self._device_online)
        global _active_manager
        _active_manager = self

    def target_fps(self, device_id: str) -> int:
        """Return the cadence this manager currently wants for one device.

        Synchronous read of the lease refcount: safe to call from the event
        loop without awaiting, and intentionally the only query surface other
        camera users need.
        """

        dev = str(device_id or "").strip()
        if not dev or self._closed:
            return 0
        return self._target_locked(dev)

    def _target_locked(self, dev: str) -> int:
        leases = self._leases.get(dev) or []
        return max(leases) if leases else 0

    async def acquire(self, device_id: str, fps: int | None = None) -> bool:
        """Acquire one preview lease at ``fps`` (default: manager fps); the device
        follows the max over its leases, so a new faster subscriber raises the cadence."""

        dev = str(device_id or "").strip()
        if not dev:
            return False
        fps_value = int(fps) if fps is not None else self._fps
        build_cam_fps_signal_pb(cam_fps=fps_value)
        first = False
        changed = False
        async with self._state_lock:
            if self._closed:
                return False
            leases = self._leases.setdefault(dev, [])
            before = max(leases) if leases else 0
            leases.append(fps_value)
            first = len(leases) == 1
            changed = max(leases) != before
            if first:
                self._refresh_tasks[dev] = asyncio.create_task(
                    self._refresh_device(dev),
                    name=f"camera-preview-refresh:{dev}",
                )
        if first or changed:
            await self._sync_device(dev, reason="first_subscriber" if first else "faster_subscriber")
        return True

    async def release(self, device_id: str, fps: int | None = None) -> None:
        """Release one lease (the one with ``fps``, else the newest); the device stops only
        after the final subscriber and slows down when the fastest one leaves."""

        dev = str(device_id or "").strip()
        if not dev:
            return
        refresh_task: asyncio.Task[None] | None = None
        last = False
        changed = False
        async with self._state_lock:
            leases = self._leases.get(dev) or []
            if not leases:
                return
            before = max(leases)
            if fps is not None and int(fps) in leases:
                leases.remove(int(fps))
            else:
                leases.pop()
            if not leases:
                last = True
                self._leases.pop(dev, None)
                refresh_task = self._refresh_tasks.pop(dev, None)
                if refresh_task is not None:
                    refresh_task.cancel()
            else:
                changed = max(leases) != before
        if last or changed:
            await self._sync_device(dev, reason="last_subscriber_left" if last else "fastest_subscriber_left")
        if refresh_task is not None:
            await asyncio.gather(refresh_task, return_exceptions=True)

    async def close(self) -> None:
        """Stop all leased streams and detach the USB-online listener."""

        global _active_manager
        if _active_manager is self:
            _active_manager = None
        self._hub.remove_online_listener(self._device_online)
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            devices = tuple(self._leases)
            self._leases.clear()
            refresh_tasks = tuple(self._refresh_tasks.values())
            self._refresh_tasks.clear()
            for task in refresh_tasks:
                task.cancel()
        if devices:
            await asyncio.gather(
                *(
                    self._sync_device(dev, reason="manager_closed")
                    for dev in devices
                )
            )
        if refresh_tasks:
            await asyncio.gather(*refresh_tasks, return_exceptions=True)

    async def _device_online(self, device_id: str) -> None:
        dev = str(device_id or "").strip()
        if not dev:
            return
        async with self._state_lock:
            if self._closed or not self._leases.get(dev):
                return
        await self._sync_device(dev, reason="device_online")

    async def _refresh_device(self, device_id: str) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                async with self._state_lock:
                    if self._closed or not self._leases.get(device_id):
                        return
                await self._sync_device(
                    device_id,
                    reason="active_subscription_refresh",
                )
            except asyncio.CancelledError:
                return

    async def _sync_device(self, device_id: str, *, reason: str) -> None:
        async with self._state_lock:
            command_lock = self._command_locks.setdefault(
                device_id,
                asyncio.Lock(),
            )
            self._command_users[device_id] = (
                self._command_users.get(device_id, 0) + 1
            )
        try:
            async with command_lock:
                sync_reason = reason
                while True:
                    async with self._state_lock:
                        before_fps = 0 if self._closed else self._target_locked(device_id)
                        subscribers = len(self._leases.get(device_id) or [])
                    delivered = await self._send(
                        device_id,
                        before_fps,
                        reason=sync_reason,
                        subscribers=subscribers,
                    )
                    if before_fps == 0 and delivered <= 0:
                        for retry_index, delay in enumerate(
                            CAMERA_PREVIEW_STOP_RETRY_DELAYS,
                            start=1,
                        ):
                            await asyncio.sleep(delay)
                            async with self._state_lock:
                                still_stopped = self._closed or not self._leases.get(device_id)
                            if not still_stopped:
                                break
                            delivered = await self._send(
                                device_id,
                                0,
                                reason=f"{reason}_retry_{retry_index}",
                                subscribers=0,
                            )
                            if delivered > 0:
                                break
                    async with self._state_lock:
                        after_fps = 0 if self._closed else self._target_locked(device_id)
                    if after_fps == before_fps:
                        return
                    # State changed while the USB send was in flight.  Keep the
                    # per-device ordering lock and immediately apply the newer
                    # desired state before another command can overtake it.
                    sync_reason = f"{reason}_state_changed"
        finally:
            async with self._state_lock:
                users = self._command_users.get(device_id, 1) - 1
                if users > 0:
                    self._command_users[device_id] = users
                else:
                    self._command_users.pop(device_id, None)
                    if (
                        not self._leases.get(device_id)
                        and device_id not in self._refresh_tasks
                        and self._command_locks.get(device_id) is command_lock
                    ):
                        self._command_locks.pop(device_id, None)

    async def _send(
        self,
        device_id: str,
        fps: int,
        *,
        reason: str,
        subscribers: int,
    ) -> int:
        # Route through the single-writer cadence controller: it merges this
        # lease target with any bounded capture boost before touching the wire.
        controller = camera_cadence_controller()
        if fps > 0:
            delivered = await controller.acquire(
                device_id,
                fps,
                hub=self._hub,
                reason=f"preview_{reason}",
            )
        else:
            delivered = await controller.release(
                device_id,
                hub=self._hub,
                reason=f"preview_{reason}",
            )
        logger.debug(
            "[camera_preview] lease target device_id=%s fps=%d "
            "reason=%s delivered=%d subscribers=%d",
            device_id,
            fps,
            reason,
            delivered,
            subscribers,
        )
        return delivered


__all__ = [
    "CameraPreviewLeaseManager",
    "CAMERA_PREVIEW_MAX_FPS",
    "CAMERA_PREVIEW_STOP_RETRY_DELAYS",
    "DEFAULT_CAMERA_PREVIEW_FPS",
    "DEFAULT_CAMERA_PREVIEW_REFRESH_SECONDS",
    "parse_preview_fps",
    "preview_target_fps",
]
