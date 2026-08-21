"""Composition Root 辅助：装配 ChatService 与基础设施适配器。"""

from __future__ import annotations

from deskbot_server.application.chat_service import ChatService
from deskbot_server.core.settings import AppSettings
from deskbot_server.infrastructure.asr.factory import build_asr_adapter
from deskbot_server.infrastructure.llm.openai_compat import OpenAiLlmAdapter
from deskbot_server.infrastructure.tts.factory import build_tts_adapter


def build_chat_service(config: dict) -> ChatService:
    settings = AppSettings.from_config(config)
    return ChatService(
        settings,
        asr=build_asr_adapter(settings),
        llm=OpenAiLlmAdapter(settings),
        tts=build_tts_adapter(settings),
    )
