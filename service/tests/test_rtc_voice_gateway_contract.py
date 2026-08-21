from __future__ import annotations

import asyncio
import sys
from array import array
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def test_playback_barge_in_quality_gate_keeps_idle_short_turns_possible():
    from deskbot_server.rtc_barge_in import (
        is_explicit_barge_in,
        should_confirm_barge_in,
    )

    # These helpers are consulted only while playback is active. The normal
    # idle STT path still accepts short turns such as "你好" unchanged.
    assert should_confirm_barge_in("你好") is False
    assert should_confirm_barge_in("嗯") is False
    assert should_confirm_barge_in("你喜欢什么") is False
    assert should_confirm_barge_in(
        "你喜欢什么", speech_seconds=1.1
    ) is True
    assert should_confirm_barge_in(
        "你喜欢什么", speech_seconds=0.4
    ) is False
    assert should_confirm_barge_in("停一下") is True
    assert should_confirm_barge_in("等等") is True
    assert is_explicit_barge_in("please stop") is True
    assert is_explicit_barge_in("你好") is False


def test_worker_final_transcript_does_not_repeat_duration_gate():
    """The STT adapter owns duration validation; final events lack duration."""

    source = Path(
        "service/src/deskbot_server/rtc_livekit_plugins.py"
    ).read_text(encoding="utf-8")
    sdk_source = Path(
        "service/src/deskbot_server/rtc_agent_sdk.py"
    ).read_text(encoding="utf-8")

    assert "confirmed_barge_in = bool(playback_recent)" in source
    assert "confirmed_barge_in = bool(playback_recent and not is_echo)" in sdk_source
    assert "and should_confirm_barge_in(transcript)" not in source
    assert "and should_confirm_barge_in(transcript)" not in sdk_source


def test_rtc_mode_signal_is_media_only_pb():
    from deskbot_server.pb.mic_signal import build_rtc_mode_signal_pb

    stable = build_rtc_mode_signal_pb(mode="stable", req="rtc-stable")
    interruptible = build_rtc_mode_signal_pb(
        mode="interruptible",
        req="rtc-interrupt",
    )
    assert stable["type"] == interruptible["type"] == "pb_single"
    assert stable["rtc_mode"] == "stable"
    assert interruptible["rtc_mode"] == "interruptible"
    assert "mic" not in stable
    assert "audio" not in stable
    assert "servo" not in stable
    assert "anim" not in stable


def test_rtc_aec_mode_fails_safe_without_device_capability():
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import (
        RtcDeviceCapabilities,
        RtcDeviceSession,
    )

    async def _sink(_pcm: bytes, _sample_rate: int, _channels: int) -> None:
        return None

    session = RtcDeviceSession(
        "dev-no-aec",
        RtcSettings(call_mode="esp32_aec"),
        playback_sink=_sink,
        capabilities=RtcDeviceCapabilities(aec=False, full_duplex=True),
    )
    assert session.effective_call_mode == "stable"


def test_rtc_aec_mode_activates_only_with_complete_device_frontend():
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import (
        RtcDeviceCapabilities,
        RtcDeviceSession,
    )

    async def _sink(_pcm: bytes, _sample_rate: int, _channels: int) -> None:
        return None

    capabilities = RtcDeviceCapabilities(
        aec=True,
        ns=True,
        vad=True,
        full_duplex=True,
    )
    session = RtcDeviceSession(
        "dev-esp-sr",
        RtcSettings(call_mode="esp32_aec"),
        playback_sink=_sink,
        capabilities=capabilities,
    )
    assert session.effective_call_mode == "esp32_aec"
    assert session.capabilities.ns is True
    assert session.capabilities.vad is True


def test_rtc_recovery_is_single_flight_and_retries_with_backoff(monkeypatch):
    from deskbot_server import rtc_gateway
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import (
        RtcDeviceCapabilities,
        RtcDeviceSession,
    )

    async def _sink(_pcm: bytes, _sample_rate: int, _channels: int) -> None:
        return None

    async def _run() -> None:
        monkeypatch.setattr(
            rtc_gateway, "_RECOVERY_MIN_COOLDOWN_SECONDS", 0.0
        )
        monkeypatch.setattr(
            rtc_gateway, "_RECOVERY_MAX_BACKOFF_SECONDS", 0.01
        )
        session = RtcDeviceSession(
            "recover-single-flight",
            RtcSettings(),
            playback_sink=_sink,
            capabilities=RtcDeviceCapabilities(),
        )
        room = object()
        session.room = room
        session.connected = True
        attempts = 0

        async def _connect() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("temporary")
            session.connected = True

        monkeypatch.setattr(session, "connect", _connect)
        session._schedule_recovery(room, "test")
        first = session._recovery_task
        assert first is not None
        session._schedule_recovery(room, "duplicate")
        assert session._recovery_task is first
        await asyncio.wait_for(first, timeout=1.0)
        assert attempts == 3
        assert session.connected is True
        assert session._recovery_attempt == 0
        await session.close()

    asyncio.run(_run())


def test_barge_in_control_accepts_only_reliable_agent_packet():
    from deskbot_server.rtc_barge_in import BARGE_IN_TOPIC
    from deskbot_server.rtc_gateway import _is_confirmed_barge_in_packet

    rtc = SimpleNamespace(
        DataPacketKind=SimpleNamespace(KIND_LOSSY=0, KIND_RELIABLE=1),
        ParticipantKind=SimpleNamespace(PARTICIPANT_KIND_AGENT=4),
    )
    agent = SimpleNamespace(kind=4)
    packet = SimpleNamespace(
        topic=BARGE_IN_TOPIC,
        data=b"cancel",
        kind=1,
        participant=agent,
    )

    assert _is_confirmed_barge_in_packet(packet, rtc) is True
    assert _is_confirmed_barge_in_packet(
        SimpleNamespace(**{**vars(packet), "kind": 0}), rtc
    ) is False
    assert _is_confirmed_barge_in_packet(
        SimpleNamespace(
            **{**vars(packet), "participant": SimpleNamespace(kind=0)}
        ),
        rtc,
    ) is False
    assert _is_confirmed_barge_in_packet(
        SimpleNamespace(**{**vars(packet), "data": b"other"}), rtc
    ) is False


def test_speech_activity_accepts_only_fixed_reliable_agent_packets():
    from deskbot_server.rtc_barge_in import (
        SPEECH_ACTIVITY_TOPIC,
        USER_SPEECH_CONFIRMED_END_PAYLOAD,
        USER_SPEECH_START_PAYLOAD,
    )
    from deskbot_server.rtc_gateway import _confirmed_speech_activity

    rtc = SimpleNamespace(
        DataPacketKind=SimpleNamespace(KIND_LOSSY=0, KIND_RELIABLE=1),
        ParticipantKind=SimpleNamespace(PARTICIPANT_KIND_AGENT=4),
    )
    agent = SimpleNamespace(kind=4)

    def _packet(payload, *, kind=1, participant=agent):
        return SimpleNamespace(
            topic=SPEECH_ACTIVITY_TOPIC,
            data=payload,
            kind=kind,
            participant=participant,
        )

    assert (
        _confirmed_speech_activity(_packet(USER_SPEECH_START_PAYLOAD), rtc)
        == "user_speech_start"
    )
    assert (
        _confirmed_speech_activity(
            _packet(USER_SPEECH_CONFIRMED_END_PAYLOAD),
            rtc,
        )
        == "user_speech_confirmed_end"
    )
    assert _confirmed_speech_activity(_packet(b"unknown"), rtc) is None
    assert (
        _confirmed_speech_activity(
            _packet(USER_SPEECH_START_PAYLOAD, kind=0),
            rtc,
        )
        is None
    )
    assert (
        _confirmed_speech_activity(
            _packet(
                USER_SPEECH_START_PAYLOAD,
                participant=SimpleNamespace(kind=0),
            ),
            rtc,
        )
        is None
    )


