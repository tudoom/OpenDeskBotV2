"""LLM ``moves`` / ``anims`` 计划：预设加载、时长缩放与 TTS 分片交错。"""

from __future__ import annotations

import copy
import logging
from typing import Any

from deskbot_server.face_expr_scenes_store import (
    _extract_frame_elements,
    default_speech_blink_scene,
    find_design_scene_by_name,
    load_face_expr_scenes_file,
)
from deskbot_server.pb.phoneme_anim import phoneme_seq_to_anim_seq
from deskbot_server.pb.servo_pcm import (
    PB_CHUNK_MS_MAX,
    anim_elements_from_row,
    make_anim_item,
)
from deskbot_server.pb.shapes import apply_anim_bg_color_elements
from deskbot_server.servo_config_store import (
    DEFAULT_SERVO_LIMITS,
    clamp_servo_step,
    load_servo_cfg_file,
    resolve_move_for_perspective,
)
from deskbot_server.servo_protocol import (
    SERVO_MAX_PLAN_DURATION_MS,
    SERVO_MAX_PLAN_STEPS,
    SERVO_MIN_SEGMENT_DURATION_MS,
    ServoProtocolError,
    validate_servo_steps,
)

logger = logging.getLogger("deskbot-server")

_FRAME_MS_MIN = 40
_FRAME_MS_MAX = 30_000


def _scale_ms_values(
    raw_ms: list[int],
    target_ms: int,
    *,
    minimum_ms: int = _FRAME_MS_MIN,
    maximum_ms: int | None = _FRAME_MS_MAX,
) -> list[int]:
    """Scale durations to an exactly representable total.

    Impossible totals are rejected instead of being silently stretched or
    truncated.  The semantic duration and the wire transaction therefore
    always describe the same plan.
    """
    n = len(raw_ms)
    if n == 0:
        return []
    target_ms = int(target_ms)
    minimum_ms = max(1, int(minimum_ms))
    if target_ms < n * minimum_ms:
        raise ValueError(
            f"target duration {target_ms} ms is below {n * minimum_ms} ms "
            f"for {n} segments"
        )
    if maximum_ms is not None:
        maximum_ms = max(minimum_ms, int(maximum_ms))
        if target_ms > n * maximum_ms:
            raise ValueError(
                f"target duration {target_ms} ms exceeds {n * maximum_ms} ms "
                f"for {n} segments"
            )
    weights = [max(1, int(m)) for m in raw_ms]
    scaled = [minimum_ms] * n
    capacities = [
        target_ms if maximum_ms is None else maximum_ms - minimum_ms
        for _ in range(n)
    ]
    remaining = target_ms - n * minimum_ms
    while remaining:
        active = [i for i, capacity in enumerate(capacities) if capacity > 0]
        if not active:
            raise ValueError(f"target duration {target_ms} ms is not representable")
        weight_total = sum(weights[i] for i in active)
        exact = {i: remaining * weights[i] / weight_total for i in active}
        increments = {
            i: min(capacities[i], int(exact[i]))
            for i in active
        }
        used = sum(increments.values())
        if used == 0:
            order = sorted(
                active,
                key=lambda i: (exact[i] - int(exact[i]), weights[i], -i),
                reverse=True,
            )
            for i in order[: min(remaining, len(order))]:
                increments[i] = 1
            used = sum(increments.values())
        for i, increment in increments.items():
            scaled[i] += increment
            capacities[i] -= increment
        remaining -= used

    if sum(scaled) != target_ms:
        raise AssertionError("duration scaler lost time")
    return scaled


