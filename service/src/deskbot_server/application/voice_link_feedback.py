"""Device-side feedback while the RTC voice link is still warming up.

Cold start leaves a window (agent import / late gateway install / binding in
progress) where the user speaks and nothing answers.  When ESP-SR reports
``speech_start`` and ``rtc_device_active()`` is still False, /asr_chat asks
this module to flash a short "语音启动中…" expression so the device does not
look dead.  Delivery is fire-and-forget and throttled per device; it must
never block or raise into the VAD processing path.

勿扰时段不影响本反馈（勿扰只约束提醒类下发）；自动应答开关关闭时由
/asr_chat 的 speech_start 门控直接跳过，不会走到这里。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

logger = logging.getLogger("deskbot-server")

# 每设备反馈最小间隔：说话人一句话内可能出现多个 speech_start 边沿，
# 冷启动最长约 1 分钟，8 秒一次的节奏足以传达"在启动了"。
VOICE_NOT_READY_FEEDBACK_MIN_INTERVAL_SEC = 8.0

VOICE_NOT_READY_TEXT = "语音启动中…"

# thinking 语义最贴近"在准备/在忙"；resolve_scene 同时接受本地库场景名
# 与状态别名，库里没有 thinking 时退回 idle 映射（resolve_state）。
_VOICE_NOT_READY_SCENE = "thinking"

# 短 lease：表情播完后很快把显示权还给生命周期状态机。
_VOICE_NOT_READY_HOLD_MS = 2500

# 低于 agent(20)/manual_tts(25)/web(30)：绝不抢占任何真实内容源。
_VOICE_NOT_READY_PRIORITY = 10

# 可 monkeypatch 的时钟（测试控制节流窗口）。
_monotonic = time.monotonic

# 节流在 **投递成功后** 记账（play_frames 受理才写 _last_feedback_at）；
# 跳过路径（runtime 缺失 / scene 缺失 / 就绪竞态）不烧窗。in-flight 去重由
# _pending_feedback 承担，避免一句话内的多个 speech_start 边沿并发投递。
_last_feedback_at: dict[str, float] = {}
_pending_feedback: set[str] = set()
_feedback_tasks: set[asyncio.Task[None]] = set()


def reset_voice_not_ready_feedback_throttle(device_id: object = None) -> None:
    """Clear throttle state (all devices, or one). Test/rebind helper."""

    if device_id is None:
        _last_feedback_at.clear()
        _pending_feedback.clear()
        return
    dev = str(device_id or "").strip()
    _last_feedback_at.pop(dev, None)
    _pending_feedback.discard(dev)


def _overlay_status_text(
    frames: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    text: str,
) -> list[dict[str, Any]]:
    """Copy scene frames and stack the status text onto the ``extra`` layer."""

    from deskbot_server.pb.text_layout import text_primitives_from_block

    prims = text_primitives_from_block(text)
    out: list[dict[str, Any]] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        copied = copy.deepcopy(frame)
        # 表情目录场景帧统一为 frame.elements；没有该键就保底不叠字，
        # 交给 design_frames_to_pb_chain 做最终校验。
        elements = copied.get("elements")
        if isinstance(elements, dict) and prims:
            layer = elements.setdefault("extra", [])
            if isinstance(layer, list):
                layer.extend(copy.deepcopy(prims))
        out.append(copied)
    return out


def _make_feedback_task_done_callback(device_id: str):
    def _on_feedback_task_done(task: asyncio.Task[None]) -> None:
        _feedback_tasks.discard(task)
        _pending_feedback.discard(device_id)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(
                "[voice_link_feedback] feedback task failed error=%s",
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    return _on_feedback_task_done


def schedule_voice_not_ready_feedback(
    device_id: object,
) -> asyncio.Task[None] | None:
    """Fire-and-forget "语音启动中" feedback, throttled per device.

    Returns the created task (for tests) or None when throttled/invalid.
    Never blocks and never raises into the caller's path.  The throttle
    window is only charged after the expression was actually accepted by
    ``play_frames`` (see ``_deliver_voice_not_ready_feedback``); skipped
    deliveries leave the window open for the next ``speech_start`` edge.
    """

    dev = str(device_id or "").strip()
    if not dev:
        return None
    if dev in _pending_feedback:
        return None
    now = _monotonic()
    last = _last_feedback_at.get(dev)
    if last is not None and (now - last) < VOICE_NOT_READY_FEEDBACK_MIN_INTERVAL_SEC:
        return None
    _pending_feedback.add(dev)
    task = asyncio.create_task(_deliver_voice_not_ready_feedback(dev))
    _feedback_tasks.add(task)
    task.add_done_callback(_make_feedback_task_done_callback(dev))
    return task


async def _deliver_voice_not_ready_feedback(device_id: str) -> None:
    from deskbot_server.application.expression_runtime import (
        get_expression_runtime,
        load_expression_catalog,
    )

    runtime = get_expression_runtime(device_id)
    if runtime is None:
        logger.info(
            "[voice_link_feedback] no expression runtime; skipped device_id=%s",
            device_id,
        )
        return
    # 表情目录读取涉及本地文件 IO：走 to_thread，绝不在事件循环上内联。
    catalog = await asyncio.to_thread(load_expression_catalog)
    scene = catalog.resolve_scene(_VOICE_NOT_READY_SCENE)
    if scene is None:
        scene = catalog.resolve_state(_VOICE_NOT_READY_SCENE)
    if scene is None:
        logger.info(
            "[voice_link_feedback] no usable scene; skipped device_id=%s",
            device_id,
        )
        return

    from deskbot_server.rtc_runtime import rtc_device_active

    if rtc_device_active(device_id):
        # binding 在准备反馈期间就绪了：正常链路即将应答，不再打扰。
        logger.info(
            "[voice_link_feedback] binding became ready; skipped device_id=%s",
            device_id,
        )
        return

    frames = _overlay_status_text(scene.frames, VOICE_NOT_READY_TEXT)
    if not frames:
        return
    result = await runtime.play_frames(
        frames,
        name="voice_link_warmup",
        title=VOICE_NOT_READY_TEXT,
        source="voice_link_feedback",
        priority=_VOICE_NOT_READY_PRIORITY,
        reason="rtc_binding_not_ready",
        hold_ms=_VOICE_NOT_READY_HOLD_MS,
        wait_for_played=False,
    )
    if result is not None and getattr(result, "ok", False):
        # 投递成功才烧节流窗；被更高优先级 defer 等失败路径留窗重试。
        _last_feedback_at[device_id] = _monotonic()
    logger.info(
        "[voice_link_feedback] delivered device_id=%s scene=%s status=%s",
        device_id,
        scene.name,
        getattr(result, "status", None),
    )
