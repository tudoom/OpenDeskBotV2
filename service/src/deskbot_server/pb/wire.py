"""pb 下行 wire 组帧：音素分片 → anim → JSON+binary 对。"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from typing import Any, Optional

from deskbot_server.constants import PB_MAX_WIRE_JSON_BYTES
from deskbot_server.pb.face_bundle import resolve_pb_face_bundle
from deskbot_server.pb.llm_display import apply_llm_display_to_rows
from deskbot_server.pb.llm_plan import (
    build_anim_rows_for_llm_plan,
    expand_llm_anims,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
)
from deskbot_server.pb.phoneme_anim import phoneme_seq_to_anim_seq
from deskbot_server.pb.servo_pcm import (
    PB_ACTION_REPLACE,
    PB_CHUNK_MS_MAX,
    align_pcm_s16le_mono_to_chunk_ms,
    apply_parallel_pb_servo,
    merge_pb_subchunks,
    pb_json_messages,
    resolve_pb_volume_hint,
)

logger = logging.getLogger("deskbot-server")

def pb_wire_json_bytes(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def compact_pb_wire_payload(
    msg: dict[str, Any], *, max_bytes: int | None = None
) -> dict[str, Any]:
    """设备 pb 下行：保留完整 ``anim[]`` 图元，不做裁剪（仅用于日志与测试）。"""
    out = copy.deepcopy(msg)
    limit = PB_MAX_WIRE_JSON_BYTES if max_bytes is None else max_bytes
    sz = pb_wire_json_bytes(out)
    if sz > limit:
        logger.warning(
            "[pb TX] wire JSON %d bytes 超过参考上限 %d（未裁剪 anim；请确认固件 WS TEXT 缓冲）",
            sz,
            limit,
        )
    return out


def device_pb_json_msg(payload: dict[str, Any]) -> str:
    """设备 pb 下行：完整 anim + 紧凑 JSON + ``t_mono``。"""
    p = compact_pb_wire_payload(payload)
    p.setdefault("t_mono", time.monotonic())
    return json.dumps(p, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "align_pcm_s16le_mono_to_chunk_ms",
    "apply_parallel_pb_servo",
    "build_pb_wire_pairs",
    "compact_pb_wire_payload",
    "device_pb_json_msg",
    "pb_wire_json_bytes",
    "merge_pb_subchunks",
    "pb_json_messages",
    "phoneme_seq_to_anim_seq",
    "resolve_pb_face_bundle",
]


def build_pb_wire_pairs(
    segs: list[dict[str, Any]],
    tts_cfg: dict[str, Any],
    *,
    moves: list[dict[str, Any]] | None = None,
    anims: list[dict[str, Any]] | None = None,
    sample_rate: int,
    request_id: Optional[str] = None,
    volume: int | None = None,
    cam_fps: int | None = None,
    images: list[dict[str, Any]] | None = None,
    action: str = PB_ACTION_REPLACE,
    mouth_only: bool = False,
) -> tuple[list[tuple[dict[str, Any], list[bytes]]], str, int, int]:
    """音素 TTS 分片 → pb wire (msg, binaries) 列表。"""
    face_bundle = resolve_pb_face_bundle(tts_cfg)
    move_steps = expand_llm_moves(moves)
    anim_frames = expand_llm_anims(anims)
    parallel_anim: list[dict[str, Any] | None] | None = None
    parallel_servo: list[
        dict[str, int] | list[dict[str, int]] | None
    ] = [None] * len(segs)

    if move_steps or anim_frames:
        segs, parallel_servo, parallel_anim = interleave_tts_segs_with_llm_plan(
            segs,
            move_steps,
            anim_frames,
            sample_rate,
        )
        logger.info(
            "[pb TX] LLM moves/anims 共享时间线 segments=%d（move_steps=%d anim_frames=%d）",
            len(segs),
            len(move_steps),
            len(anim_frames),
        )
    if parallel_anim is not None:
        anim_rows = build_anim_rows_for_llm_plan(segs, parallel_anim, face_bundle)
    else:
        anim_rows = phoneme_seq_to_anim_seq(segs, face_bundle)
    pcm_list: list[bytes] = []
    for i, s in enumerate(segs):
        raw = bytes(s.get("pcm") or b"")
        cms = int(anim_rows[i].get("chunk_ms") or s.get("ms") or 0)
        aligned, cms2 = align_pcm_s16le_mono_to_chunk_ms(raw, cms, sample_rate)
        if cms2 != cms:
            anim_rows[i]["chunk_ms"] = cms2
        pcm_list.append(aligned)

    n_llm_servo = apply_parallel_pb_servo(anim_rows, parallel_servo)
    if n_llm_servo:
        logger.info(
            "[pb TX] 已将 %d 条分片附上舵机/hold（parallel 与交错后分片对齐）",
            n_llm_servo,
        )

    merged_rows, merged_pcm = merge_pb_subchunks(
        anim_rows, pcm_list, sample_rate=sample_rate
    )
    logger.info(
        "[pb TX] 分片合并 %d → %d（单包 chunk_ms 上限 %d ms）",
        len(anim_rows),
        len(merged_rows),
        PB_CHUNK_MS_MAX,
    )
    apply_llm_display_to_rows(
        merged_rows,
        images=images,
    )

    pb_req = request_id or uuid.uuid4().hex[:16]
    from deskbot_server.pb.servo_pcm import parse_pb_cam_fps

    pb_vol = resolve_pb_volume_hint(volume)
    pb_cam_fps = parse_pb_cam_fps(cam_fps)
    output_fmt = str(tts_cfg.get("output_codec") or "s16le").lower()
    audio_blobs: list[bytes] = list(merged_pcm)
    opus_frames: list[int] | None = None
    if output_fmt == "opus":
        from deskbot_server.pipeline.opus_downlink import (
            encode_pcm_s16le_to_opus_batch,
            new_downlink_opus_encoder,
        )

        # 同一请求时间线的分片按序共用一个编码器：固件下行解码器跨 chunk
        # 持久（仅显式 reset 时重建），复用保持码流连续且省去逐 chunk 新建
        # 编码器的开销。编码器不跨请求复用——不同请求间设备可能已 reset。
        # 首个非空分片才创建编码器：纯动作（无 PCM）wire 不依赖 Opus 运行时。
        # 注意：本函数为纯 CPU 同步实现，异步调用方（chat_flow）通过
        # asyncio.to_thread 调用，避免最长 10s/chunk 的编码阻塞事件循环。
        request_encoder = None
        audio_blobs = []
        opus_frames = []
        for pcm in merged_pcm:
            if pcm and request_encoder is None:
                request_encoder = new_downlink_opus_encoder(sample_rate)
            blob, nf = encode_pcm_s16le_to_opus_batch(
                pcm,
                sample_rate,
                encoder=request_encoder,
            )
            audio_blobs.append(blob)
            opus_frames.append(nf)
        wire_fmt = "opus"
    else:
        wire_fmt = "s16le"
    pairs = pb_json_messages(
        pb_req=pb_req,
        sample_rate=sample_rate,
        fmt=wire_fmt,
        channels=1,
        anim_rows=merged_rows,
        pcm_per_idx=audio_blobs,
        opus_frames_per_idx=opus_frames,
        volume=pb_vol,
        cam_fps=pb_cam_fps,
        action=action,
        mouth_only=mouth_only,
    )
    return pairs, pb_req, len(pairs), sample_rate
