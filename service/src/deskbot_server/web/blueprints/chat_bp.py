"""控制台文字对话 /api/llm/chat 及其设备下发（从 debug_bp 拆出，逻辑未改）。"""

from __future__ import annotations

import json
import logging
import time

from flask import Blueprint, jsonify, request

from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.web.helpers import (
    ALLOWED_LLM_ROLES,
    load_config,
)

bp = Blueprint("chat", __name__)
logger = logging.getLogger("deskbot-server")


from deskbot_server.web.blueprints.debug_bp import (  # noqa: E402
    _agent_system_prompt,
    _consume_consumer_test_quota,
)


def _dispatch_chat_reply_to_device(
    device_id: str,
    reply_text: str,
    moves: list[dict],
) -> dict:
    """打字对话与语音同效：把最终回复口播 + 动作送到设备。

    走 core 的 /api/device_tts（内部与语音同一条 pb 播放链，moves 同步进
    同一播放计划）；没有口播文本时退化为 /api/device_servo 逐条动作。
    core 端 accept 后异步执行，这里只等受理结果，不等播放完成。
    """
    from urllib import request as urlrequest

    from deskbot_server.auth.api_key_service import read_free_api_key_raw
    from deskbot_server.web.helpers import deskbot_upstream_base

    base = deskbot_upstream_base().rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = read_free_api_key_raw()
    if key:
        headers["X-API-Key"] = key

    def _post(path: str, payload: dict) -> tuple[bool, str]:
        req = urlrequest.Request(
            f"{base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            ok = bool(body.get("ok", resp.status < 400))
            return ok, str(body.get("error") or body.get("message") or "")
        except Exception as exc:  # noqa: BLE001 - 下发失败不允许拖垮对话回复
            return False, str(exc)

    if reply_text:
        payload: dict = {"device_id": device_id, "text": reply_text}
        if moves:
            payload["moves"] = moves
        ok, err = _post("/api/device_tts", payload)
        return {"ok": ok, "path": "device_tts", "moves": len(moves), "error": err or None}
    errors: list[str] = []
    for index, move in enumerate(moves):
        payload = {
            "device_id": device_id,
            "preset": str(move.get("move") or ""),
            "action": "replace" if index == 0 else "append",
        }
        try:
            ms = int(move.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms > 0:
            payload["duration_ms"] = ms
        ok, err = _post("/api/device_servo", payload)
        if not ok:
            errors.append(err)
    return {
        "ok": not errors,
        "path": "device_servo",
        "moves": len(moves),
        "error": "; ".join(e for e in errors if e) or None,
    }


@bp.post("/api/llm/chat")
def llm_chat():
    payload = request.get_json(force=True, silent=True) or {}
    user_text = str(payload.get("text") or "").strip()
    raw_history = payload.get("history") or []

    if not user_text:
        return jsonify({"ok": False, "error": "空文本"}), 400

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    debug_device_id = str(payload.get("device_id") or "").strip()
    if not debug_device_id:
        # Agent 页打字不带 device_id：落到控制台当前选中的设备，
        # 让打字和语音一样能指挥机器人。
        try:
            from deskbot_server.web.session_device import get_current_device_id

            debug_device_id = str(get_current_device_id() or "").strip()
        except Exception:  # noqa: BLE001
            debug_device_id = ""

    default_system_prompt = _agent_system_prompt() or llm_cfg.get(
        "system_prompt", "你是中文助手，请简洁回答。每次回答不超过50字"
    )
    raw_sys = payload.get("system_prompt")
    if isinstance(raw_sys, str) and raw_sys.strip():
        system_prompt = raw_sys
    else:
        system_prompt = default_system_prompt

    from deskbot_server.llm.runtime import resolve_llm_config

    try:
        llm_runtime_cfg = resolve_llm_config()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not llm_runtime_cfg.api_key or "请替换" in llm_runtime_cfg.api_key:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "LLM API Key 未配置（设备 LLM 管理或环境变量 LLM_API_KEY）",
                }
            ),
            400,
        )

    from deskbot_server.llm.prompt_assembly import assemble_text_system_prompt
    from deskbot_server.llm.user_message import build_llm_user_message

    sys_content = assemble_text_system_prompt(system_prompt)

    raw_ctx = payload.get("device_context")
    device_context: str | None = None
    if isinstance(raw_ctx, dict):
        device_context = json.dumps(raw_ctx, ensure_ascii=False)
    elif isinstance(raw_ctx, str) and raw_ctx.strip():
        device_context = raw_ctx.strip()

    messages = [
        {
            "role": "system",
            "content": sys_content,
        }
    ]
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "")
        if role not in ALLOWED_LLM_ROLES or not content:
            continue
        if role == "system":
            continue
        messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": build_llm_user_message(
                user_text,
                device_id=debug_device_id or None,
                device_context=device_context,
            ),
        }
    )

    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err

    try:
        from deskbot_server.application.llm_tool_loop import (
            MAX_LLM_TOOL_ROUNDS,
            _user_explicitly_confirms,
            build_llm_tool_followup_message,
        )

        # 与语音/主循环同一套判定：用户这轮明确说"我确认/确认执行"，需要确认的
        # 高风险操作（删记忆、写文件等）才放行；否则文字对话永远走不完确认流程。
        user_confirmed = _user_explicitly_confirms(user_text)

        # 单轮调用会把工具请求当成最终回复丢弃：模型第一轮说「我帮你查一下」
        # 并请求调 miot，这里若不执行工具、不再走下一轮，用户就只能收到那句
        # 过渡语，答案永远不来——追问时模型靠系统提示里的摘要才答上。此处
        # 与语音链路同构：有 tools 就执行并回灌，直到模型给出最终回复。
        from deskbot_server.application.web_search_prefetch import (
            discard_prefetch,
            start_prefetch,
            take_prefetched_websearch,
        )
        from deskbot_server.llm.ark_single_pass import (
            ark_single_pass_completion,
            single_pass_available,
        )
        from deskbot_server.llm.runtime import chat_completion

        t0 = time.monotonic()
        loop_messages = list(messages)
        interim_replies: list[str] = []
        raw = ""
        meta: dict = {}
        parsed = parse_llm_reply("")
        temperature = float(payload.get("temperature", 0.7))
        # 方舟模型：走 Responses 单趟，模型在本轮内自行联网（≈6s，原两趟 ≈11s）。
        # 其它网关：时效性问题先并行联网，与第一轮模型调用重叠（见 web_search_prefetch）。
        single_pass = single_pass_available(llm_runtime_cfg)
        prefetch = None
        if single_pass:
            loop_messages[0] = {
                "role": "system",
                "content": assemble_text_system_prompt(system_prompt, single_pass=True),
            }
        else:
            prefetch = start_prefetch(user_text)
            if prefetch is not None:
                logger.info("web 对话联网预取已发起 text_chars=%d", len(user_text))
        for _round in range(MAX_LLM_TOOL_ROUNDS):
            t_llm = time.monotonic()
            if single_pass:
                try:
                    raw, meta = ark_single_pass_completion(
                        loop_messages, config=llm_runtime_cfg, temperature=temperature
                    )
                except Exception as exc:  # noqa: BLE001 —— 退回原链路，不让对话失败
                    logger.warning("web 对话 Responses 单趟失败，退回 chat/completions: %s", exc)
                    single_pass = False
                    # 此时再"预取"已无任何调用可重叠，只会在下面同步等它；
                    # 让正常的工具循环自己去搜。
            if not single_pass:
                raw, meta = chat_completion(
                    loop_messages,
                    device_id=debug_device_id or None,
                    temperature=temperature,
                    config=llm_runtime_cfg,
                )
            llm_ms = int((time.monotonic() - t_llm) * 1000)
            parsed = parse_llm_reply(raw)
            round_tools = [
                t for t in (parsed.get("tools") or []) if isinstance(t, dict)
            ]
            if not round_tools:
                logger.info(
                    "web 对话 round=%d llm_ms=%d path=%s web_search_calls=%s final reply_chars=%d",
                    _round + 1,
                    llm_ms,
                    "ark_single_pass" if single_pass else "chat_completions",
                    (meta or {}).get("web_search_calls"),
                    len(str(parsed.get("reply") or "")),
                )
                break
            interim = str(parsed.get("reply") or "").strip()
            if interim:
                interim_replies.append(interim)
            t_tools = time.monotonic()
            prefetched, remaining_tools, prefetch_used = take_prefetched_websearch(
                round_tools, prefetch
            )
            if prefetch_used:
                prefetch = None
            # 拍照工具（capture_camera / capture_and_describe）由 Core 抓帧后以视觉
            # 图片块回灌；同步执行器没有相机，直接交给它只会得到「未知工具」。
            from deskbot_server.application.web_chat_capture import execute_web_chat_tools

            results = prefetched + (
                execute_web_chat_tools(
                    remaining_tools,
                    device_id=debug_device_id or None,
                    user_confirmed=user_confirmed,
                )
                if remaining_tools
                else []
            )
            logger.info(
                "web 对话 round=%d llm_ms=%d tools=%s tools_ms=%d prefetch_hit=%s",
                _round + 1,
                llm_ms,
                [str(t.get("tool") or "?") for t in round_tools],
                int((time.monotonic() - t_tools) * 1000),
                prefetch_used,
            )
            loop_messages.append({"role": "assistant", "content": raw})
            loop_messages.append(
                {
                    "role": "user",
                    "content": build_llm_tool_followup_message(results),
                }
            )
        else:
            logger.warning("web 对话工具轮次达到上限，返回最后一轮内容")
        final_reply = str(parsed.get("reply") or "").strip()
        # 模型自始至终没请求 websearch：排队中的预取直接取消，别占着线程池。
        discard_prefetch(prefetch)
        if interim_replies:
            # 过渡语（「我帮你查一下」）也展示出来，和语音先播报后作答一致。
            parsed = dict(parsed)
            parsed["reply"] = chr(10).join([*interim_replies, final_reply]).strip()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # 打字与语音完全同效：最终回复由机器人口播，moves 走同一播放计划。
        device_dispatch = None
        dispatch_moves = [
            m for m in (parsed.get("moves") or []) if isinstance(m, dict)
        ]
        speak_text = final_reply if parsed.get("need_reply", True) else ""
        if debug_device_id and (speak_text or dispatch_moves):
            try:
                device_dispatch = _dispatch_chat_reply_to_device(
                    debug_device_id, speak_text, dispatch_moves
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("web 对话设备下发失败: %s", exc)
                device_dispatch = {"ok": False, "error": str(exc)}
            if device_dispatch and not device_dispatch.get("ok"):
                logger.warning(
                    "web 对话设备下发未受理 device_id=%s dispatch=%s",
                    debug_device_id,
                    device_dispatch,
                )
        # Agent 页的对话不能只活在浏览器内存里：刷新即丢，会话页和 FTS
        # 检索也都看不见它。落到 session_store，与语音共用一套账本。
        try:
            from deskbot_server.session_store import (
                append_turn,
                ensure_active_session,
            )

            _sid = ensure_active_session()["session_id"]
            append_turn(_sid, user_text, str(parsed["reply"] or ""))
        except Exception:
            logger.exception("web 对话落库失败")
        return jsonify(
            {
                "ok": True,
                "reply": parsed["reply"],
                "raw": parsed["raw"],
                "moves": parsed.get("moves") or [],
                "anims": parsed.get("anims") or [],
                "tools": parsed.get("tools") or [],
                "scenes": parsed.get("scenes") or [],
                "json_ok": parsed["json_ok"],
                "need_reply": parsed.get("need_reply", True),
                "model": meta.get("model"),
                "model_source": meta.get("source"),
                "model_display_name": meta.get("display_name"),
                "elapsed_ms": elapsed_ms,
                "device_dispatch": device_dispatch,
                "usage": meta.get("usage"),
                "quota": quota,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