def test_device_rtc_token_uses_local_agent_sdk_proxy():
    from deskbot_server.rtc_agent_sdk import AGENT_SDK_PORT, RtcAgentSdkManager

    manager = RtcAgentSdkManager(SimpleNamespace())
    assert manager.token_endpoint == f"http://127.0.0.1:{AGENT_SDK_PORT}/rtc/token"


def test_agent_sdk_local_mode_uses_root_key_without_cloud_token_endpoints(
    monkeypatch,
):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("VOLCENGINE_ASR_API_KEY", "asr-key")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "tts-key")
    settings = SimpleNamespace(
        rtc=SimpleNamespace(
            livekit_url="http://127.0.0.1:7880",
            token_endpoint="http://127.0.0.1:18790/rtc/token",
            agent_name="lampgo-jarvis",
            call_mode="stable",
        ),
        llm=SimpleNamespace(
            base_url="https://llm.invalid/v1",
            model_name="model",
        ),
    )
    manager = RtcAgentSdkManager(settings)
    manager.configure_local_livekit(api_key="local-key", api_secret="local-secret")

    roles = manager._roles()

    assert roles["livekit"] == {
        "url": "ws://127.0.0.1:7880",
        "api_key": "local-key",
        "api_secret": "local-secret",
    }
    assert "platform" not in roles
    assert "agent" not in roles


def test_local_livekit_credentials_are_generated_once(tmp_path, monkeypatch):
    from deskbot_server import local_livekit
    from deskbot_server.core.settings import AppSettings

    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setattr(local_livekit, "_DEFAULT_CREDENTIALS", credentials_path)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    manager = local_livekit.LocalLiveKitServerManager(AppSettings.from_config({}))

    first = manager._load_or_create_credentials()
    second = manager._load_or_create_credentials()

    assert first == second
    assert first.api_key.startswith("deskbot_")
    assert len(first.api_secret) >= 48
    assert first.api_secret not in manager.health_snapshot().values()


def test_agent_sdk_initial_failure_schedules_recovery(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    async def _run():
        manager = RtcAgentSdkManager(SimpleNamespace(rtc=SimpleNamespace(enabled=True)))
        monkeypatch.setattr(manager, "_can_start", lambda: False)
        entered = asyncio.Event()

        async def _recover(_process):
            entered.set()

        monkeypatch.setattr(manager, "_recover_worker", _recover)
        assert await manager.start() is False
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        assert manager.recovery_task is not None
        await manager.stop()

    asyncio.run(_run())


def test_agent_sdk_stop_inside_recovery_keeps_single_flight_marker():
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    async def _run():
        manager = RtcAgentSdkManager(SimpleNamespace())
        observed = []

        async def _recovery():
            await manager.stop()
            observed.append(manager.recovery_task is asyncio.current_task())

        task = asyncio.create_task(_recovery())
        manager.recovery_task = task
        await task

        assert observed == [True]

    asyncio.run(_run())


def test_remote_audio_sends_end_stream_after_silence(monkeypatch):
    from deskbot_server import rtc_runtime

    class _Encoder:
        def __init__(self, *_args):
            pass

        def encode(self, pcm, samples):
            assert len(pcm) == samples * 2
            return b"opus"

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 0.01)

    class _Device:
        def __init__(self):
            self.sent = []

        async def begin_audio_down_stream(self, payload):
            self.sent.append(("begin", payload, 7))
            return 7

        async def send_audio_down_end(self, *, generation=None):
            self.sent.append(("end", b"", generation))

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="end-stream"),
        )
        await binding.play_remote(b"\x64\x00" * 100, 16000, 1)
        await asyncio.sleep(0.04)
        assert device.sent == [
            ("begin", b"\x00\x04opus", 7),
            ("end", b"", 7),
        ]
        assert binding.remote_stream_open is False

    asyncio.run(_run())


def test_remote_end_watchdog_is_one_persistent_task_and_fires_timeout(
    monkeypatch,
):
    from deskbot_server import rtc_runtime

    class _Encoder:
        def __init__(self, *_args):
            pass

        def encode(self, pcm, samples):
            assert len(pcm) == samples * 2
            return b"opus"

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 0.05)

    class _Device:
        def __init__(self):
            self.sent = []

        async def begin_audio_down_stream(self, payload):
            self.sent.append(("begin", payload, 31))
            return 31

        async def send_audio_down_opus(self, payload, *, generation=None):
            self.sent.append(("data", payload, generation))

        async def send_audio_down_end(self, *, generation=None):
            self.sent.append(("end", b"", generation))

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="watchdog"),
        )
        pcm = b"\x64\x00" * 320
        await binding.play_remote(pcm, 16000, 1)
        watchdog = binding.remote_end_task
        assert watchdog is not None and not watchdog.done()
        # Subsequent frames must not cancel/recreate the timer task.
        for _ in range(5):
            await binding.play_remote(pcm, 16000, 1)
            assert binding.remote_end_task is watchdog
            assert not watchdog.done()
        # After the (unchanged) end gap elapses without frames, the watchdog
        # closes the device stream itself.
        await asyncio.sleep(0.15)
        assert device.sent[-1] == ("end", b"", 31)
        assert binding.remote_stream_open is False
        # The watchdog stays armed for the next stream and is cancelled with
        # the binding.
        assert binding.remote_end_task is watchdog
        assert not watchdog.done()
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        assert watchdog.done()

    asyncio.run(_run())


def test_remote_audio_starts_with_first_frame_and_sends_each_frame(monkeypatch):
    from deskbot_server import rtc_runtime

    class _Encoder:
        next_packet = 0

        def __init__(self, *_args):
            pass

        def encode(self, pcm, samples):
            assert len(pcm) == samples * 2
            type(self).next_packet += 1
            return bytes([type(self).next_packet])

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 10.0)

    class _Device:
        def __init__(self):
            self.sent = []

        async def begin_audio_down_stream(self, payload):
            self.sent.append(("begin", payload, 11))
            return 11

        async def send_audio_down_opus(self, payload, *, generation=None):
            self.sent.append(("data", payload, generation))

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="batch-five"),
        )
        await binding.play_remote(b"\x64\x00" * 320, 16000, 1)
        assert device.sent == [
            (
                "begin",
                b"\x00\x01\x01",
                11,
            ),
        ]
        assert binding.playback_opus_packets == []
        await binding.play_remote(b"\x64\x00" * 320, 16000, 1)
        assert device.sent[-1] == (
            "data",
            b"\x00\x01\x02",
            11,
        )
        assert binding.playback_opus_packets == []
        binding.remote_end_task.cancel()
        await asyncio.gather(binding.remote_end_task, return_exceptions=True)

    asyncio.run(_run())


