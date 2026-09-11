"""工具执行的唯一入口与渠道能力矩阵。

此前"执行一组 LLM 工具"有四份实现：``execute_llm_tools``（同步实现本体）、
``execute_tools_round``（Core 异步：相机走帧缓存）、``execute_web_chat_tools``
（Flask 进程：相机走 Core HTTP 抓拍）、``execute_rtc_tool``（RTC 桥）。它们对
"哪些工具可用、拍照怎么取帧、确认怎么透传"各自作答，于是文字对话没有拍照、
语音端米家只读、文字端确认没透传——三个问题同一个根。

现在：
- ``TOOL_CAPABILITIES`` 是唯一的渠道 × 工具矩阵，任何新工具只在这里登记。
- ``execute_tools_async`` / ``execute_tools_sync`` 是唯一的两个入口，渠道只决定
  "相机帧从哪来"，其余工具一律交给 ``execute_llm_tools``（含确认账本）。
- 旧函数名保留为薄封装，调用方与测试不必改。
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any, Callable, Optional

from deskbot_server.telemetry import emit

logger = logging.getLogger("deskbot-server")


class ToolChannel(str, enum.Enum):
    """工具调用来自哪条链路。决定相机取帧方式与可用集合。"""

    CORE = "core"  # Core 进程内：传统 ChatService / 主循环
    WEB = "web"  # Flask 进程：控制台文字对话（相机经 Core HTTP 抓拍）
    RTC = "rtc"  # RTC 桥（Core 进程内，设备已连接）


CAPTURE_TOOLS = frozenset({"capture_camera", "capture_and_describe"})
# 组合表演：要占设备 lane，和相机一样按渠道分别落地（Core 直跑 / Web 转 Core HTTP）
SCENE_TOOLS = frozenset({"perform_scene"})

# 通用工具：三条链路都可用。设备专属（play_expression / move_head）只在 RTC
# 桥里实现，由 rtc_tool_service 自行分发，不进入本执行器。
_COMMON_TOOLS = frozenset(
    {
        "capture_camera",
        "capture_and_describe",
        "memory_add",
        "memory_delete",
        "user_note",
        "schedule_task",
        "session",
        "miot",
        "webfetch",
        "websearch",
        "read",
        "write",
        "register_face",
        "update_task_result",
        "update_task_strategy",
        "propose_goal",
        "skip_goal",
        "perform_scene",
    }
)
_RTC_ONLY_TOOLS = frozenset({"move_head", "play_expression"})

TOOL_CAPABILITIES: dict[str, frozenset[ToolChannel]] = {
    **{name: frozenset(ToolChannel) for name in _COMMON_TOOLS},
    **{name: frozenset({ToolChannel.RTC}) for name in _RTC_ONLY_TOOLS},
}


def tools_for_channel(channel: ToolChannel) -> frozenset[str]:
    return frozenset(name for name, chans in TOOL_CAPABILITIES.items() if channel in chans)


def _emit_tool_event(channel: "ToolChannel", results: list[dict[str, Any]], ms: int) -> None:
    try:
        emit(
            "tool.exec",
            channel=channel.value,
            tools=",".join(str(r.get("tool") or "?") for r in results),
            ok=all(bool(r.get("ok")) for r in results) if results else True,
            ms=ms,
        )
    except Exception:  # noqa: BLE001
        logger.debug("_emit_tool_event failed", exc_info=True)


def _tool_name(raw: dict[str, Any]) -> str:
    return str(raw.get("tool") or raw.get("name") or "").strip()


def is_capture_tool(name: str) -> bool:
    return str(name or "").strip().lower() in CAPTURE_TOOLS


def is_scene_tool(name: str) -> bool:
    return str(name or "").strip().lower() in SCENE_TOOLS


async def execute_tools_async(
    tools: list[dict[str, Any]],
    *,
    channel: ToolChannel,
    device_id: str,
    user_confirmed: bool,
    asr_chat_hub: Optional[Any] = None,
    allow_camera_display: bool = False,
    device_context: str | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Core 进程内的异步执行：相机直接走帧缓存，其余交给同步实现。

    结果顺序与请求一致；同一轮里相机与非相机工具交错时按原顺序逐个执行。
    """
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    del device_context  # 目前仅由调用方用于日志，执行器不消费
    items = [t for t in (tools or []) if isinstance(t, dict)]
    t0 = time.monotonic()
    if not any(is_capture_tool(_tool_name(t)) or is_scene_tool(_tool_name(t)) for t in items):
        results = await asyncio.to_thread(
            execute_llm_tools, items, device_id=device_id, user_confirmed=user_confirmed
        )
        _emit_tool_event(channel, results, int((time.monotonic() - t0) * 1000))
        return results

    from deskbot_server.application.scene_perform import perform_scene_for_device
    from deskbot_server.device_camera_frame_store import capture_camera_for_device_async

    results: list[dict[str, Any]] = []
    for raw in items:
        name = _tool_name(raw)
        if is_capture_tool(name):
            cap = await capture_camera_for_device_async(
                device_id,
                hub=asr_chat_hub,
                display=(allow_camera_display and raw.get("display") is True),
            )
            results.append({"tool": name, **cap})
        elif is_scene_tool(name):
            results.append(await perform_scene_for_device(device_id, str(raw.get("name") or raw.get("scene") or "")))
        else:
            results.extend(
                await asyncio.to_thread(
                    execute_llm_tools, [raw], device_id=device_id, user_confirmed=user_confirmed
                )
            )
    _emit_tool_event(channel, results, int((time.monotonic() - t0) * 1000))
    return results


def execute_tools_sync(
    tools: list[dict[str, Any]],
    *,
    channel: ToolChannel = ToolChannel.WEB,
    device_id: str | None,
    user_confirmed: bool = False,
    fetch: Callable[[str], tuple[int, str, bytes]] | None = None,
) -> list[dict[str, Any]]:
    """Flask 进程的同步执行：相机经 Core HTTP 抓拍，其余交给同步实现。"""
    from deskbot_server.application.llm_tool_runner import execute_llm_tools
    from deskbot_server.application.web_chat_capture import (
        fetch_core_camera_capture,
        run_core_scene,
    )

    results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def _flush() -> None:
        if pending:
            results.extend(
                execute_llm_tools(
                    list(pending), device_id=device_id or None, user_confirmed=user_confirmed
                )
            )
            pending.clear()

    for raw in tools or []:
        if not isinstance(raw, dict):
            continue
        name = _tool_name(raw)
        if is_capture_tool(name):
            _flush()
            cap = fetch_core_camera_capture(device_id or "", fetch=fetch)
            results.append({"tool": name, **cap})
            logger.info(
                "web 对话拍照 device_id=%s ok=%s bytes=%s error=%s",
                device_id,
                cap.get("ok"),
                cap.get("jpeg_bytes"),
                cap.get("error"),
            )
        elif is_scene_tool(name):
            _flush()
            results.append(run_core_scene(device_id or "", str(raw.get("name") or raw.get("scene") or ""), fetch=fetch))
        else:
            pending.append(raw)
    _flush()
    _emit_tool_event(channel, results, int((time.monotonic() - t0) * 1000))
    return results


__all__ = [
    "CAPTURE_TOOLS",
    "SCENE_TOOLS",
    "TOOL_CAPABILITIES",
    "ToolChannel",
    "execute_tools_async",
    "execute_tools_sync",
    "is_capture_tool",
    "is_scene_tool",
    "tools_for_channel",
]
