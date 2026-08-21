"""场景编排持久化（``data/local/scene_playbooks.json``，顶层数组）。

每条编排由 ``chunks[]`` 组成，与 PB 协议一致：每包（``pb_single`` / 一轮 ``pb_chunk``）
可独立携带口播、表情、舵机，按顺序串行下发。
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Optional

from deskbot_server.constants import SCENE_PLAYBOOKS_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$", re.I)
_CLIP_MS_MIN = 40
_CLIP_MS_MAX = 120_000


def _new_clip_id() -> str:
    return uuid.uuid4().hex[:10]


def _normalize_ms(raw: object, *, default: int = 500) -> int:
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        ms = default
    return max(_CLIP_MS_MIN, min(_CLIP_MS_MAX, ms))


def _normalize_expr_part(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    scene = str(raw.get("scene") or raw.get("name") or "").strip()
    if not scene:
        return None
    return {"scene": scene, "ms": _normalize_ms(raw.get("ms"))}


def _normalize_servo_part(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    preset = str(raw.get("preset") or "").strip()
    ms = _normalize_ms(raw.get("ms"))
    if preset:
        return {"preset": preset, "ms": ms}
    if raw.get("x") is not None or raw.get("y") is not None:
        try:
            return {
                "x": int(raw.get("x", 90)),
                "y": int(raw.get("y", 90)),
                "xm": int(raw.get("xm", 0)),
                "ym": int(raw.get("ym", 0)),
                "ms": ms,
            }
        except (TypeError, ValueError) as exc:
            raise ValueError("servo needs preset or x/y") from exc
    return None


def _normalize_move_part(raw: object) -> dict[str, Any] | None:
    """Normalize one canonical timeline motion entry."""
    if not isinstance(raw, dict):
        return None
    move = str(raw.get("move") or raw.get("preset") or "").strip()
    ms = _normalize_ms(raw.get("ms"))
    if move and move != "__custom__":
        return {"move": move, "ms": ms}
    servo = _normalize_servo_part(raw)
    if servo and "preset" in servo:
        return {"move": servo["preset"], "ms": servo["ms"]}
    if servo:
        return {
            "move": "__custom__",
            "x": servo["x"],
            "y": servo["y"],
            "xm": servo["xm"],
            "ym": servo["ym"],
            "ms": servo["ms"],
        }
    return None


def _normalize_anim_part(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    anim = str(raw.get("anim") or raw.get("scene") or raw.get("name") or "").strip()
    if not anim:
        return None
    return {"anim": anim, "ms": _normalize_ms(raw.get("ms"))}


def _normalize_chunk(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("chunk must be an object")
    cid = str(raw.get("id") or _new_clip_id()).strip() or _new_clip_id()
    text = str(raw.get("text") or "").strip()
    moves_raw = raw.get("moves")
    anims_raw = raw.get("anims")
    if moves_raw is not None and not isinstance(moves_raw, list):
        raise ValueError("chunk moves must be an array")
    if anims_raw is not None and not isinstance(anims_raw, list):
        raise ValueError("chunk anims must be an array")
    if moves_raw is not None:
        moves = [
            move
            for move in (_normalize_move_part(item) for item in moves_raw)
            if move is not None
        ]
    else:
        legacy_servo = _normalize_move_part(raw.get("servo"))
        moves = [legacy_servo] if legacy_servo else []
    if anims_raw is not None:
        anims = [
            anim
            for anim in (_normalize_anim_part(item) for item in anims_raw)
            if anim is not None
        ]
    else:
        legacy_expr = _normalize_anim_part(raw.get("expr"))
        anims = [legacy_expr] if legacy_expr else []
    if not text and not moves and not anims:
        raise ValueError("chunk needs text, moves or anims")
    out: dict[str, Any] = {"id": cid, "text": text}
    if moves:
        out["moves"] = moves
    if anims:
        out["anims"] = anims
    return out


def _legacy_to_chunks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Migrate old parallel tracks into one canonical timeline chunk."""
    moves = [
        move
        for move in (
            _normalize_move_part(item) for item in (raw.get("servo_track") or [])
        )
        if move is not None
    ]
    anims = [
        anim
        for anim in (
            _normalize_anim_part(item) for item in (raw.get("expr_track") or [])
        )
        if anim is not None
    ]
    text_parts: list[str] = []
    for item in raw.get("text_track") or []:
        text = (
            str(item.get("text") or "").strip()
            if isinstance(item, dict)
            else str(item or "").strip()
        )
        if text:
            text_parts.append(text)
    top_text = str(raw.get("text") or "").strip()
    if top_text:
        text_parts.append(top_text)
    chunk: dict[str, Any] = {
        "id": "timeline",
        "text": "".join(text_parts),
    }
    if moves:
        chunk["moves"] = moves
    if anims:
        chunk["anims"] = anims
    return [chunk] if chunk["text"] or moves or anims else []


