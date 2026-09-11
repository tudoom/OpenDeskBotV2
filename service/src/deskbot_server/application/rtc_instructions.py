"""让语音 Agent（LiveKit worker，另一个进程）替 Core 说一句：主动陪伴 / 定时提醒用。

问题：语音 Agent 在线时，主人的每句话都由它接；如果主动开口走 Core 自己的播放通道，
那句话就不在语音 Agent 的对话历史里，主人回话时它接不上（2026-09-08 实测）。

做法：Core 只把「现在请你说什么」排进每台设备的指令队列；worker 每 ``POLL_SEC`` 秒
经 loopback 桥取走（``/internal/rtc/instructions``），用 ``generate_reply(instructions=…)``
让同一个 Agent 用自己的声音、自己的历史说出来，说完照常把轮次回传落库。
语音 Agent 不在线时调用方退回原来的 Core 播放路径。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("deskbot-server")

POLL_SEC = 1.0
DEFAULT_TTL_SEC = 45.0
MAX_PENDING_PER_DEVICE = 3

_queues: dict[str, deque[dict[str, Any]]] = {}
_attached_provider: Callable[[str], bool] | None = None
_stats: dict[str, Any] = {"enqueued": 0, "drained": 0, "expired": 0, "last_drain_at": 0.0}


def configure(*, attached_provider: Callable[[str], bool] | None) -> None:
    """Core 启动时注入「这台设备的语音 Agent 是否在线」判定。"""
    global _attached_provider
    _attached_provider = attached_provider


def agent_attached(device_id: str) -> bool:
    if _attached_provider is None:
        return False
    try:
        return bool(_attached_provider(str(device_id or "").strip()))
    except Exception:  # noqa: BLE001
        logger.debug("[rtc_instructions] attached_provider failed", exc_info=True)
        return False


def enqueue(
    device_id: str, text: str, *, source: str, ttl_sec: float = DEFAULT_TTL_SEC, tool_choice: Any = None,
    tools: list[str] | None = None,
) -> str:
    """排一条指令；同设备最多保留最近 MAX_PENDING_PER_DEVICE 条。返回指令 id。

    ``tool_choice`` / ``tools``：透传给语音 Agent 的 generate_reply（"required" + 只放结果工具 = 这轮必须标结果、不说话）。"""
    dev = str(device_id or "").strip()
    body = str(text or "").strip()
    if not dev or not body:
        raise ValueError("device_id and text are required")
    item = {
        "id": uuid.uuid4().hex[:12],
        "text": body,
        "source": str(source or "core"),
        "created_at": time.time(),
        "expires_at": time.time() + max(5.0, float(ttl_sec)),
        "tool_choice": tool_choice,
        "tools": [str(t) for t in tools] if tools else None,
    }
    queue = _queues.setdefault(dev, deque(maxlen=MAX_PENDING_PER_DEVICE))
    queue.append(item)
    _stats["enqueued"] += 1
    logger.info("[rtc_instructions] enqueued device_id=%s source=%s id=%s", dev, item["source"], item["id"])
    return item["id"]


def drain(device_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """取走并清空该设备的未过期指令（worker 轮询用）。"""
    dev = str(device_id or "").strip()
    queue = _queues.get(dev)
    if not queue:
        return []
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    while queue:
        item = queue.popleft()
        if item["expires_at"] < now:
            _stats["expired"] += 1
            continue
        row = {"id": item["id"], "text": item["text"], "source": item["source"]}
        if item.get("tool_choice") is not None:
            row["tool_choice"] = item["tool_choice"]
        if item.get("tools"):
            row["tools"] = list(item["tools"])
        out.append(row)
    if out:
        _stats["drained"] += len(out)
        _stats["last_drain_at"] = now
    return out


def pending_count(device_id: str) -> int:
    queue = _queues.get(str(device_id or "").strip())
    return len(queue) if queue else 0


def speak_via_agent(device_id: str, text: str, *, source: str) -> bool:
    """语音 Agent 在线 → 排队并返回 True；否则 False（调用方走 Core 播放）。"""
    dev = str(device_id or "").strip()
    if not dev or not agent_attached(dev):
        return False
    enqueue(dev, text, source=source)
    return True


def snapshot() -> dict[str, Any]:
    return {**_stats, "pending": {dev: len(q) for dev, q in _queues.items() if q}}


def _reset_for_tests() -> None:
    _queues.clear()
    _stats.update(enqueued=0, drained=0, expired=0, last_drain_at=0.0)


__all__ = [
    "DEFAULT_TTL_SEC",
    "MAX_PENDING_PER_DEVICE",
    "POLL_SEC",
    "agent_attached",
    "configure",
    "drain",
    "enqueue",
    "pending_count",
    "snapshot",
    "speak_via_agent",
]
