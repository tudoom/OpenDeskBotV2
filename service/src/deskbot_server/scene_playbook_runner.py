"""场景编排：``chunks[]`` → 分阶段 PB 下发（每 chunk 对应一轮 pb）。"""
from __future__ import annotations

from typing import Any

from deskbot_server.pb.llm_plan import expand_llm_anims, expand_llm_moves
from deskbot_server.scene_playbooks_store import normalize_playbook


def playbook_collect_text(playbook: dict[str, Any]) -> str:
    parts: list[str] = []
    for chunk in playbook.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        t = str(chunk.get("text") or "").strip()
        if t:
            parts.append(t)
    return "".join(parts)


def _chunk_to_phase(chunk: dict[str, Any]) -> dict[str, Any] | None:
    text = str(chunk.get("text") or "").strip()
    moves: list[dict[str, Any]] = []
    anims: list[dict[str, Any]] = []
    moves.extend(
        dict(move)
        for move in (chunk.get("moves") or [])
        if isinstance(move, dict)
    )
    anims.extend(
        dict(anim)
        for anim in (chunk.get("anims") or [])
        if isinstance(anim, dict)
    )
    if not text and not moves and not anims:
        return None
    if text:
        return {
            "kind": "speech",
            "text": text,
            "moves": moves,
            "anims": anims,
            "chunk_id": chunk.get("id"),
        }
    return {
        "kind": "motion",
        "text": "",
        "moves": moves,
        "anims": anims,
        "chunk_id": chunk.get("id"),
    }


def playbook_to_phases(
    playbook: dict[str, Any],
) -> list[dict[str, Any]]:
    """每个 ``chunks[]`` 条目 → 一轮 PB（口播 / 纯表情 / 纯舵机 / 组合）。"""
    pb = normalize_playbook(playbook)
    from deskbot_server.scene_playbooks_store import collect_missing_servo_presets

    missing = collect_missing_servo_presets(pb)
    if missing:
        raise ValueError("unknown servo preset(s): " + ", ".join(missing))
    phases: list[dict[str, Any]] = []
    for chunk in pb.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        phase = _chunk_to_phase(chunk)
        if phase:
            phases.append(phase)
    return phases


def playbook_to_llm_plan(
    playbook: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """兼容旧接口：合并为单条 TTS 计划（多 chunk 时仅取首段口播）。"""
    phases = playbook_to_phases(playbook)
    if not phases:
        return "", [], []
    text_parts: list[str] = []
    moves: list[dict[str, Any]] = []
    anims: list[dict[str, Any]] = []
    for p in phases:
        t = str(p.get("text") or "").strip()
        if t:
            text_parts.append(t)
        moves.extend(p.get("moves") or [])
        anims.extend(p.get("anims") or [])
    text = "".join(text_parts)
    if not text.strip() and (moves or anims):
        text = "。"
    return text, moves, anims


def playbook_debug_snapshot(
    playbook: dict[str, Any],
) -> dict[str, Any]:
    pb = normalize_playbook(playbook)
    phases = playbook_to_phases(pb)
    text = playbook_collect_text(pb)
    moves: list[dict[str, Any]] = []
    anims: list[dict[str, Any]] = []
    for p in phases:
        moves.extend(p.get("moves") or [])
        anims.extend(p.get("anims") or [])
    move_steps = expand_llm_moves(moves)
    return {
        "playbook": pb,
        "text": text,
        "phases": phases,
        "moves": moves,
        "anims": anims,
        "move_steps_expanded": move_steps,
    }


def playbook_expand_move_steps(moves: list[dict[str, Any]]) -> list[dict[str, int]]:
    return expand_llm_moves(moves)


def playbook_expand_anim_frames(anims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return expand_llm_anims(anims)
