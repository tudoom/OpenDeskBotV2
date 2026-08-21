from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from collections import deque
from dataclasses import replace

import pytest
from PIL import Image

from deskbot_server.infrastructure.serial.protocol import (
    HEADER_SIZE,
    PAYLOAD_CRC_SIZE,
    Channel,
    FrameDecoder,
    FrameFlag,
    SerialProtocolError,
    decode_control_payload,
    encode_control_payload,
    encode_frame,
)
from deskbot_server.infrastructure.serial.session import (
    PB_BINARY_FRAGMENT_BYTES,
    PB_JSON_FRAGMENT_BYTES,
    SERIAL_WRITE_CHUNK_BYTES,
    DeviceSession,
    PBTransmissionCancelled,
    SessionHandshakeError,
)
from deskbot_server.infrastructure.ws.downlink_adapter import (
    WsDownlinkAdapter,
)


def _test_jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color=(40, 120, 200)).save(
        output,
        format="JPEG",
    )
    return output.getvalue()


class FakeSerial:
    def __init__(
        self,
        *,
        partial_writes: bool = False,
        auto_frame_ack: bool = True,
        max_device_accept_per_write: int | None = None,
    ) -> None:
        self.timeout = 0.02
        self.write_timeout = 1.0
        self._condition = threading.Condition()
        self._rx = bytearray()
        self._closed = False
        self.writes: list[bytes] = []
        self.write_requests: list[bytes] = []
        self.partial_writes = partial_writes
        self.auto_frame_ack = auto_frame_ack
        self.max_device_accept_per_write = max_device_accept_per_write
        self._host_frame_decoder = FrameDecoder()
        self._device_sequence = 1000
        self.active_writers = 0
        self.max_active_writers = 0

    def inject(self, data: bytes) -> None:
        with self._condition:
            self._rx.extend(data)
            self._condition.notify_all()

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while not self._rx and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            if self._closed:
                return b""
            count = min(size, len(self._rx))
            data = bytes(self._rx[:count])
            del self._rx[:count]
            return data

    def write(self, data: bytes) -> int:
        with self._condition:
            if self._closed:
                raise OSError("closed")
            self.active_writers += 1
            self.max_active_writers = max(
                self.max_active_writers,
                self.active_writers,
            )
        try:
            time.sleep(0.001)
            count = min(len(data), 7) if self.partial_writes else len(data)
            requested = bytes(data[:count])
            accepted_count = count
            if self.max_device_accept_per_write is not None:
                accepted_count = min(
                    accepted_count,
                    self.max_device_accept_per_write,
                )
            written = bytes(data[:accepted_count])
            with self._condition:
                self.write_requests.append(requested)
                self.writes.append(written)
            if self.auto_frame_ack:
                for frame in self._host_frame_decoder.feed(written):
                    if not frame.flags & FrameFlag.ACK_REQUIRED:
                        continue
                    self._device_sequence += 1
                    self.inject(
                        encode_frame(
                            Channel.CONTROL_JSON,
                            encode_control_payload(
                                {
                                    "type": "frame_ack",
                                    "session_epoch": frame.session_epoch,
                                    "ack_sequence": frame.sequence,
                                    "ack_channel": int(frame.channel),
                                }
                            ),
                            sequence=self._device_sequence,
                            session_epoch=frame.session_epoch,
                            flags=FrameFlag.ACK,
                        )
                    )
            return count
        finally:
            with self._condition:
                self.active_writers -= 1

    def cancel_read(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def _decode_writes(fake: FakeSerial):
    decoder = FrameDecoder()
    return decoder.feed(b"".join(fake.writes))


def _block_next_audio_write(
    session: DeviceSession,
    expected_flags: FrameFlag,
) -> tuple[threading.Event, threading.Event, threading.Event]:
    original_write = session._write_all_blocking
    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()
    armed = True

    def controlled_write(data: bytes) -> None:
        nonlocal armed
        frames = FrameDecoder().feed(data)
        should_block = bool(
            armed
            and len(frames) == 1
            and frames[0].channel == Channel.AUDIO_DOWN_OPUS
            and frames[0].flags == expected_flags
        )
        if should_block:
            armed = False
            write_started.set()
            if not release_write.wait(timeout=2.0):
                raise TimeoutError("timed out releasing blocked audio write")
        try:
            original_write(data)
        finally:
            if should_block:
                write_finished.set()

    session._write_all_blocking = controlled_write  # type: ignore[method-assign]
    return write_started, release_write, write_finished


def _pb_wire_with_size(size: int, *, next_bin_len: int) -> str:
    message = {
        "type": "pb_single",
        "audio": {"next_bin_len": next_bin_len},
        "pad": "",
    }
    compact = json.dumps(message, separators=(",", ":"))
    padding = size - len(compact.encode())
    assert padding >= 0
    message["pad"] = "x" * padding
    wire = json.dumps(message, separators=(",", ":"))
    assert len(wire.encode()) == size
    return wire


def _hello_ack_frame(
    *,
    ack_client_nonce: int,
    epoch: int = 42,
    sequence: int = 10,
    heartbeat_ms: int = 100,
    timeout_ms: int = 600,
) -> bytes:
    return encode_frame(
        Channel.CONTROL_JSON,
        encode_control_payload(
            {
                "type": "hello_ack",
                "protocol": 1,
                "ack_client_nonce": ack_client_nonce,
                "device_id": "deskbot_123456abcdef",
                "product": "Deskbot",
                "firmware": "test",
                "session_epoch": epoch,
                "heartbeat_ms": heartbeat_ms,
                "timeout_ms": timeout_ms,
                "max_payload": 1024 * 1024,
                "capabilities": [
                    "control_json",
                    "pb_wire",
                    "audio_up_opus",
                    "audio_down_opus",
                    "camera_jpeg",
                    "frame_ack",
                    "pb_json_fragments",
                ],
            }
        ),
        sequence=sequence,
        session_epoch=epoch,
        flags=FrameFlag.JSON,
    )


def _inject_frame_ack(
    fake: FakeSerial,
    frame,
    *,
    sequence: int,
) -> None:
    fake.inject(
        encode_frame(
            Channel.CONTROL_JSON,
            encode_control_payload(
                {
                    "type": "frame_ack",
                    "session_epoch": frame.session_epoch,
                    "ack_sequence": frame.sequence,
                    "ack_channel": int(frame.channel),
                }
            ),
            sequence=sequence,
            session_epoch=frame.session_epoch,
            flags=FrameFlag.ACK,
        )
    )


async def _start_ready_session(
    fake: FakeSerial,
    *,
    generation: int = 1,
    on_frame=None,
) -> DeviceSession:
    session = DeviceSession(
        "COM_TEST",
        fake,
        generation=generation,
        on_frame=on_frame,
        hello_interval=0.05,
        hello_timeout=1.0,
    )
    await session.start()
    fake.inject(
        _hello_ack_frame(ack_client_nonce=session.client_nonce)
    )
    await session.wait_ready(timeout=1.0)
    return session


async def _eventually(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.005)


def test_paced_writer_preserves_frame_above_2048_byte_rx_boundary():
    payload = b"x" * 2021
    encoded = encode_frame(
        Channel.PB_WIRE,
        payload,
        sequence=1,
        session_epoch=42,
        flags=FrameFlag.JSON | FrameFlag.ACK_REQUIRED,
    )
    assert len(encoded) == 2049

    one_shot = FakeSerial(
        auto_frame_ack=False,
        max_device_accept_per_write=2048,
    )
    assert one_shot.write(encoded) == len(encoded)
    assert _decode_writes(one_shot) == []

    paced = FakeSerial(
        auto_frame_ack=False,
        max_device_accept_per_write=2048,
    )
    session = DeviceSession("COM_TEST", paced, generation=1)
    session._write_all_blocking(encoded)

    frames = _decode_writes(paced)
    assert len(frames) == 1
    assert frames[0].payload == payload
    assert max(map(len, paced.write_requests)) <= SERIAL_WRITE_CHUNK_BYTES


@pytest.mark.parametrize("wire_size", [2021, 4915])
def test_bounded_rx_delivers_large_pb_json_and_binary_with_transport_acks(
    wire_size: int,
):
    async def _run() -> None:
        binary = (bytes(range(251)) * 19)[:4548]
        wire = _pb_wire_with_size(
            wire_size,
            next_bin_len=len(binary),
        )
        fake = FakeSerial(max_device_accept_per_write=2048)
        session = await _start_ready_session(fake)

        declaration_sequence = await session.send_pb_wire(wire)
        last_binary_sequence = await session.send_pb_binary(binary)

        pb_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        ]
        json_fragments = [
            frame for frame in pb_frames if frame.flags & FrameFlag.JSON
        ]
        binary_fragments = [
            frame
            for frame in pb_frames
            if not frame.flags & FrameFlag.JSON
        ]
        assert declaration_sequence == json_fragments[-1].sequence
        assert b"".join(
            frame.payload for frame in json_fragments
        ) == wire.encode()
        assert json_fragments[0].flags == (
            FrameFlag.JSON
            | FrameFlag.ACK_REQUIRED
            | FrameFlag.BEGIN_STREAM
        )
        assert json_fragments[-1].flags == (
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED | FrameFlag.END_STREAM
        )
        assert all(
            0 < len(frame.payload) <= PB_JSON_FRAGMENT_BYTES
            for frame in json_fragments
        )
        assert all(
            frame.flags == FrameFlag.ACK_REQUIRED
            for frame in binary_fragments
        )
        assert b"".join(
            frame.payload for frame in binary_fragments
        ) == binary
        assert last_binary_sequence == binary_fragments[-1].sequence
        assert all(
            len(frame.payload) + HEADER_SIZE + PAYLOAD_CRC_SIZE < 2048
            for frame in [*json_fragments, *binary_fragments]
        )
        assert max(map(len, fake.write_requests)) <= SERIAL_WRITE_CHUNK_BYTES
        assert session._pending_pb_binary_lengths == []
        await session.close()

    asyncio.run(_run())


