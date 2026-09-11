"""带人设的一次性生成：组合表演的「AI 编排」和陪伴场景的「一句话生成」共用这一个入口。

之前 AI 编排是裸的模型调用，编出来的台词不像小歪、也不认识主人。这里把
Agent.md（人设）和 User.md（对主人的了解）拼进系统提示，再要求只输出 JSON。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from deskbot_server import agent_docs
from deskbot_server.llm.runtime import chat_completion, resolve_llm_config

logger = logging.getLogger("deskbot-server")

_JSON_RE = re.compile(r"\{.*\}", re.S)
_PERSONA_MAX_CHARS = 2400
# 人设里的回复格式 / 工具约定是给对话用的；一次性生成时要明说"这次不适用"，不然短句（"每天晚上提醒我健身"）
# 会被当成聊天，模型直接回 {"need_reply":..,"tts":..}（2026-09-10 实际发生）。
_PERSONA_BRIDGE = (
    "上面的人设只用来决定说话风格、称呼和对主人的了解；人设里关于回复格式（need_reply / tts / moves / anims / tools 这些 JSON 字段）"
    "和调用工具的规定这次一律不适用——这不是一次对话，下面才是唯一要遵守的输出要求："
)
_CHAT_REPLY_KEYS = ("need_reply", "tts", "moves", "anims")
# User.md 里「陪伴小目标 xx 达成」是做过的事，模型见了爱照抄成新目标；生成时过滤掉
_USER_DOC_SKIP = ("陪伴小目标",)


class GenerationError(RuntimeError):
    """给用户看的生成失败原因。"""


def persona_system_prefix() -> str:
    """人设 + 对主人的了解，截到合理长度；读不到时退回空串（生成照常进行）。"""
    parts: list[str] = []
    try:
        persona = str(agent_docs.system_prompt() or "").strip()
        if persona:
            parts.append("你的人设（说话风格、称呼、性格都按这里来）：\n" + persona[:_PERSONA_MAX_CHARS])
    except Exception:  # noqa: BLE001
        logger.debug("persona_system_prefix: agent doc unavailable", exc_info=True)
    try:
        user = str(agent_docs.user_prompt_appendix() or "").strip()
        if user:
            lines = [ln for ln in user.splitlines() if not any(tag in ln for tag in _USER_DOC_SKIP)]
            user = "\n".join(lines).strip()
        if user:
            parts.append(user[:_PERSONA_MAX_CHARS])
    except Exception:  # noqa: BLE001
        logger.debug("persona_system_prefix: user doc unavailable", exc_info=True)
    return "\n\n".join(parts)


def _looks_like_chat_reply(data: dict[str, Any]) -> bool:
    return any(k in data for k in _CHAT_REPLY_KEYS) and not any(k in data for k in ("tasks", "chunks"))


def generate_json(instruction: str, user_text: str, *, temperature: float = 0.7) -> dict[str, Any]:
    """按 instruction 生成一个 JSON 对象；人设自动前置。失败抛 GenerationError。"""
    text = str(user_text or "").strip()
    if not text:
        raise GenerationError("先描述一下想要什么")
    try:
        cfg = resolve_llm_config()
    except ValueError as exc:
        raise GenerationError(str(exc)) from exc
    prefix = persona_system_prefix()
    body = str(instruction or "").strip()
    system = (prefix + "\n\n" + _PERSONA_BRIDGE + "\n" if prefix else "") + body
    data = _ask(system, text, temperature=temperature, cfg=cfg)
    if _looks_like_chat_reply(data):
        # 模型把它当成了聊天：再来一次，把话说死
        logger.warning("[generate_json] 模型用了聊天回复格式，重试一次: %s", json.dumps(data, ensure_ascii=False)[:200])
        retry = system + "\n注意：刚才你用了聊天回复格式（need_reply / tts），这是错的。不要回话，只输出上面格式的 JSON 对象。"
        data = _ask(retry, text, temperature=min(temperature, 0.3), cfg=cfg)
        if _looks_like_chat_reply(data):
            raise GenerationError("模型把这句话当成了聊天，请换个说法，比如「每天晚上九点提醒我健身」")
    return data


def _ask(system: str, text: str, *, temperature: float, cfg: Any) -> dict[str, Any]:
    try:
        raw, _meta = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=temperature,
            config=cfg,
        )
    except Exception as exc:  # noqa: BLE001 —— 生成失败要给用户可读原因
        raise GenerationError(f"生成失败：{exc}") from exc
    match = _JSON_RE.search(str(raw or ""))
    if not match:
        raise GenerationError("模型没有返回有效内容，请换个说法再试")
    try:
        data = json.loads(match.group())
    except ValueError as exc:
        raise GenerationError("模型输出解析失败，请重试") from exc
    if not isinstance(data, dict):
        raise GenerationError("模型输出不是对象，请重试")
    return data


__all__ = ["GenerationError", "generate_json", "persona_system_prefix"]
