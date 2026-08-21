from __future__ import annotations

import struct

import pytest

from deskbot_server.infrastructure.serial.protocol import (
    HEADER_SIZE,
    Channel,
    FrameDecoder,
    FrameFlag,
    SerialProtocolError,
    encode_control_payload,
    encode_frame,
)


def test_frame_round_trip_survives_fragmentation_and_garbage():
    first = encode_frame(
        Channel.CONTROL_JSON,
        encode_control_payload({"type": "heartbeat"}),
        sequence=7,
        session_epoch=42,
        flags=FrameFlag.ACK_REQUIRED,
    )
    second = encode_frame(
        Channel.AUDIO_UP_OPUS,
        b"\x01\x02\x03",
        sequence=8,
        session_epoch=42,
    )
    decoder = FrameDecoder()
    frames = []
    stream = b"console noise\r\n" + first + second
    for offset in range(0, len(stream), 3):
        frames.extend(decoder.feed(stream[offset : offset + 3]))

    assert [frame.channel for frame in frames] == [
        Channel.CONTROL_JSON,
        Channel.AUDIO_UP_OPUS,
    ]
    assert frames[0].sequence == 7
    assert frames[0].session_epoch == 42
    assert frames[0].flags == FrameFlag.ACK_REQUIRED
    assert frames[1].payload == b"\x01\x02\x03"
    assert decoder.discarded_bytes >= len(b"console noise\r\n")


def test_decoder_rejects_bad_payload_crc_and_recovers_at_next_frame():
    corrupt = bytearray(
        encode_frame(
            Channel.PB_WIRE,
            b'{"type":"pb_start"}',
            sequence=1,
            session_epoch=9,
        )
    )
    corrupt[-1] ^= 0x80
    good = encode_frame(
        Channel.LOG,
        b"ready",
        sequence=2,
        session_epoch=9,
    )
    decoder = FrameDecoder()

    frames = decoder.feed(corrupt + good)

    assert len(frames) == 1
    assert frames[0].channel == Channel.LOG
    assert decoder.invalid_payloads == 1


def test_decoder_rejects_length_before_allocating_or_waiting():
    frame = bytearray(
        encode_frame(
            Channel.CONTROL_JSON,
            b"{}",
            sequence=1,
            session_epoch=0,
        )
    )
    # Corrupting length also invalidates the header CRC.  The decoder must
    # resynchronise instead of trusting the advertised 4 GiB.
    struct.pack_into("<I", frame, 16, 0xFFFFFFFF)
    decoder = FrameDecoder()
    assert decoder.feed(frame) == []
    assert decoder.invalid_headers >= 1
    assert decoder.buffered_bytes <= 3


def test_channel_size_limits_are_enforced():
    with pytest.raises(SerialProtocolError):
        encode_frame(
            Channel.CONTROL_JSON,
            b"x" * (16 * 1024 + 1),
            sequence=1,
            session_epoch=1,
        )


def test_header_size_constant_matches_wire_contract():
    assert HEADER_SIZE == 24


def test_audio_cancel_has_a_dedicated_out_of_band_frame_flag():
    """Barge-in must not masquerade as a graceful END_STREAM.

    END drains an ordinary stream. CANCEL is deliberately a distinct wire
    event so firmware can invalidate queued Opus, decoded PCM, and speaker DMA
    immediately while the host rejects writes from the old generation.
    """

    assert FrameFlag.CANCEL_STREAM == 0x40
    assert not (FrameFlag.CANCEL_STREAM & FrameFlag.END_STREAM)