def test_media_free_pb_json_waits_for_transport_ack():
    async def _run() -> None:
        fake = FakeSerial(auto_frame_ack=False)
        session = await _start_ready_session(fake)
        wire = '{"type":"pb_single","anim":[]}'

        sending = asyncio.create_task(session.send_pb_wire(wire))
        await _eventually(
            lambda: any(
                frame.channel == Channel.PB_WIRE
                for frame in _decode_writes(fake)
            )
        )
        frame = next(
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        )
        assert frame.flags == (FrameFlag.JSON | FrameFlag.ACK_REQUIRED)
        assert not sending.done()
        _inject_frame_ack(fake, frame, sequence=2000)
        assert await sending == frame.sequence
        await session.close()

    asyncio.run(_run())


def test_large_pb_json_requires_fragment_capability():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        assert session._hello is not None
        session._hello = replace(
            session._hello,
            capabilities=tuple(
                item
                for item in session._hello.capabilities
                if item != "pb_json_fragments"
            ),
        )
        wire = _pb_wire_with_size(
            PB_JSON_FRAGMENT_BYTES + 1,
            next_bin_len=0,
        )
        with pytest.raises(SessionHandshakeError, match="pb_json_fragments"):
            await session.send_pb_wire(wire)
        await session.close()

    asyncio.run(_run())


def test_pb_cancel_during_small_json_ack_does_not_commit_binary_length():
    async def _run() -> None:
        fake = FakeSerial(auto_frame_ack=False)
        session = await _start_ready_session(fake)
        wire = _pb_wire_with_size(256, next_bin_len=64)

        sending = asyncio.create_task(session.send_pb_wire(wire))
        await _eventually(
            lambda: any(
                frame.channel == Channel.PB_WIRE
                for frame in _decode_writes(fake)
            )
        )
        declaration = next(
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        )
        cancel = {"type": "pb_cancel", "req": "cancel-small-json"}
        await session.send_pb_wire(json.dumps(cancel, separators=(",", ":")))
        _inject_frame_ack(fake, declaration, sequence=2001)
        with pytest.raises(PBTransmissionCancelled):
            await sending
        assert session._pending_pb_binary_lengths == []
        await session.close()

    asyncio.run(_run())


