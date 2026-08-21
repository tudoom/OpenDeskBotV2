"""下行 pb TTS：PCM s16le → Opus batch（与上行相同的 uint16_be + frame 格式）。"""

from __future__ import annotations

import struct
from typing import Any

from deskbot_server.pipeline.opus_runtime import load_opuslib_next
from deskbot_server.pipeline.opus_uplink import opus_frame_samples

_OPUS_LP_HDR = struct.Struct("!H")


def new_downlink_opus_encoder(sample_rate: int) -> Any:
    """新建下行 TTS Opus 编码器（供同一请求时间线内多 chunk 按序复用）。"""
    opuslib_next = load_opuslib_next()
    return opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)


def encode_pcm_s16le_to_opus_batch(
    pcm: bytes,
    sample_rate: int,
    *,
    encoder: Any | None = None,
) -> tuple[bytes, int]:
    """mono s16le PCM → ``(opus_batch, frame_count)``。

    ``encoder`` 可传入 :func:`new_downlink_opus_encoder` 的实例在同一请求的
    分片间按序复用（固件端解码器跨 chunk 持久，复用保持码流连续）；缺省
    每次调用新建编码器，保持原行为。
    """
    if not pcm:
        return b"", 0
    frame_samples = opus_frame_samples(sample_rate)
    frame_bytes = frame_samples * 2
    enc = encoder if encoder is not None else new_downlink_opus_encoder(sample_rate)
    parts: list[bytes] = []
    nframes = 0
    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        opus = enc.encode(chunk, frame_samples)
        parts.append(_OPUS_LP_HDR.pack(len(opus)) + opus)
        nframes += 1
        offset += frame_bytes
    return b"".join(parts), nframes
