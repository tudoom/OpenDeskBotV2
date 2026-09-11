from __future__ import annotations

import logging

from deskbot_server.core.ports.tts import TtsPort
from deskbot_server.core.settings import AppSettings
from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
from deskbot_server.tts.doubao import PROVIDER_DISPLAY_NAME

logger = logging.getLogger("deskbot-server")


def build_tts_adapter(settings: AppSettings) -> TtsPort:
    """构建默认的火山引擎豆包流式 TTS 2.0 适配器。

    ``doubao`` 是唯一可执行后端。旧配置中的其它 provider 名称不会再
    选择另一套实现，而是给出迁移告警并统一走 V3 双向 WebSocket。
    """
    provider = (settings.tts.provider or "doubao").strip().lower()
    if provider != "doubao":
        logger.warning(
            "[TTS] provider=%r 已停用或不受支持，统一使用 %s",
            provider,
            PROVIDER_DISPLAY_NAME,
        )
    logger.info(
        "[TTS] provider=doubao transport=V3-bidirectional-websocket "
        "version=2.0（时间戳/拼音均分音素口型）"
    )
    return DoubaoPhonemeTtsAdapter(settings)