def test_rtc_low_latency_defaults_are_consistent_across_worker_patches():
    root = Path(__file__).parents[1] / "src" / "deskbot_server"
    plugins = (root / "rtc_livekit_plugins.py").read_text(encoding="utf-8")
    sdk = (root / "rtc_agent_sdk.py").read_text(encoding="utf-8")

    for source in (plugins, sdk):
        assert 'kwargs.setdefault("preemptive_generation", True)' in source
        assert 'kwargs.setdefault("min_endpointing_delay", 0.08)' in source
        assert 'kwargs.setdefault("max_endpointing_delay", 0.45)' in source


def test_rtc_first_audio_packet_has_no_artificial_prebuffer():
    from deskbot_server import rtc_runtime

    assert rtc_runtime._REMOTE_AUDIO_PREBUFFER_FRAMES == 1
    assert rtc_runtime._REMOTE_AUDIO_BATCH_FRAMES == 1


def test_continuous_remote_silence_does_not_leave_device_stream_open(
    monkeypatch,
):
    from deskbot_server import rtc_runtime

    class _Encoder:
        def __init__(self, *_args):
            pass

        def encode(self, _pcm, _samples):
            return b"opus"

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_PREBUFFER_FRAMES", 1)
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_BATCH_FRAMES", 1)
    monkeypatch.setattr(
        rtc_runtime,
        "_REMOTE_AUDIO_TRAILING_SILENCE_SEC",
        0.03,
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 10.0)

    class _Device:
        def __init__(self):
            self.sent = []

        async def begin_audio_down_stream(self, payload):
            self.sent.append(("begin", payload, 23))
            return 23

        async def send_audio_down_opus(self, payload, *, generation=None):
            self.sent.append(("data", payload, generation))

        async def send_audio_down_end(self, *, generation=None):
            self.sent.append(("end", b"", generation))

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="continuous-silence"),
        )
        # Remote-track keepalive silence must not start playback.
        await binding.play_remote(b"\x00\x00" * 320, 16000, 1)
        assert device.sent == []

        await binding.play_remote(b"\x64\x00" * 320, 16000, 1)
        assert device.sent[0][0] == "begin"
        await binding.play_remote(b"\x00\x00" * 320, 16000, 1)
        await binding.play_remote(b"\x00\x00" * 320, 16000, 1)
        assert device.sent[-1] == ("end", b"", 23)
        assert binding.remote_stream_open is False

    asyncio.run(_run())


def test_raw_device_vad_does_not_truncate_remote_stream(monkeypatch):
    from deskbot_server import rtc_runtime

    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 10.0)
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_PREBUFFER_FRAMES", 1)
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_BATCH_FRAMES", 1)

    class _Encoder:
        def __init__(self, *_args):
            pass

        def encode(self, _pcm, _samples):
            return b"opus"

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )

    class _Device:
        def __init__(self):
            self.begin_count = 0
            self.end_count = 0

        async def begin_audio_down_stream(self, payload):
            assert payload == b"\x00\x04opus"
            self.begin_count += 1
            return 19

        async def send_audio_down_end(self, *, generation=None):
            assert generation == 19
            self.end_count += 1

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="barge-in"),
        )
        binding.note_vad(active=True)
        await binding.play_remote(b"\x64\x00" * 320, 16000, 1)
        assert device.begin_count == 1
        assert device.end_count == 0
        assert binding.remote_stream_open is True
        binding.remote_end_task.cancel()
        await asyncio.gather(binding.remote_end_task, return_exceptions=True)

    asyncio.run(_run())


def test_raw_esp_sr_end_never_starts_face_search():
    source = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "ws"
        / "asr_chat.py"
    ).read_text(encoding="utf-8")
    assert "schedule_speech_face_search" not in source
    vad_handler = source.split('if msg_type == "audio_vad":', 1)[1].split(
        'if msg_type == "pb_ack":',
        1,
    )[0]
    assert "cancel_speech_face_search" not in vad_handler


def test_confirmed_barge_in_cancels_immediately_and_blocks_old_remote_pcm(
    monkeypatch,
):
    from deskbot_server import rtc_runtime

    class _Encoder:
        def __init__(self, *_args):
            pass

        def encode(self, _pcm, _samples):
            return b"opus"

    monkeypatch.setattr(
        rtc_runtime,
        "new_downlink_opus_encoder",
        lambda _sample_rate: _Encoder(),
    )
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_PREBUFFER_FRAMES", 1)
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_BATCH_FRAMES", 1)
    monkeypatch.setattr(rtc_runtime, "_REMOTE_AUDIO_END_GAP_SEC", 10.0)

    class _Device:
        def __init__(self):
            self.events = []
            self.next_generation = 40

        async def begin_audio_down_stream(self, payload):
            self.next_generation += 1
            self.events.append(("begin", payload, self.next_generation))
            return self.next_generation

        async def send_audio_down_opus(self, payload, *, generation=None):
            self.events.append(("data", payload, generation))

        async def cancel_audio_down_stream(self, *, generation=None):
            self.events.append(("cancel", b"", generation))
            return 99

    async def _run():
        device = _Device()
        binding = rtc_runtime._UsbRtcBinding(
            device,
            SimpleNamespace(device_id="confirmed-barge-in"),
        )
        binding.note_agent_state("speaking")
        pcm = b"\x64\x00" * 320
        await binding.play_remote(pcm, 16000, 1)
        assert device.events == [("begin", b"\x00\x04opus", 41)]

        await binding.cancel_remote_playback(reason="barge_in")
        assert device.events[-1] == ("cancel", b"", 41)
        assert binding.remote_stream_open is False
        assert binding.playback_buffer == bytearray()
        assert binding.playback_opus_packets == []

        # Buffered PCM from the interrupted LiveKit speech handle may still
        # arrive. Repeated `speaking` is the same turn and must not reopen it.
        await binding.play_remote(pcm, 16000, 1)
        binding.note_agent_state("speaking")
        await binding.play_remote(pcm, 16000, 1)
        assert [event[0] for event in device.events] == ["begin", "cancel"]

        # Only a non-speaking transition followed by a later speaking turn
        # may admit PCM and begin a fresh device generation.
        binding.note_agent_state("listening")
        await binding.play_remote(pcm, 16000, 1)
        assert [event[0] for event in device.events] == ["begin", "cancel"]
        binding.note_agent_state("speaking")
        await binding.play_remote(pcm, 16000, 1)
        assert device.events[-1] == ("begin", b"\x00\x04opus", 42)

        if binding.remote_end_task is not None:
            binding.remote_end_task.cancel()
            await asyncio.gather(
                binding.remote_end_task,
                return_exceptions=True,
            )

    asyncio.run(_run())