def test_session_hello_epoch_heartbeat_and_single_writer():
    async def _run() -> None:
        fake = FakeSerial(partial_writes=True)
        received = deque()
        ready = []
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=3,
            on_ready=lambda _session, hello: ready.append(hello),
            on_frame=lambda _session, frame: received.append(frame),
            hello_interval=0.05,
            hello_timeout=1.0,
        )
        await session.start()
        await _eventually(lambda: bool(_decode_writes(fake)))
        hello_frames = _decode_writes(fake)
        assert hello_frames
        assert hello_frames[0].session_epoch == 0
        assert hello_frames[0].flags & FrameFlag.JSON
        hello_message = decode_control_payload(hello_frames[0].payload)
        assert hello_message["type"] == "hello"
        assert hello_message["client_nonce"] == session.client_nonce
        assert 1 <= session.client_nonce <= 0xFFFFFFFF

        hello_ack = {
            "type": "hello_ack",
            "protocol": 1,
            "ack_client_nonce": session.client_nonce,
            "device_id": "deskbot_123456abcdef",
            "product": "Deskbot",
            "firmware": "test",
            "session_epoch": 42,
            "heartbeat_ms": 100,
            "timeout_ms": 600,
            "max_payload": 1024 * 1024,
            "reset_reason": "esp_sr_task_stall",
            "hardware_reset_reason": "software",
            "hardware_reset_code": 3,
            "last_panic": False,
            "last_restart_uptime_ms": 24_987,
            "recovery_count": 1,
            "boot_count": 7,
            "uptime_ms": 30_123,
            "usb_partial_tx_failures": 2,
            "usb_payload_crc_errors": 3,
            "usb_rx_buffer_bytes": 8192,
            "usb_rx_high_water": 4096,
            "usb_poll_max_gap_ms": 17,
            "mic_signal_healthy": False,
            "servo_ready": True,
            "servo_backend": "ledc",
            "servo_x_pin": 15,
            "servo_y_pin": 16,
            "servo_pwm_hz": 50,
            "servo_x_pulse_us": 1500,
            "servo_y_pulse_us": 1500,
            "servo_write_failures": 0,
            "capabilities": [
                "control_json",
                "pb_wire",
                "audio_up_opus",
                "audio_down_opus",
                "frame_ack",
                "pb_json_fragments",
            ],
        }
        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(hello_ack),
                sequence=10,
                session_epoch=42,
                flags=FrameFlag.JSON,
            )
        )
        info = await session.wait_ready(timeout=1.0)
        assert info.session_epoch == 42
        assert info.device_id == "deskbot_123456abcdef"
        assert info.mic_signal_healthy is False
        assert info.reset_reason == "esp_sr_task_stall"
        assert info.hardware_reset_reason == "software"
        assert info.hardware_reset_code == 3
        assert info.last_panic is False
        assert info.last_restart_uptime_ms == 24_987
        assert info.recovery_count == 1
        assert info.boot_count == 7
        assert info.uptime_ms == 30_123
        assert info.usb_partial_tx_failures == 2
        assert info.usb_payload_crc_errors == 3
        assert info.usb_rx_buffer_bytes == 8192
        assert info.usb_rx_high_water == 4096
        assert info.usb_poll_max_gap_ms == 17
        assert info.servo_ready is True
        assert info.servo_backend == "ledc"
        assert info.servo_x_pin == 15
        assert info.servo_y_pin == 16
        assert info.servo_pwm_hz == 50
        assert info.servo_x_pulse_us == 1500
        assert info.servo_y_pulse_us == 1500
        assert info.servo_write_failures == 0
        assert session.mic_signal_healthy is False
        assert len(ready) == 1
        telemetry = session.diagnostics()
        assert telemetry.usb_partial_tx_failures == 2
        assert telemetry.usb_payload_crc_errors == 3
        assert telemetry.usb_rx_buffer_bytes == 8192
        assert telemetry.usb_rx_high_water == 4096
        assert telemetry.usb_poll_max_gap_ms == 17
        summary = session._diagnostic_summary(now=time.monotonic())
        assert "usb_partial_tx_failures=2" in summary
        assert "usb_payload_crc_errors=3" in summary
        assert "usb_rx_buffer_bytes=8192" in summary
        assert "usb_rx_high_water=4096" in summary
        assert "usb_poll_max_gap_ms=17" in summary
        await _eventually(
            lambda: any(
                frame.channel == Channel.CONTROL_JSON
                and decode_control_payload(frame.payload).get("type")
                == "heartbeat"
                for frame in _decode_writes(fake)
            )
        )
        heartbeat = next(
            decode_control_payload(frame.payload)
            for frame in _decode_writes(fake)
            if frame.channel == Channel.CONTROL_JSON
            and decode_control_payload(frame.payload).get("type")
            == "heartbeat"
        )
        assert heartbeat == {"type": "heartbeat"}

        await asyncio.gather(
            *(
                session.send_pb_wire(
                    f'{{"type":"pb_start","req":"r{i}"}}'
                )
                for i in range(8)
            )
        )
        assert fake.max_active_writers == 1

        stale = encode_frame(
            Channel.LOG,
            b"stale",
            sequence=11,
            session_epoch=41,
        )
        current = encode_frame(
            Channel.LOG,
            b"current",
            sequence=12,
            session_epoch=42,
        )
        fake.inject(stale + current)
        await _eventually(lambda: len(received) == 1)
        assert received[0].payload == b"current"
        assert session.stale_epoch_frames == 1

        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "heartbeat",
                        "mic_signal_healthy": True,
                    }
                ),
                sequence=13,
                session_epoch=42,
            )
        )
        await _eventually(
            lambda: any(
                frame.channel == Channel.CONTROL_JSON
                and decode_control_payload(frame.payload).get("type")
                == "heartbeat_ack"
                for frame in _decode_writes(fake)
            )
        )
        heartbeat_ack = next(
            decode_control_payload(frame.payload)
            for frame in _decode_writes(fake)
            if frame.channel == Channel.CONTROL_JSON
            and decode_control_payload(frame.payload).get("type")
            == "heartbeat_ack"
        )
        assert heartbeat_ack == {"type": "heartbeat_ack"}
        await _eventually(lambda: session.mic_signal_healthy)
        await session.close()
        assert session.is_closed

    asyncio.run(_run())


def test_usb_telemetry_defaults_updates_and_ignores_invalid_heartbeat(caplog):
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)

        info = session.hello_info
        assert info is not None
        assert info.usb_partial_tx_failures == 0
        assert info.usb_payload_crc_errors == 0
        assert info.usb_rx_buffer_bytes == 0
        assert info.usb_rx_high_water == 0
        assert info.usb_poll_max_gap_ms == 0
        assert info.servo_ready is False
        assert info.servo_backend == ""
        assert info.servo_pwm_hz == 0
        initial = session.diagnostics()
        assert initial.usb_partial_tx_failures == 0
        assert initial.usb_payload_crc_errors == 0
        assert initial.usb_rx_buffer_bytes == 0
        assert initial.usb_rx_high_water == 0
        assert initial.usb_poll_max_gap_ms == 0

        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "heartbeat",
                        "usb_partial_tx_failures": 4,
                        "usb_payload_crc_errors": 5,
                        "usb_rx_buffer_bytes": 8192,
                        "usb_rx_high_water": 6144,
                        "usb_poll_max_gap_ms": 21,
                    }
                ),
                sequence=50,
                session_epoch=session.session_epoch,
            )
        )
        await _eventually(
            lambda: session.diagnostics().last_valid_sequence == 50
        )
        first = session.diagnostics()
        assert first.usb_partial_tx_failures == 4
        assert first.usb_payload_crc_errors == 5
        assert first.usb_rx_buffer_bytes == 8192
        assert first.usb_rx_high_water == 6144
        assert first.usb_poll_max_gap_ms == 21

        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "heartbeat_ack",
                        "usb_partial_tx_failures": 6,
                        "usb_payload_crc_errors": 7,
                        "usb_rx_high_water": 7000,
                        "usb_poll_max_gap_ms": 25,
                    }
                ),
                sequence=51,
                session_epoch=session.session_epoch,
                flags=FrameFlag.ACK,
            )
        )
        await _eventually(
            lambda: session.diagnostics().last_valid_sequence == 51
        )
        second = session.diagnostics()
        assert second.usb_partial_tx_failures == 6
        assert second.usb_payload_crc_errors == 7
        assert second.usb_rx_buffer_bytes == 8192
        assert second.usb_rx_high_water == 7000
        assert second.usb_poll_max_gap_ms == 25

        caplog.clear()
        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "heartbeat_ack",
                        "usb_partial_tx_failures": 99,
                        "usb_payload_crc_errors": -1,
                    }
                ),
                sequence=52,
                session_epoch=session.session_epoch,
                flags=FrameFlag.ACK,
            )
        )
        await _eventually(
            lambda: session.diagnostics().last_valid_sequence == 52
        )
        ignored = session.diagnostics()
        assert ignored.usb_partial_tx_failures == 6
        assert ignored.usb_payload_crc_errors == 7
        assert "ignored invalid heartbeat telemetry" in caplog.text
        assert session.is_ready
        await session.close()

    asyncio.run(_run())


