"""Transactional storage for the unified ``deskbot-face.json`` library."""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable, Optional

from deskbot_server.atomic_store import atomic_write_json, file_lock
from deskbot_server.constants import FACE_DESIGN_FILE
from deskbot_server.face_expr_scenes_store import normalize_design_scene
from deskbot_server.local_face_data import (
    ensure_face_data_file,
    global_face_design_path,
    resolve_face_data_path,
)
from deskbot_server.pb.shapes import simplify_phoneme_key

_design_cache: dict[str, tuple[int, int, dict[str, Any] | None]] = {}

FACE_DESIGN_SCHEMA_VERSION = 2
FACE_EXPRESSION_ORIGINS = frozenset({"system", "user"})


class FaceDesignRevisionConflict(ValueError):
    """Raised when an optimistic face-library write uses a stale revision."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"face design revision conflict: expected {expected}, current {actual}"
        )


def _normalize_origin(raw: object) -> str:
    origin = str(raw or "system").strip().lower()
    if origin not in FACE_EXPRESSION_ORIGINS:
        raise ValueError("expression.origin must be 'system' or 'user'")
    return origin


def _normalize_mappings(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("face design mappings must be an object")
    out: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip().lower()
        if not key:
            raise ValueError("face design mapping key required")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"scene for mapping {key!r} must be a non-empty string")
        out[key] = raw_value.strip()
    return out


def resolve_face_design_path() -> str:
    """Resolve the one editable PC-local face library."""
    return str(resolve_face_data_path())


def _normalize_expression(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("expression must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("expression.name required")
    alias_raw = raw.get("alias")
    alias = (
        [str(x).strip() for x in alias_raw if str(x).strip()] if isinstance(alias_raw, list) else []
    )
    title = str(raw.get("title") or name).strip()
    scene = normalize_design_scene({**raw, "name": name, "title": title})
    scene["alias"] = alias
    scene["origin"] = _normalize_origin(raw.get("origin"))
    return scene


def _normalize_expression_list(raw: list[object], *, field: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        expression = _normalize_expression(item)
        key = expression["name"].casefold()
        if key in seen:
            raise ValueError(f"duplicate {field} expression name {expression['name']!r}")
        seen.add(key)
        out.append(expression)
    return out


def normalize_face_design_doc(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("face design must be a JSON object")
    phonemes_raw = raw.get("phonemes")
    if phonemes_raw is None and isinstance(raw.get("phoneme_expressions"), list):
        phonemes_raw = raw.get("phoneme_expressions")
    emotions_raw = raw.get("emotions")
    if emotions_raw is None and isinstance(raw.get("emotion_expressions"), list):
        emotions_raw = raw.get("emotion_expressions")
    if not isinstance(phonemes_raw, list) or not isinstance(emotions_raw, list):
        raise ValueError("face design requires phonemes[] and emotions[]")
    try:
        revision = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("face design revision must be an integer") from exc
    return {
        "schema_version": FACE_DESIGN_SCHEMA_VERSION,
        "revision": revision,
        "name": str(raw.get("name") or "deskbot-face").strip(),
        "description": str(raw.get("description") or "").strip(),
        "mappings": _normalize_mappings(raw.get("mappings")),
        "phonemes": _normalize_expression_list(phonemes_raw, field="phoneme"),
        "emotions": _normalize_expression_list(emotions_raw, field="emotion"),
    }


def load_face_design_file(
    *,
    seed_if_missing: bool = False,
) -> Optional[dict[str, Any]]:
    if seed_if_missing:
        ensure_face_data_file()
    path = resolve_face_design_path()
    return _load_face_design_unlocked(path)


def _load_face_design_unlocked(path: str) -> Optional[dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = _migrate_legacy_expression_origins(raw, path=path)
    if isinstance(raw, dict) and "mappings" not in raw:
        # Compatibility with the old split mapping file. The next write
        # persists these mappings into this transactional document.
        from deskbot_server.emotion_expr_map_store import load_legacy_emotion_expr_map

        raw = copy.deepcopy(raw)
        raw["mappings"] = load_legacy_emotion_expr_map()
    return normalize_face_design_doc(raw)


def _migrate_legacy_expression_origins(
    raw: dict[str, Any], *, path: str
) -> dict[str, Any]:
    """Infer ownership by template membership, never by the historical name.

    A shipped system example is deliberately named ``user_expression_*``.
    Therefore old local rows are user-owned only when their stable name is not
    present in the global seed.  If the seed cannot be read, defaulting to
    system is the safe choice because it prevents destructive edits.
    """

    fields = ("phonemes", "emotions")
    if all(
        all(isinstance(item, dict) and item.get("origin") for item in raw.get(field) or [])
        for field in fields
    ):
        return raw

    template_names: dict[str, set[str]] = {field: set() for field in fields}
    template_path = global_face_design_path()
    try:
        same_file = os.path.samefile(path, template_path)
    except (FileNotFoundError, OSError):
        same_file = os.path.abspath(path) == os.path.abspath(str(template_path))
    if not same_file and template_path.is_file():
        try:
            with template_path.open(encoding="utf-8") as template_file:
                template = json.load(template_file)
            if isinstance(template, dict):
                for field in fields:
                    template_names[field] = {
                        str(item.get("name") or "").strip().casefold()
                        for item in template.get(field) or []
                        if isinstance(item, dict) and str(item.get("name") or "").strip()
                    }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    migrated = copy.deepcopy(raw)
    for field in fields:
        for item in migrated.get(field) or []:
            if not isinstance(item, dict) or item.get("origin"):
                continue
            name = str(item.get("name") or "").strip().casefold()
            item["origin"] = (
                "user"
                if not same_file and template_names[field] and name not in template_names[field]
                else "system"
            )
    return migrated


def ensure_face_design_file() -> dict[str, Any]:
    """Load the PC-local face design, seeding it from legacy/template data."""

    path = str(
        ensure_face_data_file()
    )
    doc = _load_face_design_unlocked(path)
    if doc is not None:
        return doc
    raise FileNotFoundError(f"missing initialized face design: {path}")


def update_face_design_file(
    update: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Run one complete read/modify/write transaction under the file lock."""

    path = str(
        ensure_face_data_file()
    )
    with file_lock(path):
        current = _load_face_design_unlocked(path)
        if current is None:
            raise FileNotFoundError(
                f"缺少 {os.path.basename(FACE_DESIGN_FILE)}，请在 data/global/ 下提供全局模板"
            )
        current_revision = int(current.get("revision") or 0)
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise FaceDesignRevisionConflict(int(expected_revision), current_revision)
        updated = update(copy.deepcopy(current))
        if not isinstance(updated, dict):
            raise ValueError("face design update must return an object")
        updated["schema_version"] = FACE_DESIGN_SCHEMA_VERSION
        updated["revision"] = current_revision + 1
        norm = normalize_face_design_doc(updated)
        atomic_write_json(path, norm)
    clear_face_design_cache()
    return norm


