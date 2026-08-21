from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from deskbot_server.application.llm_error_fallback import build_llm_error_fallback_plan
from deskbot_server.application.llm_tool_loop import complete_llm_with_tool_loop
from deskbot_server.auto_reply import get_asr_voice_auto_reply_enabled
from deskbot_server.core.ports.downlink import DownlinkPort, PipelineEventsPort
from deskbot_server.core.types import ChatTurnResult
from deskbot_server.device_volume_store import persist_device_volume
from deskbot_server.log_privacy import safe_log_content
from deskbot_server.pb.shapes import PB_ACTION_APPEND, PB_ACTION_REPLACE
from deskbot_server.pb.wire import build_pb_wire_pairs, device_pb_json_msg, pb_wire_json_bytes
from deskbot_server.servo_protocol import pb_sequence_completion_budget_ms
from deskbot_server.tts.text_split import split_tts_by_punctuation
from deskbot_server.util import _ms_between

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.device_pipeline import DevicePipelineBroker
    from deskbot_server.ws.registry import DeviceRegistry

logger = logging.getLogger("deskbot-server")

_SCHEDULED_TASK_PREFIX = "[系统定时任务]"


class _TtsPrefetch:
    """LLM 流式输出中 ``tts`` 字段闭合后提前启动豆包 TTS 合成。"""

    def __init__(self, chat: "ChatService") -> None:
        self._chat = chat
        self.task: asyncio.Task | None = None

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def detach_task(self) -> asyncio.Task | None:
        task = self.task
        self.task = None
        return task

    async def on_ready(self, tts: str) -> None:
        text = (tts or "").strip()
        if not text:
            return
        self.cancel()
        self.task = asyncio.create_task(self._chat.tts_phoneme_segments(text))
        logger.info(
            "[LLM] 流式 tts 就绪，提前启动 TTS prefetch content=%s",
            safe_log_content(text),
        )


async def _play_interim_tts(
    downlink: DownlinkPort,
    chat: ChatService,
    text: str,
    prefetch: _TtsPrefetch,
    *,
    request_id: Optional[str],
    device_id: Optional[str],
    round_idx: int,
) -> None:
    """工具轮过渡语：复用流式 prefetch 任务，与工具执行并行下发 pb。"""
    playback = (text or "").strip()
    if not playback:
        return
    task = prefetch.detach_task()
    if task is None:
        task = asyncio.create_task(
            chat.tts_phoneme_segments(playback)
        )
    interim_result = ChatTurnResult()
    parsed = {
        "reply": playback,
        "moves": [],
        "anims": [],
        "json_ok": True,
        "need_reply": True,
        "raw": playback,
    }
    interim_rid = f"{request_id}_interim_{round_idx}" if request_id else None
    logger.info(
        "[LLM] 工具轮过渡 TTS device_id=%s req=%s round=%d content=%s",
        device_id,
        request_id,
        round_idx,
        safe_log_content(playback),
    )
    await downlink.emit_stage(
        "tts_start",
        request_id=interim_rid,
        send_client=False,
        event_fields={
            "tts_text": playback,
            "source": "llm_tool_interim",
            "stage": f"llm_tool_{round_idx}",
        },
    )
    await _run_pb_playback(
        downlink,
        chat,
        reply_text=playback,
        parsed=parsed,
        llm_scenes=[],
        request_id=interim_rid,
        device_id=device_id,
        result=interim_result,
        t_asr_start=None,
        prefetch_tts=task,
    )


async def _play_llm_error_fallback(
    downlink: DownlinkPort,
    chat: "ChatService",
    *,
    request_id: Optional[str],
    device_id: Optional[str],
    result: ChatTurnResult,
    asr_chat_hub: Optional[Any],
    t_asr_start: Optional[float],
    llm_exc: Exception,
) -> None:
    """LLM 调用失败：口播道歉并显示 thinking 表情，不触发舵机动作。"""
    if not get_asr_voice_auto_reply_enabled():
        return
    plan = build_llm_error_fallback_plan()
    playback = plan["tts"]
    parsed = plan["parsed"]
    fallback_rid = f"{request_id}_llm_err" if request_id else None

    logger.warning(
        "[LLM] 调用失败，启动兜底 TTS device_id=%s req=%s err=%s tts=%s",
        device_id,
        request_id,
        safe_log_content(str(llm_exc)),
        safe_log_content(playback),
    )
    result.llm_text = playback
    result.llm_raw = ""
    result.need_reply = True
    result.t_llm_end = time.monotonic()
    await downlink.emit_stage(
        "llm_error_fallback",
        request_id=fallback_rid,
        send_client=False,
        event_fields={
            "llm_text": playback,
            "error": str(llm_exc),
            "source": "asr" if t_asr_start is not None else "text",
        },
    )
    await downlink.emit_stage(
        "tts_start",
        request_id=fallback_rid,
        send_client=False,
        event_fields={
            "tts_text": playback,
            "source": "llm_error_fallback",
        },
    )
    await _run_pb_playback(
        downlink,
        chat,
        reply_text=playback,
        parsed=parsed,
        llm_scenes=[],
        request_id=fallback_rid,
        device_id=device_id,
        result=result,
        t_asr_start=t_asr_start,
    )


def _is_scheduled_task_user_text(user_text: str) -> bool:
    return str(user_text or "").strip().startswith(_SCHEDULED_TASK_PREFIX)


def _scheduled_task_description(user_text: str) -> str:
    text = str(user_text or "").strip().split("\n", 1)[0]
    m = re.search(
        r"请(?:向主人朗声提醒并)?执行以下任务(?:并向主人汇报结果)?[:：](.+)$",
        text,
    )
    if m:
        return m.group(1).strip()
    return text.replace(_SCHEDULED_TASK_PREFIX, "").strip()


