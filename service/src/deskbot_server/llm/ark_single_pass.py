"""文本对话的「单趟联网」：方舟 Responses API + 内置 web_search。

控制台文字对话原本是「模型第一轮 → JSON 里请求 websearch → Core 联网 → 第二轮」，
实测 ≈ 11s。方舟 Responses API 可以把联网内容插件挂成服务端内置工具，模型在
同一轮里自行检索并直接给出结论：时效性问题实测 6–7s，普通问题 2.7s。

注意：不能同时用 ``text.format=json_object`` —— 强制 JSON 输出时模型不再真正
触发内置搜索，而是在 JSON 的 tools 里"假装"写一个 web_search。所以这里只在提示
词里要求 JSON，输出仍由 ``parse_llm_reply`` 宽松解析。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from deskbot_server.llm.runtime import ResolvedLlmConfig, build_chat_model
from deskbot_server.llm.vision_input import to_ark_responses_input
from deskbot_server.rtc_llm_adapter import responses_base_url, web_search_enabled_for
from deskbot_server.safe_fetch import safe_provider_urlopen

logger = logging.getLogger("deskbot-server")

_TIMEOUT_SEC = 60
_WEB_SEARCH_TOOL = {"type": "web_search", "max_keyword": 2, "limit": 3}

# 追加到系统提示末尾：告诉模型联网由服务端内置完成，别再走 JSON 工具协议里的 websearch。
SINGLE_PASS_PROMPT_APPENDIX = (
    "[联网说明] 本轮你可以直接使用内置的联网搜索能力（web_search）获取实时信息；"
    "涉及今天/最新的天气、新闻、股价、赛事、价格等时效性问题必须先联网再回答。"
    "不要在 tools 里写 websearch/web_search，联网后直接在 tts 里给出结论"
    "（只播报结论和关键数字，不读网址），仍然只输出一个 JSON 对象。"
)


def single_pass_available(cfg: ResolvedLlmConfig) -> bool:
    """当前 LLM 指向火山方舟且未被 DESKBOT_RTC_WEB_SEARCH=0 关闭时可用。"""
    if not cfg.api_key or "请替换" in cfg.api_key:
        return False
    return web_search_enabled_for(cfg.api_base)


def _post(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deskbot-server/0.1",
        },
        method="POST",
    )
    try:
        with safe_provider_urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:600] if exc.fp else ""
        raise RuntimeError(f"Ark Responses HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ark Responses 连接失败: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise RuntimeError("Ark Responses 响应不是 JSON 对象")
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False)[:600])
    return data


def _output_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    function_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            # 我们没有声明 function 工具，但豆包会把提示词里的 JSON 工具协议
            # 当成原生函数调用返回（如 capture_camera）。折算回 JSON 约定，
            # 交给同一条工具循环执行，而不是当成"没有文本"。
            name = str(item.get("name") or "").strip()
            try:
                args = json.loads(item.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            if name:
                function_calls.append({"tool": name, **(args if isinstance(args, dict) else {})})
            continue
        if kind != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    text = "".join(parts).strip()
    if not function_calls:
        return text
    # 文本与原生 function_call 并存（如"我看一下画面" + capture_camera）：
    # 工具不能丢。文本本身是 JSON 就把工具并进去，否则把文本当过渡语包起来。
    merged: Any = None
    if text:
        try:
            merged = json.loads(text)
        except ValueError:
            merged = None
    if isinstance(merged, dict):
        tools = [t for t in (merged.get("tools") or []) if isinstance(t, dict)]
        merged["tools"] = tools + function_calls
        return json.dumps(merged, ensure_ascii=False)
    return json.dumps(
        {"need_reply": bool(text), "tts": text, "tools": function_calls},
        ensure_ascii=False,
    )


def ark_single_pass_completion(
    messages: list[dict[str, Any]],
    *,
    config: ResolvedLlmConfig,
    temperature: float = 0.7,
) -> tuple[str, dict[str, Any]]:
    """一次 Responses 调用：模型自行联网（如需）并返回 JSON 文本。失败抛 RuntimeError。"""
    url = f"{responses_base_url(str(config.api_base or ''))}/responses"
    payload: dict[str, Any] = {
        "model": build_chat_model(config.protocol, config.model),
        "input": to_ark_responses_input(messages),
        "stream": False,
        "thinking": {"type": "disabled"},
        "temperature": float(temperature),
        "tools": [dict(_WEB_SEARCH_TOOL)],
    }
    t0 = time.monotonic()
    data = _post(url, payload, config.api_key)
    text = _output_text(data)
    if not text:
        raise RuntimeError("Ark Responses 未返回文本")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tool_usage = usage.get("tool_usage") if isinstance(usage.get("tool_usage"), dict) else {}
    meta = {
        "provider": "ark_single_pass",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "web_search_calls": int(tool_usage.get("web_search") or 0),
        "usage": usage,
    }
    return text, meta


__all__ = [
    "SINGLE_PASS_PROMPT_APPENDIX",
    "ark_single_pass_completion",
    "single_pass_available",
]
