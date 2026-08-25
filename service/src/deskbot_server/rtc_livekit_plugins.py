"""LiveKit speech adapters for the API-key based Deskbot voice services.

The team LampGo Agent SDK currently targets Volcengine's legacy
``app_id + access_token`` plugin.  Deskbot uses the newer Seed Speech APIs,
whose ASR and TTS authenticate with independent API keys.  These adapters let
the RTC worker reuse the already hardened Deskbot protocol implementations
without placing legacy credentials on the device or reverting to the old
WebSocket conversation loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from functools import wraps
from typing import Any

from deskbot_server.asr.pcm_normalization import (
    normalize_quiet_pcm_for_asr,
    utterance_p95_magnitude,
)
from deskbot_server.rtc_barge_in import (
    BARGE_IN_TOPIC,
    SPEECH_ACTIVITY_TOPIC,
    USER_SPEECH_CONFIRMED_END_PAYLOAD,
    USER_SPEECH_START_PAYLOAD,
    PlaybackEchoGuard,
    playback_echo_guard,
    should_confirm_barge_in,
)

logger = logging.getLogger("deskbot-server.rtc")

_ADAPTER_ENV = "DESKBOT_RTC_SPEECH_ADAPTER"
_API_KEY_ADAPTER = "deskbot_api_key"
_SEED_ASR_SAMPLE_RATE = 16_000

# 播放期“近端足够响则不按文本相似度否决”的 P95 幅度门限。V2 PDM 麦
# 数字增益偏低（正常语音 P95 约 600+，静息约 120-180，AEC 残留更弱），
# 现场可用环境变量校准，两种判定分支都会把实际 p95 打进日志。
_BARGE_IN_NEAR_END_P95 = max(
    0, int(os.environ.get("DESKBOT_BARGE_IN_NEAR_END_P95", "500") or 500)
)
_MAX_ERROR_DETAIL_CHARS = 320
_WORKER_IDLE_PROCESSES = 1
_WORKER_INITIALIZE_TIMEOUT_SEC = 30.0
_WORKER_LOAD_THRESHOLD = 1.0
# Six Chinese characters are normally enough for a natural first prosodic
# unit while avoiding the extra token boundary that an eight-character gate
# often required.  Punctuation still flushes immediately.
_LOW_LATENCY_TTS_CHARS = 6
_BARGE_IN_RECENT_SPEAKING_SECONDS = 3.0
_installed = False


def _deskbot_worker_load() -> float:
    """Keep the dedicated local worker available for its single Deskbot."""

    return 0.0


def _local_participant_if_connected(room: Any) -> Any | None:
    """Return the local participant without probing Room before connect()."""

    if room is None:
        return None
    try:
        return room.local_participant
    except Exception:
        # LiveKit deliberately raises here until Room.connect() has completed.
        return None


def _install_agent_server_defaults() -> None:
    """Use local-device defaults instead of LiveKit's cloud worker defaults.

    LiveKit production mode otherwise prewarms 16 job processes and marks the
    worker unavailable when the host CPU briefly exceeds 70%. On Windows that
    can delay Silero initialization long enough for the device to create its
    room before any worker is eligible for dispatch. The missed room is not
    backfilled, so no AgentSession ever subscribes to the device audio.
    """

    from livekit.agents import AgentServer

    current_init = AgentServer.__init__
    if getattr(current_init, "_deskbot_local_defaults", False):
        return

    @wraps(current_init)
    def _deskbot_agent_server_init(self, *args, **kwargs):
        kwargs.setdefault("num_idle_processes", _WORKER_IDLE_PROCESSES)
        kwargs.setdefault(
            "initialize_process_timeout",
            _WORKER_INITIALIZE_TIMEOUT_SEC,
        )
        kwargs.setdefault("load_threshold", _WORKER_LOAD_THRESHOLD)
        kwargs.setdefault("load_fnc", _deskbot_worker_load)
        return current_init(self, *args, **kwargs)

    setattr(_deskbot_agent_server_init, "_deskbot_local_defaults", True)
    AgentServer.__init__ = _deskbot_agent_server_init


def _apply_deskbot_room_audio_options(session: Any, kwargs: dict) -> None:
    """Run the room audio path at the device/provider-native sample rates.

    LiveKit Agent 1.5 defaults RoomIO to 24 kHz for both directions even
    though the device publishes 16 kHz and Seed ASR consumes 16 kHz, which
    forced a 16k->24k->16k round trip on input and an extra resample on
    output. This uses the public ``session.start(room_options=...)`` surface
    only; explicit caller-provided options always win.
    """

    if (
        kwargs.get("room_options")
        or kwargs.get("room_input_options")
        or kwargs.get("room_output_options")
    ):
        return
    try:
        from livekit.agents.voice.room_io import (
            AudioInputOptions,
            AudioOutputOptions,
            RoomOptions,
        )

        tts_rate = int(
            getattr(getattr(session, "tts", None), "sample_rate", 0) or 0
        )
        kwargs["room_options"] = RoomOptions(
            audio_input=AudioInputOptions(
                sample_rate=_SEED_ASR_SAMPLE_RATE,
            ),
            # Publishing the TTS track at the TTS's own rate removes the
            # agent-side resample; the Core gateway subscribes at 16 kHz.
            audio_output=AudioOutputOptions(
                sample_rate=tts_rate or _SEED_ASR_SAMPLE_RATE,
            ),
        )
        logger.info(
            "[rtc] room audio options input=%dHz output=%dHz",
            _SEED_ASR_SAMPLE_RATE,
            tts_rate or _SEED_ASR_SAMPLE_RATE,
        )
    except Exception:
        # An SDK without RoomOptions keeps its own defaults; the STT adapter
        # still resamples whatever the room delivers.
        logger.debug("[rtc] room audio options unavailable", exc_info=True)


def _install_agent_session_defaults() -> None:
    """Start generation while endpointing settles and keep turn gaps short."""

    from livekit.agents import AgentSession

    current_init = AgentSession.__init__
    if not getattr(current_init, "_deskbot_low_latency", False):

        @wraps(current_init)
        def _deskbot_agent_session_init(self, *args, **kwargs):
            kwargs.setdefault("preemptive_generation", True)
            kwargs.setdefault("min_endpointing_delay", 0.08)
            kwargs.setdefault("max_endpointing_delay", 0.45)
            # A raw VAD edge is only a barge-in candidate. Requiring one
            # transcribed word keeps LiveKit from cancelling TTS before Seed
            # ASR can reject residual loudspeaker echo.
            kwargs.setdefault("min_interruption_duration", 0.55)
            # Keep this at exactly 1, not 0 and not more:
            # - >0 keeps LiveKit's VAD-edge interrupt gated on transcript
            #   text. Our STT is non-streaming, so the interim transcript is
            #   always empty and raw VAD edges can never cancel TTS before
            #   Seed ASR rejects loudspeaker echo.
            # - 1 (not 4) because LiveKit's split_words(split_character=True)
            #   counts every CJK character as one "word": a 4-word gate
            #   swallowed short Chinese interruptions (e.g. 3-character stop
            #   phrases) at the end-of-turn check. The CJK-aware length gate
            #   lives in rtc_barge_in.should_confirm_barge_in(), which runs
            #   after the playback-echo classifier inside the STT adapter and
            #   empties weak transcripts, so any non-empty final transcript
            #   here has already been validated.
            kwargs.setdefault("min_interruption_words", 1)
            kwargs.setdefault("resume_false_interruption", True)
            kwargs.setdefault("false_interruption_timeout", 0.8)
            result = current_init(self, *args, **kwargs)

            # The worker uses a THREAD job executor, so module globals would
            # leak recent TTS text between rooms. One guard is shared only by
            # this session's own STT/TTS adapters.
            guard = PlaybackEchoGuard()
            self._deskbot_playback_echo_guard = guard
            self._deskbot_agent_state = "initializing"
            self._deskbot_last_speaking_mono = 0.0
            self._deskbot_barge_in_room = None
            self._deskbot_barge_in_tasks = set()
            self._deskbot_control_publish_lock = asyncio.Lock()
            self._deskbot_barge_in_handlers_installed = False
            for adapter in (kwargs.get("stt"), kwargs.get("tts")):
                bind_guard = getattr(adapter, "bind_playback_echo_guard", None)
                if callable(bind_guard):
                    bind_guard(guard)
            return result

        setattr(_deskbot_agent_session_init, "_deskbot_low_latency", True)
        AgentSession.__init__ = _deskbot_agent_session_init

    current_start = AgentSession.start
    if getattr(current_start, "_deskbot_barge_in_control", False):
        return

    @wraps(current_start)
    async def _deskbot_agent_session_start(self, *args, **kwargs):
        room = kwargs.get("room")
        if room is not None:
            self._deskbot_barge_in_room = room
            _apply_deskbot_room_audio_options(self, kwargs)

        if not getattr(self, "_deskbot_barge_in_handlers_installed", False):
            self._deskbot_barge_in_handlers_installed = True

            def _queue_fixed_controls(
                packets: tuple[tuple[str, bytes], ...],
                *,
                label: str,
            ) -> None:
                active_room = getattr(self, "_deskbot_barge_in_room", None)
                participant = _local_participant_if_connected(active_room)
                if participant is None or not packets:
                    return

                async def _publish_controls() -> None:
                    lock = self._deskbot_control_publish_lock
                    async with lock:
                        for topic, payload in packets:
                            await participant.publish_data(
                                payload,
                                reliable=True,
                                topic=topic,
                            )

                task = asyncio.create_task(
                    _publish_controls(),
                    name=f"deskbot-rtc-{label}-control",
                )
                tasks = self._deskbot_barge_in_tasks
                tasks.add(task)

                def _control_done(done: asyncio.Task) -> None:
                    tasks.discard(done)
                    if done.cancelled():
                        return
                    try:
                        error = done.exception()
                    except asyncio.CancelledError:
                        return
                    if error is not None:
                        logger.warning(
                            "[rtc][control] publish failed label=%s error=%s",
                            label,
                            type(error).__name__,
                        )

                task.add_done_callback(_control_done)

            @self.on("agent_state_changed")
            def _deskbot_agent_state_changed(event) -> None:
                now = time.monotonic()
                old_state = str(getattr(event, "old_state", "") or "")
                new_state = str(getattr(event, "new_state", "") or "")
                self._deskbot_agent_state = new_state
                if old_state == "speaking" or new_state == "speaking":
                    self._deskbot_last_speaking_mono = now
                guard = getattr(self, "_deskbot_playback_echo_guard", None)
                if guard is not None:
                    guard.set_playback_active(new_state == "speaking", now=now)

            @self.on("user_state_changed")
            def _deskbot_user_state_changed(event) -> None:
                new_state = str(getattr(event, "new_state", "") or "")
                if new_state != "speaking":
                    return
                _queue_fixed_controls(
                    (
                        (
                            SPEECH_ACTIVITY_TOPIC,
                            USER_SPEECH_START_PAYLOAD,
                        ),
                    ),
                    label="speech-start",
                )

            @self.on("user_input_transcribed")
            def _deskbot_user_input_transcribed(event) -> None:
                transcript = str(getattr(event, "transcript", "") or "").strip()
                if not getattr(event, "is_final", False) or not transcript:
                    return
                now = time.monotonic()
                state = str(getattr(self, "_deskbot_agent_state", "") or "")
                last_speaking = float(
                    getattr(self, "_deskbot_last_speaking_mono", 0.0) or 0.0
                )
                playback_recent = state == "speaking" or (
                    last_speaking > 0.0
                    and now - last_speaking <= _BARGE_IN_RECENT_SPEAKING_SECONDS
                )
                # Playback-time STT has already rejected echo and utterances
                # shorter than the sustained-speech gate before emitting this
                # final transcript. Do not run that duration gate a second
                # time here: UserInputTranscribedEvent does not carry audio
                # duration, so the old call always rejected ordinary Chinese
                # interruptions and left the new turn queued behind playback.
                # There is therefore no "weak interruption" branch at this
                # layer: every final transcript during recent playback is a
                # confirmed barge-in.  The real weak/echo gating lives inside
                # the STT (see DeskbotSeedSpeechSTT playback-time filtering).
                confirmed_barge_in = bool(playback_recent)
                packets: list[tuple[str, bytes]] = []
                if confirmed_barge_in:
                    # min_interruption_words intentionally protects playback
                    # from tiny ASR fragments. Preserve short, explicit stop
                    # commands by interrupting once their final text arrives.
                    try:
                        self.interrupt()
                    except RuntimeError:
                        logger.debug(
                            "[rtc][barge-in] confirmed interruption arrived "
                            "after speech already stopped"
                        )
                    logger.info(
                        "[rtc][barge-in] published confirmed interruption "
                        "transcript_chars=%d",
                        len(transcript),
                    )
                    packets.append((BARGE_IN_TOPIC, b"cancel"))
                packets.append(
                    (
                        SPEECH_ACTIVITY_TOPIC,
                        USER_SPEECH_CONFIRMED_END_PAYLOAD,
                    )
                )
                _queue_fixed_controls(
                    tuple(packets),
                    label=(
                        "barge-in-and-speech-end"
                        if confirmed_barge_in
                        else "speech-end"
                    ),
                )

            @self.on("conversation_item_added")
            def _deskbot_conversation_item_added(event) -> None:
                item = getattr(event, "item", None)
                role = str(getattr(item, "role", "") or "")
                metrics = getattr(item, "metrics", None)
                if role != "assistant" or not metrics:
                    return
                get_metric = getattr(metrics, "get", None)
                if not callable(get_metric):
                    return
                llm_ttft = get_metric("llm_node_ttft")
                tts_ttfb = get_metric("tts_node_ttfb")
                e2e = get_metric("e2e_latency")
                logger.info(
                    "[rtc][turn-metrics] llm_ttft_ms=%s tts_ttfb_ms=%s "
                    "e2e_ms=%s",
                    "-" if llm_ttft is None else round(float(llm_ttft) * 1000),
                    "-" if tts_ttfb is None else round(float(tts_ttfb) * 1000),
                    "-" if e2e is None else round(float(e2e) * 1000),
                )

        return await current_start(self, *args, **kwargs)

    setattr(_deskbot_agent_session_start, "_deskbot_barge_in_control", True)
    AgentSession.start = _deskbot_agent_session_start


def _safe_error_detail(exc: BaseException) -> str:
    """Keep provider diagnostics useful without copying credentials into logs."""

    detail = " ".join(str(exc).split())
    detail = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|authorization)\b"
        r"(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        detail,
    )
    if not detail:
        detail = type(exc).__name__
    return detail[:_MAX_ERROR_DETAIL_CHARS]


def _seed_asr_audio(
    rtc: Any,
    buffer: Any,
) -> tuple[bytes, dict[str, int]]:
    """Normalize LiveKit room audio to Seed ASR's 16 kHz mono PCM contract.

    LiveKit Agent 1.5 uses a 24 kHz mono RoomInput by default even when the
    publisher provides 16 kHz audio. Use LiveKit's Sox-backed resampler so the
    adapter remains compatible with current and future room input rates.
    """

    frames = [buffer] if isinstance(buffer, rtc.AudioFrame) else list(buffer)
    if not frames:
        return b"", {
            "frame_count": 0,
            "source_rate": 0,
            "source_channels": 0,
            "source_samples": 0,
            "output_samples": 0,
        }

    frame = frames[0] if len(frames) == 1 else rtc.combine_audio_frames(frames)
    source_rate = int(frame.sample_rate)
    source_channels = int(frame.num_channels)
    source_samples = int(frame.samples_per_channel)
    if source_rate <= 0:
        raise ValueError(f"invalid RTC sample rate: {source_rate}")
    if source_channels <= 0:
        raise ValueError(f"invalid RTC channel count: {source_channels}")

    if source_channels != 1:
        import numpy as np

        interleaved = np.frombuffer(frame.data, dtype="<i2")
        if interleaved.size % source_channels:
            raise ValueError(
                "RTC audio byte count is not aligned to the channel count"
            )
        mono = np.rint(
            interleaved.reshape(-1, source_channels)
            .astype(np.float32)
            .mean(axis=1)
        )
        mono = np.clip(mono, -32768, 32767).astype("<i2")
        frame = rtc.AudioFrame(
            data=mono.tobytes(),
            sample_rate=source_rate,
            num_channels=1,
            samples_per_channel=int(mono.size),
        )

    if source_rate != _SEED_ASR_SAMPLE_RATE:
        resampler = rtc.AudioResampler(
            input_rate=source_rate,
            output_rate=_SEED_ASR_SAMPLE_RATE,
            num_channels=1,
            quality=rtc.AudioResamplerQuality.HIGH,
        )
        output_frames = resampler.push(frame)
        output_frames.extend(resampler.flush())
        if not output_frames:
            return b"", {
                "frame_count": len(frames),
                "source_rate": source_rate,
                "source_channels": source_channels,
                "source_samples": source_samples,
                "output_samples": 0,
            }
        frame = (
            output_frames[0]
            if len(output_frames) == 1
            else rtc.combine_audio_frames(output_frames)
        )

    pcm = frame.data.tobytes()
    if len(pcm) % 2:
        raise ValueError("RTC PCM byte count is not aligned to s16le samples")
    return pcm, {
        "frame_count": len(frames),
        "source_rate": source_rate,
        "source_channels": source_channels,
        "source_samples": source_samples,
        "output_samples": len(pcm) // 2,
    }


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        return default


def _asr_settings():
    from deskbot_server.core.settings import AsrSettings, AsrTextFilterSettings

    return AsrSettings(
        # 设备实时语音固定使用豆包流式 ASR（RTC 内硬编码），与 Web 高级
        # 设置页的 ASR provider 选择无关——该选择只影响连通性测试与文本
        # 兼容路径。此决策已在 app2c/advanced.html 的 ASR 区文案中明示；
        # 改这里必须同步改那段披露。
        provider="doubao_streaming",
        timeout_seconds=max(1.0, _float_env("ASR_TIMEOUT_SECONDS", 30.0)),
        max_audio_bytes=max(1024, _int_env("ASR_MAX_AUDIO_BYTES", 10 * 1024 * 1024)),
        language=str(os.environ.get("ASR_LANGUAGE") or "zh-CN"),
        text_filter=AsrTextFilterSettings(
            min_text_len=1,
            min_chinese_ratio=0.0,
        ),
    )


def _build_stt():
    from livekit import rtc
    from livekit.agents import (
        DEFAULT_API_CONNECT_OPTIONS,
        APIConnectionError,
        APIConnectOptions,
        LanguageCode,
        stt,
    )
    from livekit.agents.types import NOT_GIVEN, NotGivenOr
    from livekit.agents.utils import AudioBuffer

    from deskbot_server.infrastructure.asr.volcengine_streaming import (
        VolcengineStreamingAsrAdapter,
    )

    class DeskbotSeedSpeechSTT(stt.STT):
        def __init__(self) -> None:
            super().__init__(
                capabilities=stt.STTCapabilities(
                    streaming=False,
                    interim_results=False,
                )
            )
            self._adapter = VolcengineStreamingAsrAdapter(_asr_settings())
            self._logged_input_format = False
            # Compatibility fallback for direct adapter use. AgentSession
            # replaces this with its private guard before recognition starts.
            self._playback_echo_guard = playback_echo_guard

        def bind_playback_echo_guard(self, guard: PlaybackEchoGuard) -> None:
            self._playback_echo_guard = guard

        @property
        def model(self) -> str:
            return "volcengine-seed-asr-2.0"

        @property
        def provider(self) -> str:
            return "deskbot-volcengine"

        @staticmethod
        def _final_event(request_id: str, text: str = "") -> stt.SpeechEvent:
            alternatives = (
                [
                    stt.SpeechData(
                        language=LanguageCode("zh-CN"),
                        text=text,
                        confidence=1.0,
                    )
                ]
                if text
                else []
            )
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                request_id=request_id,
                alternatives=alternatives,
            )

        async def recognize(
            self,
            buffer: AudioBuffer,
            *,
            language: NotGivenOr[str] = NOT_GIVEN,
            conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        ) -> stt.SpeechEvent:
            try:
                return await super().recognize(
                    buffer,
                    language=language,
                    conn_options=conn_options,
                )
            except APIConnectionError as exc:
                # LiveKit closes AgentSession after the final STT retry. Drop
                # only this turn so a later utterance can recover in-place.
                logger.error(
                    "[rtc][seed-asr] utterance dropped after retries; "
                    "keeping AgentSession alive error=%s",
                    _safe_error_detail(exc),
                    exc_info=True,
                )
                return self._final_event(uuid.uuid4().hex)

        async def _recognize_impl(
            self,
            buffer: AudioBuffer,
            *,
            language: NotGivenOr[str] = NOT_GIVEN,
            conn_options: APIConnectOptions,
        ) -> stt.SpeechEvent:
            del language, conn_options
            request_id = uuid.uuid4().hex
            metadata = {
                "frame_count": 0,
                "source_rate": 0,
                "source_channels": 0,
                "source_samples": 0,
                "output_samples": 0,
            }
            try:
                pcm, metadata = _seed_asr_audio(rtc, buffer)
                if not pcm:
                    logger.debug(
                        "[rtc][seed-asr] ignored empty utterance request_id=%s",
                        request_id,
                    )
                    return self._final_event(request_id)
                playback_active = self._playback_echo_guard.is_playback_active()
                if playback_active:
                    # Never turn a weak AEC residual back into intelligible
                    # speech.  A real near-end barge-in remains loud enough
                    # for Seed ASR without this offline gain stage.
                    asr_gain = 1.0
                else:
                    pcm, asr_gain = normalize_quiet_pcm_for_asr(pcm)
                if asr_gain > 1.0:
                    logger.info(
                        "[rtc][seed-asr] quiet utterance normalized "
                        "request_id=%s gain=%.2fx pcm_bytes=%d",
                        request_id,
                        asr_gain,
                        len(pcm),
                    )
                if not self._logged_input_format:
                    logger.info(
                        "[rtc][seed-asr] input ready source=%dHz/%dch "
                        "frames=%d samples=%d -> %dHz/1ch samples=%d",
                        metadata["source_rate"],
                        metadata["source_channels"],
                        metadata["frame_count"],
                        metadata["source_samples"],
                        _SEED_ASR_SAMPLE_RATE,
                        metadata["output_samples"],
                    )
                    self._logged_input_format = True
                text = (
                    await self._adapter.transcribe(pcm, _SEED_ASR_SAMPLE_RATE)
                ).strip()
                is_echo, similarity = self._playback_echo_guard.classify(text)
                if is_echo:
                    # 文本相似度分不开“AEC 残留回声”与“用户复述了回答里的
                    # 词组（或回答复述了用户的问题）”——后者曾把播放期追问
                    # “你喜欢吃什么”按 similarity=0.889 吞掉。借用上方增益
                    # 闸门的同一物理假设补最终裁决：真实近端插话足够响亮，
                    # 残留回声则很弱。足够响的语音不按文本相似度否决。
                    near_end_p95 = utterance_p95_magnitude(pcm)
                    if near_end_p95 >= _BARGE_IN_NEAR_END_P95:
                        logger.info(
                            "[rtc][barge-in] echo-similar transcript kept: "
                            "loud near-end speech request_id=%s "
                            "transcript_chars=%d similarity=%.3f p95=%d",
                            request_id,
                            len(text),
                            similarity,
                            near_end_p95,
                        )
                        is_echo = False
                    else:
                        logger.info(
                            "[rtc][barge-in] rejected playback echo "
                            "request_id=%s transcript_chars=%d "
                            "similarity=%.3f p95=%d",
                            request_id,
                            len(text),
                            similarity,
                            near_end_p95,
                        )
                        text = ""
                elif playback_active and text:
                    speech_seconds = (
                        metadata["output_samples"] / _SEED_ASR_SAMPLE_RATE
                    )
                    # 播放期的打断除文本/时长门外还必须过近端响度门：AEC
                    # 残留 + 房间噪声可能被 ASR 幻听成一段与播放文本不相似
                    # 的长句（相似度门拦不住），但残留是安静的，真人插话
                    # 是响的。实测教训：AGC 增益提高后残留触发设备 VAD，
                    # 机器人整句"打断自己"。
                    near_end_p95 = utterance_p95_magnitude(pcm)
                    if near_end_p95 < _BARGE_IN_NEAR_END_P95:
                        logger.info(
                            "[rtc][barge-in] rejected quiet playback-time "
                            "ASR request_id=%s transcript_chars=%d "
                            "speech_ms=%d p95=%d",
                            request_id,
                            len(text),
                            round(speech_seconds * 1000),
                            near_end_p95,
                        )
                        text = ""
                    elif should_confirm_barge_in(
                        text,
                        speech_seconds=speech_seconds,
                    ):
                        logger.info(
                            "[rtc][barge-in] accepted playback-time ASR "
                            "request_id=%s transcript_chars=%d speech_ms=%d "
                            "p95=%d",
                            request_id,
                            len(text),
                            round(speech_seconds * 1000),
                            near_end_p95,
                        )
                    else:
                        # Seed can occasionally turn AEC residual/noise into a
                        # plausible result. During playback a normal (non-stop)
                        # interruption requires both final text and sustained
                        # near-end audio; text length alone cannot cancel TTS.
                        logger.info(
                            "[rtc][barge-in] rejected weak playback-time ASR "
                            "request_id=%s transcript_chars=%d speech_ms=%d",
                            request_id,
                            len(text),
                            round(speech_seconds * 1000),
                        )
                        text = ""
            except Exception as exc:
                detail = _safe_error_detail(exc)
                logger.error(
                    "[rtc][seed-asr] request failed request_id=%s "
                    "source=%dHz/%dch frames=%d samples=%d output_samples=%d "
                    "error=%s: %s",
                    request_id,
                    metadata["source_rate"],
                    metadata["source_channels"],
                    metadata["frame_count"],
                    metadata["source_samples"],
                    metadata["output_samples"],
                    type(exc).__name__,
                    detail,
                    exc_info=True,
                )
                raise APIConnectionError(
                    "Deskbot Seed ASR failed: "
                    f"{type(exc).__name__}: {detail}"
                ) from exc

            return self._final_event(request_id, text)

    return DeskbotSeedSpeechSTT()


def _build_tts():
    from livekit.agents import (
        DEFAULT_API_CONNECT_OPTIONS,
        APIConnectionError,
        APIConnectOptions,
        tts,
    )

    from deskbot_server.tts.doubao import (
        load_doubao_tts_config,
        prewarm_doubao_tts,
        synthesize_doubao_tts,
    )

    config = load_doubao_tts_config()

    def _reload_tts_config(fallback):
        """取当前 .env 里的 TTS 配置，保留构造时确定的 sample_rate。"""
        import dataclasses

        try:
            fresh = load_doubao_tts_config()
        except Exception:
            return fallback
        if not fresh.speaker or not fresh.api_key:
            return fallback
        return dataclasses.replace(fresh, sample_rate=fallback.sample_rate)


    class DeskbotSeedSpeechTTS(tts.TTS):
        def __init__(self) -> None:
            super().__init__(
                capabilities=tts.TTSCapabilities(streaming=True),
                sample_rate=config.sample_rate,
                num_channels=1,
            )
            self._prewarm_task = None
            # Compatibility fallback for direct adapter use. AgentSession
            # replaces this with its private guard before synthesis starts.
            self._playback_echo_guard = playback_echo_guard

        def bind_playback_echo_guard(self, guard: PlaybackEchoGuard) -> None:
            self._playback_echo_guard = guard

        def prewarm(self) -> None:
            import asyncio

            if self._prewarm_task is None or self._prewarm_task.done():
                self._prewarm_task = asyncio.create_task(
                    prewarm_doubao_tts(config),
                    name="deskbot-seed-tts-prewarm",
                )

        @property
        def model(self) -> str:
            return config.model or "seed-tts"

        @property
        def provider(self) -> str:
            return "deskbot-volcengine"

        def synthesize(
            self,
            text: str,
            *,
            conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        ):
            return DeskbotSeedSpeechChunkedStream(
                tts=self,
                input_text=text,
                conn_options=conn_options,
            )

        def stream(
            self,
            *,
            conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        ):
            return DeskbotSeedSpeechSynthesizeStream(
                tts=self,
                conn_options=conn_options,
            )

    class DeskbotSeedSpeechChunkedStream(tts.ChunkedStream):
        async def _run(self, output_emitter: tts.AudioEmitter) -> None:
            clean = str(self._input_text or "").strip()
            if not clean:
                return
            request_id = uuid.uuid4().hex
            # 每次合成前重新读取配置：换音色是在控制台（:5050）里写 .env 的，
            # 而这里跑在 Agent 子进程，闭包里的 config 停在进程启动那一刻，
            # 于是 PC 上明明换了音色、设备仍旧用旧嗓子说话，只有重启才生效。
            # sample_rate 沿用构造时的值——它已经写进 TTSCapabilities，中途
            # 改变会和已声明的能力不一致。
            live = _reload_tts_config(config)
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=live.sample_rate,
                num_channels=1,
                mime_type=f"audio/pcm;rate={live.sample_rate}",
                stream=False,
            )
            try:
                self._tts._playback_echo_guard.note_tts_segment(
                    request_id,
                    clean,
                )
                await synthesize_doubao_tts(
                    clean,
                    live,
                    on_pcm=output_emitter.push,
                )
            except Exception as exc:
                detail = _safe_error_detail(exc)
                logger.error(
                    "[rtc][seed-tts] synthesis failed request_id=%s "
                    "text_chars=%d error=%s: %s",
                    request_id,
                    len(clean),
                    type(exc).__name__,
                    detail,
                    exc_info=True,
                )
                raise APIConnectionError(
                    "Deskbot Seed TTS failed: "
                    f"{type(exc).__name__}: {detail}"
                ) from exc

    class DeskbotSeedSpeechSynthesizeStream(tts.SynthesizeStream):
        async def _run(self, output_emitter: tts.AudioEmitter) -> None:
            request_id = uuid.uuid4().hex
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=config.sample_rate,
                num_channels=1,
                mime_type=f"audio/pcm;rate={config.sample_rate}",
                stream=True,
            )
            output_emitter.start_segment(segment_id=uuid.uuid4().hex)
            pending = ""

            async def _speak_pending() -> None:
                nonlocal pending
                clean = pending.strip()
                pending = ""
                if not clean:
                    return
                # Incremental LLM output commonly leaves a final punctuation
                # token after the preceding text chunk has already started
                # synthesis.  Seed TTS correctly returns no audio for that
                # token; treating it as an API failure used to tear down the
                # pooled WebSocket after every answer.
                if not re.search(r"[\w\u3400-\u9fff]", clean):
                    logger.debug(
                        "[rtc][seed-tts] skipped punctuation-only segment "
                        "request_id=%s chars=%d",
                        request_id,
                        len(clean),
                    )
                    return
                segment_started = time.monotonic()
                first_pcm = True

                def _push_pcm(chunk: bytes) -> None:
                    nonlocal first_pcm
                    if first_pcm:
                        first_pcm = False
                        logger.info(
                            "[rtc][seed-tts] first PCM request_id=%s "
                            "text_chars=%d ttfb_ms=%d bytes=%d",
                            request_id,
                            len(clean),
                            int((time.monotonic() - segment_started) * 1000),
                            len(chunk),
                        )
                    output_emitter.push(chunk)

                logger.info(
                    "[rtc][seed-tts] segment start request_id=%s text_chars=%d",
                    request_id,
                    len(clean),
                )
                try:
                    self._tts._playback_echo_guard.note_tts_segment(
                        request_id,
                        clean,
                    )
                    await synthesize_doubao_tts(
                        clean,
                        config,
                        on_pcm=_push_pcm,
                    )
                    output_emitter.flush()
                except Exception as exc:
                    detail = _safe_error_detail(exc)
                    logger.error(
                        "[rtc][seed-tts] streaming segment failed "
                        "request_id=%s text_chars=%d error=%s: %s",
                        request_id,
                        len(clean),
                        type(exc).__name__,
                        detail,
                        exc_info=True,
                    )
                    raise APIConnectionError(
                        "Deskbot Seed TTS streaming failed: "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc

            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    await _speak_pending()
                    continue
                pending += str(data)
                stripped = pending.rstrip()
                if (
                    len(stripped) >= _LOW_LATENCY_TTS_CHARS
                    or stripped.endswith(("。", "！", "？", "，", ".", "!", "?", ",", ";", "；"))
                ):
                    await _speak_pending()
            await _speak_pending()

    return DeskbotSeedSpeechTTS()


def install_lampgo_speech_adapters() -> bool:
    """Patch the LampGo worker factory when Deskbot API-key mode is selected."""

    global _installed
    if _installed:
        return True
    if str(os.environ.get(_ADAPTER_ENV) or "").strip().lower() != _API_KEY_ADAPTER:
        return False

    try:
        _install_agent_session_defaults()
    except ModuleNotFoundError:
        # Unit/offline tooling can patch LampGo's speech factories without
        # installing the optional LiveKit SDK. The real worker process has the
        # SDK and receives the latency defaults normally.
        logger.debug("[rtc] LiveKit AgentSession defaults unavailable")

    def _create_stt(*, config: Any, runtime: Any):
        del config, runtime
        return _build_stt()

    def _create_tts(*, config: Any, runtime: Any):
        del config, runtime
        return _build_tts()

    import lampgo_livekit_agent.speech as lampgo_speech
    import lampgo_livekit_agent.worker as lampgo_worker

    try:
        _install_agent_server_defaults()
    except ModuleNotFoundError:
        logger.debug("[rtc] LiveKit AgentServer defaults unavailable")
    lampgo_speech.create_stt = _create_stt
    lampgo_speech.create_tts = _create_tts
    # ``lampgo_livekit_agent.__init__`` imports ``process`` which imports
    # worker-level aliases before this adapter gets control. Patch both the
    # factory module and those already-bound aliases.
    lampgo_worker.create_stt = _create_stt
    lampgo_worker.create_tts = _create_tts
    _installed = True
    logger.info(
        "[rtc] installed Deskbot Seed Speech API-key adapters for LampGo worker "
        "idle_processes=%d initialize_timeout=%.1fs load=dedicated",
        _WORKER_IDLE_PROCESSES,
        _WORKER_INITIALIZE_TIMEOUT_SEC,
    )
    return True


__all__ = ["install_lampgo_speech_adapters"]