def test_publish_loop_survives_one_failed_frame():
    from deskbot_server import rtc_runtime

    class _Rtc:
        device_id = "publish-retry"

        def __init__(self):
            self.calls = []
            self.first_call = asyncio.Event()
            self.second_call = asyncio.Event()

        async def feed_pcm16(
            self,
            pcm,
            *,
            voice_active=False,
            captured_mono=None,
            max_age_seconds=None,
        ):
            self.calls.append((pcm, voice_active))
            if len(self.calls) == 1:
                self.first_call.set()
                raise RuntimeError("temporary")
            self.second_call.set()

    async def _run():
        rtc = _Rtc()
        binding = rtc_runtime._UsbRtcBinding(object(), rtc)
        binding.feed(b"first")
        task = asyncio.create_task(binding._publish_loop())
        await asyncio.wait_for(rtc.first_call.wait(), timeout=1.0)
        binding.feed(b"second")
        await asyncio.wait_for(rtc.second_call.wait(), timeout=1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert [item[0] for item in rtc.calls] == [b"first", b"second"]

    asyncio.run(_run())


def test_feed_pcm16_reconnects_once_after_capture_failure(monkeypatch):
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import RtcDeviceCapabilities, RtcDeviceSession

    class _AudioFrame:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    livekit = ModuleType("livekit")
    livekit.rtc = SimpleNamespace(AudioFrame=_AudioFrame)
    monkeypatch.setitem(sys.modules, "livekit", livekit)

    class _Source:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.frames = []

        async def capture_frame(self, frame):
            self.frames.append(frame)
            if self.fail:
                raise RuntimeError("disconnected")

    async def _sink(*_args):
        return None

    async def _run():
        session = RtcDeviceSession(
            "reconnect",
            RtcSettings(),
            playback_sink=_sink,
            capabilities=RtcDeviceCapabilities(),
        )
        first = _Source(fail=True)
        second = _Source()
        session.connected = True
        session.audio_source = first

        async def _connect():
            session.connected = True
            session.audio_source = second

        monkeypatch.setattr(session, "connect", _connect)
        await session.feed_pcm16(b"\x00\x00" * 160)
        assert len(first.frames) == 1
        assert len(second.frames) == 1

    asyncio.run(_run())


def test_remote_consumer_treats_closed_usb_sink_as_normal_disconnect(monkeypatch):
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.infrastructure.serial.session import SessionClosed
    from deskbot_server.rtc_gateway import RtcDeviceCapabilities, RtcDeviceSession

    class _Stream:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                frame=SimpleNamespace(data=bytearray(b"\x00\x00"))
            )

    class _AudioStream:
        @staticmethod
        def from_track(**_kwargs):
            return _Stream()

    livekit = ModuleType("livekit")
    livekit.rtc = SimpleNamespace(AudioStream=_AudioStream)
    monkeypatch.setitem(sys.modules, "livekit", livekit)

    async def _closed_sink(*_args):
        raise SessionClosed("COM4 is closed")

    async def _run():
        session = RtcDeviceSession(
            "closed-sink",
            RtcSettings(),
            playback_sink=_closed_sink,
            capabilities=RtcDeviceCapabilities(),
        )
        await session._consume_remote(object())

    asyncio.run(_run())


def test_seed_speech_echo_guards_are_shared_only_within_one_agent_session():
    pytest.importorskip("livekit.agents")

    from deskbot_server import rtc_livekit_plugins
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    first_stt = rtc_livekit_plugins._build_stt()
    first_tts = rtc_livekit_plugins._build_tts()
    second_stt = rtc_livekit_plugins._build_stt()
    second_tts = rtc_livekit_plugins._build_tts()
    first_guard = PlaybackEchoGuard()
    second_guard = PlaybackEchoGuard()

    first_stt.bind_playback_echo_guard(first_guard)
    first_tts.bind_playback_echo_guard(first_guard)
    second_stt.bind_playback_echo_guard(second_guard)
    second_tts.bind_playback_echo_guard(second_guard)

    assert first_stt._playback_echo_guard is first_guard
    assert first_tts._playback_echo_guard is first_guard
    assert second_stt._playback_echo_guard is second_guard
    assert second_tts._playback_echo_guard is second_guard
    assert first_guard is not second_guard

    first_guard.note_tts_segment("first-reply", "unique playback phrase", now=10.0)
    assert first_guard.classify("unique playback phrase", now=10.1)[0] is True
    assert second_guard.classify("unique playback phrase", now=10.1)[0] is False


def test_agent_session_patch_binds_one_private_echo_guard_per_session():
    from deskbot_server.rtc_agent_sdk import _SITECUSTOMIZE_CODE

    assert "_deskbot_playback_echo_guard" in _SITECUSTOMIZE_CODE
    assert "PlaybackEchoGuard()" in _SITECUSTOMIZE_CODE
    assert "bind_playback_echo_guard" in _SITECUSTOMIZE_CODE
    assert 'self.on("user_state_changed")' in _SITECUSTOMIZE_CODE
    assert "SPEECH_ACTIVITY_TOPIC" in _SITECUSTOMIZE_CODE
    assert "USER_SPEECH_START_PAYLOAD" in _SITECUSTOMIZE_CODE
    assert "USER_SPEECH_CONFIRMED_END_PAYLOAD" in _SITECUSTOMIZE_CODE


def test_livekit_local_participant_is_not_probed_before_room_connects():
    from deskbot_server.rtc_livekit_plugins import (
        _local_participant_if_connected,
    )

    participant = object()

    class _Room:
        connected = False

        @property
        def local_participant(self):
            if not self.connected:
                raise Exception("cannot access local participant before connecting")
            return participant

    room = _Room()
    assert _local_participant_if_connected(room) is None
    room.connected = True
    assert _local_participant_if_connected(room) is participant


def test_seed_adapter_patches_speech_and_worker_factories(monkeypatch):
    from deskbot_server import rtc_livekit_plugins

    package = ModuleType("lampgo_livekit_agent")
    speech = ModuleType("lampgo_livekit_agent.speech")
    worker = ModuleType("lampgo_livekit_agent.worker")
    original = object()
    speech.create_stt = original
    speech.create_tts = original
    worker.create_stt = original
    worker.create_tts = original
    package.speech = speech
    package.worker = worker

    monkeypatch.setenv("DESKBOT_RTC_SPEECH_ADAPTER", "deskbot_api_key")
    monkeypatch.setattr(rtc_livekit_plugins, "_installed", False)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent", package)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent.speech", speech)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent.worker", worker)

    assert rtc_livekit_plugins.install_lampgo_speech_adapters() is True
    assert speech.create_stt is worker.create_stt
    assert speech.create_tts is worker.create_tts
    assert speech.create_stt is not original
    assert speech.create_tts is not original


def test_seed_adapter_uses_single_ready_worker_without_cpu_load_shedding(
    monkeypatch,
):
    pytest.importorskip("livekit.agents")
    from livekit.agents import AgentServer

    from deskbot_server import rtc_livekit_plugins

    package = ModuleType("lampgo_livekit_agent")
    speech = ModuleType("lampgo_livekit_agent.speech")
    worker = ModuleType("lampgo_livekit_agent.worker")
    speech.create_stt = worker.create_stt = object()
    speech.create_tts = worker.create_tts = object()
    package.speech = speech
    package.worker = worker

    monkeypatch.setenv("DESKBOT_RTC_SPEECH_ADAPTER", "deskbot_api_key")
    monkeypatch.setattr(rtc_livekit_plugins, "_installed", False)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent", package)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent.speech", speech)
    monkeypatch.setitem(sys.modules, "lampgo_livekit_agent.worker", worker)

    assert rtc_livekit_plugins.install_lampgo_speech_adapters() is True
    server = AgentServer(port=0)
    assert server._num_idle_processes == 1
    assert server._initialize_process_timeout == 30.0
    assert server._load_threshold == 1.0
    assert server._load_fnc() == 0.0


