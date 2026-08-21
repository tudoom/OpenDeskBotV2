from __future__ import annotations

import asyncio

import numpy as np

from deskbot_server.pipeline.mic_health import (
    MIC_HEALTH_CHECKING,
    MIC_HEALTH_OK,
    MIC_NO_ACOUSTIC_SIGNAL,
    MicrophoneHealthConfig,
    MicrophoneHealthMonitor,
)


def _config() -> MicrophoneHealthConfig:
    return MicrophoneHealthConfig(
        analysis_frame_ms=100,
        flat_window_ms=1_000,
        flat_ratio=0.98,
        flat_ac_rms_max=96.0,
        flat_variation_max=256.0,
        recovery_window_ms=500,
        recovery_active_ms=300,
    )


def _tone(*, sample_rate: int = 16_000, duration_ms: int = 100) -> bytes:
    count = sample_rate * duration_ms // 1_000
    t = np.arange(count, dtype=np.float32)
    return (
        np.sin(2.0 * np.pi * 220.0 * t / sample_rate) * 1_200.0
    ).astype("<i2").tobytes()


def test_long_received_flat_pcm_reports_no_acoustic_signal():
    monitor = MicrophoneHealthMonitor(_config())
    flat = np.full(1_600, 1_200, dtype="<i2").tobytes()

    assert monitor.status == MIC_HEALTH_CHECKING
    # No received PCM means no elapsed diagnostic time.
    monitor.feed_pcm(b"")
    assert monitor.status == MIC_HEALTH_CHECKING

    for _ in range(10):
        monitor.feed_pcm(flat)

    assert monitor.status == MIC_NO_ACOUSTIC_SIGNAL
    update = monitor.consume_update()
    assert update is not None
    assert update["status"] == MIC_NO_ACOUSTIC_SIGNAL
    assert update["observed_audio_ms"] == 1_000
    assert update["ac_rms"] == 0.0
    assert update["short_term_variation"] == 0.0
    assert monitor.consume_update() is None


def test_valid_acoustic_change_recovers_without_accepting_one_impulse():
    monitor = MicrophoneHealthMonitor(_config())
    silence = b"\x00\x00" * 1_600
    for _ in range(10):
        monitor.feed_pcm(silence)
    assert monitor.status == MIC_NO_ACOUSTIC_SIGNAL
    monitor.consume_update()

    impulse = np.zeros(1_600, dtype="<i2")
    impulse[0] = 30_000
    monitor.feed_pcm(impulse.tobytes())
    assert monitor.status == MIC_NO_ACOUSTIC_SIGNAL

    for _ in range(3):
        monitor.feed_pcm(_tone())
    assert monitor.status == MIC_HEALTH_OK
    update = monitor.consume_update()
    assert update is not None
    assert update["status"] == MIC_HEALTH_OK
    assert float(update["ac_rms"]) > 96.0
    assert float(update["short_term_variation"]) > 256.0


def test_connection_session_observes_decoded_pcm_without_changing_feed_contract(
    monkeypatch,
):
    from deskbot_server.pipeline import audio

    session = audio.ConnectionSession(
        object(),
            audio.AudioConfig(
                input_codec="pcm16",
                sample_rate=16_000,
                channels=1,
            ),
    )
    session._microphone_health = MicrophoneHealthMonitor(_config())
    flat = b"\x00\x00" * 1_600

    async def _run() -> None:
        for index in range(10):
            result = await session.feed_audio(flat, "pcm16")
            assert len(result) == 3
            assert result[0] == []
            assert result[1] is (index == 0)
            assert result[2] is False

    asyncio.run(_run())
    update = session.consume_microphone_health_update()
    assert update is not None
    assert update["status"] == MIC_NO_ACOUSTIC_SIGNAL


def test_registry_exposes_bounded_microphone_health_snapshot():
    from deskbot_server.ws.registry import DeviceRegistry

    async def _run() -> None:
        registry = DeviceRegistry()
        connection = object()
        await registry.connect(
            "deskbot_mic_health",
            "usb_cdc",
            connection,
            transport="usb_cdc",
        )
        assert await registry.set_microphone_health(
            "deskbot_mic_health",
            {
                "status": MIC_NO_ACOUSTIC_SIGNAL,
                "observed_audio_ms": 20_100,
                "window_audio_ms": 20_000,
                "ac_rms": 12.345,
                "short_term_variation": 31.234,
                "frame_count": 201,
                "ignored": "do not expose arbitrary fields",
            },
        )

        health = registry.snapshot()[0]["microphone_health"]
        assert health["status"] == MIC_NO_ACOUSTIC_SIGNAL
        assert health["observed_audio_ms"] == 20_100
        assert health["ac_rms"] == 12.35
        assert health["short_term_variation"] == 31.23
        assert "ignored" not in health

        assert not await registry.set_microphone_health(
            "deskbot_mic_health",
            {"status": "not-a-real-state"},
        )

    asyncio.run(_run())
