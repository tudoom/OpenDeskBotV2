from __future__ import annotations

import asyncio

import pytest

from deskbot_server.infrastructure.serial.manager import (
    SerialDeviceManager,
    SerialManagerConfig,
)
from deskbot_server.infrastructure.serial.protocol import (
    Channel,
    encode_frame,
)
from tests.test_usb_serial_session import FakeSerial, _start_ready_session


@pytest.mark.parametrize(
    ("channel", "payload"),
    [
        (Channel.LOG, b"cpu_stats still alive"),
        (Channel.CAMERA_JPEG, b"background-camera-frame"),
    ],
)
def test_background_worker_frames_do_not_mask_dead_device_loop(
    channel: Channel,
    payload: bytes,
) -> None:
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        sequence = 4000
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.85

        while loop.time() < deadline and not session.is_closed:
            fake.inject(
                encode_frame(
                    channel,
                    payload,
                    sequence=sequence,
                    session_epoch=42,
                )
            )
            sequence += 1
            await asyncio.sleep(0.08)

        await asyncio.wait_for(session.wait_closed(), timeout=0.5)
        assert isinstance(session.last_error, TimeoutError)
        snapshot = session.diagnostics()
        assert snapshot.last_valid_channel == channel.name
        assert snapshot.last_any_rx_age_seconds is not None
        assert snapshot.last_any_rx_age_seconds < 0.3
        assert snapshot.last_interactive_channel == "CONTROL_JSON"
        assert snapshot.last_interactive_age_seconds is not None
        assert snapshot.last_interactive_age_seconds >= 0.5
        assert "last_interactive_channel=CONTROL_JSON" in str(
            session.last_error
        )

    asyncio.run(_run())


def test_in_progress_large_frame_defers_heartbeat_timeout() -> None:
    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        encoded = encode_frame(
            Channel.CAMERA_JPEG,
            b"x" * 100_000,
            sequence=5000,
            session_epoch=42,
        )
        # Feed a valid header and an intentionally incomplete payload for
        # longer than the negotiated 600 ms interactive timeout.
        offset = 0
        deadline = asyncio.get_running_loop().time() + 0.85
        while asyncio.get_running_loop().time() < deadline:
            next_offset = min(len(encoded) - 1, offset + 1024)
            fake.inject(encoded[offset:next_offset])
            offset = next_offset
            await asyncio.sleep(0.05)

        assert session.is_closed is False
        snapshot = session.diagnostics()
        assert snapshot.last_any_rx_age_seconds is not None
        assert snapshot.last_any_rx_age_seconds < 0.2
        assert snapshot.decoder_buffered_bytes > 0
        await session.close()

    asyncio.run(_run())


def test_incomplete_frame_cannot_keep_corrupt_stream_alive_forever(
    monkeypatch,
) -> None:
    from deskbot_server.infrastructure.serial import session as session_module

    monkeypatch.setattr(
        session_module,
        "INCOMPLETE_FRAME_STALL_SECONDS",
        0.30,
    )

    async def _run() -> None:
        fake = FakeSerial()
        session = await _start_ready_session(fake)
        encoded = encode_frame(
            Channel.CAMERA_JPEG,
            b"x" * 100_000,
            sequence=6000,
            session_epoch=42,
        )
        offset = 0
        deadline = asyncio.get_running_loop().time() + 0.65
        while (
            asyncio.get_running_loop().time() < deadline
            and not session.is_closed
        ):
            next_offset = min(len(encoded) - 1, offset + 512)
            fake.inject(encoded[offset:next_offset])
            offset = next_offset
            await asyncio.sleep(0.04)

        await asyncio.wait_for(session.wait_closed(), timeout=0.5)
        assert isinstance(session.last_error, TimeoutError)
        assert "decoder_buffered_bytes=" in str(session.last_error)

    asyncio.run(_run())


def test_failed_probe_retries_use_bounded_exponential_backoff() -> None:
    async def _run() -> None:
        manager = SerialDeviceManager(
            SerialManagerConfig(
                reconnect_delay=1.0,
                reconnect_max_delay=8.0,
            )
        )
        assert [
            manager._schedule_retry("COM4", failed_probe=True)
            for _ in range(6)
        ] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
        assert manager._probe_failures["COM4"] == 6

        assert manager._schedule_retry("COM4", failed_probe=False) == 1.0
        assert "COM4" not in manager._probe_failures

    asyncio.run(_run())