def resolve_anim_scene_frames(anim_name: str) -> list[dict[str, Any]]:
    """加载表情场景帧；未找到时回退 ``name=default``。"""
    rows = load_face_expr_scenes_file(seed_if_missing=True) or []
    ent = find_design_scene_by_name(rows, anim_name)
    if ent is None and str(anim_name or "").strip().lower() != "default":
        ent = find_design_scene_by_name(rows, "default")
    if ent is None:
        # The redesigned expression library calls its neutral built-in
        # ``idle`` and may legitimately have no stored row named ``default``.
        # Keep old LLM plans compatible, then fall back to the in-code neutral
        # blink so a user-edited library can never remove the safety default.
        ent = find_design_scene_by_name(rows, "idle")
    if ent is None:
        ent = default_speech_blink_scene()
    if ent is None:
        return []
    frames = ent.get("frames")
    if not isinstance(frames, list) or not frames:
        return []
    return copy.deepcopy(frames)




def expand_llm_moves(
    moves: list[dict[str, Any]] | None,
) -> list[dict[str, int]]:
    """Expand and atomically validate one semantic motion plan."""
    if not moves:
        return []
    try:
        loaded = load_servo_cfg_file()
    except (OSError, ValueError) as exc:
        raise ServoProtocolError("servo catalog is unavailable") from exc
    cfg: dict[str, Any] = dict(DEFAULT_SERVO_LIMITS)
    if loaded:
        cfg.update(loaded)
    perspective = str(cfg.get("perspective") or "viewer")
    preset_index = {
        str(preset.get("id") or "").strip().casefold(): preset
        for preset in (cfg.get("presets") or [])
        if isinstance(preset, dict) and str(preset.get("id") or "").strip()
    }

    out: list[dict[str, int]] = []
    for item_index, item in enumerate(moves):
        if not isinstance(item, dict):
            raise ServoProtocolError(f"motion plan item {item_index} must be an object")
        move_id = resolve_move_for_perspective(
            str(item.get("move") or "").strip(),
            perspective=perspective,
        )
        raw_target_ms = item.get("ms", 0)
        if isinstance(raw_target_ms, bool) or not isinstance(raw_target_ms, int):
            raise ServoProtocolError(
                f"motion plan item {item_index} has invalid duration"
            )
        target_ms = raw_target_ms
        if not move_id:
            raise ServoProtocolError(f"motion plan item {item_index} is missing move")
        if (
            target_ms < SERVO_MIN_SEGMENT_DURATION_MS
            or target_ms > SERVO_MAX_PLAN_DURATION_MS
        ):
            raise ServoProtocolError(
                f"motion plan item {item_index} duration must be between "
                f"{SERVO_MIN_SEGMENT_DURATION_MS} and "
                f"{SERVO_MAX_PLAN_DURATION_MS} ms"
            )
        if move_id == "__custom__":
            try:
                out.append(
                    clamp_servo_step(
                        {
                            "xm": item.get("xm", 0),
                            "ym": item.get("ym", 0),
                            "x": item.get("x", 90),
                            "y": item.get("y", 90),
                            "ms": target_ms,
                        },
                        limits=cfg,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ServoProtocolError(
                    f"motion plan item {item_index} is invalid"
                ) from exc
            continue
        preset = preset_index.get(move_id.casefold())
        steps = copy.deepcopy(preset.get("steps") or []) if preset else []
        if not steps:
            raise ServoProtocolError(f"unknown servo preset: {move_id}")
        raw_ms = [max(1, int(s.get("ms") or 400)) for s in steps]
        try:
            scaled = _scale_ms_values(
                raw_ms,
                target_ms,
                minimum_ms=SERVO_MIN_SEGMENT_DURATION_MS,
                maximum_ms=None,
            )
        except ValueError as exc:
            raise ServoProtocolError(
                f"servo preset {move_id!r} cannot fit {target_ms} ms"
            ) from exc
        for step, sms in zip(steps, scaled):
            try:
                out.append(
                    clamp_servo_step(
                        {
                            "xm": int(step.get("xm", 1)),
                            "ym": int(step.get("ym", 1)),
                            "x": int(step.get("x", 0)),
                            "y": int(step.get("y", 0)),
                            "ms": int(sms),
                        },
                        limits=cfg,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ServoProtocolError(
                    f"servo preset {move_id!r} contains an invalid step"
                ) from exc
    checked, _duration = validate_servo_steps(
        out,
        context="semantic motion plan",
        max_segments=SERVO_MAX_PLAN_STEPS,
        max_duration_ms=SERVO_MAX_PLAN_DURATION_MS,
    )
    return [dict(step) for step in checked]


def expand_llm_anims(
    anims: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """将 ``[{anim, ms}, ...]`` 展开为缩放后的 ``{ms, elements}`` 帧列表。"""
    out: list[dict[str, Any]] = []
    for item in anims or []:
        if not isinstance(item, dict):
            continue
        anim_name = str(item.get("anim") or "").strip()
        try:
            target_ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            continue
        if not anim_name or target_ms <= 0:
            continue
        frames = resolve_anim_scene_frames(anim_name)
        if not frames:
            logger.warning("[pb] LLM anim 未找到场景 %r（default 亦不可用）", anim_name)
            continue
        raw_ms = [max(1, int(fr.get("ms") or 500)) for fr in frames]
        try:
            scaled = _scale_ms_values(raw_ms, target_ms)
        except ValueError:
            logger.warning(
                "[pb] LLM anim duration is not representable name=%r ms=%r",
                anim_name,
                target_ms,
            )
            continue
        for fr, sms in zip(frames, scaled):
            try:
                elements = _extract_frame_elements(fr if isinstance(fr, dict) else {})
            except ValueError:
                continue
            elements = apply_anim_bg_color_elements(
                elements, bg=item.get("bg"), color=item.get("color")
            )
            out.append({"ms": int(sms), "elements": elements})
    return out


def interleave_tts_segs_with_llm_plan(
    segs: list[dict[str, Any]],
    move_steps: list[dict[str, int]],
    anim_frames: list[dict[str, Any]],
    sample_rate: int,
) -> tuple[
    list[dict[str, Any]],
    list[list[dict[str, int]] | None],
    list[dict[str, Any] | None],
]:
    """Compile audio, motion and animation onto one shared wall clock.

    Every lane is contiguous and independent.  Motion commands are attached
    exactly once at their original start time; absolute commands are never
    split into approximations.  Intervals outside real PCM are JSON-only, so
    an animation or motion tail cannot manufacture silence.
    """
    if not segs and not move_steps and not anim_frames:
        return [], [], []

    boundaries = {0}
    move_cursor = 0
    for raw_step in move_steps:
        duration = max(1, int(raw_step.get("ms") or _FRAME_MS_MIN))
        move_cursor += duration
    boundaries.add(move_cursor)

    audio_spans: list[tuple[int, int, dict[str, Any]]] = []
    audio_cursor = 0
    for raw_seg in segs:
        seg = copy.deepcopy(raw_seg)
        duration = max(1, int(seg.get("ms") or _FRAME_MS_MIN))
        start = audio_cursor
        audio_cursor += duration
        audio_spans.append((start, audio_cursor, seg))
        boundaries.add(start)
        boundaries.add(audio_cursor)

    anim_spans: list[tuple[int, int, dict[str, Any]]] = []
    anim_cursor = 0
    for raw_frame in anim_frames:
        frame = copy.deepcopy(raw_frame)
        duration = max(1, int(frame.get("ms") or _FRAME_MS_MIN))
        start = anim_cursor
        anim_cursor += duration
        anim_spans.append((start, anim_cursor, frame))
        boundaries.add(start)
        boundaries.add(anim_cursor)

    total_ms = max(move_cursor, audio_cursor, anim_cursor)
    if total_ms <= 0:
        return [], [], []
    for point in range(PB_CHUNK_MS_MAX, total_ms, PB_CHUNK_MS_MAX):
        boundaries.add(point)
    boundaries.add(total_ms)
    ordered = sorted(point for point in boundaries if 0 <= point <= total_ms)

    def _active_span(
        spans: list[tuple[int, int, dict[str, Any]]],
        at_ms: int,
    ) -> tuple[int, int, dict[str, Any]] | None:
        for span in spans:
            if span[0] <= at_ms < span[1]:
                return span
        return None

    out_segs: list[dict[str, Any]] = []
    parallel_servo: list[list[dict[str, int]] | None] = []
    parallel_anim: list[dict[str, Any] | None] = []
    sr = max(1, int(sample_rate))
    for start_ms, end_ms in zip(ordered, ordered[1:]):
        duration = end_ms - start_ms
        if duration <= 0:
            continue
        audio_span = _active_span(audio_spans, start_ms)
        if audio_span is None:
            out_seg: dict[str, Any] = {
                "phoneme": "",
                "ms": duration,
                "pcm": b"",
            }
        else:
            source_start, _source_end, source = audio_span
            out_seg = copy.deepcopy(source)
            raw_pcm = bytes(source.get("pcm") or b"")
            first_sample = (start_ms - source_start) * sr // 1000
            last_sample = (end_ms - source_start) * sr // 1000
            out_seg["pcm"] = raw_pcm[first_sample * 2 : last_sample * 2]
            out_seg["ms"] = duration
        out_segs.append(out_seg)
        parallel_servo.append(
            copy.deepcopy(move_steps) if start_ms == 0 and move_steps else None
        )
        anim_span = _active_span(anim_spans, start_ms)
        elements = anim_span[2].get("elements") if anim_span else None
        parallel_anim.append(
            copy.deepcopy(elements) if isinstance(elements, dict) else None
        )

    return out_segs, parallel_servo, parallel_anim


def merge_llm_plan_anim_rows(
    segs: list[dict[str, Any]],
    phoneme_rows: list[dict[str, Any]],
    parallel_anim: list[dict[str, Any] | None] | None,
) -> list[dict[str, Any]]:
    """合并 LLM 指定 anim 与音素口型：有真实音素口播时保留音素 ``mouth``。"""
    out: list[dict[str, Any]] = []
    for i, ph_row in enumerate(phoneme_rows):
        row = copy.deepcopy(ph_row)
        seg = segs[i] if i < len(segs) else {}
        has_audio = bool(bytes(seg.get("pcm") or b""))
        plan_el = (parallel_anim or [None] * len(phoneme_rows))[i] if parallel_anim else None
        if isinstance(plan_el, dict) and plan_el:
            merged = copy.deepcopy(plan_el)
            ph_el = anim_elements_from_row(ph_row)
            chunk_ms = int(ph_row.get("chunk_ms") or seg.get("ms") or 1)
            ph_name = str(seg.get("phoneme") or "").strip()
            if not ph_name:
                anim_list = ph_row.get("anim")
                if isinstance(anim_list, list) and anim_list:
                    ph_name = str(anim_list[0].get("phoneme") or "").strip()
            # 纯表情/舵机包的静音 PCM 不应覆盖情绪口型；仅 TTS 音素口播时保留嘴型
            if has_audio and ph_name and isinstance(ph_el.get("mouth"), list):
                merged["mouth"] = copy.deepcopy(ph_el["mouth"])
            row["anim"] = [
                make_anim_item(merged, chunk_ms, phoneme=ph_name or None)
            ]
        elif not has_audio:
            # A silent interval created only to schedule a servo plan does not
            # own the display lane.  In particular, do not let the phoneme
            # fallback manufacture a default mouth for a motor-only command.
            row.pop("anim", None)
        out.append(row)
    return out


def build_anim_rows_for_llm_plan(
    segs: list[dict[str, Any]],
    parallel_anim: list[dict[str, Any] | None] | None,
    face_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    phoneme_rows = phoneme_seq_to_anim_seq(segs, face_bundle)
    return merge_llm_plan_anim_rows(segs, phoneme_rows, parallel_anim)
