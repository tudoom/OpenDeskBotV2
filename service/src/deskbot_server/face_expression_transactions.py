"""Atomic mutations for the editable expression library.

The browser never replaces the whole face document.  It submits an optimistic
transaction containing user-scene creates/updates/deletes, mapping changes and
optional phoneme upserts.  One lock and one atomic replace commit the result.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from deskbot_server.face_design_store import (
    emotions_as_scenes,
    load_face_design_file,
    normalize_face_design_doc,
    update_face_design_file,
)
from deskbot_server.face_expr_scenes_store import (
    builtin_emotion_scenes,
    design_frames_to_pb_chain,
    find_design_scene_by_name,
    normalize_design_scene,
)
from deskbot_server.pb.shapes import normalize_primitive_shape


class FaceExpressionPermissionError(PermissionError):
    """A system expression was targeted by a user-library mutation."""


_PERSISTED_RENDERABLE_SHAPES = frozenset(
    {
        "rect",
        "rect_outline",
        "circle",
        "circle_outline",
        "line",
        "pixel",
        "hline",
        "vline",
        "ellipse",
        "ellipse_fill",
        "triangle",
        "triangle_fill",
        "round_rect",
        "round_rect_outline",
        "rotated_rect_outline",
        "rotated_rect_fill",
        "text",
    }
)

# Keep editable scenes comfortably inside both the on-disk document and the
# PB JSON envelope accepted by the device.  The browser applies the tighter
# brush-specific limit; these server-side limits also protect imports and
# direct API callers.
_MAX_EXPRESSION_FRAMES = 64
_MAX_PRIMITIVES_PER_LAYER = 16
_MAX_PRIMITIVES_PER_FRAME = _MAX_PRIMITIVES_PER_LAYER * 6
_MAX_EXPRESSION_PRIMITIVES = 2048
_MAX_EXPRESSION_JSON_BYTES = 256 * 1024


def _scene_name(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("id") or "").strip()
    return ""


def _normalize_alias(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        key = value.casefold()
        if value and key not in seen:
            out.append(value)
            seen.add(key)
    return out


def _normalize_scene(raw: object, *, name: str, origin: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("scene mutation must be an object")
    payload = copy.deepcopy(raw)
    payload["name"] = name
    payload["origin"] = origin
    scene = normalize_design_scene(payload)
    scene["alias"] = _normalize_alias(payload.get("alias"))
    scene["origin"] = origin
    _validate_persisted_expression_shapes(scene)
    _validate_persisted_expression_size_and_delivery(scene)
    return scene


def _validate_persisted_expression_shapes(expression: dict[str, Any]) -> None:
    """Reject browser-only shapes before they can become a black device scene."""

    renderable = 0
    for frame in expression.get("frames") or []:
        elements = frame.get("elements") if isinstance(frame, dict) else None
        if not isinstance(elements, dict):
            continue
        for primitives in elements.values():
            if not isinstance(primitives, list):
                continue
            for primitive in primitives:
                if not isinstance(primitive, dict):
                    raise ValueError("expression primitive must be an object")
                shape = normalize_primitive_shape(str(primitive.get("shape") or ""))
                if shape not in _PERSISTED_RENDERABLE_SHAPES:
                    raise ValueError(
                        f"unsupported device expression shape {primitive.get('shape')!r}"
                    )
                renderable += 1
    if renderable <= 0:
        raise ValueError("expression requires at least one device-renderable primitive")


def _validate_persisted_expression_size_and_delivery(
    expression: dict[str, Any],
) -> None:
    """Reject scenes that can be saved but cannot be delivered to firmware."""

    frames = expression.get("frames") or []
    if len(frames) > _MAX_EXPRESSION_FRAMES:
        raise ValueError(
            f"expression exceeds {_MAX_EXPRESSION_FRAMES} frames"
        )

    total_primitives = 0
    for frame_index, frame in enumerate(frames):
        elements = frame.get("elements") if isinstance(frame, dict) else None
        frame_primitives = 0
        if isinstance(elements, dict):
            for layer, primitives in elements.items():
                if (
                    isinstance(primitives, list)
                    and len(primitives) > _MAX_PRIMITIVES_PER_LAYER
                ):
                    raise ValueError(
                        f"expression frame {frame_index} layer {layer!r} exceeds "
                        f"{_MAX_PRIMITIVES_PER_LAYER} primitives"
                    )
            frame_primitives = sum(
                len(primitives)
                for primitives in elements.values()
                if isinstance(primitives, list)
            )
        if frame_primitives > _MAX_PRIMITIVES_PER_FRAME:
            raise ValueError(
                f"expression frame {frame_index} exceeds "
                f"{_MAX_PRIMITIVES_PER_FRAME} primitives"
            )
        total_primitives += frame_primitives
    if total_primitives > _MAX_EXPRESSION_PRIMITIVES:
        raise ValueError(
            f"expression exceeds {_MAX_EXPRESSION_PRIMITIVES} primitives"
        )

    serialized_bytes = len(
        json.dumps(
            expression,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if serialized_bytes > _MAX_EXPRESSION_JSON_BYTES:
        raise ValueError(
            f"expression exceeds {_MAX_EXPRESSION_JSON_BYTES} persisted JSON bytes"
        )

    try:
        design_frames_to_pb_chain(
            frames,
            runtime_req="expression-validation",
        )
    except ValueError as exc:
        raise ValueError(
            f"expression cannot fit device PB transport: {exc}"
        ) from exc


def _allocate_user_scene_name(used: set[str]) -> str:
    for _ in range(32):
        name = f"user_expression_{uuid.uuid4().hex[:12]}"
        if name.casefold() not in used:
            used.add(name.casefold())
            return name
    raise RuntimeError("failed to allocate a unique expression id")


def _require_user_scene(scene: dict[str, Any], *, action: str) -> None:
    if str(scene.get("origin") or "system").strip().lower() != "user":
        raise FaceExpressionPermissionError(
            f"system expression {scene.get('name')!r} cannot be {action}; copy it first"
        )


def _validate_scene_lookup_keys(rows: list[dict[str, Any]]) -> None:
    owners: dict[str, str] = {}
    for scene in rows:
        name = str(scene.get("name") or "").strip()
        for raw_key in [name, *_normalize_alias(scene.get("alias"))]:
            key = raw_key.casefold()
            previous = owners.get(key)
            if previous is not None and previous.casefold() != name.casefold():
                raise ValueError(
                    f"expression lookup key {raw_key!r} is shared by {previous!r} and {name!r}"
                )
            owners[key] = name


def _resolve_scene_name(rows: list[dict[str, Any]], raw_name: object) -> str | None:
    name = str(raw_name or "").strip()
    if not name:
        return None
    scene = find_design_scene_by_name(rows, name)
    return str(scene.get("name") or "").strip() if scene is not None else None


def _runtime_catalog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add only built-ins that are not overridden by the persisted library."""

    out = list(rows)
    seen = {
        str(row.get("name") or "").strip().casefold()
        for row in rows
        if isinstance(row, dict)
    }
    for raw in builtin_emotion_scenes():
        name = str(raw.get("name") or "").strip()
        if not name or name.casefold() in seen:
            continue
        scene = copy.deepcopy(raw)
        scene["origin"] = "system"
        scene["alias"] = _normalize_alias(scene.get("alias"))
        out.append(scene)
        seen.add(name.casefold())
    return out


