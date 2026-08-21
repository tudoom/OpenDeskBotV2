from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Optional

from deskbot_server.llm.utils import coerce_pb_v2_downlink_payload
from deskbot_server.pb.wire import device_pb_json_msg
from deskbot_server.settings import _is_pb_downlink_payload
from deskbot_server.ws.ws_send import (
    _pb_ws_chain_serial_lock,
    _PerWsFireAndForget,
    _safe_send,
    _stop_pb_device_downlink_worker,
    cancel_pb_device_downlink,
    enqueue_pb_device_downlink,
    enqueue_pb_device_downlink_unlocked,
)

logger = logging.getLogger("deskbot-server")


def _log_pb_tx_wire(
    device_id: str,
    payload: dict,
    wire: str,
    *,
    label: str = "",
    pcm_bytes: int = 0,
) -> None:
    """记录发往设备的 pb 下发帧。

    INFO 只打摘要（关键字段 + wire 长度）；完整 wire JSON（上限约 14KB/帧，
    每帧都打会刷爆 INFO 日志）仅在 DEBUG 级别输出，排障时开 DEBUG 即可复原。
    """
    tag = f" {label}" if label else ""
    bin_note = f" +binary={pcm_bytes}" if pcm_bytes else ""
    audio_n = int((payload.get("audio") or {}).get("next_bin_len") or 0)
    logger.info(
        "[pb TX]%s device_id=%s req=%s type=%s idx=%s chunk_ms=%s "
        "anim_n=%d servo_n=%d audio_next_bin_len=%d%s wire_bytes=%d",
        tag,
        device_id,
        payload.get("req"),
        payload.get("type"),
        payload.get("idx"),
        payload.get("chunk_ms"),
        len(payload.get("anim") or []) if isinstance(payload.get("anim"), list) else 0,
        len(payload.get("servo") or []) if isinstance(payload.get("servo"), list) else 0,
        audio_n,
        bin_note,
        len(wire),
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[pb TX]%s device_id=%s wire_json %s",
            tag,
            device_id,
            wire,
        )