def test_continuous_valid_audio_keeps_session_alive_until_real_silence():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        loop = asyncio.get_running_loop()
        sequence = 200
        keepalive_until = loop.time() + 1.35

        # Deliberately do not consume the WebSocket-compatible application
        # iterator. Transport liveness must follow validated frames rather
        # than application consumption speed or heartbeat-only traffic.
        while loop.time() < keepalive_until:
            fake.inject(
                encode_frame(
                    Channel.AUDIO_UP_OPUS,
                    b"\x00\x01x",
                    sequence=sequence,
                    session_epoch=42,
                )
            )
            sequence += 1
            await asyncio.sleep(0.12)
            assert not session.is_closed

        snapshot = session.diagnostics()
        assert snapshot.last_valid_channel == "AUDIO_UP_OPUS"
        assert snapshot.last_valid_sequence == sequence - 1
        assert snapshot.last_valid_epoch == 42
        assert snapshot.last_valid_age_seconds is not None
        assert snapshot.last_valid_age_seconds < 0.4
        assert snapshot.message_queue_depth == 0
        assert snapshot.audio_queue_depth > 0
        assert snapshot.last_heartbeat_queue_delay_seconds is not None
        assert snapshot.last_heartbeat_write_seconds is not None

        # Once all valid frames really stop, the negotiated 600 ms timeout
        # must still fail closed instead of keeping a zombie session.
        await asyncio.wait_for(session.wait_closed(), timeout=1.2)
        assert isinstance(session.last_error, TimeoutError)
        error = str(session.last_error)
        assert "heartbeat timeout" in error
        assert "last_channel=AUDIO_UP_OPUS" in error
        assert "rx_queue=" in error
        assert "tx_queue=" in error
        assert "message_queue=" in error
        assert "heartbeat_queue_delay_ms=" in error

    asyncio.run(_run())


def test_delayed_hello_ack_reuses_nonce_and_duplicate_ack_is_idempotent():
    async def _run() -> None:
        fake = FakeSerial()
        ready = []
        session = DeviceSession(
            "COM_RETRY",
            fake,
            generation=9,
            on_ready=lambda _session, hello: ready.append(hello),
            hello_interval=0.05,
            hello_timeout=1.0,
        )
        await session.start()

        def _hello_frames():
            return [
                frame
                for frame in _decode_writes(fake)
                if frame.channel == Channel.CONTROL_JSON
                and decode_control_payload(frame.payload).get("type")
                == "hello"
            ]

        await _eventually(lambda: len(_hello_frames()) >= 3)
        hello_messages = [
            decode_control_payload(frame.payload)
            for frame in _hello_frames()
        ]
        assert {
            message["client_nonce"] for message in hello_messages
        } == {session.client_nonce}
        assert session.client_nonce != 0

        fake.inject(
            _hello_ack_frame(
                ack_client_nonce=session.client_nonce,
                sequence=100,
            )
        )
        first = await session.wait_ready(timeout=1.0)
        assert first.session_epoch == 42
        assert len(ready) == 1

        fake.inject(
            _hello_ack_frame(
                ack_client_nonce=session.client_nonce,
                sequence=101,
            )
        )
        await asyncio.sleep(0.05)
        assert session.is_ready
        assert not session.is_closed
        assert len(ready) == 1
        assert session.hello_info is first
        await session.close()

    asyncio.run(_run())


def test_hello_ack_nonce_mismatch_fails_closed():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_NONCE_MISMATCH",
            fake,
            generation=10,
            hello_interval=0.05,
            hello_timeout=1.0,
        )
        await session.start()
        await _eventually(lambda: bool(_decode_writes(fake)))
        wrong_nonce = (
            session.client_nonce + 1
            if session.client_nonce < 0xFFFFFFFF
            else 1
        )
        fake.inject(
            _hello_ack_frame(
                ack_client_nonce=wrong_nonce,
                sequence=102,
            )
        )
        await asyncio.wait_for(session.wait_closed(), timeout=1.0)
        assert isinstance(session.last_error, SessionHandshakeError)
        assert "ack_client_nonce mismatch" in str(session.last_error)

    asyncio.run(_run())


def test_session_routes_reliable_messages_and_media_to_isolated_queues():
    async def _run() -> None:
        fake = FakeSerial()
        received = []
        session = await _start_ready_session(
            fake,
            on_frame=lambda _session, frame: received.append(frame),
        )
        control = {"type": "audio_end", "request_id": "r1"}
        pb_ack = {
            "type": "pb_ack",
            "phase": "accepted",
            "req": "pb1",
            "idx": 0,
        }
        opus = b"\x00\x02ab\x00\x03cde"
        single_opus = b"\x00\x03one"
        jpeg = _test_jpeg()
        fake.inject(
            b"".join(
                [
                    encode_frame(
                        Channel.CONTROL_JSON,
                        encode_control_payload(control),
                        sequence=20,
                        session_epoch=42,
                        flags=FrameFlag.JSON,
                    ),
                    encode_frame(
                        Channel.PB_WIRE,
                        json.dumps(pb_ack).encode(),
                        sequence=21,
                        session_epoch=42,
                        flags=FrameFlag.JSON,
                    ),
                    encode_frame(
                        Channel.AUDIO_UP_OPUS,
                        opus,
                        sequence=22,
                        session_epoch=42,
                    ),
                    encode_frame(
                        Channel.CAMERA_JPEG,
                        jpeg,
                        sequence=23,
                        session_epoch=42,
                    ),
                    encode_frame(
                        Channel.AUDIO_UP_OPUS,
                        single_opus,
                        sequence=24,
                        session_epoch=42,
                    ),
                ]
            )
        )

        messages = [
            await asyncio.wait_for(session.__anext__(), timeout=1.0)
            for _ in range(2)
        ]
        assert json.loads(messages[0]) == control
        assert json.loads(messages[1]) == pb_ack
        first_audio = await asyncio.wait_for(
            session.receive_audio_up(), timeout=1.0
        )
        second_audio = await asyncio.wait_for(
            session.receive_audio_up(), timeout=1.0
        )
        camera = await asyncio.wait_for(
            session.receive_camera_jpeg(), timeout=1.0
        )
        assert first_audio.payload == opus
        assert first_audio.opus_frames == 2
        assert first_audio.sequence == 22
        assert second_audio.payload == b"one"
        assert second_audio.opus_frames is None
        assert second_audio.sequence == 24
        assert camera.payload == jpeg
        assert camera.sequence == 23
        assert [frame.channel for frame in received] == [
            Channel.CONTROL_JSON,
            Channel.PB_WIRE,
            Channel.AUDIO_UP_OPUS,
            Channel.CAMERA_JPEG,
            Channel.AUDIO_UP_OPUS,
        ]
        await session.close()

    asyncio.run(_run())


def test_media_backpressure_drops_oldest_without_closing_session():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=1,
            hello_interval=0.05,
            hello_timeout=1.0,
            audio_queue_size=2,
            camera_queue_size=1,
        )
        await session.start()
        fake.inject(_hello_ack_frame(ack_client_nonce=session.client_nonce))
        await session.wait_ready(timeout=1.0)
        jpeg = _test_jpeg()
        fake.inject(
            b"".join(
                [
                    encode_frame(
                        Channel.AUDIO_UP_OPUS,
                        bytes([sequence]),
                        sequence=sequence,
                        session_epoch=42,
                    )
                    for sequence in range(20, 25)
                ]
                + [
                    encode_frame(
                        Channel.CAMERA_JPEG,
                        jpeg + bytes([sequence]),
                        sequence=sequence,
                        session_epoch=42,
                    )
                    for sequence in range(25, 28)
                ]
            )
        )
        deadline = time.monotonic() + 1.0
        while session.diagnostics().audio_queue_drops < 3:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)

        assert session.is_ready
        assert not session.is_closed
        assert session.audio_queue_drops == 3
        assert session.camera_queue_drops == 2
        assert [
            (await session.receive_audio_up()).sequence,
            (await session.receive_audio_up()).sequence,
        ] == [23, 24]
        assert (await session.receive_camera_jpeg()).sequence == 27
        await session.close()

    asyncio.run(_run())


