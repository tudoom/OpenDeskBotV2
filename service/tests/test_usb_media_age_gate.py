"""USB 专用媒体队列的批龄门：按 VAD 活跃分档（A3）。

静音期维持 750ms（对齐 rtc_runtime._PUBLISH_MAX_FRAME_AGE_SEC），
说话中放宽到 2.5s（对齐 _PUBLISH_MAX_FRAME_AGE_SPEECH_SEC），
避免事件循环短暂卡顿把一句话中段丢掉。
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from deskbot_server.ws.asr_chat import (
    _USB_AUDIO_MAX_AGE_IDLE_SEC,
    _USB_AUDIO_MAX_AGE_SPEECH_SEC,
    _consume_usb_audio_media,
    _DeviceVadGate,
)


class _FakeDeviceSession:
    """DeviceSession 假对象：只暴露专用音频队列接口。"""

    def __init__(self, batches):
        self._batches = list(batches)

    async def receive_audio_up(self):
        if not self._batches:
            raise ConnectionError("closed")
        return self._batches.pop(0)


class _FakeConnectionSession:
    def __init__(self):
        self.fed: list[bytes] = []
        self.last_decoded_pcm = b""

    async def feed_audio(self, payload, _codec, **_kwargs):
        self.fed.append(bytes(payload))
        return ([], False, False)

    def consume_microphone_health_update(self):
        return None


def _batch(payload: bytes, *, age_sec: float, sequence: int = 1):
    return SimpleNamespace(
        payload=payload,
        codec="pcm16",
        sample_rate=16000,
        channels=1,
        opus_frames=None,
        received_mono=time.monotonic() - age_sec,
        sequence=sequence,
    )


def _run_consumer(batches, vad_gate):
    session = _FakeConnectionSession()
    websocket = _FakeDeviceSession(batches)
    asyncio.run(
        _consume_usb_audio_media(
            websocket,
            session=session,
            device_id="deskbot_age_gate",
            registry=None,
            vad_gate=vad_gate,
        )
    )
    return session.fed


def test_fresh_batch_always_feeds():
    fed = _run_consumer([_batch(b"\x01\x02", age_sec=0.0)], _DeviceVadGate())
    assert fed == [b"\x01\x02"]


def test_idle_gate_drops_batch_older_than_750ms():
    age = _USB_AUDIO_MAX_AGE_IDLE_SEC + 0.5
    fed = _run_consumer([_batch(b"\x01\x02", age_sec=age)], _DeviceVadGate())
    assert fed == []


def test_speech_gate_widens_to_2500ms():
    gate = _DeviceVadGate()
    gate.speech_active = True
    age = _USB_AUDIO_MAX_AGE_IDLE_SEC + 0.5
    assert age < _USB_AUDIO_MAX_AGE_SPEECH_SEC
    fed = _run_consumer([_batch(b"\x03\x04", age_sec=age)], gate)
    assert fed == [b"\x03\x04"]


def test_speech_gate_still_bounded():
    gate = _DeviceVadGate()
    gate.speech_active = True
    age = _USB_AUDIO_MAX_AGE_SPEECH_SEC + 0.5
    fed = _run_consumer([_batch(b"\x05\x06", age_sec=age)], gate)
    assert fed == []


def test_in_speech_drop_warns_unthrottled(caplog):
    """说话中丢批的 warning 不限频（对齐 _record_publish_drop 语义）。"""
    gate = _DeviceVadGate()
    gate.speech_active = True
    age = _USB_AUDIO_MAX_AGE_SPEECH_SEC + 0.5
    batches = [
        _batch(b"\x07", age_sec=age, sequence=1),
        _batch(b"\x08", age_sec=age, sequence=2),
        _batch(b"\x09", age_sec=age, sequence=3),
    ]
    with caplog.at_level(logging.WARNING, logger="deskbot-server"):
        fed = _run_consumer(batches, gate)
    assert fed == []
    stale_warnings = [
        record
        for record in caplog.records
        if "stale in-speech USB audio skipped" in record.getMessage()
    ]
    assert len(stale_warnings) == 3


def test_idle_drop_warning_is_rate_limited(caplog):
    """静音期陈旧批只在首次（和每 2s）打 warning，但计数持续累计。"""
    age = _USB_AUDIO_MAX_AGE_IDLE_SEC + 0.5
    batches = [
        _batch(b"\x0a", age_sec=age, sequence=i + 1) for i in range(4)
    ]
    with caplog.at_level(logging.WARNING, logger="deskbot-server"):
        fed = _run_consumer(batches, _DeviceVadGate())
    assert fed == []
    stale_warnings = [
        record
        for record in caplog.records
        if "stale silence USB audio skipped" in record.getMessage()
    ]
    assert len(stale_warnings) == 1
