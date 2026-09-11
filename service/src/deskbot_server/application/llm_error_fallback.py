"""LLM 调用失败时的语音与屏幕表情兜底。"""
from __future__ import annotations

import random
from typing import Any

_LLM_ERROR_TTS = (
    "抱歉，我刚才走神了，你再说一遍好吗",
    "哎呀，我脑子卡住了，稍后再试试",
    "不好意思，我没听清自己想说啥，再说一次吧",
)


def llm_error_fallback_tts() -> str:
    return random.choice(_LLM_ERROR_TTS)


def llm_error_playback_anims() -> list[dict[str, Any]]:
    """Retired legacy display lane; lifecycle state owns the error face."""

    return []


def build_llm_error_fallback_plan(*, tts: str | None = None) -> dict[str, Any]:
    text = (tts or llm_error_fallback_tts()).strip()
    return {
        "tts": text,
        "parsed": {
            "reply": text,
            "moves": [],
            "anims": llm_error_playback_anims(),
            "images": [],
            "json_ok": True,
            "need_reply": True,
            "raw": text,
        },
    }
