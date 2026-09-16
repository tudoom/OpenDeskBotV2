"""工具执行的唯一入口与渠道能力矩阵。

此前"执行一组 LLM 工具"有四份实现：``llm_tool_runner.execute_llm_tools``（同步实现本体）、
``execute_tools_round``（Core 异步：相机走帧缓存）、``execute_web_chat_tools``
（Flask 进程：相机走 Core HTTP 抓拍）、``execute_rtc_tool``（RTC 桥）。它们对
"哪些工具可用、拍照怎么取帧、确认怎么透传"各自作答，于是文字对话没有拍照、
语音端米家只读、文字端确认没透传——三个问题同一个根。

现在：
- ``TOOL_CAPABILITIES`` 是唯一的渠道 × 工具矩阵，任何新工具只在这里登记。
- ``execute_tools_async`` / ``execute_tools_sync`` 是唯一的两个入口，渠道只决定
  "相机帧从哪来"，其余工具一律交给 ``llm_tool_runner.execute_llm_tools``（含确认账本）。
- 旧函数名保留为薄封装，调用方与测试不必改。
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any, Callable, Optional

from deskbot_server import device_camera_frame_store
from deskbot_server.application import (
    agent_generation,
    agent_generation_tools,
    expression_runtime,
    llm_tool_runner,
    rtc_instructions,
    scene_perform,
    web_chat_capture,
)
from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY
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

# 设备专属（play_expression / move_head）：RTC 桥由 rtc_tool_service 自行分发；Core 主循环直接
# 走表情运行时 / 舵机发送；Flask 文字对话转 Core HTTP（2026-09-14 起文字也能"做个表情/动作"）。
DEVICE_TOOLS = frozenset({"move_head", "play_expression"})
# AI 生成（2026-09-14）：编表演 / 生成陪伴场景 / 设计动作 / 卡通套图 / 画表情 / 查进度，
# 实现见 application.agent_generation_tools，渠道差异只在"怎么落到设备"。
GENERATION_TOOLS = frozenset(
    {
        "generate_scene",
        "generate_quest_scene",
        "generate_motion",
        "generate_cartoon_faces",
        "generate_expression",
        "generation_status",
    }
)

# 通用工具：三条链路都可用。
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
        *DEVICE_TOOLS,
        *GENERATION_TOOLS,
    }
)
# 历史上 move_head / play_expression 只在 RTC；现在三条链路都有，保留名字给旧调用方。
_RTC_ONLY_TOOLS: frozenset[str] = frozenset()

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


def is_device_tool(name: str) -> bool:
    return str(name or "").strip().lower() in DEVICE_TOOLS


def is_generation_tool(name: str) -> bool:
    return str(name or "").strip().lower() in GENERATION_TOOLS


def _needs_channel_dispatch(name: str) -> bool:
    return is_capture_tool(name) or is_scene_tool(name) or is_device_tool(name) or is_generation_tool(name)


async def _core_play_expression(device_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("expression") or "").strip()
    if not name:
        return {"tool": "play_expression", "ok": False, "error": "play_expression 需要 name"}
    return await expression_runtime.play_rtc_expression(device_id, name, duration_ms=raw.get("duration_ms"))


async def _core_move_head(device_id: str, raw: dict[str, Any], asr_chat_hub: Any) -> dict[str, Any]:
    # 与 RTC 桥同一实现（预设查表 / 自编步骤限位分片）；rtc_tool_service 反过来 import 本模块，只能延迟导入
    from deskbot_server.application.rtc_tool_service import _move_head

    if asr_chat_hub is None:
        return {"tool": "move_head", "ok": False, "error": "设备通道未就绪"}
    return await _move_head(
        device_id=device_id,
        arguments={k: v for k, v in raw.items() if k not in ("tool", "name")},
        asr_chat_hub=asr_chat_hub,
        call_id=str(raw.get("operation_id") or ""),
    )


def _core_channel_ops(device_id: str, asr_chat_hub: Any):
    """Core 进程里生成工具的落地方式：协程交回事件循环跑（工具本身在 to_thread 里执行）。"""
    loop = asyncio.get_running_loop()

    def _wait(coro: Any, timeout: float) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    def _capture() -> bytes | None:
        result = _wait(
            device_camera_frame_store.capture_camera_for_device_async(device_id, hub=asr_chat_hub, display=False, require_fresh=True), 25.0
        )
        image = result.get(TRANSIENT_VISION_IMAGE_KEY) if isinstance(result, dict) else None
        data = image.get("bytes") if isinstance(image, dict) else None
        return data if isinstance(data, bytes) and data else None

    def _announce(text: str) -> None:
        # 语音 Agent 在线就让它用自己的声音说；不在线（文字对话 / 没会话）就静默
        try:
            rtc_instructions.speak_via_agent(device_id, text, source="agent_generation")
        except Exception:  # noqa: BLE001
            logger.debug("agent_generation announce failed", exc_info=True)

    return agent_generation_tools.ChannelOps(
        device_id=device_id,
        perform_scene=lambda name: _wait(scene_perform.perform_scene_for_device(device_id, name), 180.0),
        move_head=lambda args: _wait(_core_move_head(device_id, dict(args), asr_chat_hub), 30.0),
        play_expression=lambda args: _wait(_core_play_expression(device_id, dict(args)), 30.0),
        apply_default_expression=lambda: _wait(agent_generation.apply_default_expression_for_device(device_id), 30.0),
        capture_frame=_capture,
        announce=_announce,
    )


def _web_channel_ops(device_id: str, fetch: Callable[[str], tuple[int, str, bytes]] | None):
    """Flask 进程里生成工具的落地方式：一律转 Core HTTP；文字对话没有主动开口通道。"""
    def _capture() -> bytes | None:
        result = web_chat_capture.fetch_core_camera_capture(device_id, fetch=fetch)
        image = result.get(TRANSIENT_VISION_IMAGE_KEY) if isinstance(result, dict) else None
        data = image.get("bytes") if isinstance(image, dict) else None
        return data if isinstance(data, bytes) and data else None

    return agent_generation_tools.ChannelOps(
        device_id=device_id,
        perform_scene=lambda name: web_chat_capture.run_core_scene(device_id, name),
        move_head=lambda args: web_chat_capture.run_core_move_head(device_id, dict(args)),
        play_expression=lambda args: web_chat_capture.run_core_play_expression(device_id, dict(args)),
        apply_default_expression=lambda: web_chat_capture.run_core_apply_default_expression(device_id),
        capture_frame=_capture,
        announce=lambda _text: None,
    )


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
    del device_context  # 目前仅由调用方用于日志，执行器不消费
    items = [t for t in (tools or []) if isinstance(t, dict)]
    t0 = time.monotonic()
    if not any(_needs_channel_dispatch(_tool_name(t)) for t in items):
        results = await asyncio.to_thread(
            llm_tool_runner.execute_llm_tools, items, device_id=device_id, user_confirmed=user_confirmed
        )
        _emit_tool_event(channel, results, int((time.monotonic() - t0) * 1000))
        return results

    results: list[dict[str, Any]] = []
    ops = None
    for raw in items:
        name = _tool_name(raw)
        if is_capture_tool(name):
            cap = await device_camera_frame_store.capture_camera_for_device_async(
                device_id,
                hub=asr_chat_hub,
                display=(allow_camera_display and raw.get("display") is True),
            )
            results.append({"tool": name, **cap})
        elif is_scene_tool(name):
            results.append(await scene_perform.perform_scene_for_device(device_id, str(raw.get("name") or raw.get("scene") or "")))
        elif name == "play_expression":
            results.append(await _core_play_expression(device_id, raw))
        elif name == "move_head":
            results.append(await _core_move_head(device_id, raw, asr_chat_hub))
        elif is_generation_tool(name):
            if ops is None:
                ops = _core_channel_ops(device_id, asr_chat_hub)
            results.append(await asyncio.to_thread(agent_generation_tools.execute_generation_tool, raw, ops=ops))
        else:
            results.extend(
                await asyncio.to_thread(
                    llm_tool_runner.execute_llm_tools, [raw], device_id=device_id, user_confirmed=user_confirmed
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
    results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    t0 = time.monotonic()
    ops = None

    def _flush() -> None:
        if pending:
            results.extend(
                llm_tool_runner.execute_llm_tools(
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
            cap = web_chat_capture.fetch_core_camera_capture(device_id or "", fetch=fetch)
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
            results.append(web_chat_capture.run_core_scene(device_id or "", str(raw.get("name") or raw.get("scene") or ""), fetch=fetch))
        elif name == "play_expression":
            _flush()
            results.append(web_chat_capture.run_core_play_expression(device_id or "", raw))
        elif name == "move_head":
            _flush()
            results.append(web_chat_capture.run_core_move_head(device_id or "", raw))
        elif is_generation_tool(name):
            _flush()
            if ops is None:
                ops = _web_channel_ops(device_id or "", fetch)
            results.append(agent_generation_tools.execute_generation_tool(raw, ops=ops))
        else:
            pending.append(raw)
    _flush()
    _emit_tool_event(channel, results, int((time.monotonic() - t0) * 1000))
    return results


__all__ = [
    "CAPTURE_TOOLS",
    "DEVICE_TOOLS",
    "GENERATION_TOOLS",
    "SCENE_TOOLS",
    "TOOL_CAPABILITIES",
    "ToolChannel",
    "execute_tools_async",
    "execute_tools_sync",
    "is_capture_tool",
    "is_device_tool",
    "is_generation_tool",
    "is_scene_tool",
    "tools_for_channel",
]
