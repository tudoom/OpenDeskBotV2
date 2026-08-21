from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChatTurnResult:
    """一轮 ASR→LLM→TTS/pb 的时序与结果摘要。"""

    llm_text: Optional[str] = None
    llm_raw: Optional[str] = None
    moves: list[Any] = field(default_factory=list)
    anims: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    need_reply: bool = True
    json_ok: bool = False
    t_llm_end: Optional[float] = None
    t_tts_synth_end: Optional[float] = None
    t_tts_end: Optional[float] = None
    status: str = "ok"
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_retryable: bool = False
    provider_status: Optional[int] = None
    voice_auto_reply_off: bool = False
    scenes: list[str] = field(default_factory=list)
    playback_status: str = "none"
    playback_request_ids: list[str] = field(default_factory=list)
    expression_result: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_text": self.llm_text,
            "llm_raw": self.llm_raw,
            "moves": self.moves,
            "anims": self.anims,
            "tools": self.tools,
            "tool_results": self.tool_results,
            "need_reply": self.need_reply,
            "json_ok": self.json_ok,
            "t_llm_end": self.t_llm_end,
            "t_tts_synth_end": self.t_tts_synth_end,
            "t_tts_end": self.t_tts_end,
            "status": self.status,
            "error": self.error,
            "error_code": self.error_code,
            "error_retryable": self.error_retryable,
            "provider_status": self.provider_status,
            "voice_auto_reply_off": self.voice_auto_reply_off,
            "scenes": self.scenes,
            "playback_status": self.playback_status,
            "playback_request_ids": self.playback_request_ids,
            "expression_result": self.expression_result,
        }