def clear_face_design_cache() -> None:
    _design_cache.clear()


def design_file_mtime() -> float:
    path = resolve_face_design_path()
    try:
        return float(os.stat(path).st_mtime)
    except OSError:
        return 0.0


def _load_face_design_cached() -> dict[str, Any] | None:
    path = resolve_face_design_path()
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = os.path.abspath(path)
    cached = _design_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    doc = load_face_design_file()
    _design_cache[key] = (stat.st_mtime_ns, stat.st_size, doc)
    return doc


def expression_match_keys(expr: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    name = str(expr.get("name") or "").strip()
    if name:
        keys.append(name)
    for raw in expr.get("alias") or []:
        s = str(raw).strip()
        if s and s not in keys:
            keys.append(s)
    return keys


def find_design_expression(
    expressions: list[dict[str, Any]], key: str, *, phoneme: bool = True
) -> Optional[dict[str, Any]]:
    want = (
        simplify_phoneme_key(str(key or "").strip()) if phoneme else str(key or "").strip().lower()
    )
    if not want:
        return None
    for expr in expressions or []:
        for cand in expression_match_keys(expr):
            norm = simplify_phoneme_key(cand) if phoneme else str(cand).strip().lower()
            if norm == want:
                return expr
    return None


def find_phoneme_expression(doc: dict[str, Any] | None, phoneme: str) -> Optional[dict[str, Any]]:
    if not isinstance(doc, dict):
        return None
    return find_design_expression(doc.get("phonemes") or [], phoneme, phoneme=True)


def find_emotion_expression(doc: dict[str, Any] | None, name: str) -> Optional[dict[str, Any]]:
    if not isinstance(doc, dict):
        return None
    return find_design_expression(merged_emotion_expressions(doc), name, phoneme=False)


def pick_expression_elements(expr: dict[str, Any] | None, *, at_ms: int = 0) -> dict[str, Any]:
    """按表达式内帧时间轴取 ``elements``（默认首帧）。"""
    if not isinstance(expr, dict):
        return {}
    frames = expr.get("frames")
    if not isinstance(frames, list) or not frames:
        return {}
    t = max(0, int(at_ms or 0))
    cursor = 0
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        dur = max(1, int(fr.get("ms") or 800))
        if t < cursor + dur:
            els = fr.get("elements")
            return els if isinstance(els, dict) else {}
        cursor += dur
    last = frames[-1]
    if isinstance(last, dict):
        els = last.get("elements")
        return els if isinstance(els, dict) else {}
    return {}


def merged_emotion_expressions(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """文件 ``emotions`` + 未被覆盖的内置情绪（``builtin_emotion_scenes``）。"""
    from deskbot_server.face_expr_scenes_store import builtin_emotion_scenes

    file_exprs: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        for expr in doc.get("emotions") or []:
            if isinstance(expr, dict) and str(expr.get("name") or "").strip():
                file_exprs.append(expr)
    seen = {str(e.get("name") or "").strip().lower() for e in file_exprs}
    out = list(file_exprs)
    for raw in builtin_emotion_scenes():
        name = str(raw.get("name") or "").strip()
        if name and name.lower() not in seen:
            out.append(raw)
            seen.add(name.lower())
    return out


def emotions_as_scenes(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``emotions`` → ``face_expr_scenes`` 兼容列表（保留 alias 供查表）。"""
    if not isinstance(doc, dict):
        return []
    out: list[dict[str, Any]] = []
    for expr in merged_emotion_expressions(doc):
        if not isinstance(expr, dict):
            continue
        row = {
            "name": str(expr.get("name") or "").strip(),
            "title": str(expr.get("title") or expr.get("name") or "").strip(),
            "origin": _normalize_origin(expr.get("origin")),
            "frames": expr.get("frames") or [],
        }
        alias = expr.get("alias")
        if isinstance(alias, list) and alias:
            row["alias"] = alias
        out.append(row)
    return out


def phonemes_to_mouth_groups(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``phonemes`` → 调试台口型组视图（仅 mouth 图元）。"""
    from deskbot_server.face_mouth_config_store import (
        collapse_simplified_groups,
        simplify_group_states,
    )

    if not isinstance(doc, dict):
        return []
    raw_groups: list[dict[str, Any]] = []
    for expr in doc.get("phonemes") or []:
        if not isinstance(expr, dict):
            continue
        mouth = pick_expression_elements(expr, at_ms=0).get("mouth")
        if not isinstance(mouth, list):
            mouth = []
        states = [simplify_phoneme_key(k) for k in expression_match_keys(expr)]
        states = simplify_group_states(states)
        if not states:
            continue
        raw_groups.append(
            {
                "states": states,
                "elements": copy.deepcopy(mouth),
                "offset": {"x": 0, "y": 0},
            }
        )
    return collapse_simplified_groups(raw_groups)


def apply_mouth_groups_to_design(
    doc: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """将口型组写回 ``phonemes`` 各帧的 ``elements.mouth``。"""
    from deskbot_server.face_mouth_config_store import normalize_face_mouth_groups

    out = copy.deepcopy(doc)
    phonemes = out.get("phonemes")
    if not isinstance(phonemes, list):
        return out
    for group in normalize_face_mouth_groups(groups):
        mouth = copy.deepcopy(group.get("elements") or [])
        for st in group.get("states") or []:
            expr = find_phoneme_expression(out, str(st))
            if not isinstance(expr, dict):
                continue
            frames = expr.get("frames")
            if not isinstance(frames, list):
                continue
            for fr in frames:
                if not isinstance(fr, dict):
                    continue
                els = fr.get("elements")
                if not isinstance(els, dict):
                    fr["elements"] = {"mouth": copy.deepcopy(mouth)}
                else:
                    els["mouth"] = copy.deepcopy(mouth)
    return out


def apply_emotion_scenes_to_design(
    doc: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """将 ``face_expr_scenes`` 行写回 ``emotions``（保留 doc 元数据与其它 phonemes）。"""
    from deskbot_server.face_expr_scenes_store import normalize_design_scene

    out = copy.deepcopy(doc)
    existing_by_name = {
        str(item.get("name") or "").strip().casefold(): item
        for item in out.get("emotions") or []
        if isinstance(item, dict)
    }
    emotions: list[dict[str, Any]] = []
    for raw in rows or []:
        scene = normalize_design_scene(raw)
        alias = raw.get("alias") if isinstance(raw, dict) else None
        if isinstance(alias, list) and alias:
            scene["alias"] = [str(x).strip() for x in alias if str(x).strip()]
        else:
            scene["alias"] = []
        existing = existing_by_name.get(scene["name"].casefold())
        raw_origin = raw.get("origin") if isinstance(raw, dict) else None
        scene["origin"] = _normalize_origin(
            raw_origin if raw_origin is not None else (existing or {}).get("origin")
        )
        emotions.append(scene)
    out["emotions"] = emotions
    return out


def _summarize_expression(expr: dict[str, Any], *, kind: str) -> dict[str, Any]:
    frames = expr.get("frames") if isinstance(expr.get("frames"), list) else []
    total_ms = 0
    for fr in frames:
        if isinstance(fr, dict):
            total_ms += max(1, int(fr.get("ms") or 800))
    alias_raw = expr.get("alias")
    alias = (
        [str(x).strip() for x in alias_raw if str(x).strip()] if isinstance(alias_raw, list) else []
    )
    return {
        "kind": kind,
        "name": str(expr.get("name") or "").strip(),
        "title": str(expr.get("title") or expr.get("name") or "").strip(),
        "origin": _normalize_origin(expr.get("origin")),
        "alias": alias,
        "frames": len(frames),
        "total_ms": total_ms,
    }


def build_face_expression_catalog() -> dict[str, Any]:
    """音素 + 情绪摘要列表，供调试页展示。"""
    doc = _load_face_design_cached()
    if not isinstance(doc, dict):
        doc = ensure_face_design_file()
    phonemes: list[dict[str, Any]] = []
    for expr in doc.get("phonemes") or []:
        if not isinstance(expr, dict) or not str(expr.get("name") or "").strip():
            continue
        if not isinstance(expr.get("frames"), list) or not expr.get("frames"):
            continue
        phonemes.append(_summarize_expression(expr, kind="phoneme"))
    emotions: list[dict[str, Any]] = []
    for expr in merged_emotion_expressions(doc):
        if not isinstance(expr.get("frames"), list) or not expr.get("frames"):
            continue
        emotions.append(_summarize_expression(expr, kind="emotion"))
    phonemes.sort(key=lambda x: (x["name"].lower(), x["name"]))
    emotions.sort(key=lambda x: (x["name"].lower(), x["name"]))
    return {
        "schema_version": int(doc.get("schema_version") or FACE_DESIGN_SCHEMA_VERSION),
        "revision": int(doc.get("revision") or 0),
        "mappings": copy.deepcopy(doc.get("mappings") or {}),
        "phonemes": phonemes,
        "emotions": emotions,
    }


def resolve_face_expression(
    doc: dict[str, Any] | None, *, kind: str, name: str
) -> Optional[dict[str, Any]]:
    """按 ``kind=phoneme|emotion`` 与 ``name``（含 alias）查表情条目。"""
    k = str(kind or "").strip().lower()
    if k == "phoneme":
        return find_phoneme_expression(doc, name)
    if k in ("emotion", "emotions"):
        return find_emotion_expression(doc, name)
    return None