def _scheduled_reminder_tts(description: str) -> str:
    desc = str(description or "").strip()
    if not desc:
        return "主人，提醒时间到了。"
    if desc.startswith("提醒"):
        body = desc[2:].strip() or "一下"
        return f"主人，该{body}啦。"
    return f"主人，{desc}。"


def _scheduled_tts_looks_like_meta_report(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    meta_markers = ("已发送", "已提醒", "已完成", "已执行", "汇报", "任务完成", "提醒过了")
    return any(m in t for m in meta_markers)


def _voice_was_played(result: ChatTurnResult) -> bool:
    if result.voice_auto_reply_off or result.error or result.status != "ok":
        return False
    return result.playback_status == "played"


async def run_chat_turn(
    downlink: DownlinkPort,
    chat: ChatService,
    user_text: str,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
    registry: Optional[DeviceRegistry] = None,
    t_asr_start: Optional[float] = None,
    t_asr_text: Optional[float] = None,
    force_voice: bool = False,
    pipeline_broker: Optional["DevicePipelineBroker"] = None,
    reuse_session_id: Optional[str] = None,
    make_session_current: bool = True,
    asr_chat_hub: Optional[Any] = None,
    playback_receipt_id: Optional[str] = None,
) -> ChatTurnResult:
    """在已有用户侧文本后执行 LLM + TTS/pb 管道（应用层，不依赖 WebSocket 类型）。"""
    result = ChatTurnResult()
    is_scheduled = _is_scheduled_task_user_text(user_text)
    sched_desc = _scheduled_task_description(user_text) if is_scheduled else ""
    session_id: Optional[str] = None
    session_turn_request_id = str(request_id or uuid.uuid4().hex[:16])
    assistant_session_recorded = False

    async def _set_interaction_state(
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        if registry is None or not device_id:
            return
        try:
            await registry.set_interaction_state(device_id, state, error=error)
        except Exception:
            logger.debug(
                "[interaction] state update failed device_id=%s state=%s",
                device_id,
                state,
                exc_info=True,
            )

    try:
        await _set_interaction_state("THINKING")
        if not force_voice and not get_asr_voice_auto_reply_enabled():
            now_m = time.monotonic()
            result.t_llm_end = now_m
            result.t_tts_synth_end = now_m
            result.t_tts_end = now_m
            result.voice_auto_reply_off = True
            logger.info(
                "[asr] 自动应答已关闭，跳过 LLM/TTS device_id=%s req=%s content=%s",
                device_id,
                request_id,
                safe_log_content(user_text),
            )
            await downlink.emit_stage(
                "voice_auto_reply_off",
                request_id=request_id,
                send_client=False,
                event_fields={
                    "asr_text": user_text,
                    "asr_ms": _ms_between(t_asr_start, t_asr_text),
                    "source": "asr" if t_asr_start is not None else "text",
                    "status": "ok",
                },
            )
            return result

        ack_ctx = None
        if registry is not None and device_id:
            ack_ctx = await registry.pb_ack_llm_context(device_id)

        history_messages: list[dict[str, str]] | None = None
        from deskbot_server.session_store import (
            ensure_active_session,
            session_history_for_llm,
        )

        if reuse_session_id:
            session_id = str(reuse_session_id).strip()
            if session_id:
                history_messages = session_history_for_llm(session_id)
        else:
            # Session rotation writes through atomic_store (fsync + cross-
            # process file lock, up to ~10s lock wait on Windows); keep it off
            # the realtime loop.  Per-session write order is preserved because
            # this turn awaits each session write before issuing the next.
            active = await asyncio.to_thread(
                ensure_active_session,
                user_text=user_text,
                make_current=make_session_current,
            )
            session_id = str(active.get("session_id") or "")
            if session_id:
                history_messages = session_history_for_llm(session_id)

        tts_prefetch = _TtsPrefetch(chat)

        async def _on_interim_tts_play(text: str, round_idx: int) -> None:
            await _set_interaction_state("SPEAKING")
            try:
                await _play_interim_tts(
                    downlink,
                    chat,
                    text,
                    tts_prefetch,
                    request_id=request_id,
                    device_id=device_id,
                    round_idx=round_idx,
                )
            finally:
                await _set_interaction_state("ACTING")

        parsed, llm_tools, tool_results, answer = await complete_llm_with_tool_loop(
            chat,
            user_text,
            device_id=device_id,
            device_context=ack_ctx,
            history_messages=history_messages,
            request_id=session_turn_request_id,
            dp_broker=pipeline_broker,
            pipeline_source="asr" if t_asr_start is not None else "text",
            on_tts_ready=tts_prefetch.on_ready,
            tts_prefetch=tts_prefetch,
            on_interim_tts_play=_on_interim_tts_play,
            on_interaction_state=_set_interaction_state,
            asr_chat_hub=asr_chat_hub,
        )

        reply_text = parsed["reply"]
        # Legacy model-authored ``anims``/``scenes`` bypassed the USB display
        # arbiter and could replace the Web/RTC face several times per reply.
        # Explicit face changes now go through the play_expression tool only.
        llm_scenes: list[str] = []
        llm_moves = list(parsed.get("moves") or [])
        llm_anims: list[dict[str, Any]] = []
        parsed["scenes"] = []
        parsed["anims"] = []
        need_reply = bool(parsed.get("need_reply", True))
        if is_scheduled:
            need_reply = True

        if parsed.get("volume") is not None and device_id:
            persist_device_volume(parsed["volume"])

        display_images = list(parsed.get("images") or [])
        display_images.extend(
            list(parsed.pop("_transient_display_images", []) or [])
        )
        parsed["images"] = display_images

        result.llm_text = reply_text
        result.llm_raw = answer or parsed.get("raw") or ""
        result.scenes = llm_scenes
        result.moves = llm_moves
        result.anims = llm_anims
        result.tools = llm_tools
        result.tool_results = tool_results
        result.need_reply = need_reply
        result.json_ok = parsed["json_ok"]
        result.t_llm_end = time.monotonic()

        if session_id:
            from deskbot_server.session_store import append_turn

            assistant_text = (reply_text or "").strip()
            if not assistant_text and not parsed.get("json_ok"):
                assistant_text = (answer or "").strip()
            try:
                await asyncio.to_thread(
                    append_turn,
                    session_id,
                    user_text,
                    assistant_text,
                    make_current=make_session_current,
                    request_id=session_turn_request_id,
                    assistant_delivery_status="generated",
                )
                assistant_session_recorded = bool(assistant_text)
            except Exception:
                logger.exception(
                    "[session] 保存对话失败 device_id=%s session_id=%s req=%s",
                    device_id,
                    session_id,
                    request_id,
                )

        llm_ms = _ms_between(t_asr_text, result.t_llm_end)
        logger.info(
            "[LLM] 回复 device_id=%s req=%s llm_ms=%s json_ok=%s need_reply=%s content=%s",
            device_id,
            request_id,
            llm_ms,
            parsed["json_ok"],
            need_reply,
            safe_log_content(parsed["raw"]),
        )
        await downlink.emit_stage(
            "llm_done",
            request_id=request_id,
            send_client=False,
            event_fields={
                "asr_text": user_text,
                "asr_ms": _ms_between(t_asr_start, t_asr_text),
                "llm_text": reply_text,
                "llm_raw": result.llm_raw,
                "llm_ms": llm_ms,
                "source": "asr" if t_asr_start is not None else "text",
            },
        )

        if not parsed["json_ok"]:
            logger.warning(
                "[LLM] 输出未通过 JSON 解析，按整段文本走 TTS。device_id=%s req=%s",
                device_id,
                request_id,
            )

        if not need_reply and not is_scheduled:
            tts_prefetch.cancel()
            has_motion = bool(llm_moves or parsed.get("images"))
            if has_motion:
                logger.info(
                    "[LLM] need_reply=false 但有 moves/anims/屏幕内容，下发动作 pb device_id=%s req=%s",
                    device_id,
                    request_id,
                )
                try:
                    await _set_interaction_state("ACTING")
                    await _run_pb_playback(
                        downlink,
                        chat,
                        reply_text="",
                        parsed=parsed,
                        llm_scenes=[],
                        request_id=request_id,
                        device_id=device_id,
                        result=result,
                        t_asr_start=t_asr_start,
                        motion_only=True,
                        playback_receipt_id=playback_receipt_id,
                    )
                except Exception as pb_exc:
                    logger.exception("[LLM] need_reply=false 动作 pb 失败")
                    result.status = "error"
                    result.error = f"motion_pb: {pb_exc}"
                return result
            logger.info(
                "[LLM] need_reply=false，跳过 TTS/pb。device_id=%s req=%s",
                device_id,
                request_id,
            )
            result.t_tts_end = time.monotonic()
            return result

        playback_text = (reply_text or "").strip()
        if is_scheduled and (
            not playback_text or _scheduled_tts_looks_like_meta_report(playback_text)
        ):
            playback_text = _scheduled_reminder_tts(sched_desc)
            logger.info(
                "[scheduler] 定时任务使用兜底提醒语 device_id=%s req=%s content=%s",
                device_id,
                request_id,
                safe_log_content(playback_text),
            )
        if not playback_text:
            if llm_moves:
                logger.info(
                    "[LLM] tts 为空但有 moves/anims，下发无音频动作 pb device_id=%s req=%s",
                    device_id,
                    request_id,
                )
                tts_prefetch.cancel()
                try:
                    await _set_interaction_state("ACTING")
                    await _run_pb_playback(
                        downlink,
                        chat,
                        reply_text="",
                        parsed=parsed,
                        llm_scenes=[],
                        request_id=request_id,
                        device_id=device_id,
                        result=result,
                        t_asr_start=t_asr_start,
                        motion_only=True,
                        playback_receipt_id=playback_receipt_id,
                    )
                except Exception as pb_exc:
                    logger.exception("[LLM] 无音频动作 pb 失败")
                    result.status = "error"
                    result.error = f"motion_pb: {pb_exc}"
                return result
            else:
                logger.info(
                    "[LLM] tts 为空且无 moves/anims，跳过 TTS/pb device_id=%s req=%s",
                    device_id,
                    request_id,
                )
                result.t_tts_end = time.monotonic()
                return result

        await downlink.emit_stage(
            "tts_start",
            request_id=request_id,
            send_client=False,
            event_fields={
                "asr_text": user_text,
                "llm_text": reply_text,
                "tts_text": playback_text,
                "source": "asr" if t_asr_start is not None else "text",
            },
        )
        await _set_interaction_state("SPEAKING")
        try:
            await _run_pb_playback(
                downlink,
                chat,
                reply_text=playback_text,
                parsed=parsed,
                llm_scenes=[],
                request_id=request_id,
                device_id=device_id,
                result=result,
                t_asr_start=t_asr_start,
                prefetch_tts=tts_prefetch.task,
                playback_receipt_id=playback_receipt_id,
            )
        except Exception as tts_exc:
            tts_prefetch.cancel()
            logger.exception("TTS 流程失败")
            result.status = "error"
            result.error = f"tts: {tts_exc}"
    except Exception as llm_exc:
        logger.exception("LLM 流程失败")
        result.status = "error"
        result.error = f"llm: {llm_exc}"
        try:
            await _set_interaction_state("SPEAKING")
            await _play_llm_error_fallback(
                downlink,
                chat,
                request_id=request_id,
                device_id=device_id,
                result=result,
                asr_chat_hub=asr_chat_hub,
                t_asr_start=t_asr_start,
                llm_exc=llm_exc,
            )
        except Exception as fallback_exc:
            logger.exception(
                "[LLM] 错误兜底 TTS/pb 失败 device_id=%s req=%s",
                device_id,
                request_id,
            )
            result.error = f"llm: {llm_exc}; fallback: {fallback_exc}"
    finally:
        if session_id and assistant_session_recorded:
            from deskbot_server.session_store import update_assistant_delivery

            delivery_status = str(result.playback_status or "none").strip().lower()
            if result.error and delivery_status != "played":
                delivery_status = "failed"
            elif delivery_status == "none":
                delivery_status = (
                    "not_requested"
                    if not result.need_reply or result.voice_auto_reply_off
                    else "failed"
                )
            try:
                # ``shield`` keeps the delivery-status write intact when this
                # turn is cancelled mid-finally (e.g. arbiter preemption): the
                # synchronous version always completed this write, and the
                # worker thread still finishes it even if the await is
                # interrupted.  It necessarily runs after ``append_turn``
                # because this ``finally`` block follows that await.
                await asyncio.shield(
                    asyncio.to_thread(
                        update_assistant_delivery,
                        session_id,
                        session_turn_request_id,
                        delivery_status,
                        error=result.error,
                    )
                )
            except Exception:
                logger.exception(
                    "[session] 更新交付状态失败 device_id=%s session_id=%s req=%s status=%s",
                    device_id,
                    session_id,
                    request_id,
                    delivery_status,
                )
        if result.error:
            await _set_interaction_state("ERROR", error=result.error)
        else:
            await _set_interaction_state("IDLE")

    return result


async def run_device_tts_only(
    downlink: DownlinkPort,
    chat: "ChatService",
    text: str,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
    scenes: Optional[list] = None,
    moves: Optional[list] = None,
    durable_replay: bool = False,
) -> ChatTurnResult:
    """跳过 LLM，将给定文本走音素 TTS 并下发 pb；可选在同一条链锁内追加场景 pb 帧。"""
    reply_text = (text or "").strip()
    result = ChatTurnResult()
    result.llm_text = reply_text
    result.t_llm_end = time.monotonic()
    await downlink.emit_stage(
        "tts_start",
        request_id=request_id,
        send_client=False,
        event_fields={
            "tts_text": reply_text,
            "source": "device_tts",
        },
    )
    parsed = {
        "reply": reply_text,
        "scenes": [],
        "json_ok": True,
        "need_reply": True,
        "raw": reply_text,
        "moves": list(moves or []),
        "anims": [],
    }
    if not reply_text:
        result.status = "error"
        result.error = "empty text"
        return result
    try:
        scene_list = [
            str(s).strip()
            for s in (scenes or [])
            if isinstance(s, str) and str(s).strip()
        ]
        if parsed["moves"]:
            scene_list = []
        expression_runtime = None
        if scene_list and device_id:
            from deskbot_server.application.expression_runtime import (
                get_expression_runtime,
            )

            expression_runtime = get_expression_runtime(device_id)
            if expression_runtime is None:
                result.status = "error"
                result.error = "expression runtime unavailable"
                return result
            for scene_name in scene_list:
                expression_result = await expression_runtime.play_scene(
                    scene_name,
                    source="manual_tts",
                    priority=25,
                    reason="device_tts_scene",
                    hold_ms=None,
                    persist_until_preempted=True,
                    wait_for_played=True,
                )
                if not expression_result.ok:
                    result.status = "error"
                    result.error = (
                        expression_result.error
                        or f"expression failed: {scene_name}"
                    )
                    return result
        await _run_pb_playback(
            downlink,
            chat,
            reply_text=reply_text,
            parsed=parsed,
            llm_scenes=[],
            request_id=request_id,
            device_id=device_id,
            result=result,
            t_asr_start=result.t_llm_end,
            durable_replay=durable_replay,
        )
    except Exception as tts_exc:
        logger.exception("[device_tts] TTS 流程失败 device_id=%s", device_id)
        result.status = "error"
        result.error = f"tts: {tts_exc}"
    return result


async def run_device_playbook(
    downlink: DownlinkPort,
    chat: "ChatService",
    playbook: dict,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
    durable_replay: bool = False,
) -> ChatTurnResult:
    """场景编排：按阶段串行下发（舵机 → 口播前表情 → 口播+并行轨）。"""
    from deskbot_server.scene_playbook_runner import playbook_to_phases

    phases = playbook_to_phases(playbook)
    if not phases:
        result = ChatTurnResult()
        result.status = "error"
        result.error = "empty playbook"
        return result

    result = ChatTurnResult()
    for pi, phase in enumerate(phases):
        phase_req = f"{request_id}_p{pi}" if request_id and len(phases) > 1 else request_id
        kind = str(phase.get("kind") or "speech")
        phase_anims = list(phase.get("anims") or [])
        if phase_anims:
            if not device_id:
                result.status = "error"
                result.error = "playbook expression requires device_id"
                return result
            from deskbot_server.application.expression_runtime import (
                get_expression_runtime,
            )

            expression_runtime = get_expression_runtime(device_id)
            if expression_runtime is None:
                result.status = "error"
                result.error = "expression runtime unavailable"
                return result
            for expression in phase_anims:
                name = str(expression.get("anim") or "").strip()
                if not name:
                    continue
                expression_result = await expression_runtime.play_scene(
                    name,
                    source="playbook",
                    priority=25,
                    reason=f"scene_playbook:{phase.get('chunk_id') or pi}",
                    hold_ms=expression.get("ms"),
                    wait_for_played=True,
                )
                if not expression_result.ok:
                    result.status = "error"
                    result.error = expression_result.error or f"expression failed: {name}"
                    return result
        if kind == "motion":
            phase_moves = list(phase.get("moves") or [])
            # A display-only phase was already played by the expression
            # runtime above. Do not manufacture an empty legacy PB afterwards:
            # it has no useful lane and can still disturb queue arbitration.
            if not phase_moves:
                continue
            parsed = {
                "reply": "",
                "scenes": [],
                "json_ok": True,
                "need_reply": True,
                "raw": "",
                "moves": phase_moves,
                "anims": [],
            }
            try:
                await _run_pb_playback(
                    downlink,
                    chat,
                    reply_text="",
                    parsed=parsed,
                    llm_scenes=[],
                    request_id=phase_req,
                    device_id=device_id,
                    result=result,
                    t_asr_start=result.t_llm_end or time.monotonic(),
                    motion_only=True,
                    durable_replay=durable_replay,
                )
            except Exception as exc:
                logger.exception("[scene_playbook] motion phase failed device_id=%s", device_id)
                result.status = "error"
                result.error = f"motion phase: {exc}"
                return result
            continue

        text = str(phase.get("text") or "").strip()
        if not text:
            text = "。"
        turn = await run_device_tts_only(
            downlink,
            chat,
            text,
            request_id=phase_req,
            device_id=device_id,
            scenes=None,
            moves=list(phase.get("moves") or []),
            durable_replay=durable_replay,
        )
        if turn.error:
            result.status = turn.status
            result.error = turn.error
            return result
        result = turn
    return result


async def _send_pb_pairs_body(
    downlink: DownlinkPort,
    *,
    pairs: list[tuple[dict, list[bytes]]],
    pb_req: str,
    device_id: Optional[str],
    n_pb: int,
    persist_played_receipt: bool = False,
    durable_replay: bool = False,
    played_timeout_sec: float | None = None,
) -> str:
    """Send one PB sequence and return ``failed|accepted|played``."""
    from deskbot_server.constants import PB_WAIT_ACK
    from deskbot_server.ws.pb_ack_waiter import (
        pb_ack_gate,
        pb_wait_ack_timeout_sec,
        pb_wait_played_timeout_sec,
    )

    send_pairs = pairs
    requires_durable_replay = durable_replay or persist_played_receipt
    if requires_durable_replay and pairs:
        # Copy only the chain head so callers can safely reuse their template.
        # Firmware keeps ordinary chat in RAM, while explicit durable work
        # additionally consumes the bounded NVS reboot-replay window.
        durable_head = dict(pairs[0][0])
        durable_head["durable"] = True
        send_pairs = [(durable_head, pairs[0][1]), *pairs[1:]]

    total_playback_ms = pb_sequence_completion_budget_ms(
        message for message, _binaries in send_pairs
    )
    gate_started = bool(device_id and PB_WAIT_ACK)
    if gate_started:
        assert device_id is not None
        await pb_ack_gate.begin_req(device_id, pb_req)

    delivery = "accepted"
    fail_safe_reason: str | None = None
    final_idx = -1

    async def _fail_safe_cancel(reason: str) -> None:
        cancel = getattr(downlink, "cancel_pb_playback", None)
        if not callable(cancel):
            logger.error(
                "[pb TX] fail-safe cancel unavailable device_id=%s req=%s reason=%s",
                device_id,
                pb_req,
                reason,
            )
            return
        try:
            cancelled = bool(await cancel(pb_req))
        except Exception:
            logger.exception(
                "[pb TX] fail-safe cancel raised device_id=%s req=%s reason=%s",
                device_id,
                pb_req,
                reason,
            )
            return
        log = logger.warning if cancelled else logger.error
        log(
            "[pb TX] fail-safe cancel attempted device_id=%s req=%s "
            "reason=%s sent=%s",
            device_id,
            pb_req,
            reason,
            cancelled,
        )

    async def _cleanup() -> None:
        try:
            if fail_safe_reason is not None:
                await _fail_safe_cancel(fail_safe_reason)
        finally:
            if gate_started:
                assert device_id is not None
                await pb_ack_gate.end_req(device_id, pb_req)

    try:
        pb_aborted = False
        for i, (msg, binaries) in enumerate(send_pairs):
            try:
                final_idx = max(final_idx, int(msg.get("idx") or 0))
            except (TypeError, ValueError):
                pass
            audio_len = int((msg.get("audio") or {}).get("next_bin_len") or 0)
            wire_text = device_pb_json_msg(msg)
            logger.info(
                "[pb TX] %d/%d wire_json bytes=%d %s",
                i + 1,
                n_pb,
                pb_wire_json_bytes(msg),
                wire_text,
            )
            ok = await downlink.send_pb_wire(wire_text, binaries=binaries)
            if binaries:
                logger.info(
                    "[pb TX] %d/%d binary idx=%s parts=%d total_bytes=%d ok=%s",
                    i + 1,
                    n_pb,
                    msg.get("idx"),
                    len(binaries),
                    sum(len(b) for b in binaries),
                    ok,
                )
            elif not ok:
                logger.warning(
                    "[pb TX] %d/%d JSON 下发失败 idx=%s device_id=%s",
                    i + 1,
                    n_pb,
                    msg.get("idx"),
                    device_id,
                )
            if not ok:
                pb_aborted = True
                fail_safe_reason = "send_failed"
                logger.error(
                    "[pb TX] 中止下发 device_id=%s pb_req=%s 失败于 %d/%d idx=%s"
                    "（常见：上一包 binary 后 ESP32 断线）",
                    device_id,
                    pb_req,
                    i + 1,
                    n_pb,
                    msg.get("idx"),
                )
                break
            if (
                binaries
                and audio_len > 0
                and device_id
                and PB_WAIT_ACK
            ):
                ack_ok = await pb_ack_gate.wait_accepted(
                    device_id,
                    pb_req,
                    int(msg.get("idx") or 0),
                    timeout=pb_wait_ack_timeout_sec(),
                )
                if not ack_ok:
                    pb_aborted = True
                    fail_safe_reason = "accepted_unconfirmed"
                    logger.error(
                        "[pb TX] 中止下发 device_id=%s pb_req=%s "
                        "未收到 idx>=%s 的 pb_ack",
                        device_id,
                        pb_req,
                        msg.get("idx"),
                    )
                    break
        delivery = "failed" if pb_aborted else "accepted"
        if (
            not pb_aborted
            and final_idx >= 0
            and device_id
            and PB_WAIT_ACK
        ):
            timeout = (
                pb_wait_played_timeout_sec(total_playback_ms)
                if played_timeout_sec is None
                else max(0.1, float(played_timeout_sec))
            )
            played_ok = await pb_ack_gate.wait_played(
                device_id,
                pb_req,
                final_idx,
                timeout=timeout,
            )
            if played_ok:
                delivery = "played"
            else:
                delivery = "failed"
                fail_safe_reason = "played_unconfirmed"
                logger.error(
                    "[pb TX] playback completion timeout device_id=%s req=%s "
                    "final_idx=%s playback_ms=%s timeout_sec=%.3f",
                    device_id,
                    pb_req,
                    final_idx,
                    total_playback_ms,
                    timeout,
                )
    except asyncio.CancelledError:
        fail_safe_reason = "sender_cancelled"
        raise
    except Exception:
        fail_safe_reason = "sender_exception"
        raise
    finally:
        cleanup_task = asyncio.create_task(_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # A second cancellation must not let the coroutine return before
            # its out-of-band stop and ACK-gate cleanup have completed.
            await cleanup_task
            raise

    if delivery == "played" and device_id and persist_played_receipt:
        from deskbot_server.playback_receipts import record_playback_receipt

        await asyncio.to_thread(
            record_playback_receipt,
            device_id,
            pb_req,
            source="pb_ack",
        )
    return delivery


async def _set_half_duplex_mic(
    downlink: DownlinkPort,
    *,
    device_id: str,
    mode: str,
) -> bool:
    """Apply a USB media-transfer mic barrier and wait until firmware accepts it."""

    from deskbot_server.constants import PB_WAIT_ACK
    from deskbot_server.pb.mic_signal import build_mic_signal_pb
    from deskbot_server.ws.pb_ack_waiter import (
        pb_ack_gate,
        pb_wait_ack_timeout_sec,
    )

    req = uuid.uuid4().hex[:16]
    if PB_WAIT_ACK:
        await pb_ack_gate.begin_req(device_id, req)
    try:
        payload = build_mic_signal_pb(mic=mode, req=req)
        sent = await downlink.send_pb_wire(device_pb_json_msg(payload))
        if not sent:
            logger.warning(
                "[pb TX] USB half-duplex mic=%s send failed "
                "device_id=%s req=%s",
                mode,
                device_id,
                req,
            )
            return False
        if not PB_WAIT_ACK:
            return True
        accepted = await pb_ack_gate.wait_accepted(
            device_id,
            req,
            0,
            timeout=pb_wait_ack_timeout_sec(),
        )
        if not accepted:
            logger.warning(
                "[pb TX] USB half-duplex mic=%s ACK timeout "
                "device_id=%s req=%s",
                mode,
                device_id,
                req,
            )
        return accepted
    except Exception:
        logger.exception(
            "[pb TX] USB half-duplex mic=%s failed device_id=%s req=%s",
            mode,
            device_id,
            req,
        )
        return False
    finally:
        if PB_WAIT_ACK:
            await pb_ack_gate.end_req(device_id, req)


async def _send_pb_pairs(
    downlink: DownlinkPort,
    *,
    pairs: list[tuple[dict, list[bytes]]],
    pb_req: str,
    device_id: Optional[str],
    n_pb: int,
    persist_played_receipt: bool = False,
    durable_replay: bool = False,
    played_timeout_sec: float | None = None,
) -> str:
    """Send PB pairs, pausing USB mic traffic before the first media declaration."""

    half_duplex = bool(
        device_id
        and getattr(downlink, "half_duplex_media_mic", False)
        and any(bool(binaries) for _message, binaries in pairs)
    )
    if not half_duplex:
        return await _send_pb_pairs_body(
            downlink,
            pairs=pairs,
            pb_req=pb_req,
            device_id=device_id,
            n_pb=n_pb,
            persist_played_receipt=persist_played_receipt,
            durable_replay=durable_replay,
            played_timeout_sec=played_timeout_sec,
        )

    assert device_id is not None
    muted = await _set_half_duplex_mic(
        downlink,
        device_id=device_id,
        mode="mute",
    )
    if not muted:
        # The firmware may already have applied ``mic=mute`` even when its ACK
        # is lost on the busy USB link.  Always send the compensating open
        # command before aborting; otherwise the device remains online with
        # camera/control traffic working but no microphone uplink.
        restored = await _set_half_duplex_mic(
            downlink,
            device_id=device_id,
            mode="open",
        )
        if not restored:
            logger.error(
                "[pb TX] USB half-duplex mic recovery after mute failure "
                "unconfirmed device_id=%s",
                device_id,
            )
        return "failed"
    try:
        return await _send_pb_pairs_body(
            downlink,
            pairs=pairs,
            pb_req=pb_req,
            device_id=device_id,
            n_pb=n_pb,
            persist_played_receipt=persist_played_receipt,
            durable_replay=durable_replay,
            played_timeout_sec=played_timeout_sec,
        )
    finally:
        restored = await _set_half_duplex_mic(
            downlink,
            device_id=device_id,
            mode="open",
        )
        if not restored:
            logger.error(
                "[pb TX] USB half-duplex mic restore unconfirmed device_id=%s",
                device_id,
            )


async def _run_pb_playback(
    downlink: DownlinkPort,
    chat: ChatService,
    *,
    reply_text: str,
    parsed: dict,
    llm_scenes: list,
    request_id: Optional[str],
    device_id: Optional[str],
    result: ChatTurnResult,
    t_asr_start: Optional[float],
    motion_only: bool = False,
    prefetch_tts: asyncio.Task | None = None,
    playback_receipt_id: Optional[str] = None,
    durable_replay: bool = False,
) -> None:
    if motion_only:
        sr_pb = int(chat.tts_cfg.get("sample_rate") or 24000)
        segs: list[dict] = []
        text_chunks = [""]
    else:
        if prefetch_tts is not None:
            text_chunks = [reply_text]
        else:
            text_chunks = split_tts_by_punctuation(reply_text)
        if len(text_chunks) > 1:
            logger.info(
                "[TTS] 按标点分 %d 段 device_id=%s req=%s content=%s",
                len(text_chunks),
                device_id,
                request_id,
                safe_log_content(text_chunks),
            )

    pb_aborted = False
    total_pb = 0
    chunk_is_last = True
    prefetch_tts_task: asyncio.Task | None = prefetch_tts
    display_images = list(parsed.get("images") or [])
    image_runtime = None
    image_lease_token: str | None = None
    image_display_recorded = False
    mouth_runtime = None
    mouth_overlay_token: str | None = None

    if display_images:
        if not device_id:
            parsed["images"] = []
            display_images = []
        else:
            from deskbot_server.application.expression_runtime import (
                get_expression_runtime,
            )

            image_runtime = get_expression_runtime(device_id)
            if image_runtime is None:
                raise RuntimeError("expression runtime unavailable for TTS image")
            image_lease_token = await image_runtime.acquire_external_display(
                name="tts_image",
                title="对话图片",
                source="tts_image",
                priority=25,
                reason="chat_tts_image",
            )
            if image_lease_token is None:
                # A Web preview or another higher-priority display remains
                # authoritative. Audio is still allowed, but the image cannot
                # secretly replace that face.
                logger.info(
                    "[expression] TTS image deferred device_id=%s owner=%s",
                    device_id,
                    image_runtime.active_source,
                )
                parsed["images"] = []
                display_images = []

    # Ordinary PB TTS changes only the mouth layer.  Publish that distinction
    # to the common diagnostics without recording it as another full face.
    if device_id and not motion_only and not display_images:
        from deskbot_server.application.expression_runtime import (
            get_expression_runtime,
        )

        mouth_runtime = get_expression_runtime(device_id)

    try:
        async with downlink.pb_serial_chain():
            for chunk_i, chunk_text in enumerate(text_chunks):
                if motion_only:
                    segs_local = segs
                    sr_pb = int(chat.tts_cfg.get("sample_rate") or 24000)
                else:
                    if prefetch_tts_task is None:
                        prefetch_tts_task = asyncio.create_task(
                            chat.tts_phoneme_segments(chunk_text)
                        )
                    sr_pb, segs_local = await prefetch_tts_task
                    prefetch_tts_task = None
                    result.t_tts_synth_end = time.monotonic()
                    result.playback_status = "synthesized"
                    pcm_ok = any(len(s.get("pcm") or b"") > 0 for s in segs_local)
                    if not segs_local or not pcm_ok:
                        raise RuntimeError(
                            "phoneme TTS 无分片或无 PCM: "
                            f"{safe_log_content(chunk_text)}"
                        )
                    if chunk_i + 1 < len(text_chunks):
                        prefetch_tts_task = asyncio.create_task(
                            chat.tts_phoneme_segments(text_chunks[chunk_i + 1])
                        )

                chunk_is_first = chunk_i == 0
                chunk_is_last = chunk_i == len(text_chunks) - 1
                # 组帧含 Opus 编码（最长 10s PCM/chunk）与 PCM 对齐等纯 CPU
                # 工作，放到工作线程执行；分片在本 for 循环内逐个 await，
                # 顺序与原实现一致。
                pairs, pb_req, n_pb, sr_pb = await asyncio.to_thread(
                    build_pb_wire_pairs,
                    segs_local,
                    chat.tts_cfg,
                    moves=list(parsed.get("moves") or []) if chunk_is_first else None,
                    anims=None,
                    sample_rate=sr_pb,
                    request_id=(
                        f"{request_id}_{chunk_i}"
                        if request_id and len(text_chunks) > 1
                        else request_id
                    ),
                    volume=parsed.get("volume") if chunk_is_first else None,
                    images=display_images or None,
                    action=PB_ACTION_REPLACE if chunk_is_first else PB_ACTION_APPEND,
                    mouth_only=(
                        not motion_only
                        and not display_images
                    ),
                )
                total_pb += n_pb
                result.playback_request_ids.append(pb_req)
                result.playback_status = "enqueued"

                image_fingerprint = None
                if display_images:
                    from deskbot_server.application.expression_runtime import (
                        fingerprint_pb_display_pairs,
                    )

                    image_fingerprint = fingerprint_pb_display_pairs(pairs)

                frame_overview = [
                    {
                        "i": i,
                        "type": m.get("type"),
                        "idx": m.get("idx"),
                        "chunk_ms": m.get("chunk_ms"),
                        "anim_n": len(m.get("anim") or []),
                        "phonemes": [
                            str(x.get("phoneme"))
                            for x in (m.get("anim") or [])
                            if isinstance(x, dict) and x.get("phoneme")
                        ],
                        "action": m.get("action"),
                        "bin_bytes": sum(len(b) for b in bins),
                    }
                    for i, (m, bins) in enumerate(pairs)
                ]
                logger.info(
                    "[pb TX] 段 %d/%d content=%s pb_req=%s segments=%d sr=%s",
                    chunk_i + 1,
                    len(text_chunks),
                    safe_log_content(chunk_text),
                    pb_req,
                    n_pb,
                    sr_pb,
                )
                logger.info("[pb TX] 帧序一览 %s", json.dumps(frame_overview, ensure_ascii=False))

                if mouth_runtime is not None and mouth_overlay_token is None:
                    # Mark the overlay only when its first PB is ready to send;
                    # TTS synthesis time is not visible mouth activity.
                    mouth_overlay_token = mouth_runtime.begin_mouth_overlay(
                        source="tts",
                        reason="phoneme_playback",
                        kind="phoneme",
                    )

                delivery = await _send_pb_pairs(
                    downlink,
                    pairs=pairs,
                    pb_req=pb_req,
                    device_id=device_id,
                    n_pb=n_pb,
                    persist_played_receipt=bool(playback_receipt_id),
                    durable_replay=durable_replay or bool(playback_receipt_id),
                )
                pb_aborted = delivery == "failed"
                if pb_aborted:
                    result.playback_status = "failed"
                    result.status = "error"
                    result.error = "playback delivery failed or ACK timed out"
                    if prefetch_tts_task is not None:
                        prefetch_tts_task.cancel()
                    break
                result.playback_status = delivery
                if (
                    delivery == "played"
                    and image_fingerprint is not None
                    and image_runtime is not None
                    and image_lease_token is not None
                    and not image_display_recorded
                ):
                    image_display_recorded = image_runtime.record_external_display(
                        image_lease_token,
                        name="tts_image",
                        title="对话图片",
                        source="tts_image",
                        reason="chat_tts_image",
                        fingerprint=image_fingerprint,
                    )

            if prefetch_tts_task is not None:
                prefetch_tts_task.cancel()

            # ``llm_scenes`` is retained temporarily for call compatibility.
            # Complete display changes are deliberately ignored here and must
            # already have gone through RtcExpressionRuntime before TTS starts.
    finally:
        if mouth_runtime is not None and mouth_overlay_token is not None:
            mouth_runtime.end_mouth_overlay(mouth_overlay_token)
        if image_runtime is not None and image_lease_token is not None:
            await image_runtime.release_external_display(
                image_lease_token,
                reason=(
                    "tts_image_complete"
                    if image_display_recorded and not pb_aborted
                    else "tts_image_failed"
                ),
                # Images live in the complete display/extra layer, whereas
                # firmware mouth-only restoration can only restore a mouth.
                # Always redraw the runtime's latest desired expression after
                # the external image lease ends, including successful playback.
                restore=True,
            )

    if (
        playback_receipt_id
        and device_id
        and not pb_aborted
        and result.playback_status == "played"
    ):
        from deskbot_server.playback_receipts import record_playback_receipts

        await asyncio.to_thread(
            record_playback_receipts,
            device_id,
            [playback_receipt_id, *result.playback_request_ids],
            source="chat_turn",
        )

    logger.info(
        "[pb TX] 下发结束 device_id=%s request_id=%s 语音 JSON=%d%s",
        device_id,
        request_id,
        total_pb,
        "（已中止）" if pb_aborted else "",
    )
    result.t_tts_end = time.monotonic()


async def publish_chat_turn(
    events: PipelineEventsPort,
    device_id: Optional[str],
    *,
    source: str,
    asr_text: Optional[str],
    t_asr_start: Optional[float],
    t_asr_text: Optional[float],
    turn: ChatTurnResult,
    request_id: Optional[str] = None,
) -> None:
    if not device_id:
        return
    flow = turn.as_dict()
    t_llm_end = flow.get("t_llm_end")
    t_tts_synth_end = flow.get("t_tts_synth_end")
    t_tts_end = flow.get("t_tts_end")
    end_t = t_tts_end or t_llm_end or t_asr_text
    evt = {
        "device_id": device_id,
        "request_id": request_id,
        "asr_text": asr_text,
        "asr_ms": _ms_between(t_asr_start, t_asr_text) if source == "asr" else None,
        "llm_text": flow.get("llm_text"),
        "llm_raw": flow.get("llm_raw"),
        "moves": list(flow.get("moves") or []),
        "anims": list(flow.get("anims") or []),
        "tools": list(flow.get("tools") or []),
        "tool_results": list(flow.get("tool_results") or []),
        "scenes": list(flow.get("scenes") or []),
        "json_ok": bool(flow.get("json_ok")),
        "need_reply": bool(flow.get("need_reply", True)),
        "voice_auto_reply_off": bool(flow.get("voice_auto_reply_off")),
        "llm_ms": _ms_between(t_asr_text, t_llm_end),
        "tts_text": flow.get("llm_text"),
        "tts_ms": _ms_between(t_llm_end, t_tts_synth_end),
        "pb_ms": _ms_between(t_tts_synth_end, t_tts_end),
        "e2e_ms": _ms_between(t_asr_start, end_t),
        "status": flow.get("status") or "ok",
        "error": flow.get("error"),
        "error_code": flow.get("error_code"),
        "error_retryable": bool(flow.get("error_retryable")),
        "provider_status": flow.get("provider_status"),
        "source": source,
    }
    await events.publish_turn(evt)
    await events.touch_device(device_id, evt["status"])
