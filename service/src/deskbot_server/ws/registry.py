from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from deskbot_server.pipeline.mic_health import MIC_HEALTH_STATES
from deskbot_server.util import _format_ts

logger = logging.getLogger("deskbot-server")

INTERACTION_STATES = frozenset(
    {
        "CONNECTING",
        "AUTHENTICATING",
        "IDLE",
        "LISTENING",
        "THINKING",
        "ACTING",
        "SPEAKING",
        "ERROR",
        "OFFLINE",
    }
)


class DeviceRegistry:
    """维护当前设备传输会话，是 `/api/devices` 的唯一真源。

    - 每个设备一条记录：device_id + 在线状态 + 各通道（WebSocket 或 USB CDC）
      当前的活跃连接数 + 最近一次事件时间。
    - 生产者握手时调用 ``connect``，断开时调用 ``disconnect``；订阅者不入库。
    - 仅保留内存态，没有持久化（重启 deskbot-server 即清空）。
    """

    def __init__(
        self,
        *,
        offline_ttl_seconds: float = 60 * 60,
        max_devices: int = 1_000,
    ) -> None:
        self._devices: dict = {}
        self._ws_to_key: dict = {}
        self._ws_objects: dict[int, Any] = {}
        self._observers: dict[int, tuple[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._offline_ttl_seconds = max(60.0, float(offline_ttl_seconds))
        self._max_devices = max(10, int(max_devices))

    def _prune_locked(self, now: float) -> int:
        """Drop stale offline observations without touching active sockets."""
        stale = [
            device_id
            for device_id, dev in self._devices.items()
            if not dev.get("online")
            and now - float(dev.get("last_seen_ts") or 0.0)
            > self._offline_ttl_seconds
        ]
        for device_id in stale:
            self._devices.pop(device_id, None)

        overflow = max(0, len(self._devices) - self._max_devices)
        if overflow:
            offline = sorted(
                (
                    (float(dev.get("last_seen_ts") or 0.0), device_id)
                    for device_id, dev in self._devices.items()
                    if not dev.get("online")
                )
            )
            for _, device_id in offline[:overflow]:
                if self._devices.pop(device_id, None) is not None:
                    stale.append(device_id)
        return len(stale)

    async def connect(
        self,
        device_id: str,
        channel: str,
        ws,
        *,
        transport: str | None = None,
        session_generation: int | None = None,
    ) -> dict:
        if not device_id:
            return {}
        async with self._lock:
            now = time.time()
            self._prune_locked(now)
            dev = self._devices.get(device_id)
            is_new = dev is None
            if is_new:
                dev = {
                    "device_id": device_id,
                    "first_seen_ts": now,
                    "first_seen": _format_ts(now),
                    "channels": {},
                    "total_connections": 0,
                }
                self._devices[device_id] = dev
            chs = dev.setdefault("channels", {})
            chs[channel] = int(chs.get(channel) or 0) + 1
            dev["last_seen_ts"] = now
            dev["last_seen"] = _format_ts(now)
            dev["online"] = True
            transport_name = str(transport or "").strip()
            if transport_name:
                dev["transport"] = transport_name
            if session_generation is not None:
                dev["session_generation"] = int(session_generation)
            if channel == "asr_chat":
                dev["interaction_state"] = "AUTHENTICATING"
                dev["interaction_state_ts"] = now
                dev["interaction_error"] = None
            dev["total_connections"] = int(dev.get("total_connections") or 0) + 1
            self._ws_to_key[id(ws)] = (device_id, channel)
            self._ws_objects[id(ws)] = ws
            snapshot_ch = dict(chs)
            total_devices = len(self._devices)
        logger.info(
            "[DeviceRegistry] %s device_id=%s channel=%s channels=%s 设备表容量=%d",
            "注册新设备" if is_new else "复用已注册设备",
            device_id,
            channel,
            snapshot_ch,
            total_devices,
        )
        try:
            from deskbot_server.hardware_catalog import mark_device_seen

            await asyncio.to_thread(mark_device_seen, device_id)
        except Exception:
            logger.debug(
                "[DeviceRegistry] persist last_seen failed device_id=%s",
                device_id,
                exc_info=True,
            )
        return dict(dev)

    async def disconnect(self, ws) -> Optional[dict]:
        async with self._lock:
            key = self._ws_to_key.pop(id(ws), None)
            self._ws_objects.pop(id(ws), None)
            if key is None:
                return None
            device_id, channel = key
            dev = self._devices.get(device_id)
            if dev is None:
                return None
            now = time.time()
            chs = dev.setdefault("channels", {})
            remain = int(chs.get(channel) or 0) - 1
            if remain <= 0:
                chs.pop(channel, None)
            else:
                chs[channel] = remain
            dev["last_seen_ts"] = now
            dev["last_seen"] = _format_ts(now)
            dev["online"] = bool(chs)
            if not dev["online"]:
                dev["interaction_state"] = "OFFLINE"
                dev["interaction_state_ts"] = now
                dev["interaction_error"] = None
            elif channel == "asr_chat" and "asr_chat" not in chs:
                dev["interaction_state"] = "ERROR"
                dev["interaction_state_ts"] = now
                dev["interaction_error"] = "asr_chat disconnected"
            snapshot_ch = dict(chs)
            still_online = dev["online"]
            self._prune_locked(now)
        logger.info(
            "[DeviceRegistry] 注销 device_id=%s channel=%s 剩余通道=%s online=%s",
            device_id,
            channel,
            snapshot_ch,
            still_online,
        )
        return dict(dev)

    async def close_device(
        self,
        device_id: str,
        *,
        code: int = 4003,
        reason: str = "device route disconnected",
        timeout: float = 5.0,
    ) -> int:
        """Close every producer channel currently registered for one device."""

        did = str(device_id or "").strip()
        if not did:
            return 0
        async with self._lock:
            targets_by_id = {
                ws_id: self._ws_objects.get(ws_id)
                for ws_id, key in self._ws_to_key.items()
                if key[0] == did
            }
            targets_by_id.update(
                {
                    ws_id: ws
                    for ws_id, (observer_device_id, ws) in self._observers.items()
                    if observer_device_id == did
                }
            )
        targets = [ws for ws in targets_by_id.values() if ws is not None]
        if targets:
            async def _close(ws) -> None:
                try:
                    await ws.close(code=code, reason=reason)
                except Exception:
                    logger.debug(
                        "[DeviceRegistry] close failed device_id=%s",
                        did,
                        exc_info=True,
                    )

            try:
                await asyncio.wait_for(
                    asyncio.gather(*(_close(ws) for ws in targets)),
                    timeout=max(0.1, timeout),
                )
            except TimeoutError:
                logger.warning(
                    "[DeviceRegistry] close timeout device_id=%s targets=%d",
                    did,
                    len(targets),
                )
        return len(targets)

    async def add_observer(self, device_id: str, ws) -> bool:
        """Track a debug observer without marking the physical device online."""

        did = str(device_id or "").strip()
        if not did:
            return False
        async with self._lock:
            self._observers[id(ws)] = (did, ws)
        return True

    async def remove_observer(self, ws) -> None:
        async with self._lock:
            self._observers.pop(id(ws), None)

    async def wait_device_offline(
        self,
        device_id: str,
        *,
        timeout: float = 5.0,
    ) -> bool:
        """Wait until all producer handlers have run their disconnect cleanup."""

        did = str(device_id or "").strip()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            async with self._lock:
                active = any(
                    key[0] == did for key in self._ws_to_key.values()
                ) or any(
                    observer_device_id == did
                    for observer_device_id, _ws in self._observers.values()
                )
            if not active:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.025)

    async def forget_device(self, device_id: str) -> None:
        """Drop the retired claim's cached status after it is fully offline."""

        did = str(device_id or "").strip()
        async with self._lock:
            if any(
                key[0] == did for key in self._ws_to_key.values()
            ) or any(
                observer_device_id == did
                for observer_device_id, _ws in self._observers.values()
            ):
                return
            self._devices.pop(did, None)

    async def touch(self, device_id: str, status: Optional[str] = None) -> None:
        """`/asr_chat` 每完成一轮流水线时调用，刷新最后状态与时间。"""
        if not device_id:
            return
        async with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                return
            now = time.time()
            dev["last_seen_ts"] = now
            dev["last_seen"] = _format_ts(now)
            if status:
                dev["last_status"] = status
            dev["event_count"] = int(dev.get("event_count") or 0) + 1

    async def set_interaction_state(
        self,
        device_id: str,
        state: str,
        *,
        error: str | None = None,
    ) -> bool:
        """Record the device interaction state used by both Web and telemetry."""

        dev_id = str(device_id or "").strip()
        normalized = str(state or "").strip().upper()
        if not dev_id or normalized not in INTERACTION_STATES:
            return False
        async with self._lock:
            dev = self._devices.get(dev_id)
            if dev is None:
                return False
            now = time.time()
            dev["last_seen_ts"] = now
            dev["last_seen"] = _format_ts(now)
            dev["interaction_state"] = normalized
            dev["interaction_state_ts"] = now
            dev["interaction_state_at"] = _format_ts(now)
            dev["interaction_error"] = (
                str(error or "").strip()[:500] or None
                if normalized == "ERROR"
                else None
            )
            dev["event_count"] = int(dev.get("event_count") or 0) + 1
            return True

    async def set_microphone_health(
        self,
        device_id: str,
        health: dict[str, object],
    ) -> bool:
        """Publish a bounded acoustic-health snapshot for the PC console."""

        dev_id = str(device_id or "").strip()
        status = str((health or {}).get("status") or "").strip().lower()
        if not dev_id or status not in MIC_HEALTH_STATES:
            return False

        def _non_negative_number(name: str) -> float:
            try:
                return max(0.0, float(health.get(name) or 0.0))
            except (TypeError, ValueError):
                return 0.0

        payload: dict[str, object] = {
            "status": status,
            "observed_audio_ms": int(_non_negative_number("observed_audio_ms")),
            "window_audio_ms": int(_non_negative_number("window_audio_ms")),
            "ac_rms": round(_non_negative_number("ac_rms"), 2),
            "short_term_variation": round(
                _non_negative_number("short_term_variation"),
                2,
            ),
            "frame_count": int(_non_negative_number("frame_count")),
        }
        async with self._lock:
            dev = self._devices.get(dev_id)
            if dev is None:
                return False
            now = time.time()
            payload["updated_at"] = now
            payload["status_at"] = _format_ts(now)
            dev["microphone_health"] = payload
            dev["last_seen_ts"] = now
            dev["last_seen"] = _format_ts(now)
            return True

    async def record_pb_ack(self, device_id: str, ack: dict[str, Any]) -> None:
        """保存该设备最近一次上行的 ``pb_ack``（内存态，供 LLM 与调试页使用）。"""
        if not device_id or not isinstance(ack, dict):
            return
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        await pb_ack_gate.notify(device_id, ack)
        async with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                logger.warning(
                    "[pb_ack] 设备未在注册表，忽略 device_id=%s",
                    device_id,
                )
                return
            now = time.time()
            dev["last_pb_ack"] = dict(ack)
            dev["last_pb_ack_ts"] = now
            dev["last_pb_ack_mono"] = time.monotonic()

    async def pb_ack_llm_context(self, device_id: Optional[str]) -> Optional[str]:
        """返回该设备最近一次 ``pb_ack`` 的紧凑 JSON 字符串；无则 ``None``。"""
        if not device_id:
            return None
        async with self._lock:
            dev = self._devices.get(device_id)
            if not dev:
                return None
            ack = dev.get("last_pb_ack")
            if not isinstance(ack, dict):
                return None
            return json.dumps(ack, ensure_ascii=False)

    def snapshot(self) -> list:
        now = time.time()
        # ``snapshot`` is intentionally synchronous for the HTTP callback.
        # Filtering here prevents stale rows from leaking even if no new
        # socket event arrives to trigger physical pruning.
        items = [
            dict(d)
            for d in self._devices.values()
            if d.get("online")
            or now - float(d.get("last_seen_ts") or 0.0)
            <= self._offline_ttl_seconds
        ]
        items.sort(
            key=lambda d: float(d.get("last_seen_ts") or 0.0),
            reverse=True,
        )
        return items