def test_application_queue_overflow_never_closes_usb_session():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=1,
            hello_interval=0.05,
            hello_timeout=1.0,
            message_queue_size=2,
        )
        await session.start()
        fake.inject(_hello_ack_frame(ack_client_nonce=session.client_nonce))
        await session.wait_ready(timeout=1.0)
        frames = []
        for sequence in range(20, 26):
            frames.append(
                encode_frame(
                    Channel.CONTROL_JSON,
                    encode_control_payload(
                        {
                            "type": "audio_vad",
                            "state": "speech_start",
                            "source": "esp_sr",
                            "sequence": sequence,
                        }
                    ),
                    sequence=sequence,
                    session_epoch=42,
                    flags=FrameFlag.JSON,
                )
            )
        fake.inject(b"".join(frames))
        deadline = time.monotonic() + 1.0
        while session.message_queue_overflows == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert session.is_ready
        assert not session.is_closed
        assert session.last_error is None
        await session.close()

    asyncio.run(_run())


def test_raw_receive_backlog_resynchronizes_without_failing_session():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=7,
            rx_queue_size=2,
        )
        # Simulate an event-loop pause after the reader delivered a partial
        # frame and filled the bounded raw-byte queue.
        session._decoder.feed(b"DB")
        assert session._decoder.buffered_bytes > 0
        session._rx_queue.put_nowait((7, b"stale-one"))
        session._rx_queue.put_nowait((7, b"stale-two"))

        newest = encode_frame(
            Channel.LOG,
            b"newest",
            sequence=99,
            session_epoch=42,
        )
        session._enqueue_rx_from_thread(7, newest)

        assert session.rx_queue_overflows == 1
        assert session._decoder.buffered_bytes == 0
        assert session._rx_queue.qsize() == 1
        assert session._rx_queue.get_nowait() == (7, newest)
        assert session.last_error is None
        assert not session.is_closed
        fake.close()

    asyncio.run(_run())


def test_pb_json_and_declared_binaries_stay_on_pb_channel():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        wire = json.dumps(
            {
                "type": "pb_single",
                "audio": {"next_bin_len": 3},
                "assets": [{"next_bin_len": 4}],
            }
        )
        async with session.downlink_chain():
            await session.send_pb_wire(wire)
            await session.send_pb_binary(b"pcm")
            await session.send_pb_binary(b"jpeg")

        pb_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        ]
        assert [frame.flags for frame in pb_frames] == [
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED,
            FrameFlag.ACK_REQUIRED,
            FrameFlag.ACK_REQUIRED,
        ]
        assert [frame.payload for frame in pb_frames] == [
            wire.encode(),
            b"pcm",
            b"jpeg",
        ]
        assert not any(
            frame.channel == Channel.AUDIO_DOWN_OPUS
            for frame in _decode_writes(fake)
        )
        assert session._pending_pb_binary_lengths == []

        await session.send_pb_wire(
            json.dumps(
                {
                    "type": "pb_single",
                    "audio": {"next_bin_len": 2},
                }
            )
        )
        with pytest.raises(SerialProtocolError, match="length mismatch"):
            await session.send_pb_binary(b"bad")
        assert session._pending_pb_binary_lengths == []
        await session.close()

    asyncio.run(_run())


def test_audio_down_begin_data_end_are_generation_ordered():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)

        first = await session.begin_audio_down_stream(b"first")
        assert first > 0
        assert (
            await session.send_audio_down_opus(
                b"first-tail",
                generation=first,
            )
            > 0
        )
        assert await session.send_audio_down_end(generation=first) > 0
        assert (
            await session.send_audio_down_opus(
                b"stale-after-end",
                generation=first,
            )
            == 0
        )

        second = await session.begin_audio_down_stream(b"second")
        assert second != first
        assert await session.send_audio_down_end(generation=first) == 0
        assert (
            await session.send_audio_down_opus(
                b"second-tail",
                generation=second,
            )
            > 0
        )
        assert await session.send_audio_down_end(generation=second) > 0

        audio_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.AUDIO_DOWN_OPUS
        ]
        assert [
            (frame.flags, frame.payload)
            for frame in audio_frames
        ] == [
            (FrameFlag.BEGIN_STREAM, b"first"),
            (FrameFlag.NONE, b"first-tail"),
            (FrameFlag.END_STREAM, b""),
            (FrameFlag.BEGIN_STREAM, b"second"),
            (FrameFlag.NONE, b"second-tail"),
            (FrameFlag.END_STREAM, b""),
        ]
        await session.close()

    asyncio.run(_run())


def test_audio_down_cancel_invalidates_current_generation_and_stale_writes():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)

        first = await session.begin_audio_down_stream(b"first")
        assert await session.cancel_audio_down_stream(generation=first) > 0
        assert session._audio_down_open_generation is None
        assert (
            await session.send_audio_down_opus(
                b"stale-data",
                generation=first,
            )
            == 0
        )
        assert await session.send_audio_down_end(generation=first) == 0

        second = await session.begin_audio_down_stream(b"second")
        assert second != first
        assert (
            await session.send_audio_down_opus(
                b"second-data",
                generation=second,
            )
            > 0
        )
        assert await session.send_audio_down_end(generation=second) > 0

        audio_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.AUDIO_DOWN_OPUS
        ]
        assert [
            (frame.flags, frame.payload)
            for frame in audio_frames
        ] == [
            (FrameFlag.BEGIN_STREAM, b"first"),
            (FrameFlag.CANCEL_STREAM, b""),
            (FrameFlag.BEGIN_STREAM, b"second"),
            (FrameFlag.NONE, b"second-data"),
            (FrameFlag.END_STREAM, b""),
        ]
        await session.close()

    asyncio.run(_run())


def test_stale_audio_cancel_cannot_close_a_new_generation():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)

        first = await session.begin_audio_down_stream(b"first")
        second = await session.begin_audio_down_stream(b"second")

        assert await session.cancel_audio_down_stream(generation=first) == 0
        assert session._audio_down_open_generation == second
        assert (
            await session.send_audio_down_opus(
                b"second-data",
                generation=second,
            )
            > 0
        )
        assert await session.cancel_audio_down_stream(generation=second) > 0

        audio_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.AUDIO_DOWN_OPUS
        ]
        assert [frame.flags for frame in audio_frames] == [
            FrameFlag.BEGIN_STREAM,
            FrameFlag.BEGIN_STREAM,
            FrameFlag.NONE,
            FrameFlag.CANCEL_STREAM,
        ]
        await session.close()

    asyncio.run(_run())