_MAPPING_FALLBACKS: dict[str, tuple[str, ...]] = {
    "idle": ("idle", "default"),
    "listening": ("listening", "idle", "default"),
    "thinking": ("thinking", "idle", "default"),
    # Speaking keeps the neutral base face; the firmware adds only the live
    # mouth overlay.  Migrated installations must not silently restore the old
    # every-reply switch to happy.
    "speaking": ("idle", "default", "happy"),
    "happy": ("happy", "idle", "default"),
    "sad": ("sad", "idle", "default"),
    "angry": ("angry", "idle", "default"),
    "surprised": ("surprised", "astonished", "idle", "default"),
    "sleepy": ("sleep", "idle", "default"),
}


def _repair_migrated_mappings(
    rows: list[dict[str, Any]], mappings: object
) -> dict[str, str]:
    """Canonicalize legacy values and discard only irreparable stale entries."""

    if not isinstance(mappings, dict):
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_value in mappings.items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        resolved = _resolve_scene_name(rows, raw_value)
        if resolved is None:
            for candidate in _MAPPING_FALLBACKS.get(key, (key,)):
                resolved = _resolve_scene_name(rows, candidate)
                if resolved is not None:
                    break
        if resolved is not None:
            out[key] = resolved
    return out


def _normalize_phoneme_upsert(
    raw: object, *, existing: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("phoneme upsert must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("phoneme upsert name required")
    origin = str(
        raw.get("origin")
        or (existing or {}).get("origin")
        or "user"
    ).strip().lower()
    probe = normalize_face_design_doc(
        {
            "phonemes": [{**copy.deepcopy(raw), "name": name, "origin": origin}],
            "emotions": [],
        }
    )
    normalized = probe["phonemes"][0]
    _validate_persisted_expression_shapes(normalized)
    _validate_persisted_expression_size_and_delivery(normalized)
    return normalized


def get_face_expression_state() -> dict[str, Any]:
    doc = load_face_design_file(seed_if_missing=True)
    if not isinstance(doc, dict):
        raise FileNotFoundError("missing face design")
    return {
        "schema_version": int(doc.get("schema_version") or 2),
        "revision": int(doc.get("revision") or 0),
        "config": emotions_as_scenes(doc),
        "map": copy.deepcopy(doc.get("mappings") or {}),
    }


def apply_face_expression_transaction(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("transaction body must be an object")
    expected_raw = payload.get("expected_revision")
    expected_revision: int | None
    if expected_raw is None:
        raise ValueError("expected_revision is required")
    else:
        try:
            expected_revision = int(expected_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_revision must be an integer") from exc
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")

    scenes = payload.get("scenes") or {}
    if not isinstance(scenes, dict):
        raise ValueError("scenes must be an object")
    creates = scenes.get("create") or []
    updates = scenes.get("update") or []
    deletes = scenes.get("delete") or []
    if not all(isinstance(value, list) for value in (creates, updates, deletes)):
        raise ValueError("scenes.create/update/delete must be arrays")
    map_patch = payload.get("map_patch") or {}
    if not isinstance(map_patch, dict):
        raise ValueError("map_patch must be an object")
    map_create_refs = payload.get("map_create_refs") or {}
    if not isinstance(map_create_refs, dict):
        raise ValueError("map_create_refs must be an object")
    phonemes = payload.get("phonemes") or {}
    if not isinstance(phonemes, dict):
        raise ValueError("phonemes must be an object")
    phoneme_upserts = phonemes.get("upsert") or []
    if not isinstance(phoneme_upserts, list):
        raise ValueError("phonemes.upsert must be an array")

    created: list[dict[str, Any]] = []

    def mutate(doc: dict[str, Any]) -> dict[str, Any]:
        rows = [copy.deepcopy(row) for row in doc.get("emotions") or []]
        by_name = {
            str(row.get("name") or "").strip().casefold(): row
            for row in rows
            if isinstance(row, dict)
        }

        delete_names: set[str] = set()
        for raw in deletes:
            name = _scene_name(raw)
            if not name:
                raise ValueError("scene delete name required")
            key = name.casefold()
            scene = by_name.get(key)
            if scene is None:
                raise ValueError(f"unknown expression {name!r}")
            _require_user_scene(scene, action="deleted")
            delete_names.add(key)
        rows = [
            row
            for row in rows
            if str(row.get("name") or "").strip().casefold() not in delete_names
        ]
        by_name = {
            str(row.get("name") or "").strip().casefold(): row for row in rows
        }

        for raw in updates:
            name = _scene_name(raw)
            if not name:
                raise ValueError("scene update name required")
            key = name.casefold()
            existing = by_name.get(key)
            if existing is None:
                raise ValueError(f"unknown expression {name!r}")
            _require_user_scene(existing, action="updated")
            normalized = _normalize_scene(raw, name=existing["name"], origin="user")
            index = rows.index(existing)
            rows[index] = normalized
            by_name[key] = normalized

        used = set(by_name)
        for raw in creates:
            name = _allocate_user_scene_name(used)
            normalized = _normalize_scene(raw, name=name, origin="user")
            rows.append(normalized)
            by_name[name.casefold()] = normalized
            created.append(copy.deepcopy(normalized))

        catalog_rows = _runtime_catalog_rows(rows)
        _validate_scene_lookup_keys(catalog_rows)
        mappings = _repair_migrated_mappings(catalog_rows, doc.get("mappings"))
        for raw_key, raw_value in map_patch.items():
            key = str(raw_key or "").strip().lower()
            if not key:
                raise ValueError("map_patch key required")
            if raw_value is None:
                mappings.pop(key, None)
                continue
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError(f"map_patch value for {key!r} must be a scene name or null")
            resolved = _resolve_scene_name(catalog_rows, raw_value)
            if resolved is None:
                raise ValueError(f"map_patch {key!r} references unknown scene {raw_value!r}")
            mappings[key] = resolved

        for raw_key, raw_index in map_create_refs.items():
            key = str(raw_key or "").strip().lower()
            if not key:
                raise ValueError("map_create_refs key required")
            if isinstance(raw_index, bool):
                raise ValueError(f"map_create_refs index for {key!r} must be an integer")
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"map_create_refs index for {key!r} must be an integer"
                ) from exc
            if index < 0 or index >= len(created):
                raise ValueError(f"map_create_refs index for {key!r} is out of range")
            mappings[key] = created[index]["name"]

        # Deleting a mapped scene without changing that mapping is unsafe.
        for key, scene_name in mappings.items():
            if _resolve_scene_name(catalog_rows, scene_name) is None:
                raise ValueError(f"mapping {key!r} references missing scene {scene_name!r}")

        phoneme_rows = [copy.deepcopy(row) for row in doc.get("phonemes") or []]
        phoneme_by_name = {
            str(row.get("name") or "").strip().casefold(): row
            for row in phoneme_rows
            if isinstance(row, dict)
        }
        for raw in phoneme_upserts:
            raw_name = _scene_name(raw)
            existing = phoneme_by_name.get(raw_name.casefold()) if raw_name else None
            normalized = _normalize_phoneme_upsert(raw, existing=existing)
            key = normalized["name"].casefold()
            if existing is None:
                phoneme_rows.append(normalized)
            else:
                phoneme_rows[phoneme_rows.index(existing)] = normalized
            phoneme_by_name[key] = normalized

        doc["emotions"] = rows
        doc["phonemes"] = phoneme_rows
        doc["mappings"] = mappings
        return doc

    saved = update_face_design_file(mutate, expected_revision=expected_revision)
    return {
        "schema_version": int(saved.get("schema_version") or 2),
        "revision": int(saved.get("revision") or 0),
        "config": emotions_as_scenes(saved),
        "map": copy.deepcopy(saved.get("mappings") or {}),
        "created": created,
    }
