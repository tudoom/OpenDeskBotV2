#!/usr/bin/env python3
"""End-to-end smoke test for one physical Deskbot USB CDC device.

The script deliberately requires an explicit port and refuses non-Espressif
USB devices. It must not run while the :9000 core service owns the same port.
No durable PB command is sent, so a reboot returns the device to its normal
runtime state.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import secrets
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from deskbot_server.infrastructure.serial.protocol import (  # noqa: E402
    Channel,
    Frame,
    FrameFlag,
)
from deskbot_server.infrastructure.serial.session import DeviceSession  # noqa: E402
from deskbot_server.pipeline.opus_downlink import (  # noqa: E402
    encode_pcm_s16le_to_opus_batch,
)

_ESPRESSIF_USB_VID = 0x303A
_REQUIRED_CAPABILITIES = {
    "control_json",
    "pb_wire",
    "audio_up_opus",
    "audio_down_opus",
    "camera_jpeg",
    "log",
    "frame_ack",
}
_TERMINAL_PB_PHASES = {"played", "cancelled", "failed"}
_MAX_PB_BINARY_SEND_SECONDS = 0.5
_STANDALONE_OPUS_BATCH_MS = 100


@dataclass
class SmokeState:
    camera_dump_dir: Path | None = None
    camera_dump_limit: int = 0
    frame_counts: Counter[str] = field(default_factory=Counter)
    audio_up_bytes: int = 0
    camera_bytes: int = 0
    camera_frames: int = 0
    marker_jpegs: int = 0
    valid_jpegs: int = 0
    camera_decode_errors: list[str] = field(default_factory=list)
    camera_dumps: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    pb_phases: dict[str, list[str]] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def on_frame(self, _session: DeviceSession, frame: Frame) -> None:
        self.frame_counts[frame.channel.name] += 1
        if frame.channel == Channel.AUDIO_UP_OPUS:
            self.audio_up_bytes += len(frame.payload)
        elif frame.channel == Channel.CAMERA_JPEG:
            self.camera_frames += 1
            self.camera_bytes += len(frame.payload)
            has_markers = frame.payload.startswith(
                b"\xff\xd8"
            ) and frame.payload.endswith(b"\xff\xd9")
            decode_error: str | None = None
            is_valid = False
            if has_markers:
                self.marker_jpegs += 1
                try:
                    with Image.open(io.BytesIO(frame.payload)) as image:
                        if image.format != "JPEG":
                            raise ValueError(
                                f"unexpected image format {image.format!r}"
                            )
                        image.load()
                    self.valid_jpegs += 1
                    is_valid = True
                except Exception as exc:
                    decode_error = f"{type(exc).__name__}: {exc}"
                    self.camera_decode_errors.append(decode_error)
                    self.camera_decode_errors[:] = self.camera_decode_errors[-20:]
            if (
                self.camera_dump_dir is not None
                and len(self.camera_dumps) < self.camera_dump_limit
            ):
                status = (
                    "valid"
                    if is_valid
                    else ("marker-invalid" if has_markers else "bad-markers")
                )
                suffix = ".jpg" if has_markers else ".bin"
                path = self.camera_dump_dir / (
                    f"frame-{self.camera_frames:04d}-{status}-"
                    f"{len(frame.payload)}B{suffix}"
                )
                path.write_bytes(frame.payload)
                self.camera_dumps.append(str(path.resolve()))
        elif frame.channel == Channel.LOG:
            self.logs.append(
                frame.payload.decode("utf-8", errors="replace").rstrip()
            )
            self.logs[:] = self.logs[-200:]
        elif frame.channel == Channel.PB_WIRE and frame.flags & FrameFlag.JSON:
            try:
                message = json.loads(frame.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = None
            if isinstance(message, dict) and message.get("type") == "pb_ack":
                request_id = str(message.get("req") or "")
                phase = str(message.get("phase") or "")
                if request_id and phase:
                    self.pb_phases.setdefault(request_id, []).append(phase)
        self.changed.set()


def _find_verified_port(port: str):
    from serial.tools import list_ports

    wanted = port.casefold()
    match = next(
        (
            info
            for info in list_ports.comports()
            if str(info.device or "").casefold() == wanted
        ),
        None,
    )
    if match is None:
        raise RuntimeError(f"serial port is not present: {port}")
    if match.vid != _ESPRESSIF_USB_VID:
        vid = "unknown" if match.vid is None else f"{match.vid:04X}"
        pid = "unknown" if match.pid is None else f"{match.pid:04X}"
        raise RuntimeError(
            f"refusing non-Espressif port {port} (VID:PID={vid}:{pid})"
        )
    return match


def _sine_pcm(
    *,
    sample_rate: int,
    duration_ms: int,
    frequency_hz: float,
    amplitude: int = 1400,
) -> bytes:
    samples = max(1, sample_rate * duration_ms // 1000)
    values = (
        int(amplitude * math.sin(2.0 * math.pi * frequency_hz * i / sample_rate))
        for i in range(samples)
    )
    return b"".join(struct.pack("<h", value) for value in values)


async def _wait_until(
    state: SmokeState,
    predicate,
    *,
    timeout: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {description}")
        state.changed.clear()
        try:
            await asyncio.wait_for(
                state.changed.wait(),
                timeout=min(0.25, remaining),
            )
        except TimeoutError:
            pass


async def _wait_pb_phase(
    state: SmokeState,
    request_id: str,
    phases: set[str],
    *,
    timeout: float,
) -> str:
    await _wait_until(
        state,
        lambda: any(
            phase in phases
            for phase in state.pb_phases.get(request_id, ())
        ),
        timeout=timeout,
        description=f"PB {request_id} phase in {sorted(phases)}",
    )
    return next(
        phase
        for phase in reversed(state.pb_phases[request_id])
        if phase in phases
    )


def _pb_base(request_id: str, *, chunk_ms: int) -> dict[str, Any]:
    return {
        "type": "pb_single",
        "req": request_id,
        "idx": 0,
        "chunk_ms": chunk_ms,
        "pb_ver": 2,
        "action": "replace",
        "level": 3,
        "anim": [{"elements": {}, "ms": chunk_ms}],
    }


def _hold_servo_segment(duration_ms: int) -> dict[str, int]:
    """Return a schema-complete PB servo segment that cannot move either axis."""
    return {"x": 0, "y": 0, "xm": 2, "ym": 2, "ms": duration_ms}


async def _drain_messages(session: DeviceSession) -> None:
    async for _message in session:
        pass


async def _set_mic_hint(
    session: DeviceSession,
    state: SmokeState,
    mic: str,
) -> None:
    request_id = secrets.token_hex(8)
    message = {
        "type": "pb_single",
        "req": request_id,
        "idx": 0,
        "chunk_ms": 1,
        "pb_ver": 2,
        "action": "replace",
        "level": 3,
        "mic": mic,
    }
    await session.send_pb_wire(
        json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    )
    terminal = await _wait_pb_phase(
        state,
        request_id,
        _TERMINAL_PB_PHASES,
        timeout=4.0,
    )
    if terminal != "played":
        raise RuntimeError(
            f"PB microphone {mic!r} terminal phase was {terminal!r}"
        )


async def run_smoke(
    port: str,
    *,
    observe_seconds: float,
    require_camera: bool = True,
    test_standalone_opus: bool = True,
    minimum_valid_jpegs: int = 1,
    mute_mic_during_observe: bool = False,
    camera_dump_dir: Path | None = None,
    camera_dump_limit: int = 20,
) -> dict[str, Any]:
    import serial

    port_info = _find_verified_port(port)
    if camera_dump_dir is not None:
        camera_dump_dir.mkdir(parents=True, exist_ok=True)
    serial_port = serial.Serial(
        port=None,
        baudrate=115_200,
        timeout=0.02,
        write_timeout=2.0,
    )
    serial_port.port = port_info.device
    serial_port.dtr = False
    serial_port.rts = False
    serial_port.open()
    state = SmokeState(
        camera_dump_dir=camera_dump_dir,
        camera_dump_limit=max(0, camera_dump_limit),
    )
    session = DeviceSession(
        port_info.device,
        serial_port,
        generation=1,
        on_frame=state.on_frame,
        hello_timeout=8.0,
    )
    consumer: asyncio.Task[None] | None = None
    mic_is_muted = False
    try:
        await session.start()
        consumer = asyncio.create_task(
            _drain_messages(session),
            name="deskbot-usb-smoke-drain",
        )
        hello = await session.wait_ready(timeout=8.0)
        missing = _REQUIRED_CAPABILITIES.difference(hello.capabilities)
        if missing:
            raise RuntimeError(
                f"device is missing capabilities: {sorted(missing)}"
            )

        if mute_mic_during_observe:
            await _wait_until(
                state,
                lambda: (
                    state.frame_counts[Channel.AUDIO_UP_OPUS.name] > 0
                    and state.frame_counts[Channel.LOG.name] > 0
                ),
                timeout=4.0,
                description="initial audio uplink and device log",
            )
            await _set_mic_hint(session, state, "mute")
            mic_is_muted = True

        try:
            await _wait_until(
                state,
                lambda: (
                    state.frame_counts[Channel.AUDIO_UP_OPUS.name] > 0
                    and state.frame_counts[Channel.LOG.name] > 0
                    and (
                        not require_camera
                        or state.valid_jpegs >= max(1, minimum_valid_jpegs)
                    )
                ),
                timeout=max(3.0, observe_seconds),
                description=(
                    "audio uplink, valid JPEG, and framed device log"
                    if require_camera
                    else "audio uplink and framed device log"
                ),
            )
        except TimeoutError as exc:
            diagnostics = session.diagnostics()
            camera_logs = [
                line
                for line in state.logs
                if "cam" in line.casefold() or "jpeg" in line.casefold()
            ]
            raise RuntimeError(
                f"{exc}; frame_counts={dict(state.frame_counts)!r}; "
                f"audio_up_bytes={state.audio_up_bytes}; "
                f"camera_bytes={state.camera_bytes}; "
                f"marker_jpegs={state.marker_jpegs}; "
                f"valid_jpegs={state.valid_jpegs}; "
                f"camera_decode_errors={state.camera_decode_errors[-5:]!r}; "
                f"camera_dumps={state.camera_dumps!r}; "
                f"camera_log_tail={camera_logs[-20:]!r}; "
                f"log_tail={state.logs[-5:]!r}; "
                f"session={diagnostics!r}"
            ) from exc

        if mic_is_muted:
            await _set_mic_hint(session, state, "open")
            mic_is_muted = False

        await session.send_control({"factory": "head_pos"})
        await _wait_until(
            state,
            lambda: any("[HEAD]" in line for line in state.logs),
            timeout=3.0,
            description="head position device log",
        )

        control_req = secrets.token_hex(8)
        control_pb = _pb_base(control_req, chunk_ms=250)
        control_pb["servo"] = [_hold_servo_segment(250)]
        await session.send_pb_wire(
            json.dumps(control_pb, ensure_ascii=False, separators=(",", ":"))
        )
        control_terminal = await _wait_pb_phase(
            state,
            control_req,
            _TERMINAL_PB_PHASES,
            timeout=5.0,
        )
        if control_terminal != "played":
            raise RuntimeError(
                f"PB control terminal phase was {control_terminal!r}"
            )

        audio_req = secrets.token_hex(8)
        pcm = _sine_pcm(
            sample_rate=24_000,
            duration_ms=180,
            frequency_hz=523.25,
        )
        audio_pb = _pb_base(audio_req, chunk_ms=180)
        audio_pb.update(
            {
                "sr": 24_000,
                "fmt": "s16le",
                "ch": 1,
                "audio": {"next_bin_len": len(pcm)},
            }
        )
        await session.send_pb_wire(
            json.dumps(audio_pb, ensure_ascii=False, separators=(",", ":"))
        )
        binary_send_started = time.monotonic()
        await session.send_pb_binary(pcm)
        binary_send_seconds = time.monotonic() - binary_send_started
        if binary_send_seconds > _MAX_PB_BINARY_SEND_SECONDS:
            raise RuntimeError(
                "PB binary transport is too slow: "
                f"{binary_send_seconds:.3f}s > "
                f"{_MAX_PB_BINARY_SEND_SECONDS:.3f}s"
            )
        audio_terminal = await _wait_pb_phase(
            state,
            audio_req,
            _TERMINAL_PB_PHASES,
            timeout=6.0,
        )
        if audio_terminal != "played":
            raise RuntimeError(
                f"PB audio terminal phase was {audio_terminal!r}"
            )

        cancel_req = secrets.token_hex(8)
        cancel_pb = _pb_base(cancel_req, chunk_ms=4_000)
        cancel_pb["servo"] = [_hold_servo_segment(4_000)]
        await session.send_pb_wire(
            json.dumps(cancel_pb, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            await _wait_pb_phase(
                state,
                cancel_req,
                {"accepted"},
                timeout=1.5,
            )
        except TimeoutError:
            # Cancellation itself remains authoritative even if an accepted
            # progress ACK was coalesced by the firmware's ACK throttle.
            pass
        await session.send_pb_wire(
            json.dumps(
                {"type": "pb_cancel", "req": cancel_req},
                separators=(",", ":"),
            )
        )
        cancel_terminal = await _wait_pb_phase(
            state,
            cancel_req,
            _TERMINAL_PB_PHASES,
            timeout=5.0,
        )
        if cancel_terminal != "cancelled":
            raise RuntimeError(
                f"PB cancel terminal phase was {cancel_terminal!r}"
            )

        opus_frames = 0
        if test_standalone_opus:
            opus_pcm = _sine_pcm(
                sample_rate=16_000,
                duration_ms=180,
                frequency_hz=659.25,
            )
            pcm_bytes_per_batch = (
                16_000 * 2 * _STANDALONE_OPUS_BATCH_MS // 1000
            )
            opus_batches: list[bytes] = []
            for offset in range(0, len(opus_pcm), pcm_bytes_per_batch):
                opus_batch, batch_frames = (
                    encode_pcm_s16le_to_opus_batch(
                        opus_pcm[offset : offset + pcm_bytes_per_batch],
                        16_000,
                    )
                )
                if not opus_batch or batch_frames < 1:
                    raise RuntimeError(
                        "failed to create standalone Opus smoke tone"
                    )
                opus_batches.append(opus_batch)
                opus_frames += batch_frames
            standalone_log_start = len(state.logs)
            for batch_index, opus_batch in enumerate(opus_batches):
                await session.send_frame(
                    Channel.AUDIO_DOWN_OPUS,
                    opus_batch,
                    flags=(
                        FrameFlag.END_STREAM
                        if batch_index == len(opus_batches) - 1
                        else FrameFlag.NONE
                    ),
                )
            await asyncio.sleep(0.5)
            standalone_errors = [
                line
                for line in state.logs[standalone_log_start:]
                if (
                    "standalone audio" in line
                    or "standalone Opus" in line
                )
                and (
                    "drop " in line
                    or "rejected" in line
                    or "failed" in line
                )
            ]
            if standalone_errors:
                raise RuntimeError(
                    "standalone Opus device path failed: "
                    + standalone_errors[-1]
                )
            if not session.is_ready or session.is_closed:
                raise RuntimeError(
                    "USB session closed during output exercise"
                )

        return {
            "ok": True,
            "port": port_info.device,
            "vid": f"{port_info.vid:04X}",
            "pid": (
                "unknown"
                if port_info.pid is None
                else f"{port_info.pid:04X}"
            ),
            "device_id": hello.device_id,
            "product": hello.product,
            "firmware": hello.firmware,
            "session_epoch": hello.session_epoch,
            "heartbeat_ms": hello.heartbeat_ms,
            "timeout_ms": hello.timeout_ms,
            "capabilities": list(hello.capabilities),
            "frame_counts": dict(state.frame_counts),
            "audio_up_bytes": state.audio_up_bytes,
            "camera_bytes": state.camera_bytes,
            "camera_frames": state.camera_frames,
            "marker_jpegs": state.marker_jpegs,
            "valid_jpegs": state.valid_jpegs,
            "minimum_valid_jpegs": max(1, minimum_valid_jpegs),
            "camera_decode_errors": state.camera_decode_errors,
            "camera_dumps": state.camera_dumps,
            "camera_required": require_camera,
            "mic_muted_during_observe": mute_mic_during_observe,
            "pb_phases": state.pb_phases,
            "pb_binary_send_seconds": round(binary_send_seconds, 3),
            "standalone_opus_frames": opus_frames,
            "standalone_opus_tested": test_standalone_opus,
            "stale_epoch_frames": session.stale_epoch_frames,
            "rx_queue_overflows": session.rx_queue_overflows,
            "message_queue_overflows": session.message_queue_overflows,
        }
    finally:
        if mic_is_muted and session.is_ready and not session.is_closed:
            try:
                await _set_mic_hint(session, state, "open")
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to restore microphone after USB smoke"
                )
        await session.close(reason="USB smoke test complete")
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        required=True,
        help="explicit Espressif USB CDC port, for example COM4",
    )
    parser.add_argument(
        "--observe-seconds",
        type=float,
        default=8.0,
        help="maximum initial camera/audio/log observation window",
    )
    parser.add_argument(
        "--allow-missing-camera",
        action="store_true",
        help="continue PB/audio output checks when no JPEG is observed",
    )
    parser.add_argument(
        "--skip-standalone-opus",
        action="store_true",
        help="skip the optional PC-generated Opus downlink tone",
    )
    parser.add_argument(
        "--min-valid-jpegs",
        type=int,
        default=1,
        help="minimum fully decodable JPEG frames required before PB checks",
    )
    parser.add_argument(
        "--mute-mic-during-observe",
        action="store_true",
        help="mute microphone uplink while collecting the required JPEGs",
    )
    parser.add_argument(
        "--dump-jpegs",
        type=Path,
        help="optional directory for raw CAMERA_JPEG payload captures",
    )
    parser.add_argument(
        "--max-jpeg-dumps",
        type=int,
        default=20,
        help="maximum raw camera payloads to save with --dump-jpegs",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        result = asyncio.run(
            run_smoke(
                args.port,
                observe_seconds=max(1.0, args.observe_seconds),
                require_camera=not args.allow_missing_camera,
                test_standalone_opus=not args.skip_standalone_opus,
                minimum_valid_jpegs=max(1, args.min_valid_jpegs),
                mute_mic_during_observe=args.mute_mic_during_observe,
                camera_dump_dir=args.dump_jpegs,
                camera_dump_limit=max(0, args.max_jpeg_dumps),
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "port": args.port,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