def test_audio_down_begin_atomically_blocks_and_drops_camera_once():
    async def _run() -> None:
        session = DeviceSession("COM_TEST", FakeSerial(), generation=1)
        begin_started = asyncio.Event()
        release_begin = asyncio.Event()
        calls: list[tuple[Channel, bytes, FrameFlag]] = []

        async def controlled_send_frame(channel, payload, **kwargs):
            normalized_channel = Channel(channel)
            flags = FrameFlag(kwargs.get("flags", FrameFlag.NONE))
            calls.append((normalized_channel, bytes(payload), flags))
            if flags & FrameFlag.BEGIN_STREAM:
                begin_started.set()
                await release_begin.wait()
            return len(calls)

        session.send_frame = controlled_send_frame  # type: ignore[method-assign]
        begin_task = asyncio.create_task(
            session.begin_audio_down_stream(b"first-opus")
        )
        await asyncio.wait_for(begin_started.wait(), timeout=1.0)

        camera_task = asyncio.create_task(
            session.send_pb_wire(
                json.dumps(
                    {
                        "type": "pb_single",
                        "req": "camera-during-begin",
                        "camera_once": True,
                    }
                )
            )
        )
        await asyncio.sleep(0)
        assert not camera_task.done()

        release_begin.set()
        generation = await begin_task
        assert generation > 0
        assert await camera_task == 0
        assert calls == [
            (
                Channel.AUDIO_DOWN_OPUS,
                b"first-opus",
                FrameFlag.BEGIN_STREAM,
            )
        ]

    asyncio.run(_run())


def test_camera_once_atomically_finishes_before_audio_down_begin():
    async def _run() -> None:
        session = DeviceSession("COM_TEST", FakeSerial(), generation=1)
        camera_started = asyncio.Event()
        release_camera = asyncio.Event()
        calls: list[tuple[Channel, bytes, FrameFlag]] = []

        async def controlled_send_frame(channel, payload, **kwargs):
            normalized_channel = Channel(channel)
            flags = FrameFlag(kwargs.get("flags", FrameFlag.NONE))
            calls.append((normalized_channel, bytes(payload), flags))
            if normalized_channel == Channel.PB_WIRE:
                camera_started.set()
                await release_camera.wait()
            return len(calls)

        session.send_frame = controlled_send_frame  # type: ignore[method-assign]
        camera_task = asyncio.create_task(
            session.send_pb_wire(
                json.dumps(
                    {
                        "type": "pb_single",
                        "req": "camera-before-begin",
                        "camera_once": True,
                    }
                )
            )
        )
        await asyncio.wait_for(camera_started.wait(), timeout=1.0)

        begin_task = asyncio.create_task(
            session.begin_audio_down_stream(b"first-opus")
        )
        await asyncio.sleep(0)
        assert not begin_task.done()

        release_camera.set()
        assert await camera_task > 0
        assert await begin_task > 0
        assert [channel for channel, _payload, _flags in calls] == [
            Channel.PB_WIRE,
            Channel.AUDIO_DOWN_OPUS,
        ]
        assert calls[0][2] == FrameFlag.JSON
        assert calls[1][2] == FrameFlag.BEGIN_STREAM

    asyncio.run(_run())


def test_camera_once_resumes_after_audio_down_end():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        generation = await session.begin_audio_down_stream(b"first-opus")
        assert await session.send_audio_down_end(generation=generation) > 0

        camera_wire = json.dumps(
            {
                "type": "pb_single",
                "req": "camera-after-end",
                "camera_once": True,
            }
        )
        assert await session.send_pb_wire(camera_wire) > 0

        relevant = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel in {Channel.AUDIO_DOWN_OPUS, Channel.PB_WIRE}
        ]
        assert [frame.channel for frame in relevant] == [
            Channel.AUDIO_DOWN_OPUS,
            Channel.AUDIO_DOWN_OPUS,
            Channel.PB_WIRE,
        ]
        assert [frame.flags for frame in relevant] == [
            FrameFlag.BEGIN_STREAM,
            FrameFlag.END_STREAM,
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED,
        ]
        assert relevant[-1].payload == camera_wire.encode()
        await session.close()

    asyncio.run(_run())


def test_cancelled_audio_begin_keeps_camera_blocked_after_write_is_queued():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        started, release, finished = _block_next_audio_write(
            session,
            FrameFlag.BEGIN_STREAM,
        )

        begin_task = asyncio.create_task(
            session.begin_audio_down_stream(b"first-opus")
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        begin_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(begin_task, timeout=1.0)

        active_generation = session._audio_down_open_generation
        assert active_generation is not None
        assert (
            await session.send_pb_wire(
                json.dumps(
                    {
                        "type": "pb_single",
                        "req": "camera-after-cancelled-begin",
                        "camera_once": True,
                    }
                )
            )
            == 0
        )

        release.set()
        assert await asyncio.to_thread(finished.wait, 1.0)
        relevant = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel in {Channel.AUDIO_DOWN_OPUS, Channel.PB_WIRE}
        ]
        assert [frame.channel for frame in relevant] == [
            Channel.AUDIO_DOWN_OPUS,
        ]
        assert relevant[0].flags == FrameFlag.BEGIN_STREAM
        await session.send_audio_down_end(generation=active_generation)
        await session.close()

    asyncio.run(_run())


def test_cancelled_audio_end_allows_camera_only_after_queued_end():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        generation = await session.begin_audio_down_stream(b"first-opus")
        started, release, finished = _block_next_audio_write(
            session,
            FrameFlag.END_STREAM,
        )

        end_task = asyncio.create_task(
            session.send_audio_down_end(generation=generation)
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        end_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(end_task, timeout=1.0)
        assert session._audio_down_open_generation is None

        camera_wire = json.dumps(
            {
                "type": "pb_single",
                "req": "camera-after-cancelled-end",
                "camera_once": True,
            }
        )
        camera_task = asyncio.create_task(session.send_pb_wire(camera_wire))
        await asyncio.sleep(0)
        assert not camera_task.done()

        release.set()
        assert await asyncio.to_thread(finished.wait, 1.0)
        assert await asyncio.wait_for(camera_task, timeout=1.0) > 0
        relevant = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel in {Channel.AUDIO_DOWN_OPUS, Channel.PB_WIRE}
        ]
        assert [frame.channel for frame in relevant] == [
            Channel.AUDIO_DOWN_OPUS,
            Channel.AUDIO_DOWN_OPUS,
            Channel.PB_WIRE,
        ]
        assert [frame.flags for frame in relevant] == [
            FrameFlag.BEGIN_STREAM,
            FrameFlag.END_STREAM,
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED,
        ]
        await session.close()

    asyncio.run(_run())


def test_large_pb_binary_is_fragmented_without_changing_logical_length():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        payload = bytes(range(251)) * 21
        wire = json.dumps(
            {
                "type": "pb_single",
                "audio": {"next_bin_len": len(payload)},
            }
        )

        await session.send_pb_wire(wire)
        last_sequence = await session.send_pb_binary(payload)

        pb_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        ]
        fragments = pb_frames[1:]
        assert pb_frames[0].flags == (
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED
        )
        assert pb_frames[0].payload == wire.encode()
        assert all(
            frame.flags == FrameFlag.ACK_REQUIRED for frame in fragments
        )
        assert all(
            0 < len(frame.payload) <= PB_BINARY_FRAGMENT_BYTES
            for frame in fragments
        )
        assert [len(frame.payload) for frame in fragments] == [
            min(PB_BINARY_FRAGMENT_BYTES, len(payload) - offset)
            for offset in range(0, len(payload), PB_BINARY_FRAGMENT_BYTES)
        ]
        assert b"".join(frame.payload for frame in fragments) == payload
        assert last_sequence == fragments[-1].sequence
        assert session._pending_pb_binary_lengths == []
        await session.close()

    asyncio.run(_run())