def test_seed_asr_adapter_resamples_livekit_default_24khz_room_audio():
    pytest.importorskip("livekit.agents")
    from livekit import rtc
    from livekit.agents import APIConnectOptions

    from deskbot_server import rtc_livekit_plugins

    captured: dict[str, object] = {}

    class _Adapter:
        async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
            captured["pcm"] = pcm
            captured["sample_rate"] = sample_rate
            return "hello"

    speech = rtc_livekit_plugins._build_stt()
    speech._adapter = _Adapter()
    source_samples = 2_400
    frame = rtc.AudioFrame(
        data=b"\x01\x00" * source_samples,
        sample_rate=24_000,
        num_channels=1,
        samples_per_channel=source_samples,
    )

    event = asyncio.run(
        speech._recognize_impl(
            frame,
            conn_options=APIConnectOptions(max_retry=0),
        )
    )

    assert captured["sample_rate"] == 16_000
    assert len(captured["pcm"]) == 3_200
    assert event.alternatives[0].text == "hello"


def test_seed_asr_adapter_normalizes_one_complete_quiet_utterance():
    pytest.importorskip("livekit.agents")
    from livekit import rtc
    from livekit.agents import APIConnectOptions

    from deskbot_server import rtc_livekit_plugins

    captured: dict[str, object] = {}

    class _Adapter:
        async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
            captured["pcm"] = pcm
            captured["sample_rate"] = sample_rate
            return "听到了"

    source = [80 + ((i % 40) - 20) * 70 for i in range(1_600)]
    source_pcm = array("h", source).tobytes()
    speech = rtc_livekit_plugins._build_stt()
    speech._adapter = _Adapter()
    frame = rtc.AudioFrame(
        data=source_pcm,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=len(source),
    )

    event = asyncio.run(
        speech._recognize_impl(
            frame,
            conn_options=APIConnectOptions(max_retry=0),
        )
    )
    output = array("h")
    output.frombytes(captured["pcm"])

    assert captured["sample_rate"] == 16_000
    assert len(output) == len(source)
    assert max(abs(value) for value in output) > max(
        abs(value - 80) for value in source
    )
    assert max(output) <= 32767
    assert min(output) >= -32768
    assert event.alternatives[0].text == "听到了"


def test_seed_asr_does_not_amplify_or_forward_recent_tts_echo():
    pytest.importorskip("livekit.agents")
    from livekit import rtc
    from livekit.agents import APIConnectOptions

    from deskbot_server import rtc_livekit_plugins
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    captured: dict[str, bytes] = {}

    class _Adapter:
        async def transcribe(self, pcm: bytes, _sample_rate: int) -> str:
            captured["pcm"] = pcm
            return "北京天气晴朗"

    source = array("h", [120 if i % 2 else -120 for i in range(1_600)])
    speech = rtc_livekit_plugins._build_stt()
    speech._adapter = _Adapter()
    guard = PlaybackEchoGuard()
    speech.bind_playback_echo_guard(guard)
    guard.note_tts_segment(
        "reply-test",
        "今天北京天气晴朗，适合出门。",
    )
    frame = rtc.AudioFrame(
        data=source.tobytes(),
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=len(source),
    )

    event = asyncio.run(
        speech._recognize_impl(
            frame,
            conn_options=APIConnectOptions(max_retry=0),
        )
    )

    assert captured["pcm"] == source.tobytes()
    assert event.alternatives == []
    guard.reset()


def test_seed_asr_adapter_ignores_empty_livekit_utterance():
    pytest.importorskip("livekit.agents")
    from livekit.agents import APIConnectOptions

    from deskbot_server import rtc_livekit_plugins

    class _Adapter:
        async def transcribe(self, _pcm: bytes, _sample_rate: int) -> str:
            raise AssertionError("empty utterance must not call Seed ASR")

    speech = rtc_livekit_plugins._build_stt()
    speech._adapter = _Adapter()
    event = asyncio.run(
        speech._recognize_impl(
            [],
            conn_options=APIConnectOptions(max_retry=0),
        )
    )

    assert event.alternatives == []


def test_seed_asr_failure_drops_only_current_turn_and_keeps_session_alive():
    pytest.importorskip("livekit.agents")
    from livekit import rtc
    from livekit.agents import APIConnectOptions

    from deskbot_server import rtc_livekit_plugins

    class _BrokenAdapter:
        async def transcribe(self, _pcm: bytes, _sample_rate: int) -> str:
            raise ValueError("provider rejected test audio")

    speech = rtc_livekit_plugins._build_stt()
    speech._adapter = _BrokenAdapter()
    frame = rtc.AudioFrame(
        data=b"\x00\x00" * 1_600,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=1_600,
    )

    event = asyncio.run(
        speech.recognize(
            frame,
            conn_options=APIConnectOptions(max_retry=0),
        )
    )

    assert event.alternatives == []


def test_pending_rtc_binding_claims_utterance(monkeypatch):
    from deskbot_server import rtc_runtime

    pending = SimpleNamespace(
        rtc_session=SimpleNamespace(connected=False),
    )
    monkeypatch.setitem(rtc_runtime._bindings, "deskbot-pending", pending)

    assert rtc_runtime.rtc_device_active("deskbot-pending") is False
    assert rtc_runtime.rtc_device_active("deskbot-missing") is False

def test_agent_sdk_interruptions_match_negotiated_safe_modes():
    from deskbot_server.rtc_agent_sdk import _allow_agent_interruptions

    assert _allow_agent_interruptions("interruptible") is True
    assert _allow_agent_interruptions("esp32_aec") is True
    assert _allow_agent_interruptions("stable") is False