class AsrChatHub:
    """按 device_id 索引当前所有 /asr_chat 长连接，允许其它通道主动下发消息。

    可将 ``face_info`` 写回同连接（与 ``device_pb_only`` 互斥）。

    ``device_pb_only`` 为 true 时：经 :meth:`send` 仅接受 ``pb_*`` 载荷，且与同连接 TTS 共用
    :func:`enqueue_pb_device_downlink` 队列顺序写出；其它载荷直接丢弃计数为 0。
    """

    def __init__(
        self,
        device_pb_only: bool = False,
        *,
        pipeline_broker: Optional[Any] = None,
    ) -> None:
        self._by_device: dict = {}
        self._lock = asyncio.Lock()
        # 给 ESP32 反压（比如它在播 TTS 时 RX 满）时不会卡住调用方
        self._fanout = _PerWsFireAndForget()
        self._device_pb_only = bool(device_pb_only)
        self.pipeline_broker = pipeline_broker
        self._online_listeners: set[Any] = set()

    def add_online_listener(self, listener) -> None:
        if callable(listener):
            self._online_listeners.add(listener)

    def remove_online_listener(self, listener) -> None:
        self._online_listeners.discard(listener)

    async def _notify_device_online(self, device_id: str) -> None:
        for listener in tuple(self._online_listeners):
            try:
                result = listener(device_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "[asr_chat_hub] online listener failed device_id=%s",
                    device_id,
                )

    async def attach(self, device_id: str, ws) -> None:
        if not device_id:
            return
        stale: list[Any] = []
        became_online = False
        async with self._lock:
            conns = self._by_device.get(device_id, set())
            became_online = not conns
            stale = [old for old in conns if old is not ws]
            self._by_device.setdefault(device_id, set()).add(ws)
        setattr(ws, "_asr_chat_pb_serial_queue", self._device_pb_only)
        for old in stale:
            await self._close_superseded_connection(device_id, old)
        if became_online:
            await self._notify_device_online(device_id)

    async def _close_superseded_connection(self, device_id: str, ws) -> None:
        """同 device 新连接接入时关闭旧 /asr_chat，避免 delivered=2 与 zombie 连接。"""
        logger.info(
            "[asr_chat_hub] 关闭同 device 旧 /asr_chat 连接 device_id=%s（新连接取代）",
            device_id,
        )
        await self.detach(device_id, ws)
        try:
            await ws.close(code=1000, reason="superseded by new connection")
        except Exception:
            logger.debug(
                "[asr_chat_hub] 旧连接 close 异常 device_id=%s",
                device_id,
                exc_info=True,
            )

    async def detach(self, device_id: str, ws) -> None:
        if not device_id:
            return
        async with self._lock:
            conns = self._by_device.get(device_id)
            if conns is None:
                return
            conns.discard(ws)
            if not conns:
                self._by_device.pop(device_id, None)
        await _stop_pb_device_downlink_worker(ws)
        self._fanout.discard(ws)

    async def first_ws(self, device_id: str):
        """返回该 device 任意一条已连接的 ``/asr_chat`` WebSocket（供 HTTP 下行复用）。"""
        if not device_id:
            return None
        async with self._lock:
            conns = self._by_device.get(device_id, ())
            return next(iter(conns), None) if conns else None

    async def first_connected_device_id(self) -> str | None:
        """Return a currently connected hardware route for local-only work."""

        async with self._lock:
            for device_id, connections in self._by_device.items():
                if connections:
                    return str(device_id)
        return None

    async def cancel_playback(self, device_id: str) -> int:
        """Cancel every active PB modality for the device immediately."""

        if not device_id:
            return 0
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        wire = device_pb_json_msg({"type": "pb_cancel"})
        results = await asyncio.gather(
            *(cancel_pb_device_downlink(ws, wire) for ws in targets),
            return_exceptions=True,
        )
        return sum(result is True for result in results)

    async def send(self, device_id: str, payload: dict, *, skip_idle_refresh: bool = False) -> int:
        if not device_id:
            return 0
        payload = coerce_pb_v2_downlink_payload(payload)
        if self._device_pb_only and not _is_pb_downlink_payload(payload):
            return 0
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        wire = device_pb_json_msg(payload)
        _log_pb_tx_wire(device_id, payload, wire, label="single")
        sent = 0
        for ws in targets:
            if getattr(ws, "_asr_chat_pb_serial_queue", False):
                await enqueue_pb_device_downlink(ws, wire, None)
                sent += 1
            elif self._fanout.submit(ws, wire):
                sent += 1
        return sent

    async def send_pb_chain_ordered(
        self,
        device_id: str,
        frames: list[dict],
        *,
        pcm_per_frame: Optional[list[Optional[bytes]]] = None,
        binaries_per_frame: Optional[list[list[bytes]]] = None,
    ) -> int:
        """按顺序逐帧下发 pb JSON（经 :func:`_json_msg`），可选每帧紧随 PCM。

        ``device_pb_only`` 连接上整链持 :func:`_pb_ws_chain_serial_lock` 后经
        :func:`enqueue_pb_device_downlink_unlocked` 入队，避免协程间插队导致仅首帧到达；
        否则仍 ``await`` :func:`_safe_send` / :func:`_safe_send_pb_json_then_pcm`。
        """
        if not device_id or not frames:
            return 0
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        n_frames = sum(1 for f in frames if isinstance(f, dict))
        n = 0
        chain_idx = 0
        for ws in targets:
            if getattr(ws, "_asr_chat_pb_serial_queue", False):
                async with _pb_ws_chain_serial_lock(ws):
                    for i, payload in enumerate(frames):
                        if not isinstance(payload, dict):
                            continue
                        payload = coerce_pb_v2_downlink_payload(payload)
                        wire = device_pb_json_msg(payload)
                        bins: list[bytes] = []
                        if binaries_per_frame is not None and i < len(binaries_per_frame):
                            bins = list(binaries_per_frame[i] or [])
                        elif pcm_per_frame is not None and i < len(pcm_per_frame):
                            raw_pcm = pcm_per_frame[i]
                            if raw_pcm:
                                bins = [raw_pcm]
                        chain_idx += 1
                        _log_pb_tx_wire(
                            device_id,
                            payload,
                            wire,
                            label=f"chain {chain_idx}/{n_frames}",
                            pcm_bytes=sum(len(b) for b in bins),
                        )
                        await enqueue_pb_device_downlink_unlocked(ws, wire, binaries=bins)
                        n += 1
            else:

                for i, payload in enumerate(frames):
                    if not isinstance(payload, dict):
                        continue
                    payload = coerce_pb_v2_downlink_payload(payload)
                    wire = device_pb_json_msg(payload)
                    bins: list[bytes] = []
                    if binaries_per_frame is not None and i < len(binaries_per_frame):
                        bins = list(binaries_per_frame[i] or [])
                    elif pcm_per_frame is not None and i < len(pcm_per_frame):
                        raw_pcm = pcm_per_frame[i]
                        if raw_pcm:
                            bins = [raw_pcm]
                    chain_idx += 1
                    _log_pb_tx_wire(
                        device_id,
                        payload,
                        wire,
                        label=f"chain {chain_idx}/{n_frames}",
                        pcm_bytes=sum(len(b) for b in bins),
                    )
                    if bins:
                        from deskbot_server.ws.ws_send import _safe_send_pb_json_then_binaries

                        ok_t, ok_b = await _safe_send_pb_json_then_binaries(ws, wire, bins)
                        if not (ok_t and ok_b):
                            continue
                    else:
                        if not await _safe_send(ws, wire):
                            continue
                    n += 1
        return n
