"""舵机交错、PCM 对齐与 pb JSON wire 消息。"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from deskbot_server.constants import (
    PB_MAX_WIRE_JSON_BYTES,
    pb_max_chunk_ms_for_pcm,
)
from deskbot_server.pb.shapes import (
    PB_ACTION_REPLACE,
    PB_LEVEL_TASK,
    attach_pb_device_hints,
    normalize_anim_list_for_wire,
)
from deskbot_server.servo_protocol import (
    SERVO_MAX_BATCH_DURATION_MS,
    SERVO_MAX_SEGMENTS_PER_PB,
    ServoProtocolError,
    validate_servo_steps,
)

logger = logging.getLogger("deskbot-server")

# 联调：单片时长上限；受 ESP32 WS BINARY 单帧上限约束（默认 10000ms≈480KB@24kHz）
_PB_CHUNK_MS_USER = max(100, int(os.environ.get("PB_CHUNK_MS_MAX", "10000")))
PB_CHUNK_MS_MAX = min(_PB_CHUNK_MS_USER, pb_max_chunk_ms_for_pcm(24000))
if PB_CHUNK_MS_MAX < _PB_CHUNK_MS_USER:
    logger.info(
        "[pb] PB_CHUNK_MS_MAX 由 %d 收紧为 %d（ESP32 PCM 单帧上限，约 %d 字节）",
        _PB_CHUNK_MS_USER,
        PB_CHUNK_MS_MAX,
        (PB_CHUNK_MS_MAX * 24000 // 1000) * 2,
    )


def make_anim_item(
    elements: dict[str, Any],
    ms: int,
    *,
    phoneme: str | None = None,
) -> dict[str, Any]:
    """构造 ``anim[]`` 单项：``{ elements, ms, phoneme? }``。"""
    item: dict[str, Any] = {
        "elements": copy.deepcopy(elements),
        "ms": max(1, int(ms)),
    }
    ph = str(phoneme or "").strip()
    if ph:
        item["phoneme"] = ph
    return item


def anim_elements_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """从 anim 行读取 ``elements``（兼容旧 ``anim: {elements}``）。"""
    anim = row.get("anim")
    if isinstance(anim, list):
        for one in anim:
            if isinstance(one, dict) and isinstance(one.get("elements"), dict):
                return one["elements"]
    if isinstance(anim, dict) and isinstance(anim.get("elements"), dict):
        return anim["elements"]
    return {}


def normalize_row_anim_list(row: dict[str, Any]) -> list[dict[str, Any]]:
    """保证 ``row['anim']`` 为 ``[{elements, ms, phoneme?}, ...]``。"""
    anim = row.get("anim")
    chunk_ms = max(1, int(row.get("chunk_ms") or 0))
    if isinstance(anim, list):
        out: list[dict[str, Any]] = []
        for one in anim:
            if not isinstance(one, dict):
                continue
            els = one.get("elements")
            if not isinstance(els, dict):
                continue
            ms = max(1, int(one.get("ms") or chunk_ms))
            out.append(make_anim_item(els, ms, phoneme=one.get("phoneme")))
        if out:
            return out
    if isinstance(anim, dict) and isinstance(anim.get("elements"), dict):
        ph = str(row.get("phoneme") or "").strip() or None
        return [make_anim_item(anim["elements"], chunk_ms, phoneme=ph)]
    return []


def merge_pb_subchunks(
    rows: list[dict[str, Any]],
    pcm_list: list[bytes],
    *,
    sample_rate: int,
    max_chunk_ms: int = PB_CHUNK_MS_MAX,
    max_wire_json_bytes: int = PB_MAX_WIRE_JSON_BYTES,
) -> tuple[list[dict[str, Any]], list[bytes]]:
    """将细粒度分片合并为 ``chunk_ms <= max_chunk_ms`` 的 pb 行。"""
    if not rows:
        return [], []

    merged_rows: list[dict[str, Any]] = []
    merged_pcm: list[bytes] = []
    batch_anim: list[dict[str, Any]] = []
    batch_servos: list[dict[str, Any]] = []
    batch_pcm = b""
    batch_ms = 0
    batch_has_audio: bool | None = None
    # Keep room for the outer PB envelope, audio metadata, device hints and
    # the timestamp added immediately before transport.
    row_json_budget = max(2048, int(max_wire_json_bytes) - 2048)

    def _row_json_size(
        animations: list[dict[str, Any]],
        servos: list[dict[str, Any]],
        duration_ms: int,
    ) -> int:
        candidate: dict[str, Any] = {
            "chunk_ms": duration_ms,
            "anim": animations,
        }
        if servos:
            candidate["servo"] = servos
        return len(
            json.dumps(
                candidate,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def _flush() -> None:
        nonlocal batch_anim, batch_servos, batch_pcm, batch_ms, batch_has_audio
        if batch_ms <= 0:
            batch_servos = []
            batch_pcm = b""
            batch_ms = 0
            batch_has_audio = None
            return
        row: dict[str, Any] = {"chunk_ms": batch_ms}
        if batch_anim:
            row["anim"] = batch_anim
        if batch_servos:
            row["servo"] = batch_servos
        merged_rows.append(row)
        merged_pcm.append(batch_pcm)
        batch_anim = []
        batch_servos = []
        batch_pcm = b""
        batch_ms = 0
        batch_has_audio = None

    def _slice_anim_items(
        items: list[dict[str, Any]],
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        sliced: list[dict[str, Any]] = []
        cursor = 0
        for item in items:
            item_ms = max(1, int(item.get("ms") or 1))
            overlap_start = max(start_ms, cursor)
            overlap_end = min(end_ms, cursor + item_ms)
            if overlap_end > overlap_start:
                piece = copy.deepcopy(item)
                piece["ms"] = overlap_end - overlap_start
                sliced.append(piece)
            cursor += item_ms
            if cursor >= end_ms:
                break
        return sliced

    for i, row in enumerate(rows):
        row_ms = max(1, int(row.get("chunk_ms") or 0))
        anim_items = normalize_row_anim_list(row)
        pcm = pcm_list[i] if i < len(pcm_list) else b""
        servos = row.get("servo")
        servo_items: list[dict[str, Any]] = []
        if isinstance(servos, list):
            raw_servo_items = servos
        elif isinstance(servos, dict) and "xm" in servos and "ym" in servos:
            raw_servo_items = [servos]
        elif servos is None:
            raw_servo_items = []
        else:
            raise ServoProtocolError(f"PB row {i} servo must be an array")
        if raw_servo_items:
            checked, _duration = validate_servo_steps(
                raw_servo_items,
                context=f"PB row {i} servo",
            )
            for cmd in checked:
                item = {
                    "xm": cmd["xm"],
                    "ym": cmd["ym"],
                    "x": cmd["x"],
                    "y": cmd["y"],
                    "ms": cmd["ms"],
                }
                for key in ("x_min", "x_max", "y_min", "y_max"):
                    if key in cmd:
                        item[key] = cmd[key]
                servo_items.append(item)

        has_audio = bool(pcm)
        if batch_ms and batch_has_audio is not has_audio:
            _flush()
        # The firmware schedules ``servo[]`` contiguously from the PB row's
        # start.  If a previous long command crossed a merge boundary, start a
        # fresh row exactly where the next command begins rather than moving it
        # earlier to the current batch origin.
        if servo_items and batch_ms:
            declared_servo_ms = sum(int(cmd["ms"]) for cmd in batch_servos)
            if declared_servo_ms != batch_ms:
                _flush()

        if row_ms > max_chunk_ms:
            _flush()
            if pcm:
                offset_ms = 0
                while offset_ms < row_ms:
                    end_ms = min(row_ms, offset_ms + max_chunk_ms)
                    first_sample = offset_ms * sample_rate // 1000
                    last_sample = end_ms * sample_rate // 1000
                    one_row: dict[str, Any] = {"chunk_ms": end_ms - offset_ms}
                    sliced_anim = _slice_anim_items(
                        anim_items, offset_ms, end_ms
                    )
                    if sliced_anim:
                        one_row["anim"] = sliced_anim
                    if offset_ms == 0 and servo_items:
                        one_row["servo"] = copy.deepcopy(servo_items)
                    merged_rows.append(one_row)
                    merged_pcm.append(pcm[first_sample * 2 : last_sample * 2])
                    offset_ms = end_ms
                continue
            one_row: dict[str, Any] = {"chunk_ms": row_ms}
            if anim_items:
                one_row["anim"] = anim_items
            if servo_items:
                one_row["servo"] = servo_items
            merged_rows.append(one_row)
            merged_pcm.append(pcm)
            continue

        candidate_anim = batch_anim + anim_items
        candidate_servos = batch_servos + servo_items
        candidate_servo_ms = sum(int(cmd["ms"]) for cmd in candidate_servos)
        exceeds_servo_budget = (
            len(candidate_servos) > SERVO_MAX_SEGMENTS_PER_PB
            or candidate_servo_ms > SERVO_MAX_BATCH_DURATION_MS
        )
        exceeds_wire_budget = (
            bool(batch_ms)
            and _row_json_size(
                candidate_anim,
                candidate_servos,
                batch_ms + row_ms,
            )
            > row_json_budget
        )
        if batch_ms and (
            batch_ms + row_ms > max_chunk_ms
            or exceeds_wire_budget
            or exceeds_servo_budget
        ):
            _flush()

        batch_anim.extend(copy.deepcopy(anim_items))
        batch_servos.extend(servo_items)
        batch_pcm += pcm
        batch_ms += row_ms
        batch_has_audio = has_audio

    _flush()

    for i, row in enumerate(merged_rows):
        raw_pcm = merged_pcm[i] if i < len(merged_pcm) else b""
        # 纯表情/场景链：PCM 故意为空，勿按 chunk_ms 填静音（否则固件 expect BIN）
        if not raw_pcm:
            continue
        aligned, cms2 = align_pcm_s16le_mono_to_chunk_ms(
            raw_pcm, int(row.get("chunk_ms") or 0), sample_rate
        )
        merged_pcm[i] = aligned
        if cms2 != int(row.get("chunk_ms") or 0):
            row["chunk_ms"] = cms2

    return merged_rows, merged_pcm


def apply_parallel_pb_servo(
    anim_rows: list[dict[str, Any]],
    parallel: list[dict[str, int] | list[dict[str, int]] | None] | None,
) -> int:
    """按与 ``anim_rows`` 等长的 ``parallel`` 写入 ``servo[]``；``None`` 表示该片不附加舵机。"""
    if not parallel:
        return 0
    n = 0
    for i, row in enumerate(anim_rows):
        if i >= len(parallel):
            break
        value = parallel[i]
        commands = value if isinstance(value, list) else [value]
        commands = [cmd for cmd in commands if isinstance(cmd, dict)]
        if not commands:
            continue
        checked, _duration = validate_servo_steps(
            commands,
            context=f"parallel PB servo row {i}",
        )
        for cmd in checked:
            item = {
                "xm": cmd["xm"],
                "ym": cmd["ym"],
                "x": cmd["x"],
                "y": cmd["y"],
                "ms": cmd["ms"],
            }
            for key in ("x_min", "x_max", "y_min", "y_max"):
                if key in cmd:
                    item[key] = cmd[key]
            row.setdefault("servo", []).append(item)
            n += 1
    return n


def align_pcm_s16le_mono_to_chunk_ms(
    pcm: bytes, chunk_ms: int, sample_rate: int
) -> tuple[bytes, int]:
    """把 mono s16le PCM 对齐到 ``chunk_ms * sample_rate // 1000 * 2`` 字节。

    与常见设备校验公式一致：``expect_len = chunk_ms * sr / 1000 * 2``（整除）。
    音素均分切片可能多出几个采样，不处理会导致 ``binary length mismatch``。

    若 ``chunk_ms<=0`` 但有 PCM，则用 PCM 长度反推 ``chunk_ms``（floor ms）。
    空 PCM 表示该时间片明确为 audio-free，必须原样保留，不能按 ``chunk_ms``
    制造静音 binary。
    """
    pcm = pcm[: len(pcm) & ~1]
    if not pcm:
        return b"", max(0, int(chunk_ms))
    if sample_rate <= 0:
        return pcm, max(0, chunk_ms)
    if chunk_ms <= 0:
        if not pcm:
            return pcm, 0
        chunk_ms = max(1, (len(pcm) // 2) * 1000 // sample_rate)
    expected = (chunk_ms * sample_rate // 1000) * 2
    if expected <= 0:
        return pcm, chunk_ms
    if len(pcm) > expected:
        return pcm[:expected], chunk_ms
    if len(pcm) < expected:
        return pcm + b"\x00" * (expected - len(pcm)), chunk_ms
    return pcm, chunk_ms


def parse_pb_volume(raw: Any) -> int | None:
    """解析 ``volume``（0–100）；无效或空则 ``None``（wire 省略，设备不改动）。"""
    if raw is None or raw == "":
        return None
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return None


def parse_pb_cam_fps(raw: Any) -> int | None:
    """解析 ``cam_fps``（>0）；无效或 0 则 ``None``（wire 省略，设备不改动）。"""
    if raw is None or raw == "":
        return None
    try:
        fps = int(raw)
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def resolve_pb_volume_hint(volume: int | None = None) -> int | None:
    """仅当调用方显式传入 ``volume`` 时写入 pb；否则 wire 省略。"""
    return parse_pb_volume(volume) if volume is not None else None




def pb_expected_binary_lengths(msg: dict[str, Any]) -> list[int]:
    """JSON 之后按序读取的 binary 长度：PCM + ``assets[]``。"""
    out: list[int] = []
    audio_n = int((msg.get("audio") or {}).get("next_bin_len") or 0)
    if audio_n > 0:
        out.append(audio_n)
    assets = msg.get("assets")
    if isinstance(assets, list):
        for one in assets:
            if not isinstance(one, dict):
                continue
            n = int(one.get("next_bin_len") or 0)
            if n > 0:
                out.append(n)
    return out


def pb_json_messages(
    *,
    pb_req: str,
    sample_rate: int,
    fmt: str,
    channels: int,
    anim_rows: list[dict[str, Any]],
    pcm_per_idx: list[bytes],
    assets_per_idx: list[list[bytes]] | None = None,
    opus_frames_per_idx: list[int] | None = None,
    action: str = PB_ACTION_REPLACE,
    level: int = PB_LEVEL_TASK,
    volume: int | None = None,
    cam_fps: int | None = None,
    mouth_only: bool = False,
) -> list[tuple[dict[str, Any], list[bytes]]]:
    """生成 ``(pb 字典, 紧随 binary 列表)``；binary 顺序：PCM（若有）→ ``assets[]``。

    单片 ``n == 1`` 使用 ``pb_single``；多片为 ``pb_start`` → ``pb_chunk``* → ``pb_end``。

    ``action``：``replace`` / ``append`` / ``default``；``level``：0–3，语义见协议文档。
    缺省 ``replace`` + ``level=1``（任务态）。

    ``volume`` / ``cam_fps``：仅显式传入时写入 pb；省略表示设备保持现状。
    """
    n = len(anim_rows)
    if n == 0:
        return []
    pairs: list[tuple[dict[str, Any], list[bytes]]] = []
    assets_per_idx = assets_per_idx or []
    audio_format_declared = False
    for i in range(n):
        row = anim_rows[i]
        is_first = i == 0
        is_last = i == n - 1
        if n == 1:
            # 单片自成一轮：下位机要求 type=pb_single，勿单发 pb_end（多片仍 start→…→end）
            typ = "pb_single"
        elif is_first:
            typ = "pb_start"
        elif is_last:
            typ = "pb_end"
        else:
            typ = "pb_chunk"
        pcm = pcm_per_idx[i] if i < len(pcm_per_idx) else b""
        row_assets = list(anim_rows[i].get("_assets") or [])
        if i < len(assets_per_idx) and assets_per_idx[i]:
            row_assets = list(assets_per_idx[i])
        try:
            anim_list = normalize_anim_list_for_wire(normalize_row_anim_list(row))
        except ValueError as exc:
            raise ServoProtocolError(
                f"PB wire message {i} cannot produce valid JSON bytes: {exc}"
            ) from exc
        msg: dict[str, Any] = {
            "type": typ,
            "req": pb_req,
            "idx": i,
            "chunk_ms": int(row.get("chunk_ms") or 0),
            "pb_ver": 2,
            "action": action,
            "level": int(level),
        }
        if anim_list:
            msg["anim"] = anim_list
        if is_first and mouth_only and anim_list:
            # A speech PB changes only the mouth and inherits the complete
            # face already displayed by the common expression runtime.
            msg["mouth_only"] = True
        servos = row.get("servo")
        if isinstance(servos, list) and servos:
            checked, _duration = validate_servo_steps(
                servos,
                context=f"PB wire message {i} servo",
            )
            msg["servo"] = copy.deepcopy(checked)
        elif servos not in (None, []):
            raise ServoProtocolError(f"PB wire message {i} servo must be an array")
        # 无 PCM 时不要带 sr/fmt/ch/audio（与 /api/device_pb_anim 一致），避免固件按 R6 误等 binary
        if pcm:
            if not audio_format_declared:
                msg["sr"] = int(sample_rate)
                msg["fmt"] = fmt
                msg["ch"] = int(channels)
                audio_format_declared = True
            audio_obj: dict[str, Any] = {"next_bin_len": len(pcm)}
            if fmt == "opus" and opus_frames_per_idx and i < len(opus_frames_per_idx):
                audio_obj["frames"] = int(opus_frames_per_idx[i])
            msg["audio"] = audio_obj
        if row_assets:
            from deskbot_server.pb.llm_display import jpeg_blob_dimensions

            msg["assets"] = [
                {
                    "fmt": "jpeg",
                    "next_bin_len": len(blob),
                    "w": aw,
                    "h": ah,
                }
                for blob in row_assets
                for aw, ah in (jpeg_blob_dimensions(blob),)
            ]
        attach_pb_device_hints(msg, volume=volume, cam_fps=cam_fps)
        wire_json_bytes = len(
            json.dumps(
                msg,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        # Transport adds ``t_mono`` immediately before sending.  Reserve a
        # small fixed margin and fail closed instead of relying on the old
        # warning-only compactor after an atomic servo plan has been built.
        if wire_json_bytes + 64 > PB_MAX_WIRE_JSON_BYTES:
            raise ServoProtocolError(
                f"PB wire message {i} exceeds {PB_MAX_WIRE_JSON_BYTES} JSON bytes"
            )
        binaries: list[bytes] = []
        if pcm:
            binaries.append(pcm)
        binaries.extend(row_assets)
        pairs.append((msg, binaries))
    return pairs
