"""LLM 多轮 tool-call 循环：中间轮执行 tools，末轮走 TTS/pb。"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from deskbot_server.application.llm_tool_runner import (
    claim_llm_tool_round,
    execute_llm_tools,
    finish_llm_tool_round,
    mark_llm_tool_round_unknown,
)
from deskbot_server.application.tool_interim_tts import build_tool_interim_tts
from deskbot_server.device_camera_frame_store import capture_camera_for_device_async
from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.llm.vision_input import (
    TRANSIENT_VISION_IMAGE_KEY,
    LlmVisionUnsupportedError,
    build_openai_vision_content,
    messages_contain_images,
    redact_images_from_messages,
)
from deskbot_server.log_privacy import safe_log_content

if TYPE_CHECKING:
    from deskbot_server.application.chat_flow import _TtsPrefetch
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.asr_chat_hub import AsrChatHub

logger = logging.getLogger("deskbot-server")

MAX_LLM_TOOL_ROUNDS = 4

_CAPTURE_TOOLS = frozenset({"capture_camera"})
_TOOL_RESULT_STRIP_KEYS = frozenset(
    {
        "jpeg_base64",
        "image_display",
        TRANSIENT_VISION_IMAGE_KEY,
    }
)
_SIDE_EFFECT_TOOLS = frozenset(
    {
        "register_face",
        "memory_add",
        "memory_delete",
        "schedule_task",
        "scheduled_task",
        "session",
        "miot",
        "mihome",
        "mijia",
        "write",
    }
)


def _with_operation_ids(
    tools: list[dict[str, Any]],
    request_id: Optional[str],
    *,
    round_index: int = 0,
) -> list[dict[str, Any]]:
    """Attach server-owned request-slot ids to every possible side effect."""

    out: list[dict[str, Any]] = []
    for slot_index, raw in enumerate(tools):
        row = dict(raw)
        name = str(row.get("tool") or row.get("name") or "").strip()
        if name in _SIDE_EFFECT_TOOLS:
            # Never trust a model-authored operation_id.  Payload-derived ids
            # allow a changed retry payload to become a brand-new operation;
            # a fixed turn/round/slot id instead produces a durable conflict.
            row["operation_id"] = (
                f"{request_id or 'turn'}:r{max(0, int(round_index))}:"
                f"s{slot_index}"
            )[:128]
        out.append(row)
    return out


def _user_explicitly_confirms(user_text: str) -> bool:
    text = " ".join(str(user_text or "").strip().lower().split())
    if not text:
        return False
    if any(
        token in text
        for token in (
            "不要执行",
            "别执行",
            "取消",
            "不确认",
            "不要确认",
            "do not",
            "don't",
            "cancel",
        )
    ):
        return False
    return any(
        token in text
        for token in (
            "我确认",
            "确认执行",
            "确定执行",
            "继续执行",
            "可以执行",
            "就这么做",
            "yes, proceed",
            "yes proceed",
            "i confirm",
        )
    )


def _user_requested_camera_display(user_text: str) -> bool:
    """Do not trust a model-authored ``display=true`` without user intent."""

    text = " ".join(str(user_text or "").strip().lower().split())
    if not text:
        return False
    if any(
        phrase in text
        for phrase in (
            "不要拍照",
            "别拍照",
            "不用拍照",
            "不要显示",
            "别显示",
            "don't take a photo",
            "do not take a photo",
            "don't display",
            "do not display",
        )
    ):
        return False
    return any(
        phrase in text
        for phrase in (
            "拍照",
            "拍张",
            "拍一张",
            "照片",
            "截图",
            "显示照片",
            "展示照片",
            "把照片显示",
            "把图片显示",
            "take a photo",
            "take a picture",
            "show the photo",
            "show the picture",
            "display the photo",
            "display the picture",
        )
    )




def _tool_result_for_llm(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    image = out.get(TRANSIENT_VISION_IMAGE_KEY)
    for key in _TOOL_RESULT_STRIP_KEYS:
        out.pop(key, None)
    if isinstance(image, dict) and image.get("bytes"):
        out["image_attached"] = True
        out["image_mime_type"] = str(image.get("mime_type") or "image/jpeg")
    return out


def _tool_result_for_storage(result: dict[str, Any]) -> dict[str, Any]:
    """Remove image bytes/data URLs before persistence, pipeline and logging."""

    out = _tool_result_for_llm(result)
    if out.pop("image_attached", False):
        out["image_transient"] = True
        out["image_retained"] = False
        out["vision_note"] = (
            "原图按隐私策略未持久化；重放时必须发起新请求重新拍摄"
        )
    return out


def _tools_need_camera(tools: list[dict[str, Any]]) -> bool:
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if tool in _CAPTURE_TOOLS:
            return True
    return False


def build_llm_tool_followup_message(
    tool_results: list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    """工具结果；视觉轮额外附一帧 JPEG，不把 base64 写入文本。"""

    slim = [_tool_result_for_llm(r) for r in tool_results]
    payload = json.dumps(slim, ensure_ascii=False)
    text = (
        "[工具执行结果]\n"
        f"{payload}\n\n"
        "请根据结果继续。若还需调用工具，请输出 JSON 且 ``tools`` 非空，"
        "并在 ``tts`` 写一句口语化过渡语（如「稍等，我帮你查一下」）以便立刻播报；"
        "若已完成，请输出最终 JSON，``tools`` 写 [] 并填写 ``tts`` 等字段。"
    )
    # One capture request carries exactly one on-demand frame.  Repeated
    # capture calls require another tool round rather than batching images.
    for result in tool_results:
        image = result.get(TRANSIENT_VISION_IMAGE_KEY)
        if isinstance(image, dict) and image.get("bytes"):
            return build_openai_vision_content(text, image)
    return text


def _capture_display_images(
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        image
        for result in tool_results
        if isinstance(result, dict)
        and result.get("display_requested") is True
        and isinstance((image := result.get("image_display")), dict)
    ]


def _vision_error_answer(error: LlmVisionUnsupportedError) -> str:
    return json.dumps(
        {
            "need_reply": True,
            "tts": str(error),
            "moves": [],
            "anims": [],
            "tools": [],
        },
        ensure_ascii=False,
    )


async def execute_tools_round(
    tools: list[dict[str, Any]],
    *,
    device_id: str,
    asr_chat_hub: Optional["AsrChatHub"],
    user_confirmed: bool,
    allow_camera_display: bool,
    device_context: str | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if _tools_need_camera(tools):
        results: list[dict[str, Any]] = []
        for raw in tools:
            if not isinstance(raw, dict):
                continue
            tool = str(raw.get("tool") or raw.get("name") or "").strip()
            if tool in _CAPTURE_TOOLS:
                cap = await capture_camera_for_device_async(
                    device_id,
                    hub=asr_chat_hub,
                    display=(
                        allow_camera_display
                        and raw.get("display") is True
                    ),
                )
                results.append({"tool": tool, **cap})
            else:
                partial = await asyncio.to_thread(
                    execute_llm_tools,
                    [raw],
                    device_id=device_id,
                    user_confirmed=user_confirmed,
                )
                results.extend(partial)
        return results

    return await asyncio.to_thread(
        execute_llm_tools,
        tools,
        device_id=device_id,
        user_confirmed=user_confirmed,
    )


async def complete_llm_with_tool_loop(
    chat: "ChatService",
    user_text: str,
    *,
    device_id: Optional[str] = None,
    device_context: Optional[str] = None,
    history_messages: Optional[list[dict[str, Any]]] = None,
    request_id: Optional[str] = None,
    dp_broker: Optional[Any] = None,
    pipeline_source: Optional[str] = None,
    asr_chat_hub: Optional["AsrChatHub"] = None,
    on_tts_ready: Optional[Callable[[str], Awaitable[None]]] = None,
    tts_prefetch: Optional["_TtsPrefetch"] = None,
    on_interim_tts_play: Optional[Callable[[str, int], Awaitable[None]]] = None,
    on_interaction_state: Optional[Callable[[str], Awaitable[None]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    """多轮 LLM：有 tools 则执行并继续，无 tools 则返回最终 parsed。

    返回 ``(parsed, all_tools, all_tool_results, last_raw_answer)``。
    """
    extra_messages: list[dict[str, Any]] = []
    all_tools: list[dict[str, Any]] = []
    all_tool_results: list[dict[str, Any]] = []
    transient_display_images: list[dict[str, Any]] = []
    answer = ""
    parsed: dict[str, Any] = parse_llm_reply("")
    user_confirmed = _user_explicitly_confirms(user_text)
    allow_camera_display = _user_requested_camera_display(user_text)
    explicit_request_id = str(request_id or "").strip()
    tool_request_id = explicit_request_id or uuid.uuid4().hex[:16]
    request_ledger_enabled = bool(explicit_request_id)

    async def _notify_state(state: str) -> None:
        if on_interaction_state is None:
            return
        try:
            await on_interaction_state(state)
        except Exception:
            logger.debug(
                "[LLM] interaction state callback failed state=%s",
                state,
                exc_info=True,
            )

    for round_idx in range(MAX_LLM_TOOL_ROUNDS):
        await _notify_state("THINKING")
        vision_in_call = messages_contain_images(extra_messages)
        try:
            answer = await chat.llm(
                user_text,
                device_context=device_context if round_idx == 0 else None,
                device_id=device_id,
                history_messages=history_messages if round_idx == 0 else None,
                extra_messages=extra_messages or None,
                on_tts_ready=on_tts_ready,
            )
        except LlmVisionUnsupportedError as exc:
            if not vision_in_call:
                raise
            # Do not ask the same text-only model to infer a scene from
            # metadata.  Return a deterministic, actionable response instead.
            answer = _vision_error_answer(exc)
            parsed = parse_llm_reply(answer)
            break
        finally:
            if vision_in_call:
                redact_images_from_messages(extra_messages)
        parsed = parse_llm_reply(answer)
        tools = _with_operation_ids(
            list(parsed.get("tools") or []),
            tool_request_id,
            round_index=round_idx,
        )

        if not tools:
            if not parsed.get("contract_ok", True):
                logger.warning(
                    "[LLM] invalid final contract round=%d device_id=%s req=%s error=%s",
                    round_idx + 1,
                    device_id,
                    request_id,
                    parsed.get("contract_error"),
                )
                if round_idx + 1 < MAX_LLM_TOOL_ROUNDS:
                    extra_messages.append({"role": "assistant", "content": answer})
                    extra_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一条回复为空。请输出完整 JSON；若无需工具，"
                                "tools 写 [] 且 tts 必须是可直接说给用户的非空文本。"
                            ),
                        }
                    )
                    continue
                parsed = parse_llm_reply("抱歉，我没有生成有效回复，请再试一次。")
            if request_ledger_enabled:
                try:
                    round_operation_id, round_claimed, replay_results = (
                        claim_llm_tool_round(
                            request_id=tool_request_id,
                            round_index=round_idx,
                            tools=[],
                        )
                    )
                except Exception:
                    logger.exception(
                        "[LLM] failed to claim final request ledger "
                        "device_id=%s req=%s round=%d",
                        device_id,
                        request_id,
                        round_idx + 1,
                    )
                    parsed = parse_llm_reply(
                        "请求状态无法安全确认，本次未执行任何操作，请稍后重试。"
                    )
                    break
                if replay_results and any(
                    row.get("tool") == "__llm_tool_round__"
                    and row.get("operation_status")
                    in {"idempotency_conflict", "running", "unknown"}
                    for row in replay_results
                ):
                    all_tool_results.extend(replay_results)
                    parsed = parse_llm_reply(
                        "同一请求编号已绑定到不同内容，已停止执行。请发起一个新请求。"
                    )
                    break
                if round_claimed:
                    try:
                        finish_llm_tool_round(
                            operation_id=round_operation_id,
                            tool_results=[],
                        )
                    except Exception:
                        logger.exception(
                            "[LLM] failed to finish final request ledger "
                            "device_id=%s req=%s round=%d",
                            device_id,
                            request_id,
                            round_idx + 1,
                        )
                        try:
                            mark_llm_tool_round_unknown(
                                operation_id=round_operation_id,
                                reason=(
                                    "The final no-tool plan could not be "
                                    "durably recorded."
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "[LLM] failed to fence final request ledger "
                                "device_id=%s req=%s",
                                device_id,
                                request_id,
                            )
            break

        if not device_id:
            logger.warning(
                "[LLM] tools 无 device_id，无法执行 device_id=%s req=%s tool_names=%s",
                device_id,
                request_id,
                [str(tool.get("tool") or tool.get("name") or "") for tool in tools],
            )
            break

        round_operation_id = ""
        round_claimed = True
        replay_results: list[dict[str, Any]] | None = None
        if request_ledger_enabled:
            try:
                round_operation_id, round_claimed, replay_results = (
                    claim_llm_tool_round(
                        request_id=tool_request_id,
                        round_index=round_idx,
                        tools=tools,
                    )
                )
            except Exception:
                logger.exception(
                    "[LLM] failed to claim tool request ledger "
                    "device_id=%s req=%s round=%d",
                    device_id,
                    request_id,
                    round_idx + 1,
                )
                parsed = parse_llm_reply(
                    "请求状态无法安全确认，本次未执行任何操作，请稍后重试。"
                )
                break

        if not round_claimed and replay_results and any(
            row.get("tool") == "__llm_tool_round__"
            and row.get("operation_status")
            in {"idempotency_conflict", "running", "unknown"}
            for row in replay_results
        ):
            all_tools.extend(tools)
            all_tool_results.extend(replay_results)
            parsed = parse_llm_reply(
                "同一请求编号已绑定到不同工具参数，已停止执行。请发起一个新请求。"
            )
            logger.warning(
                "[LLM] request-level idempotency conflict "
                "device_id=%s req=%s round=%d",
                device_id,
                request_id,
                round_idx + 1,
            )
            break

        if not round_claimed:
            vision_replay_unavailable = _tools_need_camera(tools)
            raw_tool_results = list(replay_results or [])
            tool_followup_message = build_llm_tool_followup_message(
                raw_tool_results
            )
            tool_results = [
                _tool_result_for_storage(row) for row in raw_tool_results
            ]
            logger.info(
                "[LLM] replayed durable tool round without execution "
                "device_id=%s req=%s round=%d",
                device_id,
                request_id,
                round_idx + 1,
            )
        else:
            vision_replay_unavailable = False
            # Never play model-authored claims before any tool (including
            # reads) has actually completed. Server phrases only describe work
            # in progress and are safe even when the tool later fails.
            interim_text = build_tool_interim_tts(tools)
            if interim_text and on_interim_tts_play is not None:
                logger.info(
                    "[LLM] 使用服务端工具过渡 TTS "
                    "device_id=%s req=%s content=%s",
                    device_id,
                    request_id,
                    safe_log_content(interim_text),
                )
            # The streaming parser may already have prefetched audio for the
            # model-authored tool-round ``tts``. It is untrusted interim
            # content and must never be played as a success claim.
            if tts_prefetch is not None:
                tts_prefetch.cancel()

            try:
                # 拍照须先拿到帧再播过渡 TTS（播报期间固件暂停 camera 上行）
                await _notify_state("ACTING")
                if _tools_need_camera(tools):
                    raw_tool_results = await execute_tools_round(
                        tools,
                        device_id=str(device_id),
                        asr_chat_hub=asr_chat_hub,
                        user_confirmed=user_confirmed,
                        allow_camera_display=allow_camera_display,
                        device_context=device_context,
                    )
                    if interim_text and on_interim_tts_play is not None:
                        await on_interim_tts_play(interim_text, round_idx + 1)
                else:
                    play_coro = None
                    if interim_text and on_interim_tts_play is not None:
                        play_coro = on_interim_tts_play(
                            interim_text,
                            round_idx + 1,
                        )

                    tool_coro = execute_tools_round(
                        tools,
                        device_id=str(device_id),
                        asr_chat_hub=asr_chat_hub,
                        user_confirmed=user_confirmed,
                        allow_camera_display=allow_camera_display,
                        device_context=device_context,
                    )
                    if play_coro is not None:
                        raw_tool_results, _ = await asyncio.gather(
                            tool_coro,
                            play_coro,
                        )
                    else:
                        raw_tool_results = await tool_coro
                tool_followup_message = build_llm_tool_followup_message(
                    raw_tool_results
                )
                transient_display_images.extend(
                    _capture_display_images(raw_tool_results)
                )
                tool_results = [
                    _tool_result_for_storage(row)
                    for row in raw_tool_results
                ]
                if request_ledger_enabled:
                    finish_llm_tool_round(
                        operation_id=round_operation_id,
                        tool_results=tool_results,
                    )
            except Exception:
                if request_ledger_enabled:
                    try:
                        mark_llm_tool_round_unknown(
                            operation_id=round_operation_id,
                            reason=(
                                "The tool round stopped before its complete "
                                "outcome was durably recorded."
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "[LLM] failed to fence interrupted tool round "
                            "device_id=%s req=%s round=%d",
                            device_id,
                            request_id,
                            round_idx + 1,
                        )
                raise

        all_tools.extend(tools)
        all_tool_results.extend(tool_results)
        await _notify_state("THINKING")
        logger.info(
            "[LLM] tool round=%d device_id=%s req=%s tool_names=%s results=%s",
            round_idx + 1,
            device_id,
            request_id,
            [str(tool.get("tool") or tool.get("name") or "") for tool in tools],
            safe_log_content(tool_results),
        )
        if dp_broker is not None and device_id and request_id:
            tool_names = [
                str(t.get("tool") or "").strip()
                for t in tools
                if str(t.get("tool") or "").strip()
            ]
            await dp_broker.publish(
                {
                    "device_id": device_id,
                    "request_id": request_id,
                    "source": pipeline_source or "asr",
                    "asr_text": user_text,
                    "stage": f"llm_tool_{round_idx + 1}",
                    "status": "running",
                    "llm_text": (
                        f"执行工具: {', '.join(tool_names)}"
                        if tool_names
                        else "执行工具"
                    ),
                }
            )
        if vision_replay_unavailable:
            answer = json.dumps(
                {
                    "need_reply": True,
                    "tts": (
                        "这次是重复请求，原图没有留存。请重新说一次看画面的请求，"
                        "我会重新拍一帧。"
                    ),
                    "moves": [],
                    "anims": [],
                    "tools": [],
                },
                ensure_ascii=False,
            )
            parsed = parse_llm_reply(answer)
            break
        extra_messages.append({"role": "assistant", "content": answer})
        extra_messages.append(
            {"role": "user", "content": tool_followup_message}
        )
    else:
        logger.warning(
            "[LLM] tool 循环达到上限 %d device_id=%s req=%s",
            MAX_LLM_TOOL_ROUNDS,
            device_id,
            request_id,
        )
        parsed = parse_llm_reply("抱歉，连续执行工具仍未完成，请稍后再试。")

    if transient_display_images:
        parsed["_transient_display_images"] = transient_display_images
    return parsed, all_tools, all_tool_results, answer
