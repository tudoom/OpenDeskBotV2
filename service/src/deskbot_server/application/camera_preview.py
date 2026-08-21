"""Reference-counted camera streaming leases for browser previews."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from deskbot_server.application.camera_cadence import camera_cadence_controller
from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb

logger = logging.getLogger("deskbot-server")

DEFAULT_CAMERA_PREVIEW_FPS = 2
DEFAULT_CAMERA_PREVIEW_REFRESH_SECONDS = 10.0
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
        self._counts: dict[str, int] = {}
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
        return self._fps if self._counts.get(dev, 0) > 0 else 0

    async def acquire(self, device_id: str) -> bool:
        """Acquire one preview lease, starting the device on the first lease."""

        dev = str(device_id or "").strip()
        if not dev:
            return False
        first = False
        async with self._state_lock:
            if self._closed:
                return False
            count = self._counts.get(dev, 0) + 1
            self._counts[dev] = count
            if count == 1:
                first = True
                self._refresh_tasks[dev] = asyncio.create_task(
                    self._refresh_device(dev),
                    name=f"camera-preview-refresh:{dev}",
                )
        if first:
            await self._sync_device(dev, reason="first_subscriber")
        return True

    async def release(self, device_id: str) -> None:
        """Release one lease, stopping only after the final device subscriber."""

        dev = str(device_id or "").strip()
        if not dev:
            return
        refresh_task: asyncio.Task[None] | None = None
        last = False
        async with self._state_lock:
            count = self._counts.get(dev, 0)
            if count <= 0:
                return
            if count > 1:
                self._counts[dev] = count - 1
                return
            last = True
            self._counts.pop(dev, None)
            refresh_task = self._refresh_tasks.pop(dev, None)
            if refresh_task is not None:
                refresh_task.cancel()
        if last:
            await self._sync_device(dev, reason="last_subscriber_left")
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
            devices = tuple(self._counts)
            self._counts.clear()
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
            if self._closed or self._counts.get(dev, 0) <= 0:
                return
        await self._sync_device(dev, reason="device_online")

    async def _refresh_device(self, device_id: str) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                async with self._state_lock:
                    if self._closed or self._counts.get(device_id, 0) <= 0:
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
                        before_fps = (
                            self._fps
                            if not self._closed
                            and self._counts.get(device_id, 0) > 0
                            else 0
                        )
                        subscribers = self._counts.get(device_id, 0)
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
                                still_stopped = (
                                    self._closed
                                    or self._counts.get(device_id, 0) <= 0
                                )
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
                        after_fps = (
                            self._fps
                            if not self._closed
                            and self._counts.get(device_id, 0) > 0
                            else 0
                        )
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
                        self._counts.get(device_id, 0) <= 0
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
    "CAMERA_PREVIEW_STOP_RETRY_DELAYS",
    "DEFAULT_CAMERA_PREVIEW_FPS",
    "DEFAULT_CAMERA_PREVIEW_REFRESH_SECONDS",
    "preview_target_fps",
]
