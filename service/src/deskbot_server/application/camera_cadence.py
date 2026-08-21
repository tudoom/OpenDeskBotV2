"""Per-device single writer for the continuous camera cadence.

设备连续帧率的**唯一真值**在这里计算：

    期望 fps = max(预览租约 fps, 当前有界 capture boost fps)

- 预览租约（``application.camera_preview``）通过 :meth:`acquire` /
  :meth:`release` 声明它期望的连续帧率；
- 拍照兜底流（``device_camera_frame_store``）通过 :meth:`boost` 声明一个
  **有界**的临时提速：进入时按合并后的期望值下发，退出时自动恢复到
  预览租约的期望值——不再需要各写者自己去查询/猜测恢复值，也没有任何
  写者会广播出掐死别人流的裸 0。
- RTC ``camera_once`` 快照不经本控制器（one-shot 不改连续 interval）。

对外仅暴露 ``acquire`` / ``release`` / ``boost`` 三个接口；发送本身
仍复用 ``build_cam_fps_signal_pb`` 与调用方注入的 hub。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb

logger = logging.getLogger("deskbot-server")


class CameraCadenceController:
    """Single writer that merges preview leases and bounded capture boosts."""

    def __init__(self) -> None:
        self._preview_fps: dict[str, int] = {}
        self._boost_fps: dict[str, list[int]] = {}
        # Per-device send-ordering locks.  Entries only live while a call is
        # in flight so a lock is never bound to a dead event loop.
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    # ------------------------------------------------------------------ state

    def _desired_fps(self, device_id: str) -> int:
        preview = self._preview_fps.get(device_id, 0)
        boosts = self._boost_fps.get(device_id) or []
        return max([preview, *boosts]) if boosts else preview

    @asynccontextmanager
    async def _ordered(self, device_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        self._lock_users[device_id] = self._lock_users.get(device_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            users = self._lock_users.get(device_id, 1) - 1
            if users > 0:
                self._lock_users[device_id] = users
            else:
                self._lock_users.pop(device_id, None)
                if self._locks.get(device_id) is lock:
                    self._locks.pop(device_id, None)

    async def _send_desired(
        self,
        device_id: str,
        hub: Any,
        *,
        reason: str,
    ) -> int:
        fps = self._desired_fps(device_id)
        if hub is None:
            return 0
        payload = build_cam_fps_signal_pb(cam_fps=fps)
        try:
            delivered = int(await hub.send(device_id, payload) or 0)
        except Exception:
            logger.warning(
                "[camera_cadence] cadence send failed device_id=%s fps=%d reason=%s",
                device_id,
                fps,
                reason,
                exc_info=True,
            )
            return 0
        logger.info(
            "[camera_cadence] cadence device_id=%s fps=%d reason=%s delivered=%d",
            device_id,
            fps,
            reason,
            delivered,
        )
        return delivered

    # -------------------------------------------------------------- interface

    async def acquire(
        self,
        device_id: str,
        fps: int,
        *,
        hub: Any,
        reason: str = "preview_lease",
    ) -> int:
        """Declare the preview-lease cadence for a device and send the merge.

        Idempotent: refresh calls simply re-send the current desired value.
        Returns the hub delivery count (0 on failure).
        """

        dev = str(device_id or "").strip()
        if not dev:
            return 0
        fps_value = max(0, int(fps))
        # Validate through the shared wire builder before mutating state.
        build_cam_fps_signal_pb(cam_fps=fps_value)
        async with self._ordered(dev):
            if fps_value > 0:
                self._preview_fps[dev] = fps_value
            else:
                self._preview_fps.pop(dev, None)
            return await self._send_desired(dev, hub, reason=reason)

    async def release(
        self,
        device_id: str,
        *,
        hub: Any,
        reason: str = "preview_release",
    ) -> int:
        """Drop the preview-lease cadence and send the merged desired value."""

        dev = str(device_id or "").strip()
        if not dev:
            return 0
        async with self._ordered(dev):
            self._preview_fps.pop(dev, None)
            return await self._send_desired(dev, hub, reason=reason)

    @asynccontextmanager
    async def boost(
        self,
        device_id: str,
        fps: int,
        *,
        hub: Any,
        reason: str = "capture_boost",
    ) -> AsyncIterator[None]:
        """Bounded capture boost: raise the cadence on enter, restore on exit.

        Exit always re-sends ``max(preview lease fps, remaining boosts)``, so
        leaving the boost can never kill an active browser preview stream and
        never leaves a temporary capture stream running.
        """

        dev = str(device_id or "").strip()
        fps_value = max(1, int(fps))
        if not dev:
            yield
            return
        async with self._ordered(dev):
            self._boost_fps.setdefault(dev, []).append(fps_value)
            await self._send_desired(dev, hub, reason=reason)
        try:
            yield
        finally:
            async with self._ordered(dev):
                stack = self._boost_fps.get(dev)
                if stack is not None:
                    try:
                        stack.remove(fps_value)
                    except ValueError:
                        pass
                    if not stack:
                        self._boost_fps.pop(dev, None)
                await self._send_desired(dev, hub, reason=f"{reason}_restore")


_controller = CameraCadenceController()


def camera_cadence_controller() -> CameraCadenceController:
    """Process-wide cadence single writer (state keyed per device_id)."""

    return _controller


__all__ = [
    "CameraCadenceController",
    "camera_cadence_controller",
]
