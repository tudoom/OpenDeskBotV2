"""下行 pb TTS Opus batch 编解码 roundtrip。"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from deskbot_server.pipeline.opus_runtime import load_opuslib_next

    opuslib_next = load_opuslib_next()
except Exception as exc:
    pytest.skip(
        f"native libopus runtime unavailable: {exc}",
        allow_module_level=True,
    )

from deskbot_server.pipeline.opus_downlink import (
    encode_pcm_s16le_to_opus_batch,
)

# 下行 batch 与上行使用完全相同的 uint16_be + frame 封装，
# 解码端复用 decode_opus_uplink 即可（原 decode_opus_batch_to_pcm_s16le 已删）。
from deskbot_server.pipeline.opus_uplink import (
    decode_opus_uplink,
    opus_frame_samples,
)


def _tone_pcm(sample_rate: int, duration_ms: int = 200, freq: float = 440.0) -> bytes:
    n = sample_rate * duration_ms // 1000
    t = np.arange(n, dtype=np.float32)
    wave = (np.sin(2 * np.pi * freq * t / sample_rate) * 12000).astype(np.int16)
    return wave.tobytes()


def test_opus_downlink_roundtrip_24k():
    sr = 24000
    pcm = _tone_pcm(sr, duration_ms=400)
    batch, nframes = encode_pcm_s16le_to_opus_batch(pcm, sr)
    assert nframes > 0
    assert len(batch) > 0

    dec = opuslib_next.Decoder(sr, 1)
    out = decode_opus_uplink(dec, batch, sample_rate=sr, opus_frames=nframes)
    frame_samples = opus_frame_samples(sr)
    assert len(out) >= frame_samples * (nframes - 1)


def test_opus_downlink_single_frame():
    sr = 24000
    pcm = _tone_pcm(sr, duration_ms=20)
    enc = opuslib_next.Encoder(sr, 1, opuslib_next.APPLICATION_AUDIO)
    frame_samples = opus_frame_samples(sr)
    opus = enc.encode(pcm[: frame_samples * 2], frame_samples)

    dec = opuslib_next.Decoder(sr, 1)
    out = decode_opus_uplink(dec, opus, sample_rate=sr, opus_frames=1)
    assert len(out) == frame_samples * 2
