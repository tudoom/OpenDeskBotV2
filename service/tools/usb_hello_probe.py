#!/usr/bin/env python3
"""Non-destructive raw DBOT hello probe for one explicit USB CDC port."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskbot_server.infrastructure.serial.protocol import (  # noqa: E402
    Channel,
    FrameDecoder,
    FrameFlag,
    encode_control_payload,
    encode_frame,
)


def _open_port(port: str, *, control_lines_low: bool):
    import serial

    if not control_lines_low:
        return serial.Serial(
            port=port,
            baudrate=115_200,
            timeout=0.05,
            write_timeout=2.0,
        )

    serial_port = serial.Serial()
    serial_port.port = port
    serial_port.baudrate = 115_200
    serial_port.timeout = 0.05
    serial_port.write_timeout = 2.0
    # Set the desired line state before open. ESP32-S3 USB Serial/JTAG uses
    # DTR/RTS for reset/boot control, so an ordinary client must leave both
    # deasserted while attaching to a running application.
    serial_port.dtr = False
    serial_port.rts = False
    serial_port.open()
    return serial_port


def _printable_tail(raw: bytes, limit: int = 2048) -> str:
    return "".join(
        chr(value) if value in (9, 10, 13) or 32 <= value < 127 else "."
        for value in raw[-limit:]
    )


def _printable_head(raw: bytes, limit: int = 8192) -> str:
    return "".join(
        chr(value) if value in (9, 10, 13) or 32 <= value < 127 else "."
        for value in raw[:limit]
    )


def run(port: str, *, seconds: float, control_lines_low: bool) -> dict:
    nonce = secrets.randbelow(0xFFFFFFFF) + 1
    payload = encode_control_payload(
        {
            "type": "hello",
            "protocol": 1,
            "client": "deskbot-pc-probe",
            "client_nonce": nonce,
        }
    )
    hello_frame = encode_frame(
        Channel.CONTROL_JSON,
        payload,
        sequence=1,
        session_epoch=0,
        flags=FrameFlag.JSON,
    )
    decoder = FrameDecoder()
    raw = bytearray()
    frames = []
    serial_port = _open_port(
        port,
        control_lines_low=control_lines_low,
    )
    started = time.monotonic()
    next_hello = started + 0.25
    writes = 0
    try:
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            if now >= next_hello:
                serial_port.write(hello_frame)
                serial_port.flush()
                writes += 1
                next_hello = now + 1.0
            chunk = serial_port.read(4096)
            if chunk:
                raw.extend(chunk)
                frames.extend(decoder.feed(chunk))
    finally:
        serial_port.close()

    decoded = []
    frame_counts: Counter[str] = Counter()
    control_frames = []
    log_tail = []
    for frame in frames:
        frame_counts[frame.channel.name] += 1
        entry = {
            "channel": frame.channel.name,
            "flags": int(frame.flags),
            "sequence": frame.sequence,
            "session_epoch": frame.session_epoch,
            "payload_length": len(frame.payload),
        }
        if frame.channel == Channel.CONTROL_JSON:
            try:
                entry["payload"] = json.loads(frame.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                entry["payload_text"] = frame.payload.decode(
                    "utf-8",
                    errors="replace",
                )
            control_frames.append(entry)
        elif frame.channel == Channel.LOG:
            log_tail.append(frame.payload.decode("utf-8", errors="replace"))
            log_tail[:] = log_tail[-100:]
        if len(decoded) < 20:
            decoded.append(entry)
    return {
        "port": port,
        "control_lines_low": control_lines_low,
        "duration_seconds": seconds,
        "hello_writes": writes,
        "raw_bytes": len(raw),
        "raw_magic_markers": raw.count(b"DBOT"),
        "decoded_frame_count": len(frames),
        "frame_counts": dict(frame_counts),
        "control_frames": control_frames[-20:],
        "log_tail": log_tail,
        "decoded_frame_sample": decoded,
        "decoder": {
            "buffered_bytes": decoder.buffered_bytes,
            "discarded_bytes": decoder.discarded_bytes,
            "invalid_headers": decoder.invalid_headers,
            "invalid_payloads": decoder.invalid_payloads,
        },
        "raw_printable_head": _printable_head(bytes(raw)),
        "raw_printable_tail": _printable_tail(bytes(raw)),
        "raw_hex_tail": bytes(raw[-256:]).hex(" "),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument(
        "--default-control-lines",
        action="store_true",
        help="use pyserial's default DTR/RTS state instead of deasserting both",
    )
    args = parser.parse_args()
    result = run(
        args.port,
        seconds=max(1.0, args.seconds),
        control_lines_low=not args.default_control_lines,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["decoded_frame_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
