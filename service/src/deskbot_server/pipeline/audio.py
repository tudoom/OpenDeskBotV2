from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from deskbot_server.pipeline.mic_health import MicrophoneHealthMonitor
from deskbot_server.pipeline.opus_runtime import load_opuslib_next
from deskbot_server.pipeline.opus_uplink import decode_opus_uplink

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService

logger = logging.getLogger("deskbot-server")


def _new_opus_decoder(sample_rate: int, channels: int):
    opuslib_next = load_opuslib_next()
    return opuslib_next.Decoder(sample_rate, channels)


@dataclass
class AudioConfig:
    input_codec: str
    sample_rate: int
    channels: int


class ConnectionSession:
    """Decode the USB microphone uplink for the sole RTC voice pipeline."""

    def __init__(self, pipeline: ChatService, audio_cfg: AudioConfig):
        self.pipeline = pipeline
        self.audio_cfg = audio_cfg
        # Decoder construction can load a native library; keep it lazy so the
        # USB ready callback never pays that cost on the asyncio thread.
        self.decoder = None
        self.rom_sr = audio_cfg.sample_rate
        self.rom_ch = audio_cfg.channels
        self.rom_codec = audio_cfg.input_codec
        self.lock = asyncio.Lock()
        # Opus decoder state is sequential by definition. A dedicated single
        # thread prevents camera/DB/logging work in asyncio's global executor
        # from delaying microphone decode and preserves packet order.
        self._decode_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="deskbot-opus-decode",
        )
        self._closed = False
        self._uplink_open = False
        self.last_decoded_pcm = b""
        self._microphone_health = MicrophoneHealthMonitor(
            sample_rate=audio_cfg.sample_rate,
        )

    def _decode(
        self,
        payload: bytes,
        codec: Optional[str] = None,
        *,
        opus_frames: Optional[int] = None,
    ) -> bytes:
        use_codec = (codec or self.rom_codec or self.audio_cfg.input_codec).lower()
        if use_codec == "pcm16":
            return payload
        if use_codec == "opus":
            if self.decoder is None:
                self.decoder = _new_opus_decoder(self.rom_sr, self.rom_ch)
            return decode_opus_uplink(
                self.decoder,
                payload,
                sample_rate=self.rom_sr,
                opus_frames=opus_frames,
            )
        raise ValueError(f"unsupported codec: {use_codec}")

    async def feed_audio(
        self,
        payload: bytes,
        codec: Optional[str] = None,
        *,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        opus_frames: Optional[int] = None,
    ) -> tuple[list[bytes], bool, bool]:
        """Return the legacy-shaped tuple while producing no local utterances."""
        if self._closed:
            return [], False, False
        async with self.lock:
            self.last_decoded_pcm = b""
            requested_sr = int(sample_rate) if sample_rate and sample_rate > 0 else self.rom_sr
            requested_ch = int(channels) if channels and channels > 0 else self.rom_ch
            requested_codec = (codec or self.rom_codec).lower()
            if requested_sr not in {8000, 16000}:
                raise ValueError(
                    f"unsupported RTC sample rate: {requested_sr}; expected 8000 or 16000"
                )
            if requested_ch != 1:
                raise ValueError(
                    f"unsupported RTC channel count: {requested_ch}; expected mono"
                )
            if requested_codec not in {"pcm16", "opus"}:
                raise ValueError(
                    f"unsupported audio codec: {requested_codec}; expected pcm16 or opus"
                )

            format_changed = (
                requested_sr != self.rom_sr
                or requested_ch != self.rom_ch
                or requested_codec != self.rom_codec
            )
            pcm_format_changed = (
                requested_sr != self.rom_sr or requested_ch != self.rom_ch
            )
            self.rom_sr = requested_sr
            self.rom_ch = requested_ch
            self.rom_codec = requested_codec
            if format_changed:
                self.decoder = None
            if pcm_format_changed:
                self._microphone_health.reset(sample_rate=self.rom_sr)

            loop = asyncio.get_running_loop()
            try:
                pcm = await loop.run_in_executor(
                    self._decode_executor,
                    lambda: self._decode(
                        payload,
                        codec,
                        opus_frames=opus_frames,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "[RTC uplink] audio decode skipped codec=%s len=%d sr=%d frames=%s: %s",
                    codec or self.rom_codec,
                    len(payload),
                    self.rom_sr,
                    opus_frames,
                    exc,
                )
                return [], False, False

            uplink_started = False
            if pcm and not self._uplink_open:
                self._uplink_open = True
                uplink_started = True
                logger.info(
                    "[RTC uplink] first PCM frame sr=%d ch=%d codec=%s chunk=%d",
                    self.rom_sr,
                    self.rom_ch,
                    self.rom_codec,
                    len(pcm),
                )
            if pcm:
                self._microphone_health.feed_pcm(pcm, sample_rate=self.rom_sr)
            self.last_decoded_pcm = bytes(pcm)
            return [], uplink_started, False

    def microphone_health_snapshot(self) -> dict[str, object]:
        return self._microphone_health.snapshot()

    def consume_microphone_health_update(self) -> dict[str, object] | None:
        return self._microphone_health.consume_update()

    def _reset_rom(self) -> None:
        self._uplink_open = False
        self.rom_sr = self.audio_cfg.sample_rate
        self.rom_ch = self.audio_cfg.channels
        self.rom_codec = self.audio_cfg.input_codec

    def flush(self) -> list:
        self._reset_rom()
        return []

    def cancel_rom_uplink(self) -> None:
        self._reset_rom()
        logger.info("[RTC uplink] audio_cancel reset transport state")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # ``wait=False`` keeps USB disconnect from waiting on a native decoder
        # call; queued work is cancelled and the running call exits naturally.
        self._decode_executor.shutdown(wait=False, cancel_futures=True)
