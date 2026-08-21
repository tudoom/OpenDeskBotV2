"""One generation of a Deskbot USB CDC connection."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from deskbot_server.infrastructure.serial.protocol import (
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    Channel,
    Frame,
    FrameDecoder,
    FrameFlag,
    SerialProtocolError,
    decode_control_payload,
    encode_control_payload,
    encode_frame,
)

logger = logging.getLogger("deskbot-server")

_DEVICE_ID_RE = re.compile(r"^deskbot_(?!0{12}$)(?!f{12}$)[0-9a-f]{12}$")
# Windows can complete a USB CDC write after handing the bytes to the driver,
# before the ESP32-S3 HWCDC task has drained its bounded receive queue.  The
# HWCDC ISR silently drops the tail of a USB packet when that queue is full, so
# one large WriteFile call is not safe transport backpressure.  Pace the byte
# stream in small writes; the DBOT decoder is incremental and therefore keeps
# the logical frame unchanged.
SERIAL_WRITE_CHUNK_BYTES = 512
SERIAL_WRITE_INTER_CHUNK_SECONDS = 0.003

# Keep every host-to-device PB media *wire frame* below 2 KiB even after the
# 24-byte DBOT header and 4-byte payload CRC.  ``next_bin_len`` still declares
# the logical binary length; the firmware reassembles these transport
# fragments before decoding.
PB_BINARY_FRAGMENT_BYTES = 1536
PB_JSON_FRAGMENT_BYTES = 1536
FRAME_ACK_CAPABILITY = "frame_ack"
PB_JSON_FRAGMENT_CAPABILITY = "pb_json_fragments"
_USB_TELEMETRY_FIELDS = (
    "usb_partial_tx_failures",
    "usb_payload_crc_errors",
    "usb_rx_buffer_bytes",
    "usb_rx_high_water",
    "usb_poll_max_gap_ms",
)
# A valid camera frame may take several heartbeat periods to cross CDC.  Raw
# byte activity can defer the interactive heartbeat only while an incremental
# frame is actually buffered, and never for longer than this corruption guard.
INCOMPLETE_FRAME_STALL_SECONDS = 30.0


def _normalize_opus_uplink_batch(
    payload: bytes,
) -> tuple[bytes, int | None]:
    """Recognize the firmware's repeated ``u16_be length + Opus`` batch.

    The legacy ASR loop expects a frame count for multi-packet batches, while
    its single-frame path expects raw Opus without the two-byte prefix.
    Invalid framing remains a raw packet for backward compatibility.
    """

    if len(payload) < 3:
        return payload, None
    offset = 0
    frames = 0
    while offset + 2 <= len(payload):
        packet_length = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        if packet_length <= 0 or offset + packet_length > len(payload):
            return payload, None
        offset += packet_length
        frames += 1
    if offset != len(payload) or frames == 0:
        return payload, None
    if frames == 1:
        return payload[2:], None
    return payload, frames


class SerialLike(Protocol):
    timeout: float | None
    write_timeout: float | None

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


class SessionClosed(ConnectionError):
    pass


class PBTransmissionCancelled(SessionClosed):
    """A PB bundle was superseded by an out-of-band ``pb_cancel``."""


class SessionHandshakeError(SerialProtocolError):
    pass


@dataclass(frozen=True, slots=True)
class HelloInfo:
    device_id: str
    product: str
    firmware: str
    session_epoch: int
    heartbeat_ms: int
    timeout_ms: int
    max_payload: int
    capabilities: tuple[str, ...]
    reset_reason: str = ""
    hardware_reset_reason: str = ""
    hardware_reset_code: int = 0
    last_panic: bool = False
    last_restart_uptime_ms: int = 0
    recovery_count: int = 0
    boot_count: int = 0
    uptime_ms: int = 0
    usb_partial_tx_failures: int = 0
    usb_payload_crc_errors: int = 0
    usb_rx_buffer_bytes: int = 0
    usb_rx_high_water: int = 0
    usb_poll_max_gap_ms: int = 0
    mic_signal_healthy: bool = False
    servo_ready: bool = False
    servo_backend: str = ""
    servo_x_pin: int = 0
    servo_y_pin: int = 0
    servo_pwm_hz: int = 0
    servo_x_pulse_us: int = 0
    servo_y_pulse_us: int = 0
    servo_write_failures: int = 0


@dataclass(frozen=True, slots=True)
class SessionDiagnostics:
    """A point-in-time USB CDC liveness snapshot.

    Queue sizes are intentionally approximate: they are diagnostic signals,
    not flow-control inputs.  ``last_valid_*`` only advances after framing,
    CRC, and the negotiated session epoch have all been validated.
    """

    last_valid_channel: str | None
    last_valid_sequence: int | None
    last_valid_epoch: int | None
    last_valid_age_seconds: float | None
    stale_epoch_frames: int
    last_interactive_channel: str | None
    last_interactive_sequence: int | None
    last_interactive_age_seconds: float | None
    last_any_rx_age_seconds: float | None
    decoder_buffered_bytes: int
    rx_queue_depth: int
    tx_queue_depth: int
    message_queue_depth: int
    audio_queue_depth: int
    audio_queue_drops: int
    camera_queue_depth: int
    camera_queue_drops: int
    heartbeat_pending_count: int
    heartbeat_pending_age_seconds: float | None
    last_heartbeat_queue_delay_seconds: float | None
    last_heartbeat_write_seconds: float | None
    usb_partial_tx_failures: int
    usb_payload_crc_errors: int
    usb_rx_buffer_bytes: int
    usb_rx_high_water: int
    usb_poll_max_gap_ms: int


ReadyCallback = Callable[
    ["DeviceSession", HelloInfo],
    Awaitable[None] | None,
]
FrameCallback = Callable[
    ["DeviceSession", Frame],
    Awaitable[None] | None,
]
ClosedCallback = Callable[
    ["DeviceSession", BaseException | None],
    Awaitable[None] | None,
]


@dataclass(slots=True)
class _WriteRequest:
    data: bytes
    completed: asyncio.Future[None]
    queued_mono: float
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class UplinkAudioBatch:
    """One atomic real-time microphone batch from the firmware."""

    payload: bytes
    opus_frames: int | None
    sequence: int
    received_mono: float
    sample_rate: int = 16000
    channels: int = 1
    codec: str = "opus"


@dataclass(frozen=True, slots=True)
class UplinkCameraFrame:
    """Latest-wins camera media kept outside the reliable control queue."""

    payload: bytes
    sequence: int
    received_mono: float


async def _invoke(callback, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class DeviceSession:
    """Thread-to-asyncio bridge for one opened serial port.

    Only the reader thread touches ``read``.  Every write, including hello,
    heartbeat, PB, and audio, passes through one asyncio writer task.  A
    manager-assigned ``generation`` distinguishes delayed callbacks from a
    prior open of the same COM port; the device-assigned ``session_epoch``
    rejects delayed frames within the byte stream itself.
    """

    def __init__(
        self,
        port: str,
        serial_port: SerialLike,
        *,
        generation: int,
        loop: asyncio.AbstractEventLoop | None = None,
        on_ready: ReadyCallback | None = None,
        on_frame: FrameCallback | None = None,
        on_closed: ClosedCallback | None = None,
        hello_interval: float = 1.0,
        hello_timeout: float = 6.0,
        frame_ack_timeout: float = 2.0,
        read_size: int = 4096,
        rx_queue_size: int = 256,
        tx_queue_size: int = 256,
        message_queue_size: int = 256,
        audio_queue_size: int = 8,
        camera_queue_size: int = 1,
    ) -> None:
        self.port = str(port)
        self.generation = max(1, int(generation))
        self.remote_address = ("usb_cdc", self.port)
        self._serial = serial_port
        self._loop = loop
        self._on_ready = on_ready
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._hello_interval = max(0.1, float(hello_interval))
        self._hello_timeout = max(self._hello_interval, float(hello_timeout))
        self._frame_ack_timeout = max(0.1, float(frame_ack_timeout))
        self._read_size = max(1, int(read_size))
        self._rx_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(
            maxsize=max(1, int(rx_queue_size))
        )
        self._tx_queue: asyncio.Queue[_WriteRequest | None] = asyncio.Queue(
            maxsize=max(1, int(tx_queue_size))
        )
        self._messages: asyncio.Queue[str | bytes | None] = asyncio.Queue(
            maxsize=max(1, int(message_queue_size))
        )
        # Real-time media must never share the reliable application queue.
        # Eight 100 ms microphone batches bound latency to < 1 s even when
        # another service task briefly monopolises the event loop. Camera is
        # preview/vision input and therefore strictly latest-wins.
        self._audio_up_queue: asyncio.Queue[UplinkAudioBatch | None] = (
            asyncio.Queue(maxsize=max(1, int(audio_queue_size)))
        )
        self._camera_queue: asyncio.Queue[UplinkCameraFrame | None] = (
            asyncio.Queue(maxsize=max(1, int(camera_queue_size)))
        )
        self._decoder = FrameDecoder()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._stop_reader = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._rx_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._close_lock = asyncio.Lock()
        self._chain_lock = asyncio.Lock()
        # BEGIN/DATA/END for standalone RTC playback share one lock.  A local
        # generation prevents delayed tasks from appending to a newer stream.
        self._audio_down_lock = asyncio.Lock()
        self._audio_down_generation = 0
        self._audio_down_open_generation: int | None = None
        # A logical PB binary may span several awaited serial writes.  JSON or
        # another binary must not enter the PB channel between those fragments.
        # CONTROL_JSON (including heartbeat) intentionally uses a different
        # lock and may still make progress while a large PB binary is sent.
        self._pb_frame_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._closed_callback_called = False
        self._started_mono = 0.0
        self._last_rx_mono = 0.0
        self._last_valid_mono = 0.0
        self._last_valid_channel: Channel | None = None
        self._last_valid_sequence: int | None = None
        self._last_valid_epoch: int | None = None
        self._next_sequence = 1
        self._last_interactive_channel: Channel | None = None
        self._last_interactive_sequence: int | None = None
        self._last_any_rx_mono = 0.0
        self._client_nonce = secrets.randbelow(0xFFFFFFFF) + 1
        self._hello: HelloInfo | None = None
        self._mic_signal_healthy = False
        self._last_error: BaseException | None = None
        self._heartbeat_seconds = 2.0
        self._timeout_seconds = 6.5
        self._remote_max_payload = MAX_PAYLOAD
        self.stale_epoch_frames = 0
        self.rx_queue_overflows = 0
        self.message_queue_overflows = 0
        self.audio_queue_drops = 0
        self.camera_queue_drops = 0
        self._last_ingress_drop_log_mono = 0.0
        self._heartbeat_pending_count = 0
        self._heartbeat_oldest_queued_mono = 0.0
        self._last_heartbeat_queue_delay_seconds: float | None = None
        self._last_heartbeat_write_seconds: float | None = None
        self._usb_partial_tx_failures = 0
        self._usb_payload_crc_errors = 0
        self._usb_rx_buffer_bytes = 0
        self._usb_rx_high_water = 0
        self._usb_poll_max_gap_ms = 0
        self._pending_pb_binary_lengths: list[int] = []
        self._pending_frame_acks: dict[
            int,
            tuple[Channel, asyncio.Future[None]],
        ] = {}
        self._pb_cancel_generation = 0

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._closing

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def hello_info(self) -> HelloInfo | None:
        return self._hello

    @property
    def device_id(self) -> str | None:
        return self._hello.device_id if self._hello is not None else None

    @property
    def session_epoch(self) -> int:
        return self._hello.session_epoch if self._hello is not None else 0

    @property
    def mic_signal_healthy(self) -> bool:
        return self._mic_signal_healthy

    @property
    def client_nonce(self) -> int:
        return self._client_nonce

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def diagnostics(self, *, now: float | None = None) -> SessionDiagnostics:
        """Return enough state to distinguish silence from local congestion."""

        observed_mono = time.monotonic() if now is None else float(now)
        last_valid_age = (
            max(0.0, observed_mono - self._last_valid_mono)
            if self._last_valid_mono > 0.0
            else None
        )
        last_interactive_age = (
            max(0.0, observed_mono - self._last_rx_mono)
            if self._last_rx_mono > 0.0
            else None
        )
        last_any_rx_age = (
            max(0.0, observed_mono - self._last_any_rx_mono)
            if self._last_any_rx_mono > 0.0
            else None
        )
        heartbeat_pending_age = (
            max(0.0, observed_mono - self._heartbeat_oldest_queued_mono)
            if (
                self._heartbeat_pending_count > 0
                and self._heartbeat_oldest_queued_mono > 0.0
            )
            else None
        )
        return SessionDiagnostics(
            last_valid_channel=(
                self._last_valid_channel.name
                if self._last_valid_channel is not None
                else None
            ),
            last_valid_sequence=self._last_valid_sequence,
            last_valid_epoch=self._last_valid_epoch,
            last_valid_age_seconds=last_valid_age,
            stale_epoch_frames=self.stale_epoch_frames,
            last_interactive_channel=(
                self._last_interactive_channel.name
                if self._last_interactive_channel is not None
                else None
            ),
            last_interactive_sequence=self._last_interactive_sequence,
            last_interactive_age_seconds=last_interactive_age,
            last_any_rx_age_seconds=last_any_rx_age,
            decoder_buffered_bytes=self._decoder.buffered_bytes,
            rx_queue_depth=self._rx_queue.qsize(),
            tx_queue_depth=self._tx_queue.qsize(),
            message_queue_depth=self._messages.qsize(),
            audio_queue_depth=self._audio_up_queue.qsize(),
            audio_queue_drops=self.audio_queue_drops,
            camera_queue_depth=self._camera_queue.qsize(),
            camera_queue_drops=self.camera_queue_drops,
            heartbeat_pending_count=self._heartbeat_pending_count,
            heartbeat_pending_age_seconds=heartbeat_pending_age,
            last_heartbeat_queue_delay_seconds=(
                self._last_heartbeat_queue_delay_seconds
            ),
            last_heartbeat_write_seconds=self._last_heartbeat_write_seconds,
            usb_partial_tx_failures=self._usb_partial_tx_failures,
            usb_payload_crc_errors=self._usb_payload_crc_errors,
            usb_rx_buffer_bytes=self._usb_rx_buffer_bytes,
            usb_rx_high_water=self._usb_rx_high_water,
            usb_poll_max_gap_ms=self._usb_poll_max_gap_ms,
        )

    def _diagnostic_summary(self, *, now: float) -> str:
        snapshot = self.diagnostics(now=now)

        def _milliseconds(value: float | None) -> str:
            return "none" if value is None else str(round(value * 1000.0, 1))

        return (
            f"last_channel={snapshot.last_valid_channel or 'none'} "
            f"last_sequence={snapshot.last_valid_sequence} "
            f"last_epoch={snapshot.last_valid_epoch} "
            f"last_valid_age_ms={_milliseconds(snapshot.last_valid_age_seconds)} "
            f"stale_frames={snapshot.stale_epoch_frames} "
            f"rx_queue={snapshot.rx_queue_depth} "
            f"tx_queue={snapshot.tx_queue_depth} "
            f"message_queue={snapshot.message_queue_depth} "
            f"audio_queue={snapshot.audio_queue_depth} "
            f"audio_drops={snapshot.audio_queue_drops} "
            f"camera_queue={snapshot.camera_queue_depth} "
            f"camera_drops={snapshot.camera_queue_drops} "
            f"heartbeat_pending={snapshot.heartbeat_pending_count} "
            "heartbeat_pending_age_ms="
            f"{_milliseconds(snapshot.heartbeat_pending_age_seconds)} "
            "last_interactive_channel="
            f"{snapshot.last_interactive_channel or 'none'} "
            "last_interactive_sequence="
            f"{snapshot.last_interactive_sequence} "
            "last_interactive_age_ms="
            f"{_milliseconds(snapshot.last_interactive_age_seconds)} "
            "heartbeat_queue_delay_ms="
            f"{_milliseconds(snapshot.last_heartbeat_queue_delay_seconds)} "
            "heartbeat_write_ms="
            f"{_milliseconds(snapshot.last_heartbeat_write_seconds)} "
            f"last_any_rx_age_ms={_milliseconds(snapshot.last_any_rx_age_seconds)} "
            f"decoder_buffered_bytes={snapshot.decoder_buffered_bytes} "
            f"usb_partial_tx_failures={snapshot.usb_partial_tx_failures} "
            f"usb_payload_crc_errors={snapshot.usb_payload_crc_errors} "
            f"usb_rx_buffer_bytes={snapshot.usb_rx_buffer_bytes} "
            f"usb_rx_high_water={snapshot.usb_rx_high_water} "
            f"usb_poll_max_gap_ms={snapshot.usb_poll_max_gap_ms}"
        )

    def _record_valid_rx(self, frame: Frame) -> None:
        observed_mono = time.monotonic()
        self._last_any_rx_mono = observed_mono
        self._last_valid_mono = observed_mono
        self._last_valid_channel = frame.channel
        self._last_valid_sequence = frame.sequence
        self._last_valid_epoch = frame.session_epoch
        if frame.channel in {
            Channel.CONTROL_JSON,
            Channel.PB_WIRE,
            Channel.AUDIO_UP_OPUS,
        }:
            self._last_rx_mono = observed_mono
            self._last_interactive_channel = frame.channel
            self._last_interactive_sequence = frame.sequence

    def _sequence(self) -> int:
        value = self._next_sequence
        self._next_sequence = (value + 1) & 0xFFFFFFFF
        if self._next_sequence == 0:
            self._next_sequence = 1
        return value

    async def start(self) -> "DeviceSession":
        if self._started:
            raise RuntimeError("serial session already started")
        if self._closed.is_set():
            raise SessionClosed("serial session is closed")
        self._started = True
        self._loop = self._loop or asyncio.get_running_loop()
        self._started_mono = time.monotonic()
        self._last_rx_mono = self._started_mono
        self._last_any_rx_mono = self._started_mono
        self._writer_task = asyncio.create_task(
            self._writer_loop(),
            name=f"deskbot-usb-writer:{self.port}:{self.generation}",
        )
        self._rx_task = asyncio.create_task(
            self._rx_loop(),
            name=f"deskbot-usb-rx:{self.port}:{self.generation}",
        )
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(),
            name=f"deskbot-usb-heartbeat:{self.port}:{self.generation}",
        )
        self._reader_thread = threading.Thread(
            target=self._reader_main,
            name=f"deskbot-usb-reader:{self.port}:{self.generation}",
            daemon=True,
        )
        self._reader_thread.start()
        return self

    async def wait_ready(self, timeout: float | None = None) -> HelloInfo:
        if self._hello is not None and self.is_ready:
            return self._hello
        if self._closed.is_set():
            if self._last_error is not None:
                raise self._last_error
            raise SessionClosed(f"{self.port} closed before hello")
        ready_waiter = asyncio.create_task(self._ready.wait())
        closed_waiter = asyncio.create_task(self._closed.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_waiter, closed_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"{self.port} hello timed out")
        finally:
            for waiter in (ready_waiter, closed_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                ready_waiter,
                closed_waiter,
                return_exceptions=True,
            )
        if self._hello is None or self._closing:
            if self._last_error is not None:
                raise self._last_error
            raise SessionClosed(f"{self.port} closed before hello")
        return self._hello

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def __aiter__(self) -> "DeviceSession":
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def receive_audio_up(self) -> UplinkAudioBatch:
        """Receive the next low-latency microphone batch.

        This is deliberately separate from ``__anext__`` so JSON, PB ACKs,
        camera inference and Opus decode can never corrupt each other's
        ordering or backpressure.
        """

        batch = await self._audio_up_queue.get()
        if batch is None:
            raise SessionClosed(f"{self.port} audio ingress closed")
        return batch

    async def receive_camera_jpeg(self) -> UplinkCameraFrame:
        frame = await self._camera_queue.get()
        if frame is None:
            raise SessionClosed(f"{self.port} camera ingress closed")
        return frame

    def _reader_main(self) -> None:
        assert self._loop is not None
        try:
            while not self._stop_reader.is_set():
                data = self._serial.read(self._read_size)
                if not data:
                    continue
                chunk = bytes(data)
                self._loop.call_soon_threadsafe(
                    self._enqueue_rx_from_thread,
                    self.generation,
                    chunk,
                )
        except BaseException as exc:
            if not self._stop_reader.is_set():
                self._loop.call_soon_threadsafe(self._schedule_failure, exc)

    def _enqueue_rx_from_thread(self, generation: int, data: bytes) -> None:
        if self._closing or generation != self.generation:
            return
        # This callback runs on the event loop, so recording raw activity here
        # is race-free.  Waiting until FrameDecoder yields a complete JPEG made
        # a healthy, slowly arriving camera frame look like total USB silence.
        if data:
            self._last_any_rx_mono = time.monotonic()
        try:
            self._rx_queue.put_nowait((generation, data))
        except asyncio.QueueFull:
            self.rx_queue_overflows += 1
            # The serial reader is still healthy; only the event loop fell
            # behind. Drop stale bytes, reset the incremental decoder and
            # retain the newest chunk. FrameDecoder will resynchronise at the
            # next DBOT magic header. Killing the whole USB/RTC session here
            # used to turn one scheduler pause into a reconnect storm.
            dropped_chunks = 0
            while True:
                try:
                    queued = self._rx_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued is not None:
                    dropped_chunks += 1
            self._decoder.reset()
            self._rx_queue.put_nowait((generation, data))
            now = time.monotonic()
            if (
                self.rx_queue_overflows == 1
                or now - self._last_ingress_drop_log_mono >= 2.0
            ):
                logger.warning(
                    "[usb_cdc] receive backlog dropped without disconnect "
                    "port=%s generation=%d events=%d chunks=%d",
                    self.port,
                    self.generation,
                    self.rx_queue_overflows,
                    dropped_chunks,
                )
                self._last_ingress_drop_log_mono = now

    def _schedule_failure(self, exc: BaseException) -> None:
        if self._closing:
            return
        asyncio.create_task(
            self._fail(exc),
            name=f"deskbot-usb-fail:{self.port}:{self.generation}",
        )

    async def _fail(self, exc: BaseException) -> None:
        if self._last_error is None:
            self._last_error = exc
        logger.warning(
            "[usb_cdc] session failed port=%s generation=%d err=%s",
            self.port,
            self.generation,
            exc,
        )
        await self.close(reason=str(exc))

    def _write_all_blocking(self, data: bytes) -> None:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            chunk_end = min(
                offset + SERIAL_WRITE_CHUNK_BYTES,
                len(view),
            )
            while offset < chunk_end:
                remaining = view[offset:chunk_end]
                written = self._serial.write(remaining.tobytes())
                if written is None:
                    written = 0
                written = int(written)
                if written <= 0 or written > len(remaining):
                    raise OSError(
                        f"{self.port} serial write returned {written}"
                    )
                offset += written
            if offset < len(view):
                time.sleep(SERIAL_WRITE_INTER_CHUNK_SECONDS)

    async def _writer_loop(self) -> None:
        try:
            while True:
                request = await self._tx_queue.get()
                if request is None:
                    return
                write_started_mono = time.monotonic()
                if request.kind is not None:
                    self._last_heartbeat_queue_delay_seconds = max(
                        0.0,
                        write_started_mono - request.queued_mono,
                    )
                try:
                    await asyncio.to_thread(
                        self._write_all_blocking,
                        request.data,
                    )
                except BaseException as exc:
                    if not request.completed.done():
                        request.completed.set_exception(exc)
                    raise
                else:
                    if not request.completed.done():
                        request.completed.set_result(None)
                finally:
                    if request.kind is not None:
                        self._last_heartbeat_write_seconds = max(
                            0.0,
                            time.monotonic() - write_started_mono,
                        )
                        self._heartbeat_pending_count = max(
                            0,
                            self._heartbeat_pending_count - 1,
                        )
                        if self._heartbeat_pending_count == 0:
                            self._heartbeat_oldest_queued_mono = 0.0
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)

    async def send_frame(
        self,
        channel: Channel | int,
        payload: bytes | bytearray | memoryview,
        *,
        flags: FrameFlag | int = FrameFlag.NONE,
        epoch: int | None = None,
        allow_before_ready: bool = False,
        require_ack: bool = False,
        _write_kind: str | None = None,
    ) -> int:
        if not self._started or self._closing or self._closed.is_set():
            raise SessionClosed(f"{self.port} is closed")
        if not allow_before_ready and not self.is_ready:
            raise SessionClosed(f"{self.port} handshake is not ready")
        selected_epoch = self.session_epoch if epoch is None else int(epoch)
        if not allow_before_ready and selected_epoch == 0:
            raise SessionHandshakeError("post-handshake frame cannot use epoch 0")
        if len(payload) > self._remote_max_payload:
            raise SerialProtocolError("payload exceeds device-advertised limit")
        sequence = self._sequence()
        normalized_channel = Channel(channel)
        normalized_flags = FrameFlag(flags)
        if require_ack:
            if (
                self._hello is None
                or FRAME_ACK_CAPABILITY not in self._hello.capabilities
            ):
                raise SessionHandshakeError(
                    f"{self.port} firmware does not advertise "
                    f"{FRAME_ACK_CAPABILITY!r}"
                )
            normalized_flags |= FrameFlag.ACK_REQUIRED
        data = encode_frame(
            normalized_channel,
            payload,
            sequence=sequence,
            session_epoch=selected_epoch,
            flags=normalized_flags,
        )
        assert self._loop is not None
        ack_future: asyncio.Future[None] | None = None
        if require_ack:
            if sequence in self._pending_frame_acks:
                raise SerialProtocolError(
                    f"duplicate pending frame sequence: {sequence}"
                )
            ack_future = self._loop.create_future()
            self._pending_frame_acks[sequence] = (
                normalized_channel,
                ack_future,
            )
        completed: asyncio.Future[None] = self._loop.create_future()
        queued_mono = time.monotonic()
        request = _WriteRequest(
            data=data,
            completed=completed,
            queued_mono=queued_mono,
            kind=_write_kind,
        )
        try:
            try:
                self._tx_queue.put_nowait(request)
            except asyncio.QueueFull as exc:
                raise BufferError(
                    f"{self.port} transmit queue is full"
                ) from exc
            if _write_kind is not None:
                if self._heartbeat_pending_count == 0:
                    self._heartbeat_oldest_queued_mono = queued_mono
                self._heartbeat_pending_count += 1
            await completed
            if ack_future is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(ack_future),
                        timeout=self._frame_ack_timeout,
                    )
                except TimeoutError as exc:
                    error = TimeoutError(
                        f"{self.port} frame ACK timeout sequence={sequence} "
                        f"channel={normalized_channel.name}"
                    )
                    await self._fail(error)
                    raise error from exc
            return sequence
        finally:
            pending = self._pending_frame_acks.get(sequence)
            if pending is not None and pending[1] is ack_future:
                self._pending_frame_acks.pop(sequence, None)
            if ack_future is not None and not ack_future.done():
                ack_future.cancel()

    async def send_control(
        self,
        message: dict[str, Any],
        *,
        allow_before_ready: bool = False,
        epoch: int | None = None,
        flags: FrameFlag | int = FrameFlag.NONE,
    ) -> int:
        message_type = str(message.get("type") or "")
        write_kind = (
            message_type
            if message_type in {"heartbeat", "heartbeat_ack"}
            else None
        )
        return await self.send_frame(
            Channel.CONTROL_JSON,
            encode_control_payload(message),
            flags=FrameFlag(flags) | FrameFlag.JSON,
            epoch=epoch,
            allow_before_ready=allow_before_ready,
            _write_kind=write_kind,
        )

    async def send_pb_wire(self, wire_text: str) -> int:
        if not isinstance(wire_text, str):
            raise TypeError("PB wire must be text")
        expected_lengths: list[int] = []
        message_type = ""
        decoded: Any = None
        try:
            decoded = json.loads(wire_text)
            if isinstance(decoded, dict):
                from deskbot_server.pb.servo_pcm import (
                    pb_expected_binary_lengths,
                )

                message_type = str(decoded.get("type") or "")
                expected_lengths = pb_expected_binary_lengths(decoded)
        except (ImportError, TypeError, ValueError, json.JSONDecodeError):
            pass

        # A cancel is deliberately out-of-band: a PB worker may have been
        # interrupted after its JSON declaration but before all binaries were
        # written.  Those old lengths must never be applied to a later chain.
        if message_type == "pb_cancel":
            # Cancellation is deliberately outside the PB fragment lock.  It
            # invalidates the active logical binary immediately; the sender
            # notices the generation change after its current serial write and
            # does not enqueue another fragment.
            self._pb_cancel_generation += 1
            self._pending_pb_binary_lengths.clear()
            return await self.send_control(decoded)

        async def _send_declaration() -> int:
            async with self._pb_frame_lock:
                # Stop-and-wait each PB JSON transport frame so the firmware's
                # bounded HWCDC RX queue cannot silently lose a frame tail.
                raw = wire_text.encode("utf-8")
                cancel_generation = self._pb_cancel_generation
                sequence = 0
                if len(raw) <= PB_JSON_FRAGMENT_BYTES:
                    sequence = await self.send_frame(
                        Channel.PB_WIRE,
                        raw,
                        flags=FrameFlag.JSON,
                        require_ack=True,
                    )
                else:
                    if (
                        self._hello is None
                        or PB_JSON_FRAGMENT_CAPABILITY
                        not in self._hello.capabilities
                    ):
                        raise SessionHandshakeError(
                            f"{self.port} firmware does not advertise "
                            f"{PB_JSON_FRAGMENT_CAPABILITY!r}"
                        )
                    for offset in range(0, len(raw), PB_JSON_FRAGMENT_BYTES):
                        if self._pb_cancel_generation != cancel_generation:
                            raise PBTransmissionCancelled(
                                f"{self.port} PB JSON cancelled"
                            )
                        end = min(offset + PB_JSON_FRAGMENT_BYTES, len(raw))
                        fragment_flags = FrameFlag.JSON
                        if offset == 0:
                            fragment_flags |= FrameFlag.BEGIN_STREAM
                        if end == len(raw):
                            fragment_flags |= FrameFlag.END_STREAM
                        sequence = await self.send_frame(
                            Channel.PB_WIRE,
                            raw[offset:end],
                            flags=fragment_flags,
                            require_ack=True,
                        )
                        if self._pb_cancel_generation != cancel_generation:
                            raise PBTransmissionCancelled(
                                f"{self.port} PB JSON cancelled"
                            )
                # Commit binary expectations only after the declaration
                # itself has reached the firmware when it announces media.
                # A failed JSON write or ACK must not poison the next
                # otherwise-valid PB bundle.
                if self._pb_cancel_generation != cancel_generation:
                    raise PBTransmissionCancelled(
                        f"{self.port} PB JSON cancelled"
                    )
                self._pending_pb_binary_lengths.extend(expected_lengths)
                return sequence

        camera_once = bool(
            isinstance(decoded, dict) and decoded.get("camera_once")
        )
        if camera_once:
            # A media-free one-shot camera PB starts a new firmware PB
            # sequence.  If it lands after an RTC BEGIN_STREAM, firmware
            # playback reset semantics clear that just-opened audio stream.
            # Share the audio lock so the only possible orderings are:
            #
            #   camera PB -> RTC BEGIN, or
            #   RTC BEGIN -> drop stale camera PB.
            #
            # Waiting until RTC END and sending the old capture request would
            # no longer represent the utterance that scheduled it.
            async with self._audio_down_lock:
                active_generation = self._audio_down_open_generation
                if active_generation is not None:
                    logger.info(
                        "[usb_cdc] skip camera_once during audio down "
                        "device_id=%s generation=%d req=%s",
                        self.device_id or "",
                        active_generation,
                        str(decoded.get("req") or ""),
                    )
                    return 0
                return await _send_declaration()

        return await _send_declaration()

    async def send_pb_binary(self, payload: bytes) -> int:
        raw = bytes(payload)
        async with self._pb_frame_lock:
            cancel_generation = self._pb_cancel_generation
            if not self._pending_pb_binary_lengths:
                raise SerialProtocolError(
                    "PB binary has no preceding JSON length declaration"
                )
            expected = self._pending_pb_binary_lengths.pop(0)
            if len(raw) != expected:
                self._pending_pb_binary_lengths.clear()
                raise SerialProtocolError(
                    "PB binary length mismatch: "
                    f"expected {expected}, got {len(raw)}"
                )
            try:
                last_sequence = 0
                for offset in range(0, len(raw), PB_BINARY_FRAGMENT_BYTES):
                    if self._pb_cancel_generation != cancel_generation:
                        raise PBTransmissionCancelled(
                            f"{self.port} PB binary cancelled"
                        )
                    last_sequence = await self.send_frame(
                        Channel.PB_WIRE,
                        raw[offset : offset + PB_BINARY_FRAGMENT_BYTES],
                        require_ack=True,
                    )
                    if self._pb_cancel_generation != cancel_generation:
                        raise PBTransmissionCancelled(
                            f"{self.port} PB binary cancelled"
                        )
                return last_sequence
            except BaseException:
                # Once one fragment of a declared binary fails, the remaining
                # declarations cannot be safely associated with future PB
                # messages.  The firmware clears its partial accumulator on
                # session failure or its bounded fragment-progress timeout.
                self._pending_pb_binary_lengths.clear()
                raise

    def _next_audio_down_generation(self) -> int:
        generation = (self._audio_down_generation + 1) & 0xFFFFFFFF
        if generation == 0:
            generation = 1
        self._audio_down_generation = generation
        return generation

    async def begin_audio_down_stream(self, opus_payload: bytes) -> int:
        """Atomically replace playback and send its first Opus payload."""

        if not opus_payload:
            raise SerialProtocolError(
                "standalone audio BEGIN requires an Opus payload"
            )
        async with self._audio_down_lock:
            generation = self._next_audio_down_generation()
            previous_generation = self._audio_down_open_generation
            # send_frame has no await point before the encoded frame enters
            # the writer queue.  Commit the generation first so cancellation
            # while that queued write is in flight cannot leave a physical
            # BEGIN on the device with no matching logical open state here.
            self._audio_down_open_generation = generation
            try:
                await self.send_frame(
                    Channel.AUDIO_DOWN_OPUS,
                    opus_payload,
                    flags=FrameFlag.BEGIN_STREAM,
                )
            except asyncio.CancelledError:
                # The frame is already queued and may still reach the device.
                # Keeping the new generation prevents a later camera PB from
                # resetting that stream after this task releases the lock.
                raise
            except BaseException:
                self._audio_down_open_generation = previous_generation
                raise
            logger.info(
                "[usb_cdc] audio down begin device_id=%s generation=%d bytes=%d",
                self.device_id or "",
                generation,
                len(opus_payload),
            )
            return generation

    async def send_audio_down_opus(
        self,
        opus_payload: bytes,
        *,
        generation: int | None = None,
    ) -> int:
        """Append Opus only when it still belongs to the active generation."""

        if not opus_payload:
            raise SerialProtocolError("standalone audio DATA cannot be empty")
        async with self._audio_down_lock:
            active = self._audio_down_open_generation
            if generation is None and active is None:
                # Compatibility for callers that predate explicit BEGIN.
                selected = self._next_audio_down_generation()
                self._audio_down_open_generation = selected
                try:
                    sequence = await self.send_frame(
                        Channel.AUDIO_DOWN_OPUS,
                        opus_payload,
                        flags=FrameFlag.BEGIN_STREAM,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    self._audio_down_open_generation = None
                    raise
                return sequence
            selected = active if generation is None else int(generation)
            if active is None or selected != active:
                logger.debug(
                    "[usb_cdc] skip stale audio down data device_id=%s "
                    "generation=%s active=%s",
                    self.device_id or "",
                    selected,
                    active,
                )
                return 0
            return await self.send_frame(
                Channel.AUDIO_DOWN_OPUS,
                opus_payload,
            )

    async def send_audio_down_end(
        self,
        *,
        generation: int | None = None,
    ) -> int:
        """Close the standalone RTC audio stream after its final Opus packet."""

        async with self._audio_down_lock:
            active = self._audio_down_open_generation
            selected = active if generation is None else int(generation)
            if active is None or selected != active:
                return 0
            # As with BEGIN, the first await in send_frame happens only after
            # the END is queued.  Clear the logical state first: if this task
            # is cancelled, a following camera PB will be queued behind END
            # instead of being incorrectly suppressed forever.
            self._audio_down_open_generation = None
            try:
                sequence = await self.send_frame(
                    Channel.AUDIO_DOWN_OPUS,
                    b"",
                    flags=FrameFlag.END_STREAM,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._audio_down_open_generation = active
                raise
            logger.info(
                "[usb_cdc] audio down end device_id=%s generation=%d",
                self.device_id or "",
                selected,
            )
            return sequence

    async def cancel_audio_down_stream(
        self,
        *,
        generation: int | None = None,
    ) -> int:
        """Immediately invalidate and cancel the active RTC audio stream.

        ``generation`` is a host-side concurrency token and is intentionally
        not serialized into the wire payload.  The dedicated CANCEL flag is
        ordered with BEGIN/DATA by the single writer, while firmware owns its
        corresponding decode/playback generation.
        """

        async with self._audio_down_lock:
            active = self._audio_down_open_generation
            selected = active if generation is None else int(generation)
            if active is None or selected != active:
                logger.debug(
                    "[usb_cdc] skip stale audio down cancel device_id=%s "
                    "generation=%s active=%s",
                    self.device_id or "",
                    selected,
                    active,
                )
                return 0

            # Invalidate before the first await.  send_frame queues the frame
            # before it yields, so task cancellation cannot admit old DATA or
            # END after a physical CANCEL that may still reach the device.
            self._audio_down_open_generation = None
            sequence = await self.send_frame(
                Channel.AUDIO_DOWN_OPUS,
                b"",
                flags=FrameFlag.CANCEL_STREAM,
            )
            logger.info(
                "[usb_cdc] audio down cancel device_id=%s generation=%d",
                self.device_id or "",
                selected,
            )
            return sequence


    async def send(self, message: str | bytes | bytearray | memoryview) -> None:
        """WebSocket-compatible send used by ``AsrChatHub``.

        PB JSON is assigned its dedicated channel.  Other JSON messages (for
        example stage notifications) use CONTROL_JSON; binary downlink uses the
        negotiated AUDIO_DOWN_OPUS channel.
        """

        if isinstance(message, str):
            try:
                decoded = json.loads(message)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict) and not str(
                decoded.get("type") or ""
            ).startswith("pb_"):
                await self.send_control(decoded)
            else:
                await self.send_pb_wire(message)
            return
        await self.send_pb_binary(bytes(message))

    @asynccontextmanager
    async def downlink_chain(self):
        async with self._chain_lock:
            yield

    async def _send_hello(self) -> None:
        await self.send_control(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "client": "deskbot-pc",
                "client_nonce": self._client_nonce,
            },
            allow_before_ready=True,
            epoch=0,
        )

    @staticmethod
    def _parse_int(
        message: dict[str, Any],
        name: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(message.get(name))
        except (TypeError, ValueError) as exc:
            raise SessionHandshakeError(f"hello_ack.{name} is invalid") from exc
        if not minimum <= value <= maximum:
            raise SessionHandshakeError(f"hello_ack.{name} is out of range")
        return value

    def _parse_hello_ack(
        self,
        frame: Frame,
        message: dict[str, Any],
    ) -> HelloInfo:
        protocol = self._parse_int(
            message,
            "protocol",
            minimum=PROTOCOL_VERSION,
            maximum=PROTOCOL_VERSION,
        )
        if protocol != PROTOCOL_VERSION:
            raise SessionHandshakeError("unsupported protocol version")
        ack_client_nonce = self._parse_int(
            message,
            "ack_client_nonce",
            minimum=1,
            maximum=0xFFFFFFFF,
        )
        if ack_client_nonce != self._client_nonce:
            raise SessionHandshakeError(
                "hello_ack.ack_client_nonce mismatch"
            )
        device_id = str(message.get("device_id") or "").strip().lower()
        if _DEVICE_ID_RE.fullmatch(device_id) is None:
            raise SessionHandshakeError("hello_ack.device_id is invalid")
        epoch = self._parse_int(
            message,
            "session_epoch",
            minimum=1,
            maximum=0xFFFFFFFF,
        )
        if frame.session_epoch != epoch:
            raise SessionHandshakeError("hello_ack epoch mismatch")
        heartbeat_ms = self._parse_int(
            message,
            "heartbeat_ms",
            minimum=100,
            maximum=60_000,
        )
        timeout_ms = self._parse_int(
            message,
            "timeout_ms",
            minimum=heartbeat_ms + 100,
            maximum=180_000,
        )
        max_payload = self._parse_int(
            message,
            "max_payload",
            minimum=1,
            maximum=MAX_PAYLOAD,
        )
        capabilities_raw = message.get("capabilities")
        if not isinstance(capabilities_raw, list) or not all(
            isinstance(item, str) and item.strip()
            for item in capabilities_raw
        ):
            raise SessionHandshakeError("hello_ack.capabilities is invalid")
        def optional_counter(name: str) -> int:
            raw = message.get(name)
            if raw is None:
                return 0
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise SessionHandshakeError(
                    f"hello_ack.{name} is invalid"
                )
            if not 0 <= raw <= 0xFFFFFFFF:
                raise SessionHandshakeError(
                    f"hello_ack.{name} is out of range"
                )
            return raw

        reset_reason = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "_",
            str(message.get("reset_reason") or ""),
        )[:32]
        hardware_reset_reason = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "_",
            str(message.get("hardware_reset_reason") or ""),
        )[:32]
        last_panic_raw = message.get("last_panic")
        if last_panic_raw is not None and not isinstance(
            last_panic_raw,
            bool,
        ):
            raise SessionHandshakeError(
                "hello_ack.last_panic is invalid"
            )
        mic_signal_raw = message.get("mic_signal_healthy")
        if mic_signal_raw is not None and not isinstance(
            mic_signal_raw,
            bool,
        ):
            raise SessionHandshakeError(
                "hello_ack.mic_signal_healthy is invalid"
            )
        servo_ready_raw = message.get("servo_ready")
        if servo_ready_raw is not None and not isinstance(
            servo_ready_raw,
            bool,
        ):
            raise SessionHandshakeError("hello_ack.servo_ready is invalid")
        servo_backend = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "_",
            str(message.get("servo_backend") or ""),
        )[:32]
        return HelloInfo(
            device_id=device_id,
            product=str(message.get("product") or "")[:128],
            firmware=str(message.get("firmware") or "")[:128],
            session_epoch=epoch,
            heartbeat_ms=heartbeat_ms,
            timeout_ms=timeout_ms,
            max_payload=max_payload,
            capabilities=tuple(dict.fromkeys(capabilities_raw)),
            reset_reason=reset_reason,
            hardware_reset_reason=hardware_reset_reason,
            hardware_reset_code=optional_counter("hardware_reset_code"),
            last_panic=last_panic_raw is True,
            last_restart_uptime_ms=optional_counter(
                "last_restart_uptime_ms"
            ),
            recovery_count=optional_counter("recovery_count"),
            boot_count=optional_counter("boot_count"),
            uptime_ms=optional_counter("uptime_ms"),
            usb_partial_tx_failures=optional_counter(
                "usb_partial_tx_failures"
            ),
            usb_payload_crc_errors=optional_counter(
                "usb_payload_crc_errors"
            ),
            usb_rx_buffer_bytes=optional_counter("usb_rx_buffer_bytes"),
            usb_rx_high_water=optional_counter("usb_rx_high_water"),
            usb_poll_max_gap_ms=optional_counter("usb_poll_max_gap_ms"),
            mic_signal_healthy=mic_signal_raw is True,
            servo_ready=servo_ready_raw is True,
            servo_backend=servo_backend,
            servo_x_pin=optional_counter("servo_x_pin"),
            servo_y_pin=optional_counter("servo_y_pin"),
            servo_pwm_hz=optional_counter("servo_pwm_hz"),
            servo_x_pulse_us=optional_counter("servo_x_pulse_us"),
            servo_y_pulse_us=optional_counter("servo_y_pulse_us"),
            servo_write_failures=optional_counter("servo_write_failures"),
        )

    def _update_usb_telemetry(
        self,
        message: dict[str, Any],
    ) -> None:
        """Atomically accept optional uint32 telemetry from a heartbeat.

        Telemetry must never take down an otherwise healthy RTC session.  If
        one supplied value is malformed, retain the complete last-known-good
        snapshot and ignore every telemetry field in that heartbeat.
        """

        updates: dict[str, int] = {}
        for name in _USB_TELEMETRY_FIELDS:
            if name not in message:
                continue
            raw = message[name]
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or not 0 <= raw <= 0xFFFFFFFF
            ):
                logger.warning(
                    "[usb_cdc] ignored invalid heartbeat telemetry "
                    "port=%s device_id=%s field=%s value=%r",
                    self.port,
                    self.device_id,
                    name,
                    raw,
                )
                return
            updates[name] = raw

        for name, value in updates.items():
            setattr(self, f"_{name}", value)

    def _update_mic_signal_health(
        self,
        message: dict[str, Any],
    ) -> None:
        raw = message.get("mic_signal_healthy")
        if not isinstance(raw, bool) or raw == self._mic_signal_healthy:
            return
        self._mic_signal_healthy = raw
        log = logger.info if raw else logger.warning
        log(
            "[usb_cdc] microphone signal health changed port=%s "
            "device_id=%s healthy=%s",
            self.port,
            self.device_id,
            raw,
        )

    async def _handle_frame(self, frame: Frame) -> None:
        if not self._ready.is_set():
            if frame.channel != Channel.CONTROL_JSON:
                return
            message = decode_control_payload(frame.payload)
            if message.get("type") != "hello_ack":
                return
            hello = self._parse_hello_ack(frame, message)
            self._hello = hello
            self._mic_signal_healthy = hello.mic_signal_healthy
            self._usb_partial_tx_failures = hello.usb_partial_tx_failures
            self._usb_payload_crc_errors = hello.usb_payload_crc_errors
            self._usb_rx_buffer_bytes = hello.usb_rx_buffer_bytes
            self._usb_rx_high_water = hello.usb_rx_high_water
            self._usb_poll_max_gap_ms = hello.usb_poll_max_gap_ms
            self._heartbeat_seconds = hello.heartbeat_ms / 1000.0
            self._timeout_seconds = hello.timeout_ms / 1000.0
            self._remote_max_payload = hello.max_payload
            self._record_valid_rx(frame)
            self._ready.set()
            logger.info(
                "[usb_cdc] hello ready port=%s generation=%d device_id=%s "
                "epoch=%d firmware=%s mic_signal_healthy=%s "
                "reset_reason=%s recovery_count=%d boot_count=%d uptime_ms=%d "
                "hardware_reset_reason=%s hardware_reset_code=%d "
                "last_panic=%s last_restart_uptime_ms=%d "
                "usb_partial_tx_failures=%d usb_payload_crc_errors=%d "
                "usb_rx_buffer_bytes=%d usb_rx_high_water=%d "
                "usb_poll_max_gap_ms=%d servo_ready=%s "
                "servo_backend=%s servo_pins=%d/%d servo_pwm_hz=%d "
                "servo_pulse_us=%d/%d servo_write_failures=%d",
                self.port,
                self.generation,
                hello.device_id,
                hello.session_epoch,
                hello.firmware,
                hello.mic_signal_healthy,
                hello.reset_reason or "none",
                hello.recovery_count,
                hello.boot_count,
                hello.uptime_ms,
                hello.hardware_reset_reason or "unknown",
                hello.hardware_reset_code,
                hello.last_panic,
                hello.last_restart_uptime_ms,
                hello.usb_partial_tx_failures,
                hello.usb_payload_crc_errors,
                hello.usb_rx_buffer_bytes,
                hello.usb_rx_high_water,
                hello.usb_poll_max_gap_ms,
                hello.servo_ready,
                hello.servo_backend or "unknown",
                hello.servo_x_pin,
                hello.servo_y_pin,
                hello.servo_pwm_hz,
                hello.servo_x_pulse_us,
                hello.servo_y_pulse_us,
                hello.servo_write_failures,
            )
            await _invoke(self._on_ready, self, hello)
            return

        if frame.session_epoch != self.session_epoch:
            self.stale_epoch_frames += 1
            return
        # Keep both signals: LOG/camera prove bytes still arrive, while only
        # loop-owned control/PB/audio frames prove the device application can
        # service a new request and heartbeat.
        self._record_valid_rx(frame)
        if frame.channel == Channel.CONTROL_JSON:
            message = decode_control_payload(frame.payload)
            message_type = str(message.get("type") or "")
            if message_type == "frame_ack":
                if not frame.flags & FrameFlag.ACK:
                    raise SerialProtocolError(
                        "frame_ack is missing the ACK flag"
                    )
                try:
                    ack_sequence = int(message.get("ack_sequence"))
                    ack_channel = Channel(int(message.get("ack_channel")))
                except (TypeError, ValueError) as exc:
                    raise SerialProtocolError(
                        "frame_ack fields are invalid"
                    ) from exc
                if not 1 <= ack_sequence <= 0xFFFFFFFF:
                    raise SerialProtocolError(
                        "frame_ack.ack_sequence is out of range"
                    )
                pending = self._pending_frame_acks.pop(
                    ack_sequence,
                    None,
                )
                if pending is None:
                    logger.debug(
                        "[usb_cdc] duplicate/late frame ACK port=%s "
                        "sequence=%d channel=%s",
                        self.port,
                        ack_sequence,
                        ack_channel.name,
                    )
                    return
                expected_channel, ack_future = pending
                if ack_channel != expected_channel:
                    raise SerialProtocolError(
                        "frame_ack channel mismatch: "
                        f"expected {expected_channel.name}, "
                        f"got {ack_channel.name}"
                    )
                if not ack_future.done():
                    ack_future.set_result(None)
                return
            if message_type == "heartbeat":
                self._update_usb_telemetry(message)
                self._update_mic_signal_health(message)
                await self.send_control(
                    {"type": "heartbeat_ack"},
                    flags=FrameFlag.ACK,
                )
                return
            if message_type == "heartbeat_ack":
                self._update_usb_telemetry(message)
                self._update_mic_signal_health(message)
                return
            if message_type == "session_end":
                # Firmware >= session_end capability announces its own
                # teardown while the link is still writable.  Fail this
                # session immediately instead of waiting for the heartbeat
                # timeout; the manager reconnects with a fresh generation.
                # Older firmware never sends this message, and older hosts
                # forward it to the application layer where unknown types are
                # logged and ignored.
                reason = str(message.get("reason") or "unspecified")
                logger.info(
                    "[usb_cdc] device ended session port=%s device_id=%s "
                    "reason=%s",
                    self.port,
                    self.device_id or "",
                    reason,
                )
                raise SessionClosed(
                    f"{self.port} device ended session: {reason}"
                )
            if message_type == "hello_ack":
                duplicate = self._parse_hello_ack(frame, message)
                if (
                    duplicate.session_epoch != self.session_epoch
                    or duplicate.device_id != self.device_id
                ):
                    raise SessionHandshakeError(
                        "device changed hello_ack without reconnect"
                    )
                self._mic_signal_healthy = duplicate.mic_signal_healthy
                return
        await _invoke(self._on_frame, self, frame)
        await self._enqueue_application_frame(frame)

    async def _enqueue_application_message(self, message: str | bytes) -> None:
        try:
            self._messages.put_nowait(message)
        except asyncio.QueueFull:
            self.message_queue_overflows += 1
            # Real-time media and logs never enter this queue. Coalesce only
            # superseded VAD state; PB ACKs and other control messages remain
            # reliable and may briefly apply backpressure to the RX loop.
            incoming_type = ""
            if isinstance(message, str):
                try:
                    decoded = json.loads(message)
                    if isinstance(decoded, dict):
                        incoming_type = str(decoded.get("type") or "")
                except json.JSONDecodeError:
                    pass
            retained: list[str | bytes | None] = []
            removed_vad = False
            while True:
                try:
                    queued = self._messages.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queued_type = ""
                if isinstance(queued, str):
                    try:
                        decoded = json.loads(queued)
                        if isinstance(decoded, dict):
                            queued_type = str(decoded.get("type") or "")
                    except json.JSONDecodeError:
                        pass
                if not removed_vad and queued_type == "audio_vad":
                    removed_vad = True
                    continue
                retained.append(queued)
            for queued in retained:
                self._messages.put_nowait(queued)
            if incoming_type == "audio_vad" and not removed_vad:
                # Existing reliable messages fill the queue; the next VAD
                # transition supersedes this optional hint.
                pass
            elif removed_vad:
                self._messages.put_nowait(message)
            else:
                await self._messages.put(message)
            now = time.monotonic()
            if (
                self.message_queue_overflows == 1
                or now - self._last_ingress_drop_log_mono >= 2.0
            ):
                logger.warning(
                    "[usb_cdc] application backlog dropped without "
                    "disconnect port=%s device_id=%s total=%d queue=%d",
                    self.port,
                    self.device_id or "",
                    self.message_queue_overflows,
                    self._messages.qsize(),
                )
                self._last_ingress_drop_log_mono = now

    def _enqueue_audio_up(self, frame: Frame) -> None:
        opus_payload, opus_frames = _normalize_opus_uplink_batch(
            frame.payload
        )
        batch = UplinkAudioBatch(
            payload=opus_payload,
            opus_frames=opus_frames,
            sequence=frame.sequence,
            received_mono=time.monotonic(),
        )
        try:
            self._audio_up_queue.put_nowait(batch)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._audio_up_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._audio_up_queue.put_nowait(batch)
        self.audio_queue_drops += 1
        now = time.monotonic()
        if (
            self.audio_queue_drops == 1
            or now - self._last_ingress_drop_log_mono >= 2.0
        ):
            logger.warning(
                "[usb_cdc] stale microphone batch dropped port=%s "
                "device_id=%s total=%d queue=%d newest_sequence=%d",
                self.port,
                self.device_id or "",
                self.audio_queue_drops,
                self._audio_up_queue.qsize(),
                frame.sequence,
            )
            self._last_ingress_drop_log_mono = now

    def _enqueue_camera(self, frame: Frame) -> None:
        latest = UplinkCameraFrame(
            payload=bytes(frame.payload),
            sequence=frame.sequence,
            received_mono=time.monotonic(),
        )
        try:
            self._camera_queue.put_nowait(latest)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._camera_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._camera_queue.put_nowait(latest)
        self.camera_queue_drops += 1

    async def _enqueue_application_frame(self, frame: Frame) -> None:
        if frame.channel == Channel.CONTROL_JSON:
            # ``_handle_frame`` already validated that this is a JSON object.
            await self._enqueue_application_message(
                frame.payload.decode("utf-8")
            )
            return
        if frame.channel == Channel.AUDIO_UP_OPUS:
            self._enqueue_audio_up(frame)
            return
        if frame.channel == Channel.CAMERA_JPEG:
            self._enqueue_camera(frame)
            return
        if (
            frame.channel == Channel.PB_WIRE
            and frame.flags & FrameFlag.JSON
        ):
            await self._enqueue_application_message(
                frame.payload.decode("utf-8")
            )
            return
        if frame.channel == Channel.LOG:
            text = frame.payload.decode("utf-8", errors="replace").rstrip()
            # Firmware emits several diagnostic INFO lines per second (audio
            # levels, CPU tables, camera flow, and ASR counters).  Writing all
            # of them synchronously to the host log can make this same receive
            # loop fall behind real time, eventually overflowing the
            # application ingress queue.  Preserve warnings/errors at INFO so
            # field diagnostics remain visible; routine telemetry is DEBUG.
            log_fn = (
                logger.info
                if "[WARN" in text or "[ERROR" in text
                else logger.debug
            )
            log_fn(
                "[usb_cdc][device-log] device_id=%s %s",
                self.device_id or "",
                text,
            )

    async def _rx_loop(self) -> None:
        try:
            while True:
                item = await self._rx_queue.get()
                if item is None:
                    return
                generation, data = item
                if generation != self.generation:
                    continue
                for frame in self._decoder.feed(data):
                    await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)

    async def _supervisor_loop(self) -> None:
        try:
            next_hello = 0.0
            next_heartbeat = 0.0
            while True:
                now = time.monotonic()
                if not self._ready.is_set():
                    if now - self._started_mono >= self._hello_timeout:
                        raise TimeoutError(f"{self.port} hello timeout")
                    if now >= next_hello:
                        await self._send_hello()
                        next_hello = now + self._hello_interval
                    await asyncio.sleep(
                        min(0.1, max(0.01, next_hello - now))
                    )
                    continue

                interactive_stale = (
                    now - self._last_rx_mono > self._timeout_seconds
                )
                raw_recent = (
                    now - self._last_any_rx_mono <= self._timeout_seconds
                )
                incremental_frame_pending = (
                    self._decoder.buffered_bytes > 0
                    or self._rx_queue.qsize() > 0
                )
                receive_backlog_pending = self._rx_queue.qsize() > 0
                valid_anchor = (
                    self._last_valid_mono
                    if self._last_valid_mono > 0.0
                    else self._started_mono
                )
                incomplete_frame_within_guard = (
                    raw_recent
                    and incremental_frame_pending
                    and now - valid_anchor
                    <= INCOMPLETE_FRAME_STALL_SECONDS
                )
                # A lagging host receive queue must not be mistaken for a dead
                # device loop.  Keep this exception deliberately narrow: raw
                # LOG/camera traffic or an endlessly corrupt partial frame do
                # not qualify, preserving the existing liveness guarantees.
                if (
                    interactive_stale
                    and not receive_backlog_pending
                    and not incomplete_frame_within_guard
                ):
                    raise TimeoutError(
                        f"{self.port} heartbeat timeout: "
                        f"{self._diagnostic_summary(now=now)}"
                    )
                if now >= next_heartbeat:
                    await self.send_control({"type": "heartbeat"})
                    next_heartbeat = now + self._heartbeat_seconds
                await asyncio.sleep(
                    min(
                        0.25,
                        max(0.01, next_heartbeat - time.monotonic()),
                    )
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)

    async def close(
        self,
        code: int = 1000,
        reason: str = "serial session closed",
    ) -> None:
        del code  # WebSocket compatibility; serial has no close code on wire.
        async with self._close_lock:
            if self._closed.is_set():
                return
            self._closing = True
            self._pb_cancel_generation += 1
            self._pending_pb_binary_lengths.clear()
            pending_frame_acks = tuple(self._pending_frame_acks.values())
            self._pending_frame_acks.clear()
            for _channel, future in pending_frame_acks:
                if not future.done():
                    future.set_exception(
                        SessionClosed(f"{self.port} closed: {reason}")
                    )
            self._stop_reader.set()
            cancel_read = getattr(self._serial, "cancel_read", None)
            if callable(cancel_read):
                try:
                    cancel_read()
                except Exception:
                    pass
            try:
                self._serial.close()
            except Exception:
                logger.debug(
                    "[usb_cdc] serial close failed port=%s",
                    self.port,
                    exc_info=True,
                )

            # Terminate the WebSocket-compatible async iterator before waiting
            # for its handler in the manager's close callback.
            while self._messages.full():
                try:
                    self._messages.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self._messages.put_nowait(None)
            except asyncio.QueueFull:
                pass
            for media_queue in (self._audio_up_queue, self._camera_queue):
                while media_queue.full():
                    try:
                        media_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    media_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

            current = asyncio.current_task()
            tasks = (
                self._supervisor_task,
                self._rx_task,
                self._writer_task,
            )
            pending = []
            for task in tasks:
                if task is not None and task is not current and not task.done():
                    task.cancel()
                    pending.append(task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            while True:
                try:
                    request = self._tx_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if request is not None and not request.completed.done():
                    if request.kind is not None:
                        self._heartbeat_pending_count = max(
                            0,
                            self._heartbeat_pending_count - 1,
                        )
                    request.completed.set_exception(
                        SessionClosed(f"{self.port} closed: {reason}")
                    )
            if self._heartbeat_pending_count == 0:
                self._heartbeat_oldest_queued_mono = 0.0

            reader = self._reader_thread
            if (
                reader is not None
                and reader.is_alive()
                and reader is not threading.current_thread()
            ):
                await asyncio.to_thread(reader.join, 0.5)
            self._closed.set()

            if not self._closed_callback_called:
                self._closed_callback_called = True
                await _invoke(self._on_closed, self, self._last_error)


SerialDeviceSession = DeviceSession