def test_pb_binary_waits_for_transport_ack_before_next_fragment():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        payload = b"x" * (PB_BINARY_FRAGMENT_BYTES * 2 + 17)
        await session.send_pb_wire(
            json.dumps(
                {
                    "type": "pb_single",
                    "audio": {"next_bin_len": len(payload)},
                }
            )
        )
        fake.auto_frame_ack = False

        def fragments():
            return [
                frame
                for frame in _decode_writes(fake)
                if (
                    frame.channel == Channel.PB_WIRE
                    and frame.flags & FrameFlag.ACK_REQUIRED
                    and not frame.flags & FrameFlag.JSON
                )
            ]

        def inject_ack(frame, device_sequence: int) -> None:
            fake.inject(
                encode_frame(
                    Channel.CONTROL_JSON,
                    encode_control_payload(
                        {
                            "type": "frame_ack",
                            "session_epoch": frame.session_epoch,
                            "ack_sequence": frame.sequence,
                            "ack_channel": int(frame.channel),
                        }
                    ),
                    sequence=device_sequence,
                    session_epoch=frame.session_epoch,
                    flags=FrameFlag.ACK,
                )
            )

        sending = asyncio.create_task(session.send_pb_binary(payload))
        await _eventually(lambda: len(fragments()) == 1)
        await asyncio.sleep(0.02)
        assert len(fragments()) == 1

        inject_ack(fragments()[0], 2001)
        await _eventually(lambda: len(fragments()) == 2)
        await asyncio.sleep(0.02)
        assert len(fragments()) == 2

        inject_ack(fragments()[1], 2002)
        await _eventually(lambda: len(fragments()) == 3)
        assert not sending.done()
        inject_ack(fragments()[2], 2003)
        assert await sending == fragments()[2].sequence
        await session.close()

    asyncio.run(_run())


def test_pb_binary_declaration_waits_for_transport_ack():
    async def _run() -> None:
        fake = FakeSerial(auto_frame_ack=False)
        session = await _start_ready_session(fake)
        wire = json.dumps(
            {
                "type": "pb_single",
                "audio": {"next_bin_len": 3},
            }
        )

        sending = asyncio.create_task(session.send_pb_wire(wire))
        await _eventually(
            lambda: any(
                frame.channel == Channel.PB_WIRE
                for frame in _decode_writes(fake)
            )
        )
        declaration = next(
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        )
        assert declaration.flags == (
            FrameFlag.JSON | FrameFlag.ACK_REQUIRED
        )
        assert not sending.done()
        assert session._pending_pb_binary_lengths == []

        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "frame_ack",
                        "session_epoch": declaration.session_epoch,
                        "ack_sequence": declaration.sequence,
                        "ack_channel": int(declaration.channel),
                    }
                ),
                sequence=2000,
                session_epoch=declaration.session_epoch,
                flags=FrameFlag.ACK,
            )
        )
        assert await sending == declaration.sequence
        assert session._pending_pb_binary_lengths == [3]
        await session.close()

    asyncio.run(_run())


def test_ack_required_frame_rejects_legacy_firmware_without_capability():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=1,
            hello_interval=0.05,
            hello_timeout=1.0,
        )
        await session.start()
        legacy_ack = encode_frame(
            Channel.CONTROL_JSON,
            encode_control_payload(
                {
                    "type": "hello_ack",
                    "protocol": 1,
                    "ack_client_nonce": session.client_nonce,
                    "device_id": "deskbot_123456abcdef",
                    "product": "Deskbot",
                    "firmware": "legacy",
                    "session_epoch": 42,
                    "heartbeat_ms": 100,
                    "timeout_ms": 600,
                    "max_payload": 1024 * 1024,
                    "capabilities": ["control_json", "pb_wire"],
                }
            ),
            sequence=10,
            session_epoch=42,
            flags=FrameFlag.JSON,
        )
        fake.inject(legacy_ack)
        await session.wait_ready(timeout=1.0)
        with pytest.raises(
            SessionHandshakeError,
            match="does not advertise 'frame_ack'",
        ):
            await session.send_frame(
                Channel.PB_WIRE,
                b"fragment",
                require_ack=True,
            )
        await session.close()

    asyncio.run(_run())


def test_wait_ready_surfaces_session_failure_without_waiting_for_timeout():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TEST",
            fake,
            generation=1,
            hello_interval=0.05,
            hello_timeout=10.0,
        )
        await session.start()
        failure = RuntimeError("serial reader failed")
        waiting = asyncio.create_task(session.wait_ready(timeout=10.0))
        await asyncio.sleep(0)
        await session._fail(failure)
        with pytest.raises(RuntimeError, match="serial reader failed") as exc:
            await asyncio.wait_for(waiting, timeout=0.5)
        assert exc.value is failure

    asyncio.run(_run())


def test_pb_json_cannot_interleave_between_binary_fragments():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        payload = b"x" * (PB_BINARY_FRAGMENT_BYTES * 2 + 7)
        await session.send_pb_wire(
            json.dumps(
                {
                    "type": "pb_single",
                    "audio": {"next_bin_len": len(payload)},
                }
            )
        )

        calls: list[tuple[Channel, bytes, FrameFlag]] = []
        first_fragment_started = asyncio.Event()
        release_first_fragment = asyncio.Event()

        async def controlled_send_frame(channel, frame_payload, **kwargs):
            normalized_channel = Channel(channel)
            flags = FrameFlag(kwargs.get("flags", FrameFlag.NONE))
            calls.append((normalized_channel, bytes(frame_payload), flags))
            if (
                normalized_channel == Channel.PB_WIRE
                and flags == FrameFlag.NONE
                and not first_fragment_started.is_set()
            ):
                first_fragment_started.set()
                await release_first_fragment.wait()
            return len(calls)

        session.send_frame = controlled_send_frame  # type: ignore[method-assign]
        binary_task = asyncio.create_task(session.send_pb_binary(payload))
        await asyncio.wait_for(first_fragment_started.wait(), timeout=1.0)
        json_task = asyncio.create_task(
            session.send_pb_wire(
                json.dumps(
                    {
                        "type": "pb_single",
                        "anim": [{"elements": {}, "ms": 1}],
                    }
                )
            )
        )
        await asyncio.sleep(0)
        assert not json_task.done()
        release_first_fragment.set()
        await binary_task
        await json_task

        assert [flags for _channel, _payload, flags in calls] == [
            FrameFlag.NONE,
            FrameFlag.NONE,
            FrameFlag.NONE,
            FrameFlag.JSON,
        ]
        assert b"".join(
            frame_payload
            for _channel, frame_payload, flags in calls
            if flags == FrameFlag.NONE
        ) == payload
        await session.close()

    asyncio.run(_run())


