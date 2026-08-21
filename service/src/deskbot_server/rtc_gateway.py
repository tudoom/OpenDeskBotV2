"""Per-device LiveKit RTC audio gateway for USB Deskbot devices."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

from deskbot_server.core.settings import RtcSettings
from deskbot_server.rtc_barge_in import (
    BARGE_IN_TOPIC,
    SPEECH_ACTIVITY_TOPIC,
    USER_SPEECH_CONFIRMED_END_PAYLOAD,
    USER_SPEECH_START_PAYLOAD,
)

logger = logging.getLogger("deskbot-server")

PcmSink = Callable[[bytes, int, int], Awaitable[None]]
PlaybackControlSink = Callable[
    [str, dict[str, object]],
    Awaitable[None],
]

_AGENT_STATE_ATTRIBUTE = "lk.agent.state"
_AGENT_STATES = {"initializing", "idle", "listening", "thinking", "speaking"}
_RECOVERY_MIN_COOLDOWN_SECONDS = 2.0
_RECOVERY_MAX_BACKOFF_SECONDS = 15.0


def _ws_url(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("https://"):
        return "wss://" + value[8:]
    if value.startswith("http://"):
        return "ws://" + value[7:]
    return value


def _is_confirmed_barge_in_packet(packet, rtc) -> bool:
    """Accept only the reliable control emitted by the dispatched Agent."""

    participant = getattr(packet, "participant", None)
    return bool(
        getattr(packet, "topic", None) == BARGE_IN_TOPIC
        and getattr(packet, "data", None) == b"cancel"
        and getattr(packet, "kind", None)
        == rtc.DataPacketKind.KIND_RELIABLE
        and participant is not None
        and getattr(participant, "kind", None)
        == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    )


def _confirmed_speech_activity(packet, rtc) -> str | None:
    """Decode only fixed, reliable speech controls from the room Agent."""

    participant = getattr(packet, "participant", None)
    if not (
        getattr(packet, "topic", None) == SPEECH_ACTIVITY_TOPIC
        and getattr(packet, "kind", None)
        == rtc.DataPacketKind.KIND_RELIABLE
        and participant is not None
        and getattr(participant, "kind", None)
        == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    ):
        return None
    payload = getattr(packet, "data", None)
    if payload == USER_SPEECH_START_PAYLOAD:
        return "user_speech_start"
    if payload == USER_SPEECH_CONFIRMED_END_PAYLOAD:
        return "user_speech_confirmed_end"
    return None


@dataclass(slots=True)
class RtcDeviceCapabilities:
    aec: bool = False
    ns: bool = False
    vad: bool = False
    full_duplex: bool = False


class RtcDeviceSession:
    """One persistent LiveKit room and its USB audio tracks."""

    def __init__(
        self,
        device_id: str,
        settings: RtcSettings,
        *,
        playback_sink: PcmSink,
        playback_control_sink: PlaybackControlSink | None = None,
        capabilities: RtcDeviceCapabilities,
        token_endpoint: str | None = None,
    ) -> None:
        self.device_id = device_id
        self.settings = settings
        self.playback_sink = playback_sink
        self.playback_control_sink = playback_control_sink
        self.capabilities = capabilities
        self.token_endpoint = str(
            token_endpoint or settings.token_endpoint
        ).strip()
        self.room = None
        self.audio_source = None
        self.audio_track = None
        self.remote_tasks: set[asyncio.Task] = set()
        self.control_tasks: set[asyncio.Task] = set()
        self.remote_agent_attached = False
        self.remote_agent_state = ""
        self.connected = False
        self.connection_generation = 0
        self.connected_mono = 0.0
        self.last_remote_track_mono = 0.0
        self._lock = asyncio.Lock()
        self._recovery_task: asyncio.Task | None = None
        self._last_recovery_started_mono = 0.0
        self._recovery_attempt = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        """A closed session can never reconnect; callers must replace it."""

        return self._closed

    @property
    def effective_call_mode(self) -> str:
        requested = self.settings.call_mode
        if requested == "esp32_aec" and not (
            self.capabilities.aec
            and self.capabilities.ns
            and self.capabilities.vad
            and self.capabilities.full_duplex
        ):
            return "stable"
        if requested == "interruptible" and not self.capabilities.full_duplex:
            return "stable"
        return requested

    async def connect(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("RTC device session is closed")
            if self.connected and self.room is not None and self.audio_source is not None:
                return
            await self._close_connection_locked()
            from livekit import rtc

            room_name = f"deskbot-{self.device_id}-{uuid.uuid4().hex[:8]}"
            identity = f"deskbot-usb-{self.device_id}"
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as client:
                async with client.post(
                    self.token_endpoint,
                    json={
                        "room_name": room_name,
                        "user_identity": identity,
                        "voice_agent": self.settings.agent_name,
                    },
                ) as response:
                    response.raise_for_status()
                    token_data = await response.json()

            room = rtc.Room()

            def _is_agent(participant) -> bool:
                return bool(
                    participant is not None
                    and getattr(participant, "kind", None)
                    == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                )

            def _agent_state(participant) -> str:
                if not _is_agent(participant):
                    return ""
                attributes = getattr(participant, "attributes", {}) or {}
                state = str(attributes.get(_AGENT_STATE_ATTRIBUTE) or "").strip()
                return state if state in _AGENT_STATES else ""

            @room.on("track_subscribed")
            def _track_subscribed(track, _publication, participant):
                if track.kind != rtc.TrackKind.KIND_AUDIO:
                    return
                task = asyncio.create_task(self._consume_remote(track))
                self.remote_tasks.add(task)
                self.remote_agent_attached = True
                self.last_remote_track_mono = time.monotonic()
                task.add_done_callback(self._remote_task_done)
                state = _agent_state(participant)
                if state and state != self.remote_agent_state:
                    self.remote_agent_state = state
                    self._queue_playback_control(
                        "agent_state",
                        {"state": state},
                        source_room=room,
                    )

            @room.on("track_unsubscribed")
            def _track_unsubscribed(track, _publication, participant):
                if track.kind != rtc.TrackKind.KIND_AUDIO or not _is_agent(participant):
                    return
                self._schedule_recovery(room, "agent_audio_unsubscribed")

            @room.on("participant_disconnected")
            def _participant_disconnected(participant):
                if _is_agent(participant):
                    self._schedule_recovery(room, "agent_participant_disconnected")

            @room.on("participant_attributes_changed")
            def _participant_attributes_changed(changed_attributes, participant):
                if not _is_agent(participant):
                    return
                if _AGENT_STATE_ATTRIBUTE not in changed_attributes:
                    return
                state = str(
                    changed_attributes.get(_AGENT_STATE_ATTRIBUTE) or ""
                ).strip()
                if state not in _AGENT_STATES:
                    return
                if state == self.remote_agent_state:
                    return
                self.remote_agent_state = state
                self._queue_playback_control(
                    "agent_state",
                    {"state": state},
                    source_room=room,
                )

            @room.on("data_received")
            def _data_received(packet):
                # Only the dispatched LiveKit Agent can authorize a device
                # playback cancellation. Payload and topic are deliberately
                # fixed so arbitrary room data can never reach USB control.
                if _is_confirmed_barge_in_packet(packet, rtc):
                    self._queue_playback_control(
                        "barge_in",
                        {},
                        source_room=room,
                    )
                    return
                activity = _confirmed_speech_activity(packet, rtc)
                if activity is not None:
                    self._queue_playback_control(
                        activity,
                        {},
                        source_room=room,
                    )

            @room.on("disconnected")
            def _disconnected(*_args):
                if self.room is room:
                    self.connected = False
                    self.remote_agent_attached = False
                    self.remote_agent_state = ""
                    # AudioStream tasks do not always finish when LiveKit's
                    # reconnect state machine dies.  Never let those stale
                    # tasks make an RTC-only device look healthy.
                    for task in tuple(self.remote_tasks):
                        task.cancel()
                    self.remote_tasks.clear()
                    self._queue_playback_control(
                        "connection_closed",
                        {},
                        source_room=room,
                    )
                    self._schedule_recovery(room, "room_disconnected")

            try:
                await room.connect(
                    token_data.get("serverUrl") or _ws_url(self.settings.livekit_url),
                    token_data["token"],
                )
                source = rtc.AudioSource(16000, 1)
                track = rtc.LocalAudioTrack.create_audio_track(
                    "deskbot-usb-mic",
                    source,
                )
                await room.local_participant.publish_track(
                    track,
                    rtc.TrackPublishOptions(
                        source=rtc.TrackSource.SOURCE_MICROPHONE
                    ),
                )
            except BaseException:
                try:
                    await room.disconnect()
                except Exception:
                    pass
                raise
            self.room = room
            self.audio_source = source
            self.audio_track = track
            generation = (self.connection_generation + 1) & 0xFFFFFFFF
            self.connection_generation = generation or 1
            self.connected = True
            self.connected_mono = time.monotonic()
            logger.info(
                "[rtc] connected device_id=%s room=%s mode=%s generation=%d",
                self.device_id,
                room_name,
                self.effective_call_mode,
                self.connection_generation,
            )

    def _queue_playback_control(
        self,
        event: str,
        details: dict[str, object],
        *,
        source_room=None,
    ) -> None:
        if self.playback_control_sink is None:
            return

        async def _dispatch() -> None:
            if source_room is not None and self.room is not source_room:
                return
            assert self.playback_control_sink is not None
            await self.playback_control_sink(event, dict(details))

        task = asyncio.create_task(
            _dispatch(),
            name=f"deskbot-rtc-control:{self.device_id}:{event}",
        )
        self.control_tasks.add(task)
        task.add_done_callback(self._control_task_done)

    def _control_task_done(self, task: asyncio.Task) -> None:
        self.control_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "[rtc] playback control failed device_id=%s error=%s",
                self.device_id,
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _schedule_recovery(self, source_room, reason: str) -> None:
        """Single-flight room recovery with cooldown and bounded backoff."""

        if self._closed or self.room is not source_room:
            return
        self.remote_agent_attached = False
        self.remote_agent_state = ""
        current = self._recovery_task
        if current is not None and not current.done():
            return

        async def _recover() -> None:
            now = time.monotonic()
            cooldown = max(
                0.0,
                _RECOVERY_MIN_COOLDOWN_SECONDS
                - (now - self._last_recovery_started_mono),
            )
            if cooldown:
                await asyncio.sleep(cooldown)
            self._last_recovery_started_mono = time.monotonic()
            first_attempt = True
            while not self._closed:
                if first_attempt:
                    async with self._lock:
                        if self._closed or self.room is not source_room:
                            return
                        logger.warning(
                            "[rtc] remote Agent unhealthy; rebuilding room "
                            "device_id=%s reason=%s",
                            self.device_id,
                            reason,
                        )
                        await self._close_connection_locked()
                    first_attempt = False
                try:
                    await self.connect()
                    self._recovery_attempt = 0
                    logger.info(
                        "[rtc] room recovery complete device_id=%s reason=%s",
                        self.device_id,
                        reason,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._recovery_attempt += 1
                    delay = min(
                        _RECOVERY_MAX_BACKOFF_SECONDS,
                        float(2 ** min(self._recovery_attempt - 1, 4)),
                    )
                    logger.warning(
                        "[rtc] room recovery failed; retrying "
                        "device_id=%s reason=%s attempt=%d delay_s=%.1f",
                        self.device_id,
                        reason,
                        self._recovery_attempt,
                        delay,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)

        task = asyncio.create_task(
            _recover(),
            name=f"deskbot-rtc-recover:{self.device_id}",
        )
        self._recovery_task = task

    async def feed_pcm16(
        self,
        pcm: bytes,
        *,
        voice_active: bool = False,
        captured_mono: float | None = None,
        max_age_seconds: float = 0.75,
    ) -> bool:
        if not pcm:
            return False
        from livekit import rtc

        samples = len(pcm) // 2
        if samples <= 0:
            return False
        last_error: Exception | None = None
        for attempt in range(2):
            if (
                captured_mono is not None
                and time.monotonic() - captured_mono > max_age_seconds
            ):
                # Discarding a frame of active speech truncates the user's
                # sentence; that must never disappear as a silent info line.
                log = logger.warning if voice_active else logger.info
                log(
                    "[rtc] discard stale microphone frame device_id=%s "
                    "age_ms=%.1f voice_active=%s",
                    self.device_id,
                    (time.monotonic() - captured_mono) * 1000.0,
                    voice_active,
                )
                return False
            # A room can remain locally connected after the dispatched Agent
            # participant/track vanished.  Give initial dispatch time to
            # attach, then rebuild the whole room instead of publishing into
            # a permanently unattended room.
            if (
                self.connected
                and not self.remote_agent_attached
                and self.connected_mono > 0
                and time.monotonic() - self.connected_mono >= 15.0
            ):
                logger.warning(
                    "[rtc] remote Agent missing; scheduling room recovery "
                    "device_id=%s",
                    self.device_id,
                )
                room = self.room
                if room is not None:
                    self._schedule_recovery(room, "agent_attach_timeout")
                return False
            if not self.connected or self.audio_source is None:
                recovery = self._recovery_task
                if recovery is not None and not recovery.done():
                    return False
                await self.connect()
            source = self.audio_source
            if source is None:
                continue
            frame = rtc.AudioFrame(
                data=pcm,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples,
            )
            try:
                await source.capture_frame(frame)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[rtc] microphone publish failed; scheduling recovery "
                    "device_id=%s attempt=%d error=%s",
                    self.device_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                room = self.room
                if room is not None:
                    self._schedule_recovery(
                        room,
                        "microphone_publish_failed",
                    )
                    return False
                await self._reset_connection()
        if last_error is not None:
            raise last_error
        return False

    async def _consume_remote(self, track) -> None:
        from livekit import rtc

        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=16000,
            num_channels=1,
        )
        try:
            async for event in stream:
                await self.playback_sink(bytes(event.frame.data), 16000, 1)
        except asyncio.CancelledError:
            raise
        except ConnectionError as exc:
            # The USB session can disappear while a remote LiveKit track still
            # has buffered audio.  That is an ordinary per-device disconnect,
            # not an RTC task failure.
            logger.info(
                "[rtc] remote playback stopped after device disconnect "
                "device_id=%s error=%s",
                self.device_id,
                type(exc).__name__,
            )
        except Exception:
            logger.exception(
                "[rtc] remote audio consumer stopped device_id=%s",
                self.device_id,
            )

    def _remote_task_done(self, task: asyncio.Task) -> None:
        self.remote_tasks.discard(task)
        if not self.remote_tasks:
            self.remote_agent_attached = False
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # _consume_remote handles ordinary stream/sink failures itself.
            # Keep this final guard so an unexpected BaseException is still
            # retrieved instead of becoming "Task exception was never
            # retrieved" and taking down later reconnect diagnostics.
            logger.error(
                "[rtc] remote audio task failed device_id=%s error=%s",
                self.device_id,
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )
        room = self.room
        if self.connected and room is not None and not self.remote_tasks:
            self._schedule_recovery(room, "agent_audio_stream_ended")

    async def _reset_connection(self) -> None:
        async with self._lock:
            await self._close_connection_locked()

    async def _close_connection_locked(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in tuple(self.remote_tasks)
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.remote_tasks = {
            task
            for task in self.remote_tasks
            if task is current and not task.done()
        }
        room = self.room
        self.room = None
        self.audio_source = None
        self.audio_track = None
        self.connected = False
        self.connected_mono = 0.0
        self.remote_agent_attached = False
        self.remote_agent_state = ""
        if room is not None and self.playback_control_sink is not None:
            try:
                await self.playback_control_sink("connection_closed", {})
            except Exception:
                logger.debug(
                    "[rtc] playback close control failed device_id=%s",
                    self.device_id,
                    exc_info=True,
                )
        if room is not None:
            try:
                await room.disconnect()
            except Exception:
                logger.debug(
                    "[rtc] room disconnect failed device_id=%s",
                    self.device_id,
                    exc_info=True,
                )

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._close_connection_locked()
            self.remote_tasks.clear()
            tasks = [task for task in self.control_tasks if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.control_tasks.clear()
        recovery = self._recovery_task
        if (
            recovery is not None
            and recovery is not asyncio.current_task()
            and not recovery.done()
        ):
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        self._recovery_task = None


class DeskbotRtcGateway:
    """Owns isolated RTC sessions so reconnecting one device cannot cancel another."""

    def __init__(
        self,
        settings: RtcSettings,
        *,
        token_endpoint: str | None = None,
    ) -> None:
        self.settings = settings
        self.token_endpoint = str(
            token_endpoint or settings.token_endpoint
        ).strip()
        self._sessions: dict[str, RtcDeviceSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        device_id: str,
        *,
        playback_sink: PcmSink,
        playback_control_sink: PlaybackControlSink | None = None,
        capabilities: RtcDeviceCapabilities | None = None,
    ) -> RtcDeviceSession:
        async with self._lock:
            session = self._sessions.get(device_id)
            if session is not None and session.closed:
                # A failed-bind cleanup can close a session while the registry
                # still caches it.  ``connect()`` on a closed session raises
                # forever, which would turn every later bind retry into the
                # same failure; drop the dead entry and build a fresh one.
                logger.info(
                    "[rtc] replacing closed cached session device_id=%s",
                    device_id,
                )
                self._sessions.pop(device_id, None)
                session = None
            if session is None:
                session = RtcDeviceSession(
                    device_id,
                    self.settings,
                    playback_sink=playback_sink,
                    playback_control_sink=playback_control_sink,
                    capabilities=capabilities or RtcDeviceCapabilities(),
                    token_endpoint=self.token_endpoint,
                )
                self._sessions[device_id] = session
            return session

    async def close_device(self, device_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(device_id, None)
        if session is not None:
            await session.close()

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