def normalize_playbook(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("playbook must be an object")
    name = str(raw.get("name") or raw.get("id") or "").strip()
    if not name:
        raise ValueError("name required")
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}")
    title = str(raw.get("title") or name).strip()

    chunks_raw = raw.get("chunks")
    if isinstance(chunks_raw, list) and chunks_raw:
        chunks = [_normalize_chunk(c) for c in chunks_raw]
    else:
        chunks = _legacy_to_chunks(raw)
    if not chunks:
        raise ValueError("playbook needs a non-empty chunks array")

    return {"name": name, "title": title, "chunks": chunks}


def normalize_scene_playbooks(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("body must be a JSON array")
    return [normalize_playbook(x) for x in raw]


def _seed_default_playbooks() -> list[dict[str, Any]]:
    return [
        {
            "name": "demo_greet",
            "title": "演示问候",
            "chunks": [
                {"id": "c1", "text": "", "servo": {"preset": "look_left", "ms": 500}},
                {"id": "c2", "text": "", "servo": {"preset": "center", "ms": 500}},
                {
                    "id": "c3",
                    "text": "你好，很高兴见到你",
                    "expr": {"scene": "happy_smile", "ms": 1500},
                },
            ],
        },
    ]


_STORE = JsonDocumentStore(
    lambda: resolve_json_path(SCENE_PLAYBOOKS_FILE),
    normalize=normalize_scene_playbooks,
    # save 侧的领域校验（缺失舵机预设、动作展开）在
    # save_scene_playbooks_file 里完成，写盘不再二次 normalize。
    normalize_save=lambda rows: rows,
)


def load_scene_playbooks_file(
    *, seed_if_missing: bool = True
) -> Optional[list[dict[str, Any]]]:
    if not _STORE.path().is_file():
        if not seed_if_missing:
            return None
        rows = _seed_default_playbooks()
        save_scene_playbooks_file(rows)
        return rows
    return _STORE.load()


def save_scene_playbooks_file(rows: list[dict[str, Any]]) -> None:
    norm = normalize_scene_playbooks(rows)
    missing = collect_missing_servo_presets(norm)
    if missing:
        raise ValueError("unknown servo preset(s): " + ", ".join(missing))
    from deskbot_server.pb.llm_plan import expand_llm_moves

    for playbook in norm:
        for chunk in playbook.get("chunks") or []:
            expand_llm_moves(list(chunk.get("moves") or []))
    _STORE.save(norm)


def find_playbook_by_name(rows: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    want = str(name or "").strip().lower()
    if not want:
        return None
    for row in rows:
        if str(row.get("name") or "").strip().lower() == want:
            return copy.deepcopy(row)
    return None


def collect_missing_servo_presets(
    playbooks: list[dict[str, Any]] | dict[str, Any],
) -> list[str]:
    """编排引用的 ``servo.preset`` 在 ``servo.json`` 中不存在时返回 id 列表。"""
    from deskbot_server.servo_config_store import servo_preset_catalog

    source_rows = playbooks if isinstance(playbooks, list) else [playbooks]
    rows: list[dict[str, Any]] = []
    for playbook in source_rows:
        if not isinstance(playbook, dict):
            continue
        try:
            rows.append(normalize_playbook(playbook))
        except ValueError:
            continue
    available = {
        str(preset.get("id") or "").strip().casefold()
        for preset in servo_preset_catalog()
        if isinstance(preset, dict) and str(preset.get("id") or "").strip()
    }
    missing: set[str] = set()
    for pb in rows:
        if not isinstance(pb, dict):
            continue
        for chunk in pb.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            candidates = list(
                move
                for move in (chunk.get("moves") or [])
                if isinstance(move, dict)
            )
            for move in candidates:
                preset = str(move.get("move") or "").strip()
                if not preset or preset == "__custom__":
                    continue
                if preset.casefold() not in available:
                    missing.add(preset)
    return sorted(missing)
