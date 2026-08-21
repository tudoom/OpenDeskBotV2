"""音素序列 → 逐帧 anim 行（嘴/眼/鼻/extra）。"""

from __future__ import annotations

import copy
from typing import Any

from deskbot_server.pb.shapes import (
    _default_mouth_fallback_shape,
    _normalize_mouth_entry,
    expand_mouth_by_phoneme,
    simplify_phoneme_key,
)


def _anim_row_from_elements(elements: dict[str, Any], *, chunk_ms: int, phoneme: str = "") -> dict[str, Any]:
    el = elements if isinstance(elements, dict) else {}
    row: dict[str, Any] = {
        "elements": {
            # Speech owns only the mouth layer. Missing layers deliberately
            # inherit the current Web/RTC/Agent face in firmware, so TTS can
            # no longer replace eyes, nose or decorations on every phoneme.
            "mouth": copy.deepcopy(el.get("mouth") if isinstance(el.get("mouth"), list) else []),
        },
        "ms": chunk_ms,
    }
    if phoneme:
        row["phoneme"] = phoneme
    return row


def _phoneme_seq_from_design(
    segments: list[dict[str, Any]],
    design: dict[str, Any],
) -> list[dict[str, Any]]:
    """``deskbot-face.json`` 音素表达式：每片直接使用匹配帧的完整 ``elements``。"""
    out: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments or []):
        ph = str(seg.get("phoneme") or "").strip()
        chunk_ms = int(seg.get("ms") or 0)
        from deskbot_server.face_design_store import (
            find_phoneme_expression,
            pick_expression_elements,
        )

        expr = find_phoneme_expression(design, ph)
        if expr is None and ph:
            expr = find_phoneme_expression(design, "_") or find_phoneme_expression(design, "sil")
        elements = pick_expression_elements(expr, at_ms=0)
        if not elements:
            elements = pick_expression_elements(find_phoneme_expression(design, "sil"), at_ms=0)
        out.append(
            {
                "idx": idx,
                "chunk_ms": chunk_ms,
                "anim": [_anim_row_from_elements(elements, chunk_ms=chunk_ms, phoneme=ph)],
            }
        )
    return out


def phoneme_seq_to_anim_seq(
    segments: list[dict[str, Any]],
    face_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    """返回每片 ``idx, chunk_ms, phoneme, anim``（与仿真页 / pb 一致）。

    语音链路 **只写 mouth 层**：每片 anim 行的 ``elements`` 仅含 ``mouth``，
    缺失的 bg/nose/eye/extra 层由固件继承当前 Web/RTC/Agent 完整脸，TTS
    不再逐音素替换眼、鼻或装饰（见 ``_anim_row_from_elements``）。

    - 默认链路：``deskbot-face.json`` 带 ``phonemes`` 时短路走
      ``_phoneme_seq_from_design``，按音素查设计文档表达式。
    - 兜底链路（显式 ``DESKBOT_PB_FACE_BUNDLE*`` 高级档才可达，见
      ``pb.face_bundle`` 模块文档）：仅消费 ``face_bundle`` 的
      ``mouth_by_phoneme`` / ``mouth_by_phoneme_groups``（经
      ``expand_mouth_by_phoneme`` 展开，同音素以单键覆盖共享条）；口型
      ``offset`` 与 bundle 的眼/鼻/extra/眨眼数据在本函数中 **不参与** 输出。
    """
    from deskbot_server.face_design_store import _load_face_design_cached

    design = _load_face_design_cached()
    if isinstance(design, dict) and design.get("phonemes"):
        return _phoneme_seq_from_design(segments, design)

    work = copy.deepcopy(face_bundle) if isinstance(face_bundle, dict) else {}

    mouth_raw = work.get("mouth_by_phoneme") if isinstance(work, dict) else None
    mouth_gr = work.get("mouth_by_phoneme_groups") if isinstance(work, dict) else None
    mouth_by = expand_mouth_by_phoneme(
        mouth_raw if isinstance(mouth_raw, dict) else {},
        mouth_gr if isinstance(mouth_gr, list) else None,
    )
    fb_mouth = _normalize_mouth_entry(mouth_by.get("_"))
    if not fb_mouth["elements"]:
        fb_mouth = _default_mouth_fallback_shape()

    out: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments or []):
        ph = str(seg.get("phoneme") or "").strip()
        chunk_ms = int(seg.get("ms") or 0)
        lookup = simplify_phoneme_key(ph)
        raw_mouth = mouth_by.get(ph)
        if raw_mouth is None and lookup != ph:
            raw_mouth = mouth_by.get(lookup)
        if raw_mouth is None:
            raw_mouth = mouth_by.get("_")
        mouth_entry = _normalize_mouth_entry(raw_mouth if raw_mouth is not None else fb_mouth)
        if not mouth_entry["elements"]:
            mouth_entry = copy.deepcopy(fb_mouth)
        mouth_prims = copy.deepcopy(mouth_entry["elements"])
        out.append(
            {
                "idx": idx,
                "chunk_ms": chunk_ms,
                "anim": [
                    {
                        "elements": {
                            "mouth": mouth_prims,
                        },
                        "ms": chunk_ms,
                        **({"phoneme": ph} if ph else {}),
                    }
                ],
            }
        )
    return out