def test_pb_cancel_interrupts_remaining_binary_fragments_out_of_band():
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        payload = b"x" * (PB_BINARY_FRAGMENT_BYTES * 3)
        await session.send_pb_wire(
            json.dumps(
                {
                    "type": "pb_single",
                    "req": "cancel-fragments",
                    "audio": {"next_bin_len": len(payload)},
                }
            )
        )

        calls: list[tuple[Channel, bytes, FrameFlag]] = []
        first_fragment_started = asyncio.Event()
        release_first_fragment = asyncio.Event()

        async def controlled_send_frame(channel, frame_payload, **kwargs):
            normalized_channel = Channel(channel)
            flags = FrameFlag(kwargs.get("flags", FrameFlag.NONE))
            calls.append((normalized_channel, bytes(frame_payload), flags))
            if (
                normalized_channel == Channel.PB_WIRE
                and flags == FrameFlag.NONE
                and not first_fragment_started.is_set()
            ):
                first_fragment_started.set()
                await release_first_fragment.wait()
            return len(calls)

        session.send_frame = controlled_send_frame  # type: ignore[method-assign]
        binary_task = asyncio.create_task(session.send_pb_binary(payload))
        await asyncio.wait_for(first_fragment_started.wait(), timeout=1.0)

        cancel = {
            "type": "pb_cancel",
            "req": "cancel-fragments",
        }
        await session.send_pb_wire(json.dumps(cancel))
        release_first_fragment.set()
        with pytest.raises(PBTransmissionCancelled):
            await binary_task

        pb_binary_calls = [
            frame_payload
            for channel, frame_payload, flags in calls
            if channel == Channel.PB_WIRE and flags == FrameFlag.NONE
        ]
        control_calls = [
            json.loads(frame_payload)
            for channel, frame_payload, flags in calls
            if channel == Channel.CONTROL_JSON and flags & FrameFlag.JSON
        ]
        assert pb_binary_calls == [payload[:PB_BINARY_FRAGMENT_BYTES]]
        assert control_calls == [cancel]
        assert session._pending_pb_binary_lengths == []
        await session.close()

    asyncio.run(_run())


def test_pb_failed_declaration_and_cancel_do_not_leave_binary_lengths():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession("COM_FAIL", fake, generation=1)
        sent = []

        async def fail_send_frame(*_args, **_kwargs):
            raise OSError("write failed")

        session.send_frame = fail_send_frame  # type: ignore[method-assign]
        wire = json.dumps(
            {
                "type": "pb_single",
                "audio": {"next_bin_len": 3},
            }
        )
        with pytest.raises(OSError, match="write failed"):
            await session.send_pb_wire(wire)
        assert session._pending_pb_binary_lengths == []

        async def record_send_frame(channel, payload, **kwargs):
            sent.append((channel, bytes(payload), kwargs))
            return len(sent)

        session.send_frame = record_send_frame  # type: ignore[method-assign]
        await session.send_pb_wire(wire)
        assert session._pending_pb_binary_lengths == [3]
        await session.send_pb_wire(json.dumps({"type": "pb_cancel"}))
        assert session._pending_pb_binary_lengths == []
        assert sent[-1][0] == Channel.CONTROL_JSON
        assert sent[-1][2]["flags"] == FrameFlag.JSON

    asyncio.run(_run())


def test_ws_downlink_adapter_over_device_session_orders_pb_frames():
    """WsDownlinkAdapter 包裹 DeviceSession（USB 桥）：pb JSON + binaries 保序。

    原 SerialDownlinkAdapter 已删；USB 下行统一走 WsDownlinkAdapter，
    经 ``DeviceSession.send`` 路由到 PB_WIRE / PB binary / CONTROL_JSON。
    """

    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        adapter = WsDownlinkAdapter(
            session,
            settings=object(),  # type: ignore[arg-type]
            device_id="deskbot_123456abcdef",
            dp_broker=None,
        )
        assert adapter.half_duplex_media_mic is True
        pcm = bytes(bytearray(b"pcm"))
        asset = b"jpeg"
        wire = json.dumps(
            {
                "type": "pb_single",
                "audio": {"next_bin_len": 3},
                "assets": [{"next_bin_len": 4}],
            }
        )
        async with adapter.pb_serial_chain():
            ok = await asyncio.wait_for(
                adapter.send_pb_wire(
                    wire,
                    binaries=[asset],
                    pcm=pcm,
                ),
                timeout=1.0,
            )
            deduped_ok = await asyncio.wait_for(
                adapter.send_pb_wire(
                    wire,
                    binaries=[pcm, asset],
                    pcm=pcm,
                ),
                timeout=1.0,
            )
            cancelled = await asyncio.wait_for(
                adapter.cancel_pb_playback("cancel-nested-chain"),
                timeout=1.0,
            )
        assert ok is True
        assert deduped_ok is True
        assert cancelled is True
        assert session._pending_pb_binary_lengths == []
        pb_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.PB_WIRE
        ]
        assert [frame.payload for frame in pb_frames] == [
            wire.encode(),
            pcm,
            asset,
            wire.encode(),
            pcm,
            asset,
        ]
        control_frames = [
            frame
            for frame in _decode_writes(fake)
            if frame.channel == Channel.CONTROL_JSON
        ]
        assert json.loads(control_frames[-1].payload)["type"] == "pb_cancel"
        assert json.loads(control_frames[-1].payload)["req"] == "cancel-nested-chain"
        await session.close()

    asyncio.run(_run())


def test_session_fails_closed_when_hello_never_arrives():
    async def _run() -> None:
        fake = FakeSerial()
        session = DeviceSession(
            "COM_TIMEOUT",
            fake,
            generation=1,
            hello_interval=0.02,
            hello_timeout=0.08,
        )
        await session.start()
        await asyncio.wait_for(session.wait_closed(), timeout=1.0)
        assert isinstance(session.last_error, TimeoutError)

    asyncio.run(_run())


def test_session_end_control_message_disconnects_immediately():
    """Firmware's best-effort goodbye must not wait for the heartbeat window."""

    fake = FakeSerial()

    async def _run() -> DeviceSession:
        session = await _start_ready_session(fake)
        assert not session.is_closed
        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "session_end",
                        "session_epoch": 42,
                        "reason": "payload_crc_mismatch",
                    }
                ),
                sequence=11,
                session_epoch=42,
            )
        )
        await asyncio.wait_for(session.wait_closed(), timeout=1.0)
        return session

    session = asyncio.run(_run())
    assert session.is_closed
    assert isinstance(session.last_error, ConnectionError)
    assert "payload_crc_mismatch" in str(session.last_error)


def test_stale_epoch_session_end_cannot_close_current_session():
    fake = FakeSerial()

    async def _run() -> None:
        session = await _start_ready_session(fake)
        fake.inject(
            encode_frame(
                Channel.CONTROL_JSON,
                encode_control_payload(
                    {
                        "type": "session_end",
                        "session_epoch": 41,
                        "reason": "payload_crc_mismatch",
                    }
                ),
                sequence=11,
                session_epoch=41,
            )
        )
        await _eventually(lambda: session.stale_epoch_frames >= 1)
        assert not session.is_closed
        assert session.last_error is None
        await session.close()

    asyncio.run(_run())
