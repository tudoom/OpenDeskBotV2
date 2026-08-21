"""Process-wide wiring between USB device sessions and the RTC gateway."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import numpy as np

from deskbot_server.application.expression_runtime import (
    RtcExpressionRuntime,
    get_expression_runtime,
    register_expression_runtime,
    unregister_expression_runtime,
)
from deskbot_server.pipeline.opus_downlink import (
    _OPUS_LP_HDR,
    new_downlink_opus_encoder,
)
from deskbot_server.pipeline.opus_uplink import opus_frame_samples
from deskbot_server.rtc_gateway import (
    DeskbotRtcGateway,
    RtcDeviceCapabilities,
    RtcDeviceSession,
)

logger = logging.getLogger("deskbot-server")

# Device playback runs on the fixed 16 kHz mono downlink contract.
_PLAYBACK_SAMPLE_RATE = 16000

_gateway: DeskbotRtcGateway | None = None
# True 当本次运行的网关决策已定（已安装真实网关，或声明不会安装）。
_gateway_decision_final = False
_bindings: dict[str, "_UsbRtcBinding"] = {}
_expression_sessions: dict[str, object] = {}
_REMOTE_AUDIO_END_GAP_SEC = 1.25
# Cadence of the per-binding remote-end watchdog.  The end-of-stream check
# only needs gap resolution (~1.25 s), so a 100 ms poll adds at most 100 ms
# to the END frame while avoiding a cancel+create timer task per 10-20 ms
# remote frame (50-100 task churns per second per device).
_REMOTE_END_WATCH_INTERVAL_SEC = 0.10
_REMOTE_AUDIO_TRAILING_SILENCE_SEC = 0.9
_REMOTE_AUDIO_SILENCE_MEAN_ABS = 24
# LiveKit already emits ordered 20 ms PCM frames.  Waiting for five frames
# added a fixed 100 ms before the USB BEGIN_STREAM, then retained two frames
# behind a three-frame batch.  Start from the first audible frame and keep the
# steady-state batches small; USB framing already supplies backpressure.
_REMOTE_AUDIO_BATCH_FRAMES = 1
_REMOTE_AUDIO_PREBUFFER_FRAMES = 1
_PUBLISH_QUEUE_BATCHES = 5
_PUBLISH_LIVE_BACKLOG_BATCHES = 2
_PUBLISH_MAX_FRAME_AGE_SEC = 0.75
# Dropping by age alone silently loses half an utterance whenever the event
# loop stalls briefly while somebody is talking, and Seed ASR then transcribes
# the mangled remainder.  While speech is active the publisher prefers latency
# over loss: the backlog and age bounds widen (still bounded), and only silent
# frames stay on the aggressive path.
_PUBLISH_QUEUE_SPEECH_BATCHES = 25
_PUBLISH_LIVE_BACKLOG_SPEECH_BATCHES = 25
_PUBLISH_MAX_FRAME_AGE_SPEECH_SEC = 2.5
_BIND_RETRY_MAX_DELAY_SEC = 15.0


@dataclass(slots=True)
class _UsbRtcBinding:
    device_session: object
    rtc_session: RtcDeviceSession
    expression_runtime: RtcExpressionRuntime | None = None
    pcm_queue: asyncio.Queue[tuple[bytes, bool, float, int]] = field(
        # Firmware normally delivers 100 ms Opus batches.  The hard bound is
        # the in-speech allowance (~2.5 s); ``feed`` enforces the tighter
        # 500 ms silence bound itself, and the publisher trims further before
        # feeding a real-time LiveKit AudioSource.
        default_factory=lambda: asyncio.Queue(
            maxsize=_PUBLISH_QUEUE_SPEECH_BATCHES
        )
    )
    publish_task: asyncio.Task | None = None
    playback_buffer: bytearray = field(default_factory=bytearray)
    playback_opus_packets: list[bytes] = field(default_factory=list)
    opus_encoder: object | None = None
    last_remote_frame_mono: float = 0.0
    remote_silence_seconds: float = 0.0
    device_vad_active: bool = False
    remote_end_task: asyncio.Task | None = None
    remote_stream_open: bool = False
    playback_started: bool = False
    device_audio_generation: int | None = None
    agent_state: str = ""
    # Agent ``listening`` is also its steady ready state, so it cannot by
    # itself prove that a person is speaking.  Keep a separate bit driven by
    # the Agent's reliable speech-activity packets; only that signal is
    # allowed to put the screen into the visible listening expression.
    user_speech_active: bool = False
    remote_playback_blocked: bool = False
    blocked_seen_non_speaking: bool = False
    playback_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    publish_drop_count: int = 0
    last_publish_drop_log_mono: float = 0.0

    async def start(self) -> None:
        await self.rtc_session.connect()
        self._discard_old_connection_audio()
        if self.publish_task is None or self.publish_task.done():
            self.publish_task = asyncio.create_task(
                self._publish_loop(),
                name=f"deskbot-rtc-publish:{self.rtc_session.device_id}",
            )

    def _speech_active(self) -> bool:
        """Whether frames being queued belong to an in-progress utterance.

        The device VAD sideband marks capture-time speech; the Agent's
        confirmed speech-activity packets cover VAD-less devices and the tail
        between raw VAD end and the confirmed utterance end.
        """

        return bool(self.device_vad_active or self.user_speech_active)

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        import time

        queued = (
            bytes(pcm),
            self.device_vad_active,
            time.monotonic(),
            int(getattr(self.rtc_session, "connection_generation", 0)),
        )
        limit = (
            _PUBLISH_QUEUE_SPEECH_BATCHES
            if self._speech_active()
            else _PUBLISH_QUEUE_BATCHES
        )
        while self.pcm_queue.qsize() >= limit:
            try:
                dropped = self.pcm_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._record_publish_drop("queue_full", speech=bool(dropped[1]))
        try:
            self.pcm_queue.put_nowait(queued)
        except asyncio.QueueFull:
            try:
                dropped = self.pcm_queue.get_nowait()
                self._record_publish_drop(
                    "queue_full",
                    speech=bool(dropped[1]),
                )
            except asyncio.QueueEmpty:
                pass
            self.pcm_queue.put_nowait(queued)

    def _record_publish_drop(
        self,
        reason: str,
        count: int = 1,
        *,
        speech: bool = False,
    ) -> None:
        import time

        self.publish_drop_count += max(1, int(count))
        now = time.monotonic()
        if (
            speech
            or self.publish_drop_count == 1
            or now - self.last_publish_drop_log_mono >= 2.0
        ):
            # Losing in-speech audio truncates the user's sentence, so those
            # drops are never rate-limited away.
            logger.warning(
                "[rtc] dropped %s microphone batches device_id=%s "
                "reason=%s total=%d queue=%d generation=%d",
                "in-speech" if speech else "stale",
                self.rtc_session.device_id,
                reason,
                self.publish_drop_count,
                self.pcm_queue.qsize(),
                int(getattr(self.rtc_session, "connection_generation", 0)),
            )
            self.last_publish_drop_log_mono = now

    def _discard_old_connection_audio(self) -> None:
        generation = int(
            getattr(self.rtc_session, "connection_generation", 0)
        )
        retained: list[tuple[bytes, bool, float, int]] = []
        dropped = 0
        while True:
            try:
                item = self.pcm_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item[3] == generation:
                retained.append(item)
            else:
                dropped += 1
        keep_from = len(retained) - _PUBLISH_LIVE_BACKLOG_BATCHES
        for index, item in enumerate(retained):
            # Keep every same-generation speech frame; only excess silence is
            # trimmed down to the live backlog window.
            if item[1] or index >= keep_from:
                self.pcm_queue.put_nowait(item)
            else:
                dropped += 1
        if dropped:
            self._record_publish_drop("connection_generation", dropped)

    async def _publish_loop(self) -> None:
        while True:
            pcm, voice_active, captured_mono, generation = (
                await self.pcm_queue.get()
            )
            # A real-time AudioSource consumes at wall-clock speed and can
            # never catch up once reconnect work creates a backlog.  Keep only
            # the newest ~200 ms of silence rather than delaying every later
            # utterance — but never trade away frames of an active utterance:
            # those ride out the (still bounded) speech backlog with latency.
            trimmed = 0
            trimmed_speech = 0
            while (
                self.pcm_queue.qsize()
                >= (
                    _PUBLISH_LIVE_BACKLOG_SPEECH_BATCHES
                    if voice_active
                    else _PUBLISH_LIVE_BACKLOG_BATCHES
                )
            ):
                try:
                    replacement = self.pcm_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if voice_active:
                    trimmed_speech += 1
                else:
                    trimmed += 1
                pcm, voice_active, captured_mono, generation = replacement
            if trimmed:
                self._record_publish_drop("live_backlog", trimmed)
            if trimmed_speech:
                self._record_publish_drop(
                    "live_backlog",
                    trimmed_speech,
                    speech=True,
                )
            current_generation = int(
                getattr(self.rtc_session, "connection_generation", 0)
            )
            if generation != current_generation:
                self._record_publish_drop("connection_generation")
                continue
            try:
                await self.rtc_session.feed_pcm16(
                    pcm,
                    voice_active=voice_active,
                    captured_mono=captured_mono,
                    max_age_seconds=(
                        _PUBLISH_MAX_FRAME_AGE_SPEECH_SEC
                        if voice_active
                        else _PUBLISH_MAX_FRAME_AGE_SEC
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[rtc] dropped microphone frame after reconnect failure "
                    "device_id=%s",
                    self.rtc_session.device_id,
                )
                await asyncio.sleep(0.25)

    async def play_remote(self, pcm: bytes, sample_rate: int, channels: int) -> None:
        async with self.playback_lock:
            try:
                await self._play_remote_locked(pcm, sample_rate, channels)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._clear_remote_stream_state()
                raise

    async def _play_remote_locked(
        self,
        pcm: bytes,
        sample_rate: int,
        channels: int,
    ) -> None:
        import time

        if channels != 1 or not pcm:
            return
        if self.remote_playback_blocked:
            # LiveKit/USB can retain a short tail after an Agent speech handle
            # is interrupted. Do not let those old frames create a new BEGIN.
            return
        now = time.monotonic()
        if (
            self.remote_stream_open
            and self.last_remote_frame_mono > 0
            and now - self.last_remote_frame_mono > _REMOTE_AUDIO_END_GAP_SEC
        ):
            await self._finish_remote_stream_locked()
        if sample_rate != 16000:
            source = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            if source.size == 0:
                return
            output_count = max(1, round(source.size * 16000 / sample_rate))
            old_x = np.arange(source.size, dtype=np.float32)
            new_x = np.linspace(
                0,
                max(0, source.size - 1),
                output_count,
                dtype=np.float32,
            )
            pcm = np.interp(new_x, old_x, source).astype("<i2").tobytes()

        signal = np.frombuffer(pcm, dtype="<i2")
        if signal.size == 0:
            return
        mean_abs = float(
            np.abs(signal.astype(np.int32)).mean(dtype=np.float64)
        )
        duration_seconds = signal.size / 16000.0
        silent = mean_abs <= _REMOTE_AUDIO_SILENCE_MEAN_ABS
        if silent and not self.remote_stream_open:
            # LiveKit remote tracks remain alive by emitting zero PCM.  Such
            # keepalive audio must never create an endless device stream.
            return
        if silent:
            self.remote_silence_seconds += duration_seconds
            if (
                self.remote_silence_seconds
                >= _REMOTE_AUDIO_TRAILING_SILENCE_SEC
            ):
                logger.info(
                    "[rtc] ending continuous-silence playback "
                    "device_id=%s silence_ms=%d",
                    self.rtc_session.device_id,
                    round(self.remote_silence_seconds * 1000),
                )
                await self._finish_remote_stream_locked()
                return
        else:
            self.remote_silence_seconds = 0.0
            if not self.remote_stream_open:
                self.remote_stream_open = True

        self.last_remote_frame_mono = now
        self.playback_buffer.extend(pcm)
        frame_samples = opus_frame_samples(_PLAYBACK_SAMPLE_RATE)
        frame_bytes = frame_samples * 2
        if self.opus_encoder is None:
            self.opus_encoder = new_downlink_opus_encoder(
                _PLAYBACK_SAMPLE_RATE
            )
        while len(self.playback_buffer) >= frame_bytes:
            frame = bytes(self.playback_buffer[:frame_bytes])
            del self.playback_buffer[:frame_bytes]
            opus = self.opus_encoder.encode(frame, frame_samples)
            self.playback_opus_packets.append(opus)
        await self._flush_playback_packets_locked(force=False)
        self._arm_remote_end()

    async def _flush_playback_packets_locked(self, *, force: bool) -> None:
        if not self.playback_opus_packets:
            return
        if (
            not self.playback_started
            and not force
            and len(self.playback_opus_packets)
            < _REMOTE_AUDIO_PREBUFFER_FRAMES
        ):
            return
        while (
            len(self.playback_opus_packets) >= _REMOTE_AUDIO_BATCH_FRAMES
            or (force and self.playback_opus_packets)
        ):
            count = min(
                _REMOTE_AUDIO_BATCH_FRAMES,
                len(self.playback_opus_packets),
            )
            packets = self.playback_opus_packets[:count]
            del self.playback_opus_packets[:count]
            # Same length-prefixed batch framing as the pb TTS downlink
            # (uint16_be length + opus frame; see pipeline/opus_downlink.py).
            payload = b"".join(
                _OPUS_LP_HDR.pack(len(packet)) + packet
                for packet in packets
            )
            if not self.playback_started:
                self.device_audio_generation = (
                    await self.device_session.begin_audio_down_stream(payload)
                )
                self.playback_started = True
            else:
                await self.device_session.send_audio_down_opus(
                    payload,
                    generation=self.device_audio_generation,
                )

    def _clear_remote_stream_state(self) -> None:
        self.playback_buffer.clear()
        self.playback_opus_packets.clear()
        self.remote_stream_open = False
        self.playback_started = False
        self.device_audio_generation = None
        self.last_remote_frame_mono = 0.0
        self.remote_silence_seconds = 0.0
        self.opus_encoder = None

    def _arm_remote_end(self) -> None:
        """Ensure the per-binding remote-end watchdog task is running.

        Remote frames arrive every 10-20 ms.  Frames only refresh
        ``last_remote_frame_mono`` (single writer, under the playback lock);
        one persistent watchdog polls for the unchanged
        ``_REMOTE_AUDIO_END_GAP_SEC`` timeout instead of cancelling and
        recreating a timer task per frame.  ``cancel_remote_playback`` and
        ``close`` cancel the watchdog; the next remote frame re-arms it.
        """

        task = self.remote_end_task
        if task is not None and not task.done():
            return
        self.remote_end_task = asyncio.create_task(
            self._remote_end_watchdog(),
            name=f"deskbot-rtc-playback-end:{self.rtc_session.device_id}",
        )

    async def _remote_end_watchdog(self) -> None:
        import time

        try:
            while True:
                # Tests monkeypatch the gap far below the watchdog cadence;
                # follow the smaller of the two so short gaps stay observable.
                await asyncio.sleep(
                    min(
                        _REMOTE_END_WATCH_INTERVAL_SEC,
                        _REMOTE_AUDIO_END_GAP_SEC / 2,
                    )
                )
                if not self.remote_stream_open:
                    continue
                last = self.last_remote_frame_mono
                if last <= 0:
                    continue
                if time.monotonic() - last < _REMOTE_AUDIO_END_GAP_SEC:
                    continue
                try:
                    # ``_finish_remote_stream_locked`` re-checks
                    # ``remote_stream_open`` under the playback lock, so a
                    # concurrent cancel/clear makes this a no-op.
                    await self._finish_remote_stream()
                except Exception as exc:
                    # A USB disconnect can race the silence timeout.  Consume
                    # it here so the watchdog never leaks an unobserved task
                    # exception and stays armed for a later stream.
                    self._clear_remote_stream_state()
                    logger.info(
                        "[rtc] playback end skipped after device disconnect "
                        "device_id=%s error=%s",
                        self.rtc_session.device_id,
                        type(exc).__name__,
                    )
        except asyncio.CancelledError:
            return

    async def _finish_remote_stream(self) -> None:
        async with self.playback_lock:
            await self._finish_remote_stream_locked()

    async def _finish_remote_stream_locked(self) -> None:
        if not self.remote_stream_open:
            return
        try:
            frame_samples = opus_frame_samples(_PLAYBACK_SAMPLE_RATE)
            frame_bytes = frame_samples * 2
            if self.playback_buffer:
                self.playback_buffer.extend(
                    b"\x00" * (frame_bytes - len(self.playback_buffer))
                )
                if self.opus_encoder is None:
                    self.opus_encoder = new_downlink_opus_encoder(
                        _PLAYBACK_SAMPLE_RATE
                    )
                opus = self.opus_encoder.encode(
                    bytes(self.playback_buffer),
                    frame_samples,
                )
                self.playback_buffer.clear()
                self.playback_opus_packets.append(opus)
            await self._flush_playback_packets_locked(force=True)
            if self.playback_started:
                await self.device_session.send_audio_down_end(
                    generation=self.device_audio_generation,
                )
        finally:
            self._clear_remote_stream_state()

    def note_vad(self, *, active: bool) -> None:
        """Annotate microphone uplink without truncating RTC playback.

        ESP-SR is intentionally tuned to notice quiet speech on the V2 board,
        so its raw start/end sideband can chatter around the noise floor.  The
        LiveKit agent owns conversational interruption using the actual audio
        stream; treating every hardware VAD edge as an immediate downlink
        cancel discards already generated TTS before it reaches the speaker.
        """

        self.device_vad_active = bool(active)

    def note_agent_state(self, state: str) -> bool:
        """Advance the post-cancel fence and reject duplicate state events."""

        normalized = str(state or "").strip().lower()
        if normalized not in {
            "initializing",
            "idle",
            "listening",
            "thinking",
            "speaking",
        }:
            return False
        previous = self.agent_state
        self.agent_state = normalized
        if not self.remote_playback_blocked:
            return normalized != previous
        if normalized != "speaking":
            self.blocked_seen_non_speaking = True
            return normalized != previous
        if not self.blocked_seen_non_speaking:
            # A duplicate `speaking` update still belongs to the interrupted
            # speech handle and must not reopen device playback.
            return False
        self.remote_playback_blocked = False
        self.blocked_seen_non_speaking = False
        logger.info(
            "[rtc] admitted new Agent playback after interruption "
            "device_id=%s",
            self.rtc_session.device_id,
        )
        return True

    async def cancel_remote_playback(
        self,
        *,
        reason: str,
        block_old_pcm: bool = True,
    ) -> None:
        """Immediately invalidate buffered and device-side remote playback."""

        async with self.playback_lock:
            end_task = self.remote_end_task
            self.remote_end_task = None
            if end_task is not None and end_task is not asyncio.current_task():
                end_task.cancel()
                await asyncio.gather(end_task, return_exceptions=True)

            generation = self.device_audio_generation
            playback_started = self.playback_started and generation is not None
            self._clear_remote_stream_state()
            self.remote_playback_blocked = bool(block_old_pcm)
            self.blocked_seen_non_speaking = bool(
                block_old_pcm and self.agent_state != "speaking"
            )

            if playback_started:
                cancel = getattr(
                    self.device_session,
                    "cancel_audio_down_stream",
                    None,
                )
                if cancel is None:
                    cancel = getattr(
                        self.device_session,
                        "send_audio_down_cancel",
                        None,
                    )
                if callable(cancel):
                    await cancel(generation=generation)
                else:
                    # Compatibility with a service/firmware rolling upgrade:
                    # END still stops playback, but lacks CANCEL semantics.
                    await self.device_session.send_audio_down_end(
                        generation=generation,
                    )
            logger.info(
                "[rtc] remote playback cancelled device_id=%s reason=%s "
                "generation=%s blocked=%s",
                self.rtc_session.device_id,
                reason,
                generation,
                self.remote_playback_blocked,
            )

    async def reset_remote_playback(self, *, reason: str) -> None:
        """Stop playback and clear interruption state on RTC disconnect."""

        await self.cancel_remote_playback(
            reason=reason,
            block_old_pcm=False,
        )
        self.agent_state = ""
        self.user_speech_active = False
        self.remote_playback_blocked = False
        self.blocked_seen_non_speaking = False

    async def close(self, *, reason: str = "rtc_binding_closed") -> None:
        self.expression_runtime = None
        if self.remote_end_task is not None:
            self.remote_end_task.cancel()
            await asyncio.gather(self.remote_end_task, return_exceptions=True)
            self.remote_end_task = None
        if self.remote_stream_open:
            try:
                await self._finish_remote_stream()
            except Exception:
                logger.debug(
                    "[rtc] failed to close device playback stream device_id=%s",
                    self.rtc_session.device_id,
                    exc_info=True,
                )
        if self.publish_task is not None:
            self.publish_task.cancel()
            await asyncio.gather(self.publish_task, return_exceptions=True)
            self.publish_task = None
        self.agent_state = ""
        self.user_speech_active = False
        self.remote_playback_blocked = False
        self.blocked_seen_non_speaking = False
        await self.rtc_session.close()


def install_rtc_gateway(gateway: DeskbotRtcGateway | None) -> None:
    global _gateway, _gateway_decision_final
    _gateway = gateway
    # 安装真实网关即视为决策已定；安装 None 是启动前/关闭时的复位，
    # 决策标志一并复位，等待中的 bind 恢复等待新一轮决策。
    _gateway_decision_final = gateway is not None


def mark_rtc_gateway_unavailable() -> None:
    """声明本次运行不会再安装 RTC 网关（配置关闭或启动失败）。

    释放 bind_usb_device 中等待网关的任务：没有这个信号，RTC 关闭的
    部署里每台设备的 bind 任务会在会话存活期间空等（并使直接 await
    bind_usb_device 的调用方永不返回）。
    """

    global _gateway_decision_final
    _gateway_decision_final = True


async def bind_usb_expression_runtime(
    device_id: str,
    device_session: object,
) -> RtcExpressionRuntime:
    """Attach the sole display arbiter as soon as the USB session is ready."""

    dev = str(device_id or "").strip()
    if not dev:
        raise ValueError("device_id is required for expression runtime")
    current = get_expression_runtime(dev)
    current_session = _expression_sessions.get(dev)
    if current is not None and current_session is device_session:
        return current
    if current is not None:
        unregister_expression_runtime(current)
        await current.close(restore_idle=False)
    runtime = RtcExpressionRuntime(dev, device_session)
    register_expression_runtime(runtime)
    _expression_sessions[dev] = device_session
    try:
        # USB attachment has one deterministic display initialization. A
        # separate wake scene followed by idle produced two unrelated faces
        # before the first user action and made Web/device parity impossible
        # to reason about.
        await runtime.transition(
            "idle",
            force=True,
            reason="usb_session_ready",
        )
    except Exception:
        logger.warning(
            "[expression] boot idle delivery failed device_id=%s",
            dev,
            exc_info=True,
        )
    return runtime


async def unbind_usb_expression_runtime(
    device_id: str,
    device_session: object,
) -> None:
    dev = str(device_id or "").strip()
    if _expression_sessions.get(dev) is not device_session:
        return
    _expression_sessions.pop(dev, None)
    runtime = get_expression_runtime(dev)
    if runtime is None or getattr(runtime, "device_session", device_session) is not device_session:
        return
    unregister_expression_runtime(runtime)
    # USB is already closing. Never enqueue an idle PB onto a dead route.
    await runtime.close(restore_idle=False)


async def bind_usb_device(
    device_id: str,
    device_session: object,
    *,
    aec: bool = False,
    ns: bool = False,
    vad: bool = False,
    full_duplex: bool = False,
    gateway_capable: bool = True,
    ) -> None:
    expression_runtime = await bind_usb_expression_runtime(device_id, device_session)
    gateway = _gateway
    if gateway is None:
        # 启动并行化后，设备 hello 可能早于后台 rtc-startup 任务安装网关。
        # 此时不能直接放弃（否则该设备语音在拔插前永远不会绑定）；在 USB
        # 会话存活期间等待网关出现。RTC 被配置关闭时网关始终为 None，等待
        # 随会话结束（unbind 会取消本任务）自然退出。
        logger.info(
            "[rtc] gateway not installed yet; bind waiting device_id=%s",
            device_id,
        )
        while (
            _gateway is None
            and not _gateway_decision_final
            and not bool(getattr(device_session, "is_closed", False))
        ):
            await asyncio.sleep(1.0)
        gateway = _gateway
        if gateway is None:
            return
    if not gateway.settings.enabled or not gateway_capable:
        return
    old = _bindings.pop(device_id, None)
    if old is not None:
        await old.close(reason="rtc_rebind")
    await gateway.close_device(device_id)

    attempt = 0
    while not bool(getattr(device_session, "is_closed", False)):
        attempt += 1
        binding: _UsbRtcBinding | None = None
        try:

            binding_box: dict[str, _UsbRtcBinding] = {}

            async def _playback(
                pcm: bytes,
                sample_rate: int,
                channels: int,
            ) -> None:
                active = binding_box.get("binding")
                if active is not None:
                    await active.play_remote(pcm, sample_rate, channels)

            async def _playback_control(
                event: str,
                details: dict[str, object],
            ) -> None:
                active = binding_box.get("binding")
                if active is None:
                    return
                if event == "barge_in":
                    active.user_speech_active = True
                    await active.cancel_remote_playback(reason="barge_in")
                    if active.expression_runtime is not None:
                        preempt = getattr(
                            active.expression_runtime,
                            "preempt_lease",
                            None,
                        )
                        if callable(preempt):
                            await preempt(reason="barge_in")
                        await active.expression_runtime.transition(
                            "listening",
                            reason="barge_in",
                        )
                elif event == "user_speech_start":
                    active.user_speech_active = True
                    if active.expression_runtime is not None:
                        preempt = getattr(
                            active.expression_runtime,
                            "preempt_lease",
                            None,
                        )
                        if callable(preempt):
                            await preempt(reason="user_speech_start")
                        await active.expression_runtime.transition(
                            "listening",
                            reason="user_speech_start",
                        )
                elif event == "user_speech_confirmed_end":
                    active.user_speech_active = False
                    if active.expression_runtime is not None:
                        await active.expression_runtime.transition(
                            "thinking",
                            reason="user_speech_confirmed_end",
                        )
                elif event == "agent_state":
                    state = str(details.get("state") or "")
                    if active.note_agent_state(state):
                        # LiveKit uses ``listening`` both while it is ready and
                        # while real speech is arriving.  The ready state is
                        # not a visible user event: at boot it used to replace
                        # idle immediately, and after every reply it left the
                        # robot permanently on the listening face.  Reliable
                        # user_speech_start/barge_in packets own visible
                        # listening; a steady ready state resolves to idle.
                        if state in {"thinking", "speaking", "idle"}:
                            active.user_speech_active = False
                        visible_state = state
                        visible_reason = "agent_state"
                        if state == "initializing":
                            visible_state = ""
                        elif state == "listening":
                            if active.user_speech_active:
                                visible_state = ""
                            else:
                                visible_state = "idle"
                                visible_reason = "agent_ready"
                        if visible_state and active.expression_runtime is not None:
                            await active.expression_runtime.transition(
                                visible_state,
                                reason=visible_reason,
                            )
                elif event == "connection_closed":
                    await active.reset_remote_playback(reason="rtc_disconnect")
                    if active.expression_runtime is not None:
                        # RTC transport recovery must not revoke a persistent
                        # Web/Agent display lease.  Updating desired state is
                        # enough: it draws idle when RTC owns the screen and is
                        # deferred when an explicit expression owns it.
                        await active.expression_runtime.transition(
                            "idle",
                            reason="rtc_disconnect",
                        )

            rtc_session = await gateway.get_or_create(
                device_id,
                playback_sink=_playback,
                playback_control_sink=_playback_control,
                capabilities=RtcDeviceCapabilities(
                    aec=aec,
                    ns=ns,
                    vad=vad,
                    full_duplex=full_duplex,
                ),
            )
            binding = _UsbRtcBinding(
                device_session,
                rtc_session,
                expression_runtime=expression_runtime,
            )
            binding_box["binding"] = binding
            _bindings[device_id] = binding

            import json

            from deskbot_server.pb.mic_signal import build_rtc_mode_signal_pb

            await device_session.send(
                json.dumps(
                    build_rtc_mode_signal_pb(mode=rtc_session.effective_call_mode),
                    separators=(",", ":"),
                )
            )
            await binding.start()
            logger.info(
                "[rtc] USB binding ready device_id=%s attempt=%d", device_id, attempt
            )
            return
        except asyncio.CancelledError:
            if binding is not None and _bindings.get(device_id) is binding:
                _bindings.pop(device_id, None)
            try:
                if binding is not None:
                    await binding.close(reason="rtc_bind_cancelled")
                await gateway.close_device(device_id)
            except Exception:
                logger.debug(
                    "[rtc] cancellation cleanup failed device_id=%s",
                    device_id,
                    exc_info=True,
                )
            raise
        except Exception:
            if binding is not None and _bindings.get(device_id) is binding:
                _bindings.pop(device_id, None)
            try:
                if binding is not None:
                    await binding.close(reason="rtc_bind_failed")
                # ``binding.close()`` marks the gateway session closed but the
                # gateway registry may still cache it.  A cached closed session
                # makes every ``connect()`` retry fail immediately, so the
                # gateway entry must always be evicted alongside the binding —
                # exactly like the CancelledError branch above.
                await gateway.close_device(device_id)
            except Exception:
                logger.debug(
                    "[rtc] failed binding cleanup device_id=%s",
                    device_id,
                    exc_info=True,
                )
            retry_delay = min(
                _BIND_RETRY_MAX_DELAY_SEC,
                float(2 ** min(attempt, 4)),
            )
            logger.warning(
                "[rtc] USB binding failed; retrying while device is online "
                "device_id=%s attempt=%d delay_s=%.1f",
                device_id,
                attempt,
                retry_delay,
                exc_info=True,
            )
            await asyncio.sleep(retry_delay)


def publish_usb_pcm(device_id: str | None, pcm: bytes) -> None:
    binding = _bindings.get(str(device_id or "").strip())
    if binding is not None:
        binding.feed(pcm)


def notify_usb_vad(device_id: str | None, *, active: bool) -> None:
    """Attach ESP-SR VAD state to subsequent LiveKit frames."""

    dev = str(device_id or "").strip()
    binding = _bindings.get(dev)
    if binding is not None:
        binding.note_vad(active=active)


def rtc_device_active(device_id: str | None) -> bool:
    """Return whether a remote RTC agent is actually attached to the device.

    A locally connected LiveKit room is not sufficient: dispatch can fail or
    lag after the token request succeeds, leaving no agent to consume audio.
    The remote agent publishes its output audio track when its job attaches;
    only then does the device voice link count as ready.  RTC is the sole
    device voice pipeline, so callers (VAD handling, health snapshot) use
    this to decide whether to surface "voice not ready" feedback instead of
    expecting any fallback transcription path.
    """

    binding = _bindings.get(str(device_id or "").strip())
    if binding is None or not binding.rtc_session.connected:
        return False
    return bool(
        binding.rtc_session.remote_agent_attached
        and binding.rtc_session.remote_tasks
    )


def rtc_health_snapshot() -> dict[str, object]:
    """Expose real room/participant state without treating tasks as health."""

    devices: dict[str, object] = {}
    for device_id, binding in tuple(_bindings.items()):
        session = binding.rtc_session
        devices[device_id] = {
            "room_connected": bool(session.connected),
            "agent_attached": bool(session.remote_agent_attached),
            "remote_audio_tasks": sum(
                1 for task in session.remote_tasks if not task.done()
            ),
            "connection_generation": int(session.connection_generation),
            "active": rtc_device_active(device_id),
        }
    return {"devices": devices}


async def unbind_usb_device(device_id: str, device_session: object) -> None:
    binding = _bindings.get(device_id)
    if binding is not None and binding.device_session is device_session:
        _bindings.pop(device_id, None)
        await binding.close(reason="usb_unbind")
    await unbind_usb_expression_runtime(device_id, device_session)


async def shutdown_rtc_runtime() -> None:
    binding_items = list(_bindings.items())
    _bindings.clear()
    await asyncio.gather(
        *(
            binding.close(reason="runtime_shutdown")
            for _device_id, binding in binding_items
        ),
        return_exceptions=True,
    )
    expression_items = list(_expression_sessions.items())
    _expression_sessions.clear()
    expression_runtimes = []
    for device_id, session in expression_items:
        runtime = get_expression_runtime(device_id)
        if runtime is not None and runtime.device_session is session:
            unregister_expression_runtime(runtime)
            expression_runtimes.append(runtime)
    if expression_runtimes:
        await asyncio.gather(
            *(runtime.close(restore_idle=False) for runtime in expression_runtimes),
            return_exceptions=True,
        )
    if _gateway is not None:
        await _gateway.close()
