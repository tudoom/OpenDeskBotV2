from __future__ import annotations

import asyncio
import logging

from deskbot_server.core.ports.tts import PhonemeSegment
from deskbot_server.core.settings import AppSettings
from deskbot_server.log_privacy import safe_log_content
from deskbot_server.tts.doubao import load_doubao_tts_config, synthesize_doubao_tts
from deskbot_server.tts.doubao_phoneme_align import build_phoneme_segments

logger = logging.getLogger("deskbot-server")


class DoubaoPhonemeTtsAdapter:
    """豆包 TTS 适配器：时间戳 / 拼音均分 → 音素分片（口型）。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def synthesize_phoneme_segments(
        self,
        text: str,
    ) -> tuple[int, list[PhonemeSegment]]:
        clean = (text or "").strip()
        if not clean:
            sr = int(self._settings.tts.sample_rate or 16000)
            return sr, []

        cfg = load_doubao_tts_config()
        result = await synthesize_doubao_tts(clean, cfg)
        pcm = bytes(result.pcm or b"")
        sr = int(result.sample_rate or cfg.sample_rate or 16000)
        if not pcm:
            raise RuntimeError(f"豆包 TTS 无 PCM: {safe_log_content(clean)}")

        # English G2P may import NLTK resources on first use and phoneme
        # alignment also walks/copies the complete PCM buffer.  Keep that
        # blocking/CPU work off the shared asyncio loop; the cloud websocket
        # synthesis above remains native async I/O.
        segs = await asyncio.to_thread(
            build_phoneme_segments,
            text=clean,
            pcm=pcm,
            sample_rate=sr,
            sentence_end=result.sentence_end,
            subtitles=result.subtitles,
        )
        logger.info(
            "[TTS/doubao] 音素分片 n=%d pcm_bytes=%d elapsed_ms=%d content=%s",
            len(segs),
            len(pcm),
            result.elapsed_ms,
            safe_log_content(clean),
        )
        return sr, segs
