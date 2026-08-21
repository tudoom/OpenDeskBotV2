"""Deskbot USB CDC binary framing.

Wire format (little endian)::

    u32 magic = 0x544f4244  # bytes ``DBOT``
    u8  version = 1
    u8  header_size = 24
    u8  channel
    u8  flags
    u32 sequence
    u32 session_epoch
    u32 payload_length
    u32 header_crc32        # CRC-32/IEEE over the first 20 bytes
    u8  payload[payload_length]
    u32 payload_crc32       # CRC-32/IEEE over payload

The incremental decoder never trusts a length until the magic, fixed fields,
and header CRC have all passed.  Invalid input advances one byte and searches
for the next magic marker, which makes a noisy serial stream self-healing.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Any

MAGIC = 0x544F4244
MAGIC_BYTES = b"DBOT"
PROTOCOL_VERSION = 1
HEADER_SIZE = 24
PAYLOAD_CRC_SIZE = 4

MAX_PAYLOAD = 1024 * 1024
MAX_CONTROL_PAYLOAD = 16 * 1024
MAX_LOG_PAYLOAD = 16 * 1024
MAX_AUDIO_PAYLOAD = 64 * 1024

_HEADER_PREFIX = struct.Struct("<IBBBBIII")
_HEADER = struct.Struct("<IBBBBIIII")
_CRC32 = struct.Struct("<I")


class SerialProtocolError(ValueError):
    """A frame or control message violates the USB transport contract."""


class Channel(IntEnum):
    CONTROL_JSON = 1
    PB_WIRE = 2
    AUDIO_UP_OPUS = 3
    AUDIO_DOWN_OPUS = 4
    CAMERA_JPEG = 5
    LOG = 6


class FrameFlag(IntFlag):
    NONE = 0
    ACK_REQUIRED = 0x01
    ACK = 0x02
    ERROR = 0x04
    END_STREAM = 0x08
    JSON = 0x10
    BEGIN_STREAM = 0x20
    CANCEL_STREAM = 0x40


_CHANNEL_LIMITS: dict[Channel, int] = {
    Channel.CONTROL_JSON: MAX_CONTROL_PAYLOAD,
    Channel.PB_WIRE: MAX_PAYLOAD,
    Channel.AUDIO_UP_OPUS: MAX_AUDIO_PAYLOAD,
    Channel.AUDIO_DOWN_OPUS: MAX_AUDIO_PAYLOAD,
    Channel.CAMERA_JPEG: MAX_PAYLOAD,
    Channel.LOG: MAX_LOG_PAYLOAD,
}


@dataclass(frozen=True, slots=True)
class Frame:
    channel: Channel
    flags: FrameFlag
    sequence: int
    session_epoch: int
    payload: bytes


def crc32(data: bytes | bytearray | memoryview) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def channel_payload_limit(channel: Channel | int) -> int:
    try:
        normalized = Channel(channel)
    except ValueError as exc:
        raise SerialProtocolError(f"unsupported channel: {channel!r}") from exc
    return _CHANNEL_LIMITS[normalized]


def _u32(value: int, name: str) -> int:
    normalized = int(value)
    if not 0 <= normalized <= 0xFFFFFFFF:
        raise SerialProtocolError(f"{name} must fit uint32")
    return normalized


def encode_frame(
    channel: Channel | int,
    payload: bytes | bytearray | memoryview,
    *,
    sequence: int,
    session_epoch: int,
    flags: FrameFlag | int = FrameFlag.NONE,
) -> bytes:
    try:
        normalized_channel = Channel(channel)
    except ValueError as exc:
        raise SerialProtocolError(f"unsupported channel: {channel!r}") from exc
    raw = bytes(payload)
    limit = channel_payload_limit(normalized_channel)
    if len(raw) > limit:
        raise SerialProtocolError(
            f"{normalized_channel.name} payload exceeds {limit} bytes"
        )
    seq = _u32(sequence, "sequence")
    epoch = _u32(session_epoch, "session_epoch")
    normalized_flags = int(flags)
    if not 0 <= normalized_flags <= 0xFF:
        raise SerialProtocolError("flags must fit uint8")

    prefix = _HEADER_PREFIX.pack(
        MAGIC,
        PROTOCOL_VERSION,
        HEADER_SIZE,
        int(normalized_channel),
        normalized_flags,
        seq,
        epoch,
        len(raw),
    )
    header = prefix + _CRC32.pack(crc32(prefix))
    return header + raw + _CRC32.pack(crc32(raw))


def encode_control_payload(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise SerialProtocolError("control payload must be an object")
    raw = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_CONTROL_PAYLOAD:
        raise SerialProtocolError("control JSON exceeds channel limit")
    return raw


def decode_control_payload(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_CONTROL_PAYLOAD:
        raise SerialProtocolError("control JSON exceeds channel limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SerialProtocolError("invalid control JSON") from exc
    if not isinstance(value, dict):
        raise SerialProtocolError("control JSON must be an object")
    return value


class FrameDecoder:
    """Incrementally decode and resynchronise a serial byte stream."""

    def __init__(self, *, max_payload: int = MAX_PAYLOAD) -> None:
        self._buffer = bytearray()
        self._max_payload = min(MAX_PAYLOAD, max(1, int(max_payload)))
        self.discarded_bytes = 0
        self.invalid_headers = 0
        self.invalid_payloads = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def _discard_prefix(self, count: int) -> None:
        if count <= 0:
            return
        del self._buffer[:count]
        self.discarded_bytes += count

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        if data:
            self._buffer.extend(data)
        decoded: list[Frame] = []

        while True:
            marker = self._buffer.find(MAGIC_BYTES)
            if marker < 0:
                # A split magic marker can contribute at most its final three
                # bytes to the next read.
                keep = min(len(MAGIC_BYTES) - 1, len(self._buffer))
                self._discard_prefix(len(self._buffer) - keep)
                return decoded
            if marker:
                self._discard_prefix(marker)
            if len(self._buffer) < HEADER_SIZE:
                return decoded

            (
                magic,
                version,
                header_size,
                channel_raw,
                flags_raw,
                sequence,
                session_epoch,
                payload_len,
                header_crc,
            ) = _HEADER.unpack_from(self._buffer)
            # Do not retain a memoryview while resynchronisation may resize the
            # bytearray; CPython correctly rejects that with ``BufferError``.
            prefix = bytes(self._buffer[: _HEADER_PREFIX.size])

            valid_header = (
                magic == MAGIC
                and version == PROTOCOL_VERSION
                and header_size == HEADER_SIZE
                and crc32(prefix) == header_crc
            )
            try:
                channel = Channel(channel_raw)
            except ValueError:
                valid_header = False
                channel = Channel.CONTROL_JSON
            if valid_header:
                limit = min(self._max_payload, channel_payload_limit(channel))
                valid_header = payload_len <= limit
            if not valid_header:
                self.invalid_headers += 1
                self._discard_prefix(1)
                continue

            frame_size = HEADER_SIZE + payload_len + PAYLOAD_CRC_SIZE
            if len(self._buffer) < frame_size:
                return decoded
            payload_start = HEADER_SIZE
            payload_end = payload_start + payload_len
            payload = bytes(self._buffer[payload_start:payload_end])
            (payload_crc,) = _CRC32.unpack_from(self._buffer, payload_end)
            if crc32(payload) != payload_crc:
                self.invalid_payloads += 1
                # The validated length provides a safe boundary.  Dropping the
                # complete corrupt frame avoids treating magic-like audio data
                # as a nested frame.
                self._discard_prefix(frame_size)
                continue

            decoded.append(
                Frame(
                    channel=channel,
                    flags=FrameFlag(flags_raw),
                    sequence=sequence,
                    session_epoch=session_epoch,
                    payload=payload,
                )
            )
            del self._buffer[:frame_size]