def test_agent_sdk_fails_closed_without_speech_credentials(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    for name in (
        "VOLCENGINE_ASR_API_KEY",
        "ASR_API_KEY",
        "DOUBAO_TTS_API_KEY",
        "VOLCENGINE_TTS_API_KEY",
        "SEED_TTS_API_KEY",
        "BYTEPLUS_SEED_SPEECH_API_KEY",
        "DOUBAO_ASR_APP_ID",
        "DOUBAO_TTS_APP_ID",
        "VOLCENGINE_APP_ID",
        "DOUBAO_ASR_ACCESS_TOKEN",
        "DOUBAO_TTS_ACCESS_TOKEN",
        "VOLCENGINE_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test")

    settings = SimpleNamespace(
        rtc=SimpleNamespace(
            livekit_url="https://rtc.invalid",
            token_endpoint="https://rtc.invalid/rtc/token",
            agent_name="agent",
        ),
        llm=SimpleNamespace(base_url="https://llm.invalid", model_name="model"),
    )
    manager = RtcAgentSdkManager(settings)
    assert manager._can_start() is False
    assert "Missing RTC configuration" in manager.last_error
    assert "ASR_API_KEY" in manager.last_error


def test_unknown_agent_sdk_port_owner_is_never_terminated(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    manager = RtcAgentSdkManager(SimpleNamespace())
    monkeypatch.setattr(manager, "_find_port_listener_pid", lambda: 4321)
    monkeypatch.setattr(manager, "_identify_sdk_port_owner", lambda _pid: None)
    monkeypatch.setattr(manager, "_describe_process", lambda _pid: "foreign.exe")

    def _must_not_terminate(*_args, **_kwargs):
        raise AssertionError("unknown process must not be terminated")

    monkeypatch.setattr(manager, "_terminate_process_tree", _must_not_terminate)
    assert asyncio.run(manager._release_sdk_port()) is False
    assert "refusing to terminate" in manager.last_error


def test_agent_sdk_ready_requires_worker_marker_and_health(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    async def _run():
        manager = RtcAgentSdkManager(SimpleNamespace())
        manager.process = SimpleNamespace(returncode=None)

        async def _healthy():
            return True

        monkeypatch.setattr(manager, "_health_ready", _healthy)
        assert await manager.wait_ready(0.02) is False

        manager._ready_event.set()
        assert await manager.wait_ready(0.02) is True

    asyncio.run(_run())


def test_agent_sdk_recovery_retries_until_ready(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    async def _run():
        manager = RtcAgentSdkManager(SimpleNamespace())
        process = SimpleNamespace(pid=1234, returncode=0)
        outcomes = iter((False, False, True))
        attempts = []

        async def _start():
            attempts.append(True)
            return next(outcomes)

        async def _no_delay(_seconds):
            return None

        monkeypatch.setattr(manager, "start", _start)
        monkeypatch.setattr(asyncio, "sleep", _no_delay)
        await manager._recover_worker(process)

        assert len(attempts) == 3
        assert manager._recovery_attempt == 3

    asyncio.run(_run())


def test_gateway_get_or_create_replaces_closed_cached_session():
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import DeskbotRtcGateway

    async def _sink(_pcm: bytes, _sample_rate: int, _channels: int) -> None:
        return None

    async def _run() -> None:
        gateway = DeskbotRtcGateway(RtcSettings())
        first = await gateway.get_or_create(
            "closed-cache",
            playback_sink=_sink,
        )
        await first.close()
        # ``RtcDeviceSession.close`` does not know about the registry, so the
        # dead session stays cached; the registry itself must replace it.
        assert first.closed is True
        assert gateway._sessions["closed-cache"] is first
        second = await gateway.get_or_create(
            "closed-cache",
            playback_sink=_sink,
        )
        assert second is not first
        assert second.closed is False
        await gateway.close()

    asyncio.run(_run())


def test_failed_bind_evicts_gateway_session_so_retry_gets_a_fresh_one(
    monkeypatch,
):
    from deskbot_server import rtc_runtime
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import DeskbotRtcGateway, RtcDeviceSession

    class _Device:
        is_closed = False

        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    async def _run() -> None:
        gateway = DeskbotRtcGateway(RtcSettings())
        monkeypatch.setattr(rtc_runtime, "_gateway", gateway)
        monkeypatch.setattr(rtc_runtime, "_bindings", {})
        monkeypatch.setattr(rtc_runtime, "_BIND_RETRY_MAX_DELAY_SEC", 0.0)

        async def _fake_expression_runtime(_device_id, _device_session):
            return None

        monkeypatch.setattr(
            rtc_runtime,
            "bind_usb_expression_runtime",
            _fake_expression_runtime,
        )

        created: list[RtcDeviceSession] = []
        original_get_or_create = gateway.get_or_create

        async def _tracking_get_or_create(device_id, **kwargs):
            session = await original_get_or_create(device_id, **kwargs)
            if all(session is not known for known in created):
                created.append(session)
            return session

        monkeypatch.setattr(gateway, "get_or_create", _tracking_get_or_create)

        attempts = {"count": 0}

        async def _fake_connect(self):
            attempts["count"] += 1
            if self._closed:
                raise RuntimeError("RTC device session is closed")
            if attempts["count"] == 1:
                raise RuntimeError("simulated LiveKit connect failure")
            self.connected = True
            self.connection_generation += 1

        monkeypatch.setattr(RtcDeviceSession, "connect", _fake_connect)

        device = _Device()
        await asyncio.wait_for(
            rtc_runtime.bind_usb_device("bind-retry", device),
            timeout=2.0,
        )

        # The first failed attempt must not leave its closed session cached in
        # the gateway; the retry has to receive a brand-new session and bind.
        assert attempts["count"] == 2
        assert len(created) == 2
        assert created[0].closed is True
        binding = rtc_runtime._bindings["bind-retry"]
        assert binding.rtc_session is created[1]
        assert gateway._sessions["bind-retry"] is created[1]
        assert binding.rtc_session.closed is False
        assert binding.rtc_session.connected is True

        await rtc_runtime.unbind_usb_device("bind-retry", device)
        assert "bind-retry" not in rtc_runtime._bindings

    asyncio.run(_run())


def test_publish_backlog_keeps_speech_frames_and_drops_silence():
    from deskbot_server import rtc_runtime

    class _Rtc:
        device_id = "speech-backlog"
        connection_generation = 0

        def __init__(self):
            self.frames = []
            self.done = asyncio.Event()

        async def feed_pcm16(
            self,
            pcm,
            *,
            voice_active=False,
            captured_mono=None,
            max_age_seconds=None,
        ):
            self.frames.append((pcm, voice_active, max_age_seconds))
            if pcm == b"speech-3":
                self.done.set()

    async def _run() -> None:
        rtc = _Rtc()
        binding = rtc_runtime._UsbRtcBinding(object(), rtc)
        binding.note_vad(active=False)
        for index in range(3):
            binding.feed(f"silence-{index}".encode())
        binding.note_vad(active=True)
        for index in range(1, 4):
            binding.feed(f"speech-{index}".encode())

        task = asyncio.create_task(binding._publish_loop())
        await asyncio.wait_for(rtc.done.wait(), timeout=1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # A stalled publisher may drop silence, but every frame of the active
        # utterance must survive and ride the widened in-speech age budget.
        assert [item[0] for item in rtc.frames] == [
            b"speech-1",
            b"speech-2",
            b"speech-3",
        ]
        assert all(item[1] is True for item in rtc.frames)
        assert all(
            item[2] == rtc_runtime._PUBLISH_MAX_FRAME_AGE_SPEECH_SEC
            for item in rtc.frames
        )

    asyncio.run(_run())


def test_speech_queue_stays_bounded_and_speech_drops_are_logged(caplog):
    from deskbot_server import rtc_runtime

    class _Rtc:
        device_id = "speech-bound"
        connection_generation = 0

    async def _run() -> None:
        binding = rtc_runtime._UsbRtcBinding(object(), _Rtc())
        binding.note_vad(active=True)
        with caplog.at_level("WARNING"):
            for index in range(
                rtc_runtime._PUBLISH_QUEUE_SPEECH_BATCHES + 1
            ):
                binding.feed(f"frame-{index}".encode())

        # Latency-for-completeness is still bounded: the oldest speech frame
        # is dropped, and never silently.
        assert (
            binding.pcm_queue.qsize()
            == rtc_runtime._PUBLISH_QUEUE_SPEECH_BATCHES
        )
        items = []
        while not binding.pcm_queue.empty():
            items.append(binding.pcm_queue.get_nowait()[0])
        assert items[0] == b"frame-1"
        assert items[-1] == (
            f"frame-{rtc_runtime._PUBLISH_QUEUE_SPEECH_BATCHES}".encode()
        )
        assert "in-speech" in caplog.text

    asyncio.run(_run())


def test_rtc_health_snapshot_distinguishes_room_and_agent(monkeypatch):
    from deskbot_server import rtc_runtime

    live_task = SimpleNamespace(done=lambda: False)
    session = SimpleNamespace(
        connected=True,
        remote_agent_attached=True,
        remote_tasks=[live_task],
        connection_generation=7,
    )
    monkeypatch.setitem(
        rtc_runtime._bindings,
        "deskbot-health",
        SimpleNamespace(rtc_session=session),
    )

    snapshot = rtc_runtime.rtc_health_snapshot()["devices"]["deskbot-health"]
    assert snapshot == {
        "room_connected": True,
        "agent_attached": True,
        "remote_audio_tasks": 1,
        "connection_generation": 7,
        "active": True,
    }


def test_auto_reply_switch_gates_usb_audio_into_rtc(monkeypatch):
    """实验台「关闭自动应答」必须切断设备语音进入 RTC 的上行。"""
    from deskbot_server import rtc_runtime
    from deskbot_server.auto_reply import (
        get_asr_voice_auto_reply_enabled,
        set_asr_voice_auto_reply_enabled,
    )
    from deskbot_server.ws import asr_chat

    published: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        rtc_runtime,
        "publish_usb_pcm",
        lambda device_id, pcm: published.append((device_id, pcm)),
    )

    class _Session:
        last_decoded_pcm = b"\x01\x02"

        async def feed_audio(self, *_args, **_kwargs):
            return ([], False, False)

        def consume_microphone_health_update(self):
            return None

    original = get_asr_voice_auto_reply_enabled()

    async def _run():
        set_asr_voice_auto_reply_enabled(False)
        await asr_chat._feed_rom_uplink(
            b"payload",
            "pcm16",
            session=_Session(),
            device_id="gate-dev",
        )
        # 关闭时音频完全不进入 RTC binding。
        assert published == []
        # 翻回后无需重连立即恢复。
        set_asr_voice_auto_reply_enabled(True)
        await asr_chat._feed_rom_uplink(
            b"payload",
            "pcm16",
            session=_Session(),
            device_id="gate-dev",
        )
        assert published == [("gate-dev", b"\x01\x02")]

    try:
        asyncio.run(_run())
    finally:
        set_asr_voice_auto_reply_enabled(original)


def test_vad_listening_expression_respects_auto_reply_switch():
    """自动应答关闭时 speech_start 不应触发 listening 表情/VAD 转发。"""
    source = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "ws"
        / "asr_chat.py"
    ).read_text(encoding="utf-8")
    vad_handler = source.split('if msg_type == "audio_vad":', 1)[1].split(
        'if msg_type == "pb_ack":',
        1,
    )[0]
    speech_start_branch = vad_handler.split(
        'if vad_state == "speech_start":', 1
    )[1]
    gate, forward = speech_start_branch.split(
        "notify_usb_vad(device_id, active=True)", 1
    )
    assert "get_asr_voice_auto_reply_enabled()" in gate
    assert "continue" in gate
    del forward


def test_bind_waits_for_late_gateway_install_and_then_binds(monkeypatch):
    """设备 hello 早于后台 rtc-startup 安装网关时，绑定必须等待而非放弃。

    回归自桌面客户端实测：USB 先行启动后设备 34 秒先于网关完成 hello，
    bind_usb_device 在网关为 None 时直接 return，该设备语音在拔插前永久
    rtc_active=False。
    """
    from deskbot_server import rtc_runtime
    from deskbot_server.core.settings import RtcSettings
    from deskbot_server.rtc_gateway import DeskbotRtcGateway, RtcDeviceSession

    class _Device:
        is_closed = False

        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    async def _run() -> None:
        monkeypatch.setattr(rtc_runtime, "_gateway", None)
        # 套内先跑的测试可能把决策标志留成 True（install 真网关会置位），
        # 必须显式复位，否则 bind 不等待直接返回、断言失效。
        monkeypatch.setattr(rtc_runtime, "_gateway_decision_final", False)
        monkeypatch.setattr(rtc_runtime, "_bindings", {})
        monkeypatch.setattr(rtc_runtime, "_BIND_RETRY_MAX_DELAY_SEC", 0.0)

        async def _fake_expression_runtime(_device_id, _device_session):
            return None

        monkeypatch.setattr(
            rtc_runtime,
            "bind_usb_expression_runtime",
            _fake_expression_runtime,
        )

        connected = {"count": 0}

        async def _fake_connect(self):
            if self._closed:
                raise RuntimeError("RTC device session is closed")
            connected["count"] += 1
            self.connected = True
            self.connection_generation += 1

        monkeypatch.setattr(RtcDeviceSession, "connect", _fake_connect)

        device = _Device()
        bind_task = asyncio.create_task(
            rtc_runtime.bind_usb_device("deskbot_waitgw", device)
        )
        # 网关未安装期间：等待而不是放弃，也不应有任何连接尝试。
        await asyncio.sleep(0.2)
        assert not bind_task.done()
        assert connected["count"] == 0

        rtc_runtime.install_rtc_gateway(DeskbotRtcGateway(RtcSettings()))
        # 等待轮询周期（1s）发现网关后完成绑定。
        for _ in range(40):
            await asyncio.sleep(0.1)
            if connected["count"] > 0:
                break
        assert connected["count"] > 0
        assert "deskbot_waitgw" in rtc_runtime._bindings

        device.is_closed = True
        binding = rtc_runtime._bindings.get("deskbot_waitgw")
        if binding is not None:
            await binding.close(reason="test_teardown")
        bind_task.cancel()
        try:
            await bind_task
        except asyncio.CancelledError:
            pass
        rtc_runtime.install_rtc_gateway(None)

    asyncio.run(_run())


def _voice_feedback_test_catalog():
    from deskbot_server.application.expression_runtime import (
        build_expression_catalog,
    )

    return build_expression_catalog(
        {
            "emotions": [
                {
                    "name": "thinking",
                    "frames": [
                        {
                            "ms": 400,
                            "elements": {
                                "eye_l": [
                                    {"shape": "circle", "x": 40, "y": 60, "r": 6}
                                ],
                            },
                        }
                    ],
                },
                {
                    "name": "idle",
                    "frames": [{"ms": 400, "elements": {"eye_l": []}}],
                },
            ],
        }
    )


class _FakeExpressionRuntime:
    def __init__(self):
        self.calls = []

    async def play_frames(self, frames, **kwargs):
        self.calls.append((list(frames), kwargs))
        return SimpleNamespace(status="accepted", ok=True)


def test_voice_not_ready_feedback_triggers_once_then_throttles(monkeypatch):
    """未就绪 + speech_start：触发一次「语音启动中…」反馈，8 秒内不重复。"""
    from deskbot_server import rtc_runtime
    from deskbot_server.application import expression_runtime as expr_mod
    from deskbot_server.application import voice_link_feedback as vlf

    catalog = _voice_feedback_test_catalog()
    runtime = _FakeExpressionRuntime()
    monkeypatch.setattr(expr_mod, "get_expression_runtime", lambda dev: runtime)
    monkeypatch.setattr(expr_mod, "load_expression_catalog", lambda: catalog)
    monkeypatch.setattr(rtc_runtime, "rtc_device_active", lambda dev: False)
    clock = {"now": 100.0}
    monkeypatch.setattr(vlf, "_monotonic", lambda: clock["now"])
    vlf.reset_voice_not_ready_feedback_throttle()

    async def _run() -> None:
        task = vlf.schedule_voice_not_ready_feedback("vlf-dev")
        assert task is not None
        await task
        assert len(runtime.calls) == 1
        frames, kwargs = runtime.calls[0]
        # 表情走统一仲裁入口，低优先级 + 短 lease，不抢真实内容源。
        assert kwargs["source"] == "voice_link_feedback"
        assert kwargs["priority"] < 20
        assert kwargs["hold_ms"] is not None
        # 表情帧保留原脸，并在 extra 层叠加「语音启动中…」文案。
        assert frames and frames[0]["elements"]["eye_l"]
        texts = [
            prim.get("text")
            for frame in frames
            for prim in frame["elements"].get("extra", [])
            if isinstance(prim, dict) and prim.get("shape") == "text"
        ]
        assert any("语音启动中" in str(text) for text in texts)

        # 8 秒内重复 speech_start：节流，不再下发。
        clock["now"] += 5.0
        assert vlf.schedule_voice_not_ready_feedback("vlf-dev") is None
        assert len(runtime.calls) == 1

        # 超过 8 秒后允许再次反馈。
        clock["now"] += 3.5
        task2 = vlf.schedule_voice_not_ready_feedback("vlf-dev")
        assert task2 is not None
        await task2
        assert len(runtime.calls) == 2

    try:
        asyncio.run(_run())
    finally:
        vlf.reset_voice_not_ready_feedback_throttle()


def test_voice_not_ready_feedback_skips_after_binding_becomes_ready(monkeypatch):
    """binding 已就绪时不下发反馈（含调度后就绪的竞态复查）；跳过路径不烧节流窗。"""
    from deskbot_server import rtc_runtime
    from deskbot_server.application import expression_runtime as expr_mod
    from deskbot_server.application import voice_link_feedback as vlf

    catalog = _voice_feedback_test_catalog()
    runtime = _FakeExpressionRuntime()
    ready = {"active": True}
    monkeypatch.setattr(expr_mod, "get_expression_runtime", lambda dev: runtime)
    monkeypatch.setattr(expr_mod, "load_expression_catalog", lambda: catalog)
    monkeypatch.setattr(rtc_runtime, "rtc_device_active", lambda dev: ready["active"])
    vlf.reset_voice_not_ready_feedback_throttle()

    async def _run() -> None:
        task = vlf.schedule_voice_not_ready_feedback("vlf-ready-dev")
        assert task is not None
        await task
        assert runtime.calls == []

        # 竞态跳过不记账：binding 随后又掉线时，同一 8 秒窗内仍能反馈。
        ready["active"] = False
        retry = vlf.schedule_voice_not_ready_feedback("vlf-ready-dev")
        assert retry is not None
        await retry
        assert len(runtime.calls) == 1

    try:
        asyncio.run(_run())
    finally:
        vlf.reset_voice_not_ready_feedback_throttle()


def test_voice_not_ready_feedback_charges_window_only_after_accepted_delivery(
    monkeypatch,
):
    """A4 契约：``_last_feedback_at`` 在 play_frames 受理后写入。

    - runtime 缺失跳过：不烧窗，下一次 speech_start 仍可反馈。
    - 投递 in-flight 期间的重复边沿：去重为单次投递。
    """
    from deskbot_server import rtc_runtime
    from deskbot_server.application import expression_runtime as expr_mod
    from deskbot_server.application import voice_link_feedback as vlf

    catalog = _voice_feedback_test_catalog()
    runtime = _FakeExpressionRuntime()
    runtime_box = {"runtime": None}
    monkeypatch.setattr(
        expr_mod, "get_expression_runtime", lambda dev: runtime_box["runtime"]
    )
    monkeypatch.setattr(expr_mod, "load_expression_catalog", lambda: catalog)
    monkeypatch.setattr(rtc_runtime, "rtc_device_active", lambda dev: False)
    clock = {"now": 200.0}
    monkeypatch.setattr(vlf, "_monotonic", lambda: clock["now"])
    vlf.reset_voice_not_ready_feedback_throttle()

    async def _run() -> None:
        # runtime 缺失：投递被跳过，节流窗保持打开。
        task = vlf.schedule_voice_not_ready_feedback("vlf-a4-dev")
        assert task is not None
        await task
        assert runtime.calls == []

        clock["now"] += 1.0
        runtime_box["runtime"] = runtime
        task2 = vlf.schedule_voice_not_ready_feedback("vlf-a4-dev")
        assert task2 is not None
        # in-flight 去重：任务未完成前的重复边沿不再新建任务。
        assert vlf.schedule_voice_not_ready_feedback("vlf-a4-dev") is None
        await task2
        assert len(runtime.calls) == 1

        # 受理后记账：同窗内再次调度被节流。
        clock["now"] += 1.0
        assert vlf.schedule_voice_not_ready_feedback("vlf-a4-dev") is None

    try:
        asyncio.run(_run())
    finally:
        vlf.reset_voice_not_ready_feedback_throttle()


def test_voice_not_ready_feedback_wiring_in_speech_start_branch():
    """asr_chat 接线契约：反馈在自动应答门控之后、且仅在 RTC 未就绪时触发。"""
    source = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "ws"
        / "asr_chat.py"
    ).read_text(encoding="utf-8")
    vad_handler = source.split('if msg_type == "audio_vad":', 1)[1].split(
        'if msg_type == "pb_ack":',
        1,
    )[0]
    speech_start_branch = vad_handler.split(
        'if vad_state == "speech_start":', 1
    )[1]
    gate, forward = speech_start_branch.split(
        "notify_usb_vad(device_id, active=True)", 1
    )
    # 自动应答关闭：整个 speech_start 分支 continue，不触发反馈。
    assert "get_asr_voice_auto_reply_enabled()" in gate
    assert "schedule_voice_not_ready_feedback" not in gate
    # 反馈只在 binding 未就绪时触发，且为 fire-and-forget（无 await）。
    guard_idx = forward.index("not rtc_device_active(device_id)")
    call_idx = forward.index("schedule_voice_not_ready_feedback(device_id)")
    assert call_idx > guard_idx
    assert "await schedule_voice_not_ready_feedback" not in forward
