"""组合表演作为 Agent 的一个工具：``perform_scene``。

三条链路的落点：
- Core / RTC：``perform_scene_for_device`` 在事件循环里经 ``device_turn_arbiter`` 跑
  ``run_device_playbook``（口播 + 表情 + 动作串行下发），用户开口可打断；
- Web（Flask 进程）：``application.web_chat_capture.run_core_scene`` 转 Core 的
  ``/api/scene_playbook/run``；
- 定时提醒 / 主动陪伴已经在自己的 lane 里，用 ``run_scene_on_lane`` 直接跑，不再进 arbiter。

提示词侧：``scene_catalog_prompt``（最多 ``PROMPT_MAX_SCENES`` 段）列出可用表演。
Core 启动时 ``configure`` 注入 chat / hub / broker；未配置时工具返回可读错误。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from deskbot_server.application.chat_flow import run_device_playbook
from deskbot_server.application.turn_arbiter import PRIORITY_AUTOMATION, device_turn_arbiter
from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter
from deskbot_server.scene_playbooks_store import (
    PROMPT_MAX_SCENES,
    find_playbook_by_name,
    load_scene_playbooks_file,
    normalize_playbook,
    scene_catalog,
    scene_catalog_prompt,
)

logger = logging.getLogger("deskbot-server")

TOOL_NAME = "perform_scene"
SOURCE_AGENT_TOOL = "agent_perform_scene"

_state: dict[str, Any] = {"chat": None, "hub": None, "broker": None}


def configure(*, chat: Any, asr_chat_hub: Any, dp_broker: Any) -> None:
    """Core 启动时注入运行依赖（Flask 进程不调用）。"""
    _state.update(chat=chat, hub=asr_chat_hub, broker=dp_broker)


def resolve_scene(name: str) -> dict[str, Any] | None:
    key = str(name or "").strip()
    if not key:
        return None
    try:
        rows = load_scene_playbooks_file() or []
    except Exception:  # noqa: BLE001
        return None
    raw = find_playbook_by_name(rows, key)
    if raw is None:
        # 也允许用标题点名（模型常复述标题而不是 name）
        raw = next((r for r in rows if str(r.get("title") or "").strip() == key), None)
    if raw is None:
        return None
    try:
        return normalize_playbook(raw)
    except Exception:  # noqa: BLE001
        return None


async def run_scene_on_lane(
    downlink: Any, chat: Any, device_id: str, name: str, *, request_id: str | None = None
) -> dict[str, Any]:
    """在调用方已经持有的设备 lane 里跑一段表演（提醒 / 主动陪伴用）。"""
    pb = resolve_scene(name)
    if pb is None:
        return {"ok": False, "error": f"没有叫「{name}」的表演", "status": "not_found"}
    res = await run_device_playbook(
        downlink, chat, pb, request_id=request_id or f"scene_{uuid.uuid4().hex[:10]}", device_id=device_id
    )
    ok = (getattr(res, "status", "ok") or "ok") == "ok" and not getattr(res, "error", None)
    return {
        "ok": ok,
        "name": pb.get("name"),
        "title": pb.get("title") or pb.get("name"),
        "status": getattr(res, "status", "ok"),
        "error": getattr(res, "error", None),
    }


async def perform_scene_for_device(
    device_id: str, name: str, *, source: str = SOURCE_AGENT_TOOL, priority: int = PRIORITY_AUTOMATION
) -> dict[str, Any]:
    """Agent 工具入口（Core 进程）：找表演 → 进 arbiter → 跑完返回。"""
    dev = str(device_id or "").strip()
    if not dev:
        return {"tool": TOOL_NAME, "ok": False, "error": "没有目标设备"}
    key = str(name or "").strip()
    if not key:
        return {"tool": TOOL_NAME, "ok": False, "error": "perform_scene 需要 name"}
    chat, hub, broker = _state.get("chat"), _state.get("hub"), _state.get("broker")
    if chat is None or hub is None:
        return {"tool": TOOL_NAME, "ok": False, "error": "表演运行时还没就绪"}
    if resolve_scene(key) is None:
        names = "、".join(s["name"] for s in scene_catalog())
        return {"tool": TOOL_NAME, "ok": False, "error": f"没有叫「{key}」的表演；可用：{names or '（无）'}"}
    ws = await hub.first_ws(dev)
    if ws is None:
        return {"tool": TOOL_NAME, "ok": False, "error": "小歪没有连接，演不了"}
    downlink = WsDownlinkAdapter(ws, settings=chat.settings, device_id=dev, dp_broker=broker)
    req_id = f"scene_{uuid.uuid4().hex[:10]}"

    async def _run():
        return await run_scene_on_lane(downlink, chat, dev, key, request_id=req_id)

    try:
        out = await device_turn_arbiter.run(dev, _run, source=source, priority=priority, preemptible=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[perform_scene] 失败 device_id=%s name=%s", dev, key, exc_info=True)
        return {"tool": TOOL_NAME, "ok": False, "error": f"表演中断：{str(exc)[:120]}"}
    return {"tool": TOOL_NAME, **out}


__all__ = [
    "PROMPT_MAX_SCENES",
    "SOURCE_AGENT_TOOL",
    "TOOL_NAME",
    "configure",
    "perform_scene_for_device",
    "resolve_scene",
    "run_scene_on_lane",
    "scene_catalog",
    "scene_catalog_prompt",
]
