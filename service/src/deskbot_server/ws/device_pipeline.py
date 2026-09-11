from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.constants import DEVICE_PIPELINE_MAX_EVENTS
from deskbot_server.util import (
    _extract_device_id,
    _format_ts,
    _json_msg,
    _parse_query,
    _peer_str,
    _split_path,
    _ws_request_path,
)
from deskbot_server.ws.ws_send import _PerWsFireAndForget, _safe_send

logger = logging.getLogger("deskbot-server")

# 高频自动下发在流水窗口内按设备去重（只保留最新一条，避免淹没对话记录）
_PIPELINE_DEDUPE_SOURCES = frozenset({"auto_idle_silence"})


def _pipeline_dedupe_request_id(source: str, device_id: str) -> Optional[str]:
    if source in _PIPELINE_DEDUPE_SOURCES:
        return f"__{source}__:{device_id}"
    return None


class DevicePipelineBroker:
    """维护设备流水线事件的滚动窗口（默认最近 100 条，最新在前），并向订阅者实时广播。

    - 生产者：设备或上游服务把 ASR/LLM/TTS 各阶段的指标推过来。
    - 订阅者：web 后台订阅全部设备或按 device_id 过滤。
    """

    def __init__(self, max_events: int = DEVICE_PIPELINE_MAX_EVENTS) -> None:
        self._max_events = max_events
        self._events: deque = deque(maxlen=max_events)
        # ws -> Optional[str] 过滤的 device_id；None 表示全部
        self._subscribers: dict = {}
        self._lock = asyncio.Lock()
        self._seq = 0
        self._fanout = _PerWsFireAndForget()

    @property
    def max_events(self) -> int:
        return self._max_events

    async def has_subscribers_for_device(self, device_id: Optional[str] = None) -> bool:
        """是否有调试订阅者在监听该设备（或全部设备）。"""
        device_id = str(device_id or "").strip() or None
        async with self._lock:
            for _ws, flt in self._subscribers.items():
                if not flt or flt == device_id:
                    return True
        return False

    async def publish(self, event: dict) -> dict:
        async with self._lock:
            self._seq += 1
            evt = dict(event)
            evt["seq"] = self._seq
            if evt.get("received_ts") is None:
                evt["received_ts"] = time.time()
            if not evt.get("received_at"):
                evt["received_at"] = _format_ts(float(evt["received_ts"]))
            device_id = str(evt.get("device_id") or "unknown")
            evt["device_id"] = device_id
            source = str(evt.get("source") or "")
            dedupe_rid = _pipeline_dedupe_request_id(source, device_id)
            if dedupe_rid:
                evt["request_id"] = dedupe_rid
                self._events = deque(
                    (
                        e
                        for e in self._events
                        if e.get("request_id") != dedupe_rid
                    ),
                    maxlen=self._max_events,
                )
            else:
                rid = str(evt.get("request_id") or "").strip()
                if rid:
                    self._events = deque(
                        (e for e in self._events if e.get("request_id") != rid),
                        maxlen=self._max_events,
                    )
            self._events.appendleft(evt)

            targets = []
            for ws, flt in self._subscribers.items():
                if flt and flt != device_id:
                    continue
                targets.append(ws)

        msg = json.dumps({"type": "pipeline_event", "event": evt})
        for ws in targets:
            self._fanout.submit(ws, msg)
        return evt

    async def add_subscriber(self, ws, device_filter: Optional[str] = None) -> None:
        async with self._lock:
            self._subscribers[ws] = device_filter
            if device_filter:
                snap = [e for e in self._events if e.get("device_id") == device_filter]
            else:
                snap = list(self._events)
            max_events = self._max_events
        self._fanout.submit(
            ws,
            json.dumps(
                {
                    "type": "pipeline_snapshot",
                    "items": snap,
                    "device_filter": device_filter,
                    "max_events": max_events,
                }
            ),
        )

    async def remove_subscriber(self, ws) -> None:
        async with self._lock:
            self._subscribers.pop(ws, None)
        self._fanout.discard(ws)

    async def clear_device(self, device_id: str) -> int:
        """Remove a retired connection generation's in-memory event history."""

        did = str(device_id or "").strip()
        if not did:
            return 0
        async with self._lock:
            before = len(self._events)
            self._events = deque(
                (event for event in self._events if event.get("device_id") != did),
                maxlen=self._max_events,
            )
            return before - len(self._events)

    async def broadcast_to_device(self, device_id: str, payload: dict) -> None:
        """向订阅了该 device_id（或订阅全部）的订阅者广播一条原始消息，不进入事件窗口。

        用于 ``pipeline_stage`` 等实时推送——它们不是一轮完整事件，
        不适合进入 ``pipeline_recent`` 的滚动窗口。
        """
        device_id = str(device_id or "unknown")
        async with self._lock:
            targets = [
                ws
                for ws, flt in self._subscribers.items()
                if not flt or flt == device_id
            ]
        if not targets:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        for ws in targets:
            self._fanout.submit(ws, msg)

    def snapshot_events(self, device_id: Optional[str] = None, limit: int = 100) -> list:
        if device_id:
            items = [e for e in self._events if e.get("device_id") == device_id]
        else:
            items = list(self._events)
        if limit > 0:
            items = items[:limit]
        return items

async def publish_auto_dispatch_event(
    broker: Optional["DevicePipelineBroker"],
    *,
    device_id: str,
    request_id: str,
    source: str,
    summary: str,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    """无交互自动下发写入流水窗口（idle、定时任务等）。"""
    if broker is None or not device_id:
        return
    evt: dict = {
        "device_id": device_id,
        "request_id": request_id,
        "source": source,
        "summary": summary,
        "llm_text": summary,
        "status": status,
        "error": error,
    }
    await broker.publish(evt)


async def handle_device_pipeline(
    websocket,
    broker: DevicePipelineBroker,
) -> None:
    """Serve one authenticated browser pipeline subscriber.

    Physical devices and all producer roles are rejected by the router and
    again here as a defence-in-depth boundary. Device events enter the broker
    only from the in-process USB CDC pipeline.
    """
    req_path = _ws_request_path(websocket)
    _, query = _split_path(req_path)
    qargs = _parse_query(query)
    role = (qargs.get("role") or "").lower() or None
    is_subscriber = role in ("subscriber", "sub", "viewer", "consumer")
    device_filter = _extract_device_id(qargs)
    peer = _peer_str(websocket)

    if not is_subscriber:
        logger.warning(
            "[/device_pipeline] rejected non-subscriber peer=%s path=%s; "
            "device producers must use USB CDC",
            peer,
            _split_path(req_path)[0],
        )
        await _safe_send(
            websocket,
            _json_msg(
                {
                    "type": "error",
                    "error": "usb_cdc_required",
                    "message": "device producers must use USB CDC",
                }
            ),
        )
        await websocket.close(
            code=1008,
            reason="physical devices must use USB CDC",
        )
        return

    await _safe_send(
        websocket,
        _json_msg(
            {
                "type": "ready",
                "channel": "device_pipeline",
                "max_events": broker.max_events,
                "device_id": None,
                "device_filter": device_filter,
            }
        ),
    )

    try:
        logger.info(
            "[/device_pipeline] browser subscriber connected peer=%s "
            "device_filter=%s",
            peer,
            device_filter,
        )
        await broker.add_subscriber(websocket, device_filter)
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(message)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and data.get("type") == "ping":
                    await _safe_send(websocket, _json_msg({"type": "pong"}))
        finally:
            await broker.remove_subscriber(websocket)
    except ConnectionClosed as closed:
        logger.info("/device_pipeline browser subscriber closed: %s", closed)
