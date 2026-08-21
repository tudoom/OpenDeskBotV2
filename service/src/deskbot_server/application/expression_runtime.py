"""USB-owned facial-expression state, arbitration and playback runtime.

The RTC worker and the Core process intentionally share only the canonical
``data/local/deskbot-face.json`` library.  Core owns the physical USB session,
so all expression PB is resolved and written here rather than in the worker.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import math
import time
import uuid
import zlib
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from deskbot_server.face_expr_scenes_store import (
    design_frames_to_pb_chain,
    load_face_expr_scenes_file,
)
from deskbot_server.pb.shapes import (
    PB_ACTION_APPEND,
    PB_ACTION_REPLACE,
    PB_LEVEL_TASK,
)

logger = logging.getLogger("deskbot-server")

CANONICAL_EXPRESSION_STATES: tuple[str, ...] = (
    "idle",
    "listening",
    "thinking",
    "speaking",
    "happy",
    "sad",
    "angry",
    "surprised",
    "sleepy",
)

# Lifecycle faces are owned by the RTC state machine.  They are valid
# transition inputs, but advertising them to the model lets a tool call fight
# the same state machine that already displays them.
MODEL_EXPRESSION_STATES: tuple[str, ...] = (
    "happy",
    "sad",
    "angry",
    "surprised",
    "sleepy",
)

_MODEL_BLOCKED_EXPRESSION_KEYS = frozenset(
    {
        "idle",
        "default",
        "neutral",
        "ready",
        "listening",
        "listen",
        "hearing",
        "thinking",
        "think",
        "pondering",
        "speaking",
        "speak",
        "talk",
        "talking",
    }
)

# These are accepted runtime inputs, not guesses delegated to the model.  The
# play_expression schema is generated from this exact set plus real file names
# and aliases, so every advertised value is resolvable by the same code path.
EXPRESSION_STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "idle": ("default", "neutral", "ready"),
    "listening": ("listen", "hearing"),
    "thinking": ("think", "pondering"),
    "speaking": ("speak", "talk", "talking"),
    "happy": ("joy", "joyful"),
    "sad": ("unhappy", "sorrow"),
    "angry": ("mad", "anger"),
    "surprised": ("surprise", "astonished"),
    "sleepy": ("sleep", "tired"),
}

_STATE_BY_KEY: dict[str, str] = {
    state: state for state in CANONICAL_EXPRESSION_STATES
}
for _state, _aliases in EXPRESSION_STATE_ALIASES.items():
    for _alias in _aliases:
        _STATE_BY_KEY.setdefault(_alias.casefold(), _state)

_EXPRESSION_MS_MIN = 100
_EXPRESSION_MS_MAX = 30_000
_EXPRESSION_TIMELINE_MS_MAX = 300_000
_EXPRESSION_LEASE_MS_MAX = _EXPRESSION_TIMELINE_MS_MAX + 500


def normalize_expression_state(value: object) -> str | None:
    """Return one canonical state for a supported state name or alias."""

    key = str(value or "").strip().casefold()
    return _STATE_BY_KEY.get(key)


@dataclass(frozen=True, slots=True)
class ExpressionScene:
    name: str
    title: str
    aliases: tuple[str, ...]
    frames: tuple[dict[str, Any], ...]


@dataclass(slots=True)
class ExpressionCatalog:
    """Validated, case-insensitive view of the local expression library."""

    scenes: tuple[ExpressionScene, ...]
    state_mappings: dict[str, str]
    fallback_name: str | None
    _scene_by_key: dict[str, ExpressionScene] = field(repr=False)

    def resolve_scene(self, value: object) -> ExpressionScene | None:
        """Resolve a real scene name/alias, then a supported state/alias."""

        key = str(value or "").strip().casefold()
        if not key:
            return None
        direct = self._scene_by_key.get(key)
        if direct is not None:
            return direct
        state = normalize_expression_state(key)
        if state is None:
            return None
        target = self.state_mappings.get(state)
        return self._scene_by_key.get(str(target or "").casefold())

    def resolve_state(self, value: object) -> ExpressionScene | None:
        state = normalize_expression_state(value)
        if state is None:
            state = "idle"
        target = self.state_mappings.get(state) or self.fallback_name
        return self._scene_by_key.get(str(target or "").casefold())

    def tool_values(self) -> tuple[str, ...]:
        """All and only names accepted by ``play_expression``."""

        values: list[str] = []
        seen: set[str] = set()

        def _add(raw: object) -> None:
            value = str(raw or "").strip()
            key = value.casefold()
            if (
                value
                and key not in _MODEL_BLOCKED_EXPRESSION_KEYS
                and key not in seen
                and self.resolve_scene(value) is not None
            ):
                seen.add(key)
                values.append(value)

        for scene in self.scenes:
            _add(scene.name)
            for alias in scene.aliases:
                _add(alias)
        for state in MODEL_EXPRESSION_STATES:
            _add(state)
            for alias in EXPRESSION_STATE_ALIASES[state]:
                _add(alias)
        return tuple(values)

    def aliases_for_tools(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for value in self.tool_values():
            scene = self.resolve_scene(value)
            if scene is not None and value.casefold() != scene.name.casefold():
                aliases[value] = scene.name
        return aliases

    def summary(self) -> dict[str, Any]:
        return {
            "values": list(self.tool_values()),
            "aliases": self.aliases_for_tools(),
            "state_mappings": dict(self.state_mappings),
            "expressions": [
                {
                    "name": scene.name,
                    "title": scene.title,
                    "aliases": list(scene.aliases),
                }
                for scene in self.scenes
            ],
        }


def _iter_scene_rows(
    face_doc: object,
    explicit_scenes: Iterable[object] | None,
) -> Iterable[object]:
    if explicit_scenes is not None:
        return explicit_scenes
    if not isinstance(face_doc, dict):
        return ()
    for key in ("emotions", "emotion_expressions", "scenes"):
        raw = face_doc.get(key)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return (
                ({**value, "name": name} if isinstance(value, dict) else {})
                for name, value in raw.items()
            )
    return ()


def _normalize_mapping_rows(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_state, raw_target in raw.items():
        state = normalize_expression_state(raw_state)
        target = str(raw_target or "").strip()
        if state is not None and target:
            normalized[state] = target
    return normalized


def build_expression_catalog(
    face_doc: object,
    *,
    scenes: Iterable[object] | None = None,
    legacy_mappings: object = None,
) -> ExpressionCatalog:
    """Build a catalog without accepting mappings to nonexistent scenes.

    ``legacy_mappings`` is consulted only when the unified face document does
    not contain a top-level ``mappings`` member.  An intentionally empty
    canonical mapping therefore never resurrects retired split-file values.
    """

    scene_rows = _iter_scene_rows(face_doc, scenes)
    parsed: list[ExpressionScene] = []
    names_seen: set[str] = set()
    for raw in scene_rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("id") or "").strip()
        frames = raw.get("frames")
        if not name or name.casefold() in names_seen:
            continue
        if not isinstance(frames, list) or not frames:
            continue
        raw_aliases = raw.get("alias")
        if raw_aliases is None:
            raw_aliases = raw.get("aliases")
        aliases: list[str] = []
        alias_seen: set[str] = {name.casefold()}
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        if isinstance(raw_aliases, (list, tuple)):
            for item in raw_aliases:
                alias = str(item or "").strip()
                if alias and alias.casefold() not in alias_seen:
                    alias_seen.add(alias.casefold())
                    aliases.append(alias)
        parsed.append(
            ExpressionScene(
                name=name,
                title=str(raw.get("title") or name).strip(),
                aliases=tuple(aliases),
                frames=tuple(copy.deepcopy(frames)),
            )
        )
        names_seen.add(name.casefold())

    # Names always beat aliases.  This makes a conflicting user alias unable
    # to shadow a separately configured expression name.
    scene_by_key: dict[str, ExpressionScene] = {
        scene.name.casefold(): scene for scene in parsed
    }
    for scene in parsed:
        for alias in scene.aliases:
            scene_by_key.setdefault(alias.casefold(), scene)

    canonical_has_mappings = isinstance(face_doc, dict) and "mappings" in face_doc
    mapping_source = (
        face_doc.get("mappings")
        if canonical_has_mappings and isinstance(face_doc, dict)
        else legacy_mappings
    )
    configured = _normalize_mapping_rows(mapping_source)

    def _real_scene(raw: object) -> ExpressionScene | None:
        return scene_by_key.get(str(raw or "").strip().casefold())

    idle_target = _real_scene(configured.get("idle"))
    if idle_target is None:
        idle_target = _real_scene("idle") or _real_scene("default")
    fallback_name = idle_target.name if idle_target is not None else None

    state_mappings: dict[str, str] = {}
    for state in CANONICAL_EXPRESSION_STATES:
        configured_target = configured.get(state)
        target = _real_scene(configured_target)
        if configured_target is not None and target is None:
            # A typo in the canonical map is never passed to USB or exposed to
            # the model.  Explicitly invalid entries fall back to idle.
            target = idle_target
        elif target is None:
            target = _real_scene(state)
            if target is None and state == "sleepy":
                target = _real_scene("sleep")
            if target is None:
                target = idle_target
        if target is not None:
            state_mappings[state] = target.name

    return ExpressionCatalog(
        scenes=tuple(parsed),
        state_mappings=state_mappings,
        fallback_name=fallback_name,
        _scene_by_key=scene_by_key,
    )


def load_expression_catalog() -> ExpressionCatalog:
    """Load the editable PC-local face document and its real scene aliases."""

    face_doc: object = None
    scene_rows: Iterable[object] | None = None
    legacy_mappings: object = None
    try:
        from deskbot_server.face_design_store import load_face_design_file

        face_doc = load_face_design_file(seed_if_missing=True)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        logger.warning(
            "[expression] failed to load canonical face document",
            exc_info=True,
        )

    if not isinstance(face_doc, dict):
        try:
            scene_rows = load_face_expr_scenes_file(seed_if_missing=False) or []
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            scene_rows = []

    if not isinstance(face_doc, dict) or "mappings" not in face_doc:
        try:
            from deskbot_server.emotion_expr_map_store import (
                load_legacy_emotion_expr_map,
            )

            legacy_mappings = load_legacy_emotion_expr_map()
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            legacy_mappings = {}
    return build_expression_catalog(
        face_doc,
        scenes=scene_rows,
        legacy_mappings=legacy_mappings,
    )


def expression_tool_catalog() -> dict[str, Any]:
    """Return the live names and aliases used to construct the worker tool."""

    return load_expression_catalog().summary()


def _bounded_duration(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(_EXPRESSION_MS_MIN, min(_EXPRESSION_MS_MAX, parsed))


def _scene_playback_ms(
    scene: ExpressionScene,
    *,
    duration_ms: object = None,
) -> int:
    """Return the same bounded duration later declared on the PB timeline."""

    scaled = _bounded_duration(duration_ms)
    if scaled is not None:
        return scaled
    total = 0
    for frame in scene.frames:
        try:
            frame_ms = int(frame.get("ms") or 500)
        except (AttributeError, TypeError, ValueError):
            frame_ms = 500
        total += max(40, min(_EXPRESSION_MS_MAX, frame_ms))
    return max(_EXPRESSION_MS_MIN, min(_EXPRESSION_TIMELINE_MS_MAX, total))


def _lease_duration_ms(
    scene: ExpressionScene,
    *,
    duration_ms: object = None,
    requested_lease_ms: object = None,
) -> int:
    """Keep an explicit source authoritative through its final displayed pose."""

    playback_ms = _scene_playback_ms(scene, duration_ms=duration_ms)
    try:
        requested = int(requested_lease_ms) if requested_lease_ms is not None else 0
    except (TypeError, ValueError):
        requested = 0
    requested = max(0, min(_EXPRESSION_LEASE_MS_MAX, requested))
    # The final frame is held briefly after terminal playback so a late RTC
    # callback cannot make a Web/Agent expression appear to change by itself.
    return min(_EXPRESSION_LEASE_MS_MAX, max(playback_ms + 500, requested))


def _scale_messages(messages: list[dict[str, Any]], duration_ms: int | None) -> None:
    if duration_ms is None:
        return
    total_ms = sum(max(1, int(message.get("chunk_ms") or 0)) for message in messages)
    if total_ms <= 0:
        return
    scale = duration_ms / total_ms
    for message in messages:
        message["chunk_ms"] = max(
            1,
            int(round(max(1, int(message.get("chunk_ms") or 0)) * scale)),
        )
        anim = message.get("anim")
        if isinstance(anim, list):
            for item in anim:
                if isinstance(item, dict) and "ms" in item:
                    item["ms"] = max(
                        1,
                        int(round(max(1, int(item.get("ms") or 0)) * scale)),
                    )
        elif isinstance(anim, dict) and "ms" in anim:
            anim["ms"] = max(
                1,
                int(round(max(1, int(anim.get("ms") or 0)) * scale)),
            )


def build_expression_pb_frames(
    scene: ExpressionScene,
    *,
    request_id: str,
    duration_ms: object = None,
    replace: bool = True,
    level: int = PB_LEVEL_TASK,
    voice_mouth: bool = False,
) -> list[dict[str, Any]]:
    """Build an ordered, media-free PB chain for one validated scene."""

    pairs = design_frames_to_pb_chain(
        [copy.deepcopy(frame) for frame in scene.frames],
        runtime_req=request_id,
    )
    if any(bool(binaries) for _message, binaries in pairs):
        raise ValueError("RTC expression scene unexpectedly produced binary PB")
    messages = [copy.deepcopy(message) for message, _binaries in pairs]
    _scale_messages(messages, _bounded_duration(duration_ms))
    for index, message in enumerate(messages):
        message["action"] = (
            PB_ACTION_REPLACE if replace and index == 0 else PB_ACTION_APPEND
        )
        message["level"] = int(level)
        if index == 0:
            # The device's local RTC mouth overlay is opt-in per display
            # owner. Web/Agent/boot faces must remain pixel-identical to the
            # frames sent by the PC, while RTC speaking may animate its mouth.
            message["voice_mouth"] = bool(voice_mouth)
    return messages


def _state_entry_frames(
    scene: ExpressionScene,
    state: str | None,
) -> ExpressionScene:
    """Lifecycle states are short transitions whose final frame stays visible."""

    limits = {"listening": 3, "thinking": 2}
    frame_limit = limits.get(str(state or ""))
    if frame_limit is None or len(scene.frames) <= frame_limit:
        return scene
    frames = tuple(copy.deepcopy(frame) for frame in scene.frames[:frame_limit])
    target_ms = 600 if state == "listening" else 500
    per_frame = max(100, target_ms // max(1, len(frames)))
    normalized = []
    for frame in frames:
        frame["ms"] = per_frame
        normalized.append(frame)
    return ExpressionScene(
        name=scene.name,
        title=scene.title,
        aliases=scene.aliases,
        frames=tuple(normalized),
    )


def join_expression_pb_chains(
    *chains: Iterable[dict[str, Any]],
    request_id: str,
    replace: bool = True,
    level: int = PB_LEVEL_TASK,
) -> list[dict[str, Any]]:
    """Join media-free expression chains into one canonical PB request.

    A PB request has exactly one terminal frame.  Concatenating independently
    built scenes verbatim would leave an intermediate ``pb_single``/``pb_end``
    and then start a second request with ``append``.  The device deliberately
    rejects that cross-request append while the first scene is still playing.
    Re-indexing the combined frames keeps the scene and its idle tail atomic:
    one ``pb_start``/``pb_end`` chain, one request id and one final seal.
    """

    messages = [copy.deepcopy(message) for chain in chains for message in chain]
    total = len(messages)
    for index, message in enumerate(messages):
        if total == 1:
            message_type = "pb_single"
        elif index == 0:
            message_type = "pb_start"
        elif index == total - 1:
            message_type = "pb_end"
        else:
            message_type = "pb_chunk"
        message["type"] = message_type
        message["req"] = request_id
        message["idx"] = index
        message["action"] = (
            PB_ACTION_REPLACE if replace and index == 0 else PB_ACTION_APPEND
        )
        message["level"] = int(level)
    return messages


def _stable_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_DISPLAY_LAYER_ORDER: tuple[str, ...] = (
    "bg",
    "nose",
    "mouth",
    "eye_l",
    "eye_r",
    "extra",
)
_DISPLAY_SHAPE_ENUM: dict[str, int] = {
    "rect": 1,
    "rect_outline": 2,
    "circle": 3,
    "circle_outline": 4,
    "line": 5,
    "pixel": 6,
    "hline": 7,
    "vline": 8,
    "ellipse": 9,
    "ellipse_fill": 10,
    "triangle": 11,
    "triangle_fill": 12,
    "round_rect": 13,
    "round_rect_outline": 14,
    "rotated_rect_outline": 15,
    "rotated_rect_fill": 16,
    "text": 17,
    "image": 18,
}


def _cpp_lround(value: object, default: int = 0) -> int:
    """Match C++ ``lroundf`` for the finite values accepted by the firmware."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if not math.isfinite(number):
        return int(default)
    return math.floor(number + 0.5) if number >= 0 else math.ceil(number - 0.5)


def _cpp_int(value: object, default: int = 0) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if not math.isfinite(number):
        return int(default)
    return int(number)


def _i16(value: object) -> int:
    integer = _cpp_int(value) & 0xFFFF
    return integer - 0x10000 if integer >= 0x8000 else integer


def _display_primitive_state(
    raw: object,
    *,
    asset_crc32: tuple[int, ...],
) -> tuple[int, ...] | tuple[tuple[int, ...], bytes] | None:
    """Decode one primitive exactly as ``json_fill_layer`` stores it.

    The returned fields deliberately describe the firmware's semantic
    ``StoredPrim`` rather than the source JSON. Unsupported or non-renderable
    primitives are skipped just as they are on the ESP32.
    """

    if not isinstance(raw, dict):
        return None
    shape_name = str(raw.get("shape") or "")
    shape = _DISPLAY_SHAPE_ENUM.get(shape_name)
    if shape is None:
        return None

    values = {
        "shape": shape,
        "color": _cpp_int(raw.get("c", 0xFFFF)) & 0xFFFF,
        "x": 0,
        "y": 0,
        "w": 0,
        "h": 0,
        "r": 0,
        "x1": 0,
        "y1": 0,
        "x2": 0,
        "y2": 0,
        "rot_cx": 0,
        "rot_cy": 0,
        "angle": _i16(_cpp_lround(raw.get("rotation", raw.get("angle", 0)))),
        "stroke_width": max(
            1,
            min(
                12,
                _cpp_int(
                    raw.get(
                        "stroke_width",
                        raw.get("sw", 1),
                    ),
                    1,
                ),
            ),
        ),
        "text_size": 0,
        "asset_index": 0,
        "asset_crc32": 0,
    }

    def assign_xy() -> None:
        values["x"] = _i16(raw.get("x", 0))
        values["y"] = _i16(raw.get("y", 0))

    def assign_wh() -> None:
        values["w"] = _i16(raw.get("w", 0))
        values["h"] = _i16(raw.get("h", 0))

    def assign_rotation(default_x: int, default_y: int) -> None:
        values["rot_cx"] = _i16(
            _cpp_lround(raw.get("rot_cx", raw.get("cx", default_x)))
        )
        values["rot_cy"] = _i16(
            _cpp_lround(raw.get("rot_cy", raw.get("cy", default_y)))
        )

    text = b""
    if shape_name in {"rect", "rect_outline", "round_rect", "round_rect_outline"}:
        assign_xy()
        assign_wh()
        if values["w"] < 1 or values["h"] < 1:
            return None
        if shape_name in {"round_rect", "round_rect_outline"}:
            values["r"] = _i16(raw.get("radius", raw.get("r", 0)))
            if values["r"] < 0:
                return None
        assign_rotation(values["x"] + values["w"] // 2, values["y"] + values["h"] // 2)
    elif shape_name in {"circle", "circle_outline"}:
        assign_xy()
        values["r"] = _i16(raw.get("r", 0))
        if values["r"] < 1:
            return None
        assign_rotation(values["x"], values["y"])
    elif shape_name == "line":
        values["x1"] = _i16(raw.get("x1", 0))
        values["y1"] = _i16(raw.get("y1", 0))
        values["x2"] = _i16(raw.get("x2", 0))
        values["y2"] = _i16(raw.get("y2", 0))
        assign_rotation(
            int((values["x1"] + values["x2"]) / 2),
            int((values["y1"] + values["y2"]) / 2),
        )
    elif shape_name == "pixel":
        assign_xy()
        assign_rotation(values["x"], values["y"])
    elif shape_name in {"hline", "vline"}:
        assign_xy()
        if shape_name == "hline":
            values["w"] = _i16(raw.get("w", 0))
            if values["w"] < 1:
                return None
            assign_rotation(values["x"] + (values["w"] - 1) // 2, values["y"])
        else:
            values["h"] = _i16(raw.get("h", 0))
            if values["h"] < 1:
                return None
            assign_rotation(values["x"], values["y"] + (values["h"] - 1) // 2)
    elif shape_name in {"ellipse", "ellipse_fill"}:
        assign_xy()
        values["w"] = _i16(raw.get("rw", raw.get("w", 0)))
        values["h"] = _i16(raw.get("rh", raw.get("h", 0)))
        if values["w"] < 1 or values["h"] < 1:
            return None
        assign_rotation(values["x"], values["y"])
    elif shape_name in {"triangle", "triangle_fill"}:
        values["x"] = _i16(raw.get("x0", raw.get("x", 0)))
        values["y"] = _i16(raw.get("y0", raw.get("y", 0)))
        values["x1"] = _i16(raw.get("x1", 0))
        values["y1"] = _i16(raw.get("y1", 0))
        values["x2"] = _i16(raw.get("x2", 0))
        values["y2"] = _i16(raw.get("y2", 0))
        assign_rotation(
            int((values["x"] + values["x1"] + values["x2"]) / 3),
            int((values["y"] + values["y1"] + values["y2"]) / 3),
        )
    elif shape_name in {"rotated_rect_outline", "rotated_rect_fill"}:
        assign_xy()
        assign_wh()
        if values["w"] < 1 or values["h"] < 1:
            return None
        assign_rotation(values["x"], values["y"])
    elif shape_name == "text":
        assign_xy()
        raw_text = raw.get("text", raw.get("s", raw.get("str")))
        if raw_text is None or not str(raw_text):
            return None
        text = str(raw_text).encode("utf-8")[:128]
        values["text_size"] = max(
            1,
            min(3, _cpp_int(raw.get("size", raw.get("text_size", 1)), 1)),
        )
        assign_rotation(values["x"], values["y"])
    elif shape_name == "image":
        assign_xy()
        assign_wh()
        if values["w"] < 1 or values["h"] < 1:
            return None
        asset_index = max(0, min(255, _cpp_int(raw.get("asset", 0))))
        if asset_index >= len(asset_crc32):
            return None
        values["asset_index"] = asset_index
        values["asset_crc32"] = int(asset_crc32[asset_index]) & 0xFFFFFFFF

    fields = (
        values["shape"],
        values["color"],
        values["x"],
        values["y"],
        values["w"],
        values["h"],
        values["r"],
        values["x1"],
        values["y1"],
        values["x2"],
        values["y2"],
        values["rot_cx"],
        values["rot_cy"],
        values["angle"],
        values["stroke_width"],
        values["text_size"],
        values["asset_index"],
        values["asset_crc32"],
    )
    return (fields, text)


def display_semantic_crc32(
    messages: Iterable[dict[str, Any]],
    *,
    asset_crc32_by_message: Iterable[Iterable[int]] = (),
) -> str | None:
    """Return the final firmware ``StoredLayer`` CRC for a complete face.

    This is intentionally independent from the SHA-256 transport fingerprint:
    the ESP32 calculates the same CRC after decoding and committing its final
    frame. A mouth-only sequence depends on a pre-existing baseline, so it is
    left unverifiable here instead of manufacturing a false match.
    """

    message_list = [message for message in messages if isinstance(message, dict)]
    if any(bool(message.get("mouth_only")) for message in message_list):
        return None
    asset_rows = [tuple(int(value) & 0xFFFFFFFF for value in row) for row in asset_crc32_by_message]
    layers: dict[str, list[tuple[tuple[int, ...], bytes]]] = {
        layer: [] for layer in _DISPLAY_LAYER_ORDER
    }
    frame_bg = 0
    have_frame = False
    for message_index, message in enumerate(message_list):
        assets = asset_rows[message_index] if message_index < len(asset_rows) else ()
        anim = message.get("anim")
        if not isinstance(anim, list) or not anim:
            continue
        # The firmware installs a request-owned asset table for every display
        # chunk and drops inherited image primitives immediately before
        # decoding that chunk. Pure audio/servo chunks never enter the display
        # worker and therefore leave the currently visible image untouched.
        for layer in _DISPLAY_LAYER_ORDER:
            layers[layer] = [
                primitive
                for primitive in layers[layer]
                if primitive[0][0] != _DISPLAY_SHAPE_ENUM["image"]
            ]
        for frame in anim:
            if not isinstance(frame, dict) or not isinstance(frame.get("elements"), dict):
                continue
            elements = frame["elements"]
            for layer in _DISPLAY_LAYER_ORDER:
                if layer not in elements or not isinstance(elements[layer], list):
                    continue
                decoded: list[tuple[tuple[int, ...], bytes]] = []
                for primitive in elements[layer][:16]:
                    state = _display_primitive_state(primitive, asset_crc32=assets)
                    if state is not None:
                        decoded.append(state)
                layers[layer] = decoded
            raw_bg = frame.get("bg")
            frame_bg = (
                _cpp_int(raw_bg) & 0xFFFF
                if isinstance(raw_bg, (int, float))
                and not isinstance(raw_bg, bool)
                else 0
            )
            have_frame = any(layers[layer] for layer in _DISPLAY_LAYER_ORDER)
    if not have_frame:
        return None

    payload = bytearray(b"DBD1")
    payload.extend(int(frame_bg).to_bytes(2, "little", signed=False))
    for layer in _DISPLAY_LAYER_ORDER:
        primitives = layers[layer]
        payload.append(len(primitives) & 0xFF)
        for fields, text in primitives:
            payload.append(fields[0] & 0xFF)
            payload.extend(int(fields[1]).to_bytes(2, "little", signed=False))
            for value in fields[2:14]:
                payload.extend(int(value).to_bytes(2, "little", signed=True))
            payload.extend(bytes((fields[14] & 0xFF, fields[15] & 0xFF, fields[16] & 0xFF)))
            payload.extend(int(fields[17]).to_bytes(4, "little", signed=False))
            payload.extend(len(text).to_bytes(2, "little", signed=False))
            payload.extend(text)
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def fingerprint_expression_messages(
    messages: Iterable[dict[str, Any]],
    *,
    asset_hashes: Iterable[str] = (),
    asset_crc32_by_message: Iterable[Iterable[int]] = (),
) -> dict[str, Any]:
    """Fingerprint the normalized display timeline, independent of request IDs.

    Callers pass messages after the PB wire normalizer has converted colors to
    RGB565 and shape aliases to device names.  Transport-only fields such as
    ``req`` and ``idx`` are deliberately excluded, so Web, operation history
    and the runtime can compare the exact same visual payload.
    """

    message_list = [raw for raw in messages if isinstance(raw, dict)]
    asset_crc_rows = [tuple(row) for row in asset_crc32_by_message]
    timeline: list[dict[str, Any]] = []
    composed: dict[str, Any] = {}
    final_frame_index = -1
    timeline_ms = 0
    voice_mouth = False
    mouth_only = False
    for raw in message_list:
        voice_mouth = voice_mouth or bool(raw.get("voice_mouth"))
        mouth_only = mouth_only or bool(raw.get("mouth_only"))
        anim = raw.get("anim")
        if not isinstance(anim, list):
            continue
        normalized_anim = [copy.deepcopy(item) for item in anim if isinstance(item, dict)]
        if not normalized_anim:
            continue
        chunk_ms = max(0, int(raw.get("chunk_ms") or 0))
        timeline_ms += chunk_ms
        timeline.append(
            {
                "chunk_ms": chunk_ms,
                "anim": normalized_anim,
            }
        )
        for item in normalized_anim:
            final_frame_index += 1
            elements = item.get("elements")
            if not isinstance(elements, dict):
                continue
            for layer, value in elements.items():
                composed[str(layer)] = copy.deepcopy(value)

    assets = [str(value) for value in asset_hashes if str(value)]
    canonical = {
        "timeline": timeline,
        "voice_mouth": voice_mouth,
        "mouth_only": mouth_only,
        "assets": assets,
    }
    final_payload = {
        "elements": composed,
        "voice_mouth": voice_mouth,
        "mouth_only": mouth_only,
        "assets": assets,
    }
    return {
        "frame_fingerprint": _stable_fingerprint(canonical),
        "final_frame_fingerprint": _stable_fingerprint(final_payload),
        "final_frame_index": final_frame_index,
        "frame_count": final_frame_index + 1,
        "timeline_ms": timeline_ms,
        "voice_mouth": voice_mouth,
        "mouth_only": mouth_only,
        "expected_display_crc32": display_semantic_crc32(
            message_list,
            asset_crc32_by_message=asset_crc_rows,
        ),
    }


def fingerprint_pb_display_pairs(
    pairs: Iterable[tuple[dict[str, Any], Iterable[bytes]]],
) -> dict[str, Any]:
    """Fingerprint an externally-produced PB display, including image assets."""

    messages: list[dict[str, Any]] = []
    asset_hashes: list[str] = []
    asset_crc_rows: list[list[int]] = []
    for message, binaries in pairs:
        if not isinstance(message, dict):
            continue
        messages.append(message)
        assets = message.get("assets")
        asset_count = len(assets) if isinstance(assets, list) else 0
        blobs = list(binaries or [])
        crc_row: list[int] = []
        if asset_count:
            for blob in blobs[-asset_count:]:
                payload = bytes(blob)
                asset_hashes.append(hashlib.sha256(payload).hexdigest())
                crc_row.append(zlib.crc32(payload) & 0xFFFFFFFF)
        asset_crc_rows.append(crc_row)
    return fingerprint_expression_messages(
        messages,
        asset_hashes=asset_hashes,
        asset_crc32_by_message=asset_crc_rows,
    )


@dataclass(frozen=True, slots=True)
class ExpressionSendResult:
    requested: str
    resolved: str | None
    status: str
    frames: int = 0
    delivered: int = 0
    state: str | None = None
    source: str | None = None
    reason: str | None = None
    title: str | None = None
    frame_fingerprint: str | None = None
    final_frame_fingerprint: str | None = None
    final_frame_index: int | None = None
    frame_count: int | None = None
    timeline_ms: int | None = None
    operation_id: str | None = None
    expected_display_crc32: str | None = None
    device_display_crc32: str | None = None
    display_crc_match: bool | None = None
    previous_source: str | None = None
    preempt_reason: str | None = None
    lease_token: str | None = None
    coalesced: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"accepted", "played", "unchanged"}

    def as_tool_result(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": "play_expression",
            "ok": self.ok,
            "status": self.status,
            "expression": self.resolved or self.requested,
            "requested_expression": self.requested,
            "frames": self.frames,
            "delivered": self.delivered,
        }
        if self.state is not None:
            payload["state"] = self.state
        if self.source is not None:
            payload["source"] = self.source
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.previous_source is not None:
            payload["previous_source"] = self.previous_source
        if self.preempt_reason is not None:
            payload["preempt_reason"] = self.preempt_reason
        if self.title is not None:
            payload["title"] = self.title
        if self.frame_fingerprint is not None:
            payload["frame_fingerprint"] = self.frame_fingerprint
        if self.final_frame_fingerprint is not None:
            payload["final_frame_fingerprint"] = self.final_frame_fingerprint
        if self.final_frame_index is not None:
            payload["final_frame_index"] = self.final_frame_index
        if self.frame_count is not None:
            payload["frame_count"] = self.frame_count
        if self.timeline_ms is not None:
            payload["timeline_ms"] = self.timeline_ms
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
        if self.expected_display_crc32 is not None:
            payload["expected_display_crc32"] = self.expected_display_crc32
        if self.device_display_crc32 is not None:
            payload["device_display_crc32"] = self.device_display_crc32
        if self.display_crc_match is not None:
            payload["display_crc_match"] = self.display_crc_match
        if self.lease_token is not None:
            # The full token is an internal capability. Diagnostics only need
            # a short correlation id and must not expose the capability itself.
            payload["lease_id"] = self.lease_token[:8]
        if self.coalesced:
            payload["coalesced"] = True
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class _ExpressionRequest:
    requested: str
    state: str | None
    duration_ms: object
    restore_idle: bool
    force: bool
    reason: str
    source: str
    future: asyncio.Future[ExpressionSendResult]
    wait_for_played: bool = True
    operation_id: str | None = None
    scene_override: ExpressionScene | None = None
    lease_token: str | None = None
    kind: str = "state"
    state_generation: int = 0
    explicit_generation: int = 0
    cancel_generation: int = 0
    previous_source: str | None = None
    preempt_reason: str | None = None
    superseded: asyncio.Event = field(default_factory=asyncio.Event)
    request_id: str | None = None
    title: str | None = None
    fingerprint: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _ExpressionLease:
    token: str
    source: str
    priority: int
    generation: int
    duration_ms: int | None = None
    expires_at: float | None = None
    acquired_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _DisplayedExpression:
    name: str
    title: str
    source: str
    reason: str
    status: str
    frame_fingerprint: str
    final_frame_fingerprint: str
    final_frame_index: int
    frame_count: int
    timeline_ms: int
    operation_id: str | None
    state: str | None
    voice_mouth: bool
    mouth_only: bool
    expected_display_crc32: str | None
    device_display_crc32: str | None
    display_crc_match: bool | None
    generation: int
    updated_at: float


@dataclass(frozen=True, slots=True)
class _ExpressionOperation:
    request_id: str
    name: str
    title: str
    source: str
    reason: str
    status: str
    frame_fingerprint: str
    final_frame_fingerprint: str
    final_frame_index: int
    frame_count: int
    timeline_ms: int
    operation_id: str | None
    state: str | None
    voice_mouth: bool
    mouth_only: bool
    expected_display_crc32: str | None
    device_display_crc32: str | None
    display_crc_match: bool | None
    updated_at: float


@asynccontextmanager
async def _ordered_session_chain(session: object):
    # Share the hub's whole-PB-chain lock as well as the USB adapter lock.  RTC
    # writes go to the exact session directly, but must not splice themselves
    # into a boot/TTS chain that happens to use AsrChatHub on that same object.
    try:
        from deskbot_server.ws.ws_send import _pb_ws_chain_serial_lock

        hub_chain_lock = _pb_ws_chain_serial_lock(session)
    except (ImportError, AttributeError, TypeError):
        hub_chain_lock = asyncio.Lock()
    chain_factory = getattr(session, "downlink_chain", None)
    async with hub_chain_lock:
        if callable(chain_factory):
            async with chain_factory():
                yield
            return
        else:
            yield


class RtcExpressionRuntime:
    """Latest-wins expression arbiter bound to one exact USB session.

    The historical class name is retained for compatibility.  Its lifecycle is
    USB-owned: RTC is only one state producer alongside Web, Agent and boot.
    """

    def __init__(
        self,
        device_id: str,
        device_session: object,
        *,
        catalog_loader: Callable[[], ExpressionCatalog] = load_expression_catalog,
    ) -> None:
        self.device_id = str(device_id or "").strip()
        self.device_session = device_session
        self._catalog_loader = catalog_loader
        self._queue_lock = asyncio.Lock()
        self._pending: list[_ExpressionRequest] = []
        self._active_request: _ExpressionRequest | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_scene_name: str | None = None
        self._displayed: _DisplayedExpression | None = None
        self._operations: deque[_ExpressionOperation] = deque(maxlen=32)
        # Keep enough ownership changes to diagnose a complete voice turn even
        # when listening/thinking/Agent/image events happen in quick succession.
        self._display_history: deque[_DisplayedExpression] = deque(maxlen=32)
        self._display_generation = 0
        self._accepted_generation = 0
        self._desired_state = "idle"
        self._mouth_overlay: dict[str, Any] | None = None
        self._lease: _ExpressionLease | None = None
        self._lease_generation = 0
        self._lease_timer_task: asyncio.Task[None] | None = None
        self._completion_tasks: set[asyncio.Task[None]] = set()
        self._state_generation = 0
        self._explicit_generation = 0
        self._cancel_generation = 0

    @property
    def last_scene_name(self) -> str | None:
        return self._last_scene_name

    @property
    def desired_state(self) -> str:
        return self._desired_state

    @property
    def active_source(self) -> str:
        return self._displayed.source if self._displayed is not None else "none"

    def _record_displayed(
        self,
        *,
        name: str,
        title: str,
        source: str,
        reason: str,
        status: str,
        fingerprint: dict[str, Any],
        operation_id: str | None = None,
        state: str | None = None,
        device_display_crc32: str | None = None,
    ) -> None:
        now = time.time()
        previous = self._displayed
        same_accepted_playback = bool(
            previous is not None
            and previous.status == "accepted"
            and status == "played"
            and previous.name == name
            and previous.source == source
            and previous.reason == reason
            and previous.operation_id == operation_id
            and previous.state == state
            and previous.frame_fingerprint
            == str(fingerprint["frame_fingerprint"])
            and previous.final_frame_fingerprint
            == str(fingerprint["final_frame_fingerprint"])
        )
        generation = previous.generation if same_accepted_playback else self._display_generation + 1
        displayed = _DisplayedExpression(
            name=name,
            title=title,
            source=source,
            reason=reason,
            status=status,
            frame_fingerprint=str(fingerprint["frame_fingerprint"]),
            final_frame_fingerprint=str(fingerprint["final_frame_fingerprint"]),
            final_frame_index=int(fingerprint["final_frame_index"]),
            frame_count=max(0, int(fingerprint.get("frame_count") or 0)),
            timeline_ms=max(0, int(fingerprint.get("timeline_ms") or 0)),
            operation_id=(str(operation_id).strip() or None) if operation_id else None,
            state=state,
            voice_mouth=bool(fingerprint.get("voice_mouth")),
            mouth_only=bool(fingerprint.get("mouth_only")),
            expected_display_crc32=(
                str(fingerprint.get("expected_display_crc32") or "") or None
            ),
            device_display_crc32=(
                str(device_display_crc32 or "").strip().lower() or None
            ),
            display_crc_match=(
                str(device_display_crc32 or "").strip().lower()
                == str(fingerprint.get("expected_display_crc32") or "").strip().lower()
                if device_display_crc32
                and fingerprint.get("expected_display_crc32")
                else None
            ),
            generation=generation,
            updated_at=now,
        )
        if same_accepted_playback:
            if self._display_history and self._display_history[-1].generation == generation:
                self._display_history.pop()
            self._display_history.append(displayed)
        else:
            self._display_generation = generation
            self._display_history.append(displayed)
        self._last_scene_name = name
        self._displayed = displayed

    def _record_operation(
        self,
        *,
        request_id: str,
        name: str,
        title: str,
        source: str,
        reason: str,
        status: str,
        fingerprint: dict[str, Any],
        operation_id: str | None = None,
        state: str | None = None,
        device_display_crc32: str | None = None,
    ) -> None:
        operation = _ExpressionOperation(
            request_id=request_id,
            name=name,
            title=title,
            source=source,
            reason=reason,
            status=status,
            frame_fingerprint=str(fingerprint.get("frame_fingerprint") or ""),
            final_frame_fingerprint=str(
                fingerprint.get("final_frame_fingerprint") or ""
            ),
            final_frame_index=int(fingerprint.get("final_frame_index") or 0),
            frame_count=max(0, int(fingerprint.get("frame_count") or 0)),
            timeline_ms=max(0, int(fingerprint.get("timeline_ms") or 0)),
            operation_id=(str(operation_id).strip() or None) if operation_id else None,
            state=state,
            voice_mouth=bool(fingerprint.get("voice_mouth")),
            mouth_only=bool(fingerprint.get("mouth_only")),
            expected_display_crc32=(
                str(fingerprint.get("expected_display_crc32") or "") or None
            ),
            device_display_crc32=(
                str(device_display_crc32 or "").strip().lower() or None
            ),
            display_crc_match=(
                str(device_display_crc32 or "").strip().lower()
                == str(fingerprint.get("expected_display_crc32") or "").strip().lower()
                if device_display_crc32
                and fingerprint.get("expected_display_crc32")
                else None
            ),
            updated_at=time.time(),
        )
        self._operations = deque(
            (item for item in self._operations if item.request_id != request_id),
            maxlen=32,
        )
        self._operations.append(operation)

    def _record_result_operation(
        self,
        request: _ExpressionRequest,
        *,
        result: ExpressionSendResult,
    ) -> None:
        """Keep every resolved send attempt attributable in diagnostics."""

        if not request.request_id:
            return
        self._record_operation(
            request_id=request.request_id,
            name=(
                request.scene_override.name
                if request.scene_override is not None
                else result.resolved or request.requested
            ),
            title=(
                request.title
                or (
                    request.scene_override.title
                    if request.scene_override is not None
                    else result.resolved or request.requested
                )
            ),
            source=request.source,
            reason=request.reason,
            status=result.status,
            fingerprint=request.fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=result.device_display_crc32,
        )

    @staticmethod
    def _displayed_payload(displayed: _DisplayedExpression) -> dict[str, Any]:
        return {
            "expression": displayed.name,
            "title": displayed.title,
            "source": displayed.source,
            "reason": displayed.reason,
            "status": displayed.status,
            "state": displayed.state,
            "voice_mouth": displayed.voice_mouth,
            "mouth_only": displayed.mouth_only,
            "expected_display_crc32": displayed.expected_display_crc32,
            "device_display_crc32": displayed.device_display_crc32,
            "display_crc_match": displayed.display_crc_match,
            "frame_fingerprint": displayed.frame_fingerprint,
            "final_frame_fingerprint": displayed.final_frame_fingerprint,
            "final_frame_index": displayed.final_frame_index,
            "frame_count": displayed.frame_count,
            "timeline_ms": displayed.timeline_ms,
            "operation_id": displayed.operation_id,
            "generation": displayed.generation,
            "displayed_at": displayed.updated_at,
        }

    @staticmethod
    def _operation_payload(operation: _ExpressionOperation) -> dict[str, Any]:
        return {
            "request_id": operation.request_id,
            "expression": operation.name,
            "title": operation.title,
            "source": operation.source,
            "reason": operation.reason,
            "status": operation.status,
            "state": operation.state,
            "voice_mouth": operation.voice_mouth,
            "mouth_only": operation.mouth_only,
            "frame_fingerprint": operation.frame_fingerprint,
            "final_frame_fingerprint": operation.final_frame_fingerprint,
            "final_frame_index": operation.final_frame_index,
            "frame_count": operation.frame_count,
            "timeline_ms": operation.timeline_ms,
            "operation_id": operation.operation_id,
            "expected_display_crc32": operation.expected_display_crc32,
            "device_display_crc32": operation.device_display_crc32,
            "display_crc_match": operation.display_crc_match,
            "updated_at": operation.updated_at,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return enough ownership state to identify every visible face."""

        lease = self._lease
        active = self._active_request
        displayed = self._displayed
        rtc_mouth_active = bool(
            displayed is not None
            and displayed.state == "speaking"
            and displayed.voice_mouth
            and self._desired_state == "speaking"
        )
        overlay = self._mouth_overlay
        mouth_overlay = (
            dict(overlay)
            if overlay is not None
            else {
                "active": rtc_mouth_active,
                "kind": "rtc_pcm" if rtc_mouth_active else None,
                "source": "rtc" if rtc_mouth_active else None,
                "reason": "agent_speaking" if rtc_mouth_active else None,
                "started_at": displayed.updated_at if rtc_mouth_active else None,
            }
        )
        return {
            "device_id": self.device_id,
            "displayed_expression": displayed.name if displayed is not None else None,
            "displayed_title": displayed.title if displayed is not None else None,
            "displayed_source": displayed.source if displayed is not None else None,
            "displayed_reason": displayed.reason if displayed is not None else None,
            "displayed_status": displayed.status if displayed is not None else None,
            "displayed_state": displayed.state if displayed is not None else None,
            "voice_mouth_enabled": (
                displayed.voice_mouth if displayed is not None else False
            ),
            "mouth_only": displayed.mouth_only if displayed is not None else False,
            "expected_display_crc32": (
                displayed.expected_display_crc32 if displayed is not None else None
            ),
            "device_display_crc32": (
                displayed.device_display_crc32 if displayed is not None else None
            ),
            "display_crc_match": (
                displayed.display_crc_match if displayed is not None else None
            ),
            "mouth_overlay": mouth_overlay,
            "frame_fingerprint": (
                displayed.frame_fingerprint if displayed is not None else None
            ),
            "final_frame_fingerprint": (
                displayed.final_frame_fingerprint if displayed is not None else None
            ),
            "final_frame_index": (
                displayed.final_frame_index if displayed is not None else None
            ),
            "frame_count": displayed.frame_count if displayed is not None else 0,
            "timeline_ms": displayed.timeline_ms if displayed is not None else 0,
            "displayed_operation_id": (
                displayed.operation_id if displayed is not None else None
            ),
            "display_generation": displayed.generation if displayed is not None else 0,
            "displayed_at": displayed.updated_at if displayed is not None else None,
            # Newest first: this makes a lifecycle sequence such as
            # listening -> thinking -> speaking -> idle directly auditable
            # instead of looking like unrelated faces chosen by the device.
            "history": [
                self._displayed_payload(item)
                for item in reversed(self._display_history)
            ],
            "operations": [
                self._operation_payload(item) for item in reversed(self._operations)
            ],
            "desired_state": self._desired_state,
            # Kept for API compatibility: it now means the source that last
            # reached the display, rather than whichever lease happens to live.
            "active_source": displayed.source if displayed is not None else "none",
            "lease": (
                {
                    "lease_id": lease.token[:8],
                    "source": lease.source,
                    "priority": lease.priority,
                    "duration_ms": lease.duration_ms,
                    "expires_at": lease.expires_at,
                    "reason": lease.acquired_reason,
                }
                if lease is not None
                else None
            ),
            "active_request": (
                {
                    "requested": active.requested,
                    "state": active.state,
                    "source": active.source,
                    "reason": active.reason,
                    "kind": active.kind,
                }
                if active is not None
                else None
            ),
            "pending": [
                {
                    "requested": request.requested,
                    "state": request.state,
                    "source": request.source,
                    "reason": request.reason,
                    "kind": request.kind,
                }
                for request in self._pending
            ],
        }

    def begin_mouth_overlay(
        self,
        *,
        source: str,
        reason: str,
        kind: str = "phoneme",
    ) -> str:
        """Expose a temporary mouth-only writer without treating it as a face."""

        token = uuid.uuid4().hex
        self._mouth_overlay = {
            "active": True,
            "kind": str(kind or "phoneme"),
            "source": str(source or "tts"),
            "reason": str(reason or "mouth_overlay"),
            "started_at": time.time(),
            "overlay_id": token[:8],
        }
        return token

    def end_mouth_overlay(self, token: str) -> None:
        overlay = self._mouth_overlay
        if overlay is None or overlay.get("overlay_id") != str(token or "")[:8]:
            return
        self._mouth_overlay = None

    async def transition(
        self,
        state: object,
        *,
        force: bool = False,
        reason: str = "agent_state",
    ) -> ExpressionSendResult:
        canonical = normalize_expression_state(state) or "idle"
        async with self._queue_lock:
            self._desired_state = canonical
            if self._lease is not None and not force:
                logger.info(
                    "[expression] deferred device_id=%s source=rtc "
                    "requested=%s resolved=%s owner=%s reason=%s",
                    self.device_id,
                    canonical,
                    self._last_scene_name or "",
                    self._lease.source,
                    reason,
                )
                return ExpressionSendResult(
                    requested=canonical,
                    resolved=self._last_scene_name,
                    status="deferred",
                    state=canonical,
                    source="rtc",
                    reason=reason,
                    previous_source=self._lease.source,
                )
            if not force:
                active = self._active_request
                duplicate_active = bool(
                    active is not None
                    and active.kind == "state"
                    and active.state == canonical
                    and not active.superseded.is_set()
                )
                duplicate_pending = any(
                    pending.kind == "state"
                    and pending.state == canonical
                    and not pending.superseded.is_set()
                    for pending in self._pending
                )
                if duplicate_active or duplicate_pending:
                    logger.info(
                        "[expression] coalesced duplicate lifecycle "
                        "device_id=%s state=%s reason=%s",
                        self.device_id,
                        canonical,
                        reason,
                    )
                    return ExpressionSendResult(
                        requested=canonical,
                        resolved=self._last_scene_name,
                        status="coalesced",
                        state=canonical,
                        source="rtc",
                        reason=reason,
                        coalesced=True,
                    )
        return await self._submit(
            requested=canonical,
            state=canonical,
            duration_ms=None,
            restore_idle=False,
            force=force,
            reason=reason,
            source="rtc",
            kind="cancel" if force and canonical == "idle" else "state",
        )

    set_state = transition

    async def restore_idle(
        self,
        *,
        reason: str,
        force: bool = False,
    ) -> ExpressionSendResult:
        return await self.transition("idle", force=force, reason=reason)

    async def play_expression(
        self,
        name: object,
        *,
        duration_ms: object = None,
        restore_idle: bool = True,
        reason: str = "tool",
        source: str = "agent",
        priority: int = 20,
        lease_ms: object = None,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        del restore_idle
        normalized_operation_id = str(operation_id or "").strip() or None
        duration = _bounded_duration(duration_ms)
        catalog = self._catalog_loader()
        scene = catalog.resolve_scene(name)
        if scene is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=None,
                status="not_found",
                source=source,
                reason=reason,
                operation_id=normalized_operation_id,
                error=f"unknown expression: {str(name or '').strip()}",
            )
        lease_duration = _lease_duration_ms(
            scene,
            duration_ms=duration,
            requested_lease_ms=lease_ms,
        )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=lease_duration,
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=str(name or "").strip(),
                state=None,
                duration_ms=duration,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def play_frames(
        self,
        frames: Iterable[dict[str, Any]],
        *,
        name: object = None,
        title: object = None,
        source: str = "web",
        priority: int = 30,
        reason: str = "web_preview",
        hold_ms: object = None,
        persist_until_preempted: bool = False,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        """Play validated ad-hoc Web frames through the same source arbiter."""

        normalized_operation_id = str(operation_id or "").strip() or None
        copied = tuple(copy.deepcopy(list(frames)))
        scene_name = str(name or "").strip() or f"adhoc_{source}"
        scene_title = str(title or "").strip() or scene_name
        scene = ExpressionScene(
            name=scene_name,
            title=scene_title,
            aliases=(),
            frames=copied,
        )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=(
                None
                if persist_until_preempted
                else _lease_duration_ms(
                    scene,
                    requested_lease_ms=hold_ms,
                )
            ),
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=scene.name,
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=scene.name,
                state=None,
                duration_ms=None,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def play_scene(
        self,
        name: object,
        *,
        source: str,
        priority: int,
        reason: str,
        hold_ms: object = None,
        persist_until_preempted: bool = False,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        """Play a catalog scene under an explicit source lease."""

        normalized_operation_id = str(operation_id or "").strip() or None
        catalog = self._catalog_loader()
        scene = catalog.resolve_scene(name)
        if scene is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=None,
                status="not_found",
                source=source,
                reason=reason,
                operation_id=normalized_operation_id,
                error=f"unknown expression: {str(name or '').strip()}",
            )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=(
                None
                if persist_until_preempted
                else _lease_duration_ms(
                    scene,
                    requested_lease_ms=hold_ms,
                )
            ),
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=str(name or "").strip(),
                state=None,
                duration_ms=None,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def acquire_external_display(
        self,
        *,
        name: str,
        title: str,
        source: str,
        priority: int,
        reason: str,
    ) -> str | None:
        """Acquire the display lane for a PB writer that carries binary assets."""

        return await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=None,
            reason=reason,
        )

    def external_display_matches(self, token: str) -> bool:
        lease = self._lease
        return bool(lease is not None and lease.token == str(token or ""))

    async def release_external_display(
        self,
        token: str,
        *,
        reason: str,
        restore: bool = True,
    ) -> ExpressionSendResult | None:
        """Release a binary display lease, optionally without a redundant redraw."""

        async with self._queue_lock:
            if self._lease is None or self._lease.token != token:
                return None
            lease = self._lease
            self._lease = None
            self._explicit_generation += 1
            timer = self._lease_timer_task
            self._lease_timer_task = None
            desired = self._desired_state
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        if not restore:
            logger.info(
                "[expression] external lease released device_id=%s source=%s "
                "restore=false reason=%s",
                self.device_id,
                lease.source,
                reason,
            )
            return None
        return await self._submit(
            requested=desired,
            state=desired,
            duration_ms=None,
            restore_idle=False,
            force=True,
            reason=f"restore_desired:{reason}",
            source="rtc",
            kind="state",
            previous_source=lease.source,
            preempt_reason=reason,
        )

    def record_external_display(
        self,
        token: str,
        *,
        name: str,
        title: str,
        source: str,
        reason: str,
        fingerprint: dict[str, Any],
    ) -> bool:
        """Record an externally-sent image only while its lease is current."""

        if not self.external_display_matches(token):
            return False
        self._record_displayed(
            name=str(name or "external_display"),
            title=str(title or name or "external display"),
            source=str(source or "external"),
            reason=str(reason or "external_display"),
            status="played",
            fingerprint=fingerprint,
        )
        logger.info(
            "[expression] external played device_id=%s source=%s name=%s "
            "fingerprint=%s reason=%s",
            self.device_id,
            source,
            name,
            str(fingerprint.get("frame_fingerprint") or "")[:12],
            reason,
        )
        return True

    async def preempt_lease(self, *, reason: str) -> None:
        """End any explicit lease without first restoring a stale state.

        权威语义 · 生命周期 preempt 矩阵（与 ``acquire_lease`` 的数字优先级
        表配对；正式文档由后续 docs 波次引用此处）：

        ============================  ============================================
        触发事件（rtc_runtime）        对显示权的影响
        ============================  ============================================
        barge_in                      ``preempt_lease`` → ``transition("listening")``
        user_speech_start             同上
        user_speech_confirmed_end     不 preempt；``transition("thinking")``
        agent_state（生命周期态）      不 preempt；``transition(state)``（持有租约
                                      期间仅记 ``_desired_state``，返回 deferred）
        ============================  ============================================

        **preempt 无视优先级**：无论当前租约是 10（voice_link_feedback）、
        20（agent）、25（manual_tts）还是 30（web），用户开口说话
        （barge_in / user_speech_start）都会立即终结租约——真人语音边沿
        是比任何显式表情源都高的事实输入，不参与数字比较。

        其余租约终点（对照）：

        - ``release_lease`` / 租约到期 ``_expire_lease``：恢复
          ``_desired_state``（``restore_desired:<reason>``）。
        - ``cancel``（会话取消/断开）：desired 置 idle 后走 release。
        - 同级或更高优先级新租约：``acquire_lease`` 直接替换旧租约。

        preempt 本身 **不下发** 新表情：只作废租约与 explicit 队列，随后的
        lifecycle ``transition`` 决定屏幕内容。
        """

        async with self._queue_lock:
            lease = self._lease
            if lease is None:
                return
            self._lease = None
            self._explicit_generation += 1
            timer = self._lease_timer_task
            self._lease_timer_task = None
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            stale = [
                request for request in self._pending if request.kind == "explicit"
            ]
            if stale:
                self._pending[:] = [
                    request for request in self._pending if request.kind != "explicit"
                ]
                self._coalesce_requests(stale)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        logger.info(
            "[expression] lease preempted device_id=%s source=%s reason=%s",
            self.device_id,
            lease.source,
            reason,
        )

    async def acquire_lease(
        self,
        *,
        source: str,
        priority: int,
        duration_ms: object = None,
        reason: str | None = None,
    ) -> str | None:
        """Take the explicit display lane, arbitrated by numeric priority.

        权威语义 · 数字优先级表（与 ``preempt_lease`` 的生命周期矩阵配对；
        正式文档由后续 docs 波次引用此处）：

        ====  ===================  ==========================================
        优先  source               入口 / 用途
        ====  ===================  ==========================================
        10    voice_link_feedback  「语音启动中…」冷启动反馈，绝不抢真实内容
        20    agent                LLM 工具 ``play_expression``
        25    manual_tts           手动 TTS 链路（chat_flow）的表情
        30    web                  Web 预览 / 下发（人工意图，最高）
        ====  ===================  ==========================================

        判定规则：

        - 仅当 ``priority < 当前租约.priority`` 时拒绝（返回 ``None``，
          调用方上报 ``deferred``）。
        - **同级替换**：``priority >= 当前租约.priority`` 即替换旧租约并作废
          其 explicit 队列（后到者胜，无排队等待）。
        - 生命周期状态机（source="rtc" 的 ``transition``）不走租约：持有
          租约期间它只更新 ``_desired_state``；租约结束（release/到期/
          preempt）后恢复该 desired 态。
        - 用户语音边沿通过 ``preempt_lease`` 无条件终结任何租约，不参与
          本表的数字比较。
        """
        try:
            duration = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None:
            duration = max(_EXPRESSION_MS_MIN, min(_EXPRESSION_LEASE_MS_MAX, duration))
        async with self._queue_lock:
            current = self._lease
            if current is not None and int(priority) < current.priority:
                logger.info(
                    "[expression] lease denied device_id=%s source=%s "
                    "priority=%d owner=%s owner_priority=%d reason=%s",
                    self.device_id,
                    source,
                    int(priority),
                    current.source,
                    current.priority,
                    reason or "",
                )
                return None
            replaced_source = current.source if current is not None else None
            self._lease_generation += 1
            self._explicit_generation += 1
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            replaced = [
                request
                for request in self._pending
                if request.kind == "explicit"
            ]
            if replaced:
                self._pending[:] = [
                    request
                    for request in self._pending
                    if request.kind != "explicit"
                ]
                self._coalesce_requests(replaced)
            token = uuid.uuid4().hex
            self._lease = _ExpressionLease(
                token=token,
                source=str(source or "explicit"),
                priority=int(priority),
                generation=self._lease_generation,
                duration_ms=duration,
                expires_at=None,
                acquired_reason=str(reason or "").strip() or None,
            )
            if self._lease_timer_task is not None:
                self._lease_timer_task.cancel()
                self._lease_timer_task = None
            logger.info(
                "[expression] lease acquired device_id=%s source=%s "
                "priority=%d previous_source=%s duration_ms=%s reason=%s token=%s",
                self.device_id,
                source,
                int(priority),
                replaced_source or "",
                "persistent" if duration is None else duration,
                reason or "",
                token[:8],
            )
            return token

    async def _arm_lease_timer(self, token: str) -> None:
        """Start the hold only after the target face reached the device."""

        loop = asyncio.get_running_loop()
        async with self._queue_lock:
            lease = self._lease
            if lease is None or lease.token != token:
                return
            duration = lease.duration_ms
            if duration is None:
                return
            previous = self._lease_timer_task
            lease.expires_at = loop.time() + duration / 1000.0
            timer = asyncio.create_task(
                self._expire_lease(token, duration / 1000.0),
                name=f"deskbot-expression-lease:{self.device_id}",
            )
            self._lease_timer_task = timer
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)

    async def _expire_lease(self, token: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            await self.release_lease(token, reason="lease_expired")
        except asyncio.CancelledError:
            return

    async def release_lease(self, token: str, *, reason: str) -> ExpressionSendResult | None:
        async with self._queue_lock:
            if self._lease is None or self._lease.token != token:
                return None
            lease = self._lease
            self._lease = None
            self._explicit_generation += 1
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            stale_explicit = [
                request
                for request in self._pending
                if request.kind == "explicit"
            ]
            if stale_explicit:
                self._pending[:] = [
                    request
                    for request in self._pending
                    if request.kind != "explicit"
                ]
                self._coalesce_requests(stale_explicit)
            timer = self._lease_timer_task
            self._lease_timer_task = None
            desired = self._desired_state
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        return await self._submit(
            requested=desired,
            state=desired,
            duration_ms=None,
            restore_idle=False,
            force=True,
            reason=f"restore_desired:{reason}",
            source="rtc",
            kind="state",
            previous_source=lease.source,
            preempt_reason=reason,
        )

    async def cancel(self, *, reason: str = "cancel") -> ExpressionSendResult:
        """Coalesce queued work and replace the display with idle."""
        async with self._queue_lock:
            self._desired_state = "idle"
            lease = self._lease
        if lease is not None:
            async with self._queue_lock:
                self._cancel_generation += 1
            released = await self.release_lease(lease.token, reason=reason)
            if released is not None:
                return released
        return await self.restore_idle(reason=reason, force=True)

    async def close(self, *, restore_idle: bool = True) -> None:
        if self._closed:
            return
        if restore_idle:
            try:
                await asyncio.wait_for(
                    self.cancel(reason="disconnect"),
                    timeout=2.0,
                )
            except TimeoutError:
                logger.info(
                    "[expression] timed out restoring idle during close "
                    "device_id=%s",
                    self.device_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "[expression] idle restore failed during close "
                    "device_id=%s",
                    self.device_id,
                    exc_info=True,
                )
        async with self._queue_lock:
            self._closed = True
            self._lease = None
            self._mouth_overlay = None
            lease_timer = self._lease_timer_task
            self._lease_timer_task = None
            pending = list(self._pending)
            self._pending.clear()
            active = self._active_request
            if active is not None:
                active.superseded.set()
            for request in pending:
                request.superseded.set()
                if request.future.done():
                    continue
                request.future.set_result(
                    ExpressionSendResult(
                        requested=request.requested,
                        resolved=None,
                        status="closed",
                        state=request.state,
                        source=request.source,
                        reason=request.reason,
                        previous_source=request.previous_source,
                        preempt_reason=request.preempt_reason,
                        lease_token=request.lease_token,
                        operation_id=request.operation_id,
                    )
                )
            worker = self._worker_task
            completion_tasks = list(self._completion_tasks)
            self._completion_tasks.clear()
        if lease_timer is not None:
            lease_timer.cancel()
        for task in completion_tasks:
            task.cancel()
        if lease_timer is not None or completion_tasks:
            await asyncio.gather(
                *([lease_timer] if lease_timer is not None else []),
                *completion_tasks,
                return_exceptions=True,
            )
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(worker, return_exceptions=True),
                    timeout=0.5,
                )
            except TimeoutError:
                logger.info(
                    "[expression] worker did not stop before close bound "
                    "device_id=%s",
                    self.device_id,
                )

    async def _submit(
        self,
        *,
        requested: str,
        state: str | None,
        duration_ms: object,
        restore_idle: bool,
        force: bool,
        reason: str,
        source: str,
        kind: str,
        wait_for_played: bool = True,
        operation_id: str | None = None,
        scene_override: ExpressionScene | None = None,
        lease_token: str | None = None,
        previous_source: str | None = None,
        preempt_reason: str | None = None,
    ) -> ExpressionSendResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ExpressionSendResult] = loop.create_future()
        request = _ExpressionRequest(
            requested=requested,
            state=state,
            duration_ms=duration_ms,
            restore_idle=restore_idle,
            force=force,
            reason=reason,
            source=source,
            future=future,
            wait_for_played=wait_for_played,
            operation_id=operation_id,
            scene_override=scene_override,
            lease_token=lease_token,
            kind=kind,
            previous_source=previous_source,
            preempt_reason=preempt_reason,
            request_id=f"rtc-expr-{uuid.uuid4().hex[:12]}",
        )
        async with self._queue_lock:
            if self._closed:
                return ExpressionSendResult(
                    requested=requested,
                    resolved=None,
                    status="closed",
                    state=state,
                    source=source,
                    reason=reason,
                    previous_source=previous_source,
                    preempt_reason=preempt_reason,
                    lease_token=lease_token,
                    operation_id=operation_id,
                )
            if kind == "cancel":
                self._cancel_generation += 1
                self._state_generation += 1
                request.cancel_generation = self._cancel_generation
                request.state_generation = self._state_generation
                if self._active_request is not None:
                    self._active_request.superseded.set()
                self._coalesce_requests(self._pending, status="cancelled")
                self._pending.clear()
                self._pending.append(request)
            elif kind == "explicit":
                # Explicit tool calls outrank automatic participant state.
                # Preserve the order of other explicit tools, but invalidate
                # an active/queued state so it cannot delay this request.
                self._state_generation += 1
                request.state_generation = self._state_generation
                request.explicit_generation = self._explicit_generation
                request.cancel_generation = self._cancel_generation
                if (
                    self._active_request is not None
                    and self._active_request.kind == "state"
                ):
                    self._active_request.superseded.set()
                retained: list[_ExpressionRequest] = []
                removed: list[_ExpressionRequest] = []
                for queued in self._pending:
                    (removed if queued.kind == "state" else retained).append(queued)
                self._coalesce_requests(removed)
                self._pending[:] = [*retained, request]
            else:
                self._state_generation += 1
                request.state_generation = self._state_generation
                request.cancel_generation = self._cancel_generation
                if (
                    self._active_request is not None
                    and self._active_request.kind == "state"
                ):
                    self._active_request.superseded.set()
                if self._pending and self._pending[-1].kind == "state":
                    superseded = self._pending.pop()
                    self._coalesce_requests([superseded])
                self._pending.append(request)
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(
                    self._run_queue(),
                    name=f"deskbot-rtc-expression:{self.device_id}",
                )
        return await future

    def _coalesce_requests(
        self,
        requests: Iterable[_ExpressionRequest],
        *,
        status: str = "coalesced",
    ) -> None:
        for request in requests:
            request.superseded.set()
            if request.request_id:
                self._record_operation(
                    request_id=request.request_id,
                    name=(
                        request.scene_override.name
                        if request.scene_override is not None
                        else request.requested
                    ),
                    title=(
                        request.title
                        or (
                            request.scene_override.title
                            if request.scene_override is not None
                            else request.requested
                        )
                    ),
                    source=request.source,
                    reason=request.reason,
                    status=status,
                    fingerprint=request.fingerprint,
                    operation_id=request.operation_id,
                    state=request.state,
                )
            if request.future.done():
                continue
            request.future.set_result(
                ExpressionSendResult(
                    requested=request.requested,
                    resolved=None,
                    status=status,
                    state=request.state,
                    source=request.source,
                    reason=request.reason,
                    previous_source=request.previous_source,
                    preempt_reason=request.preempt_reason,
                    lease_token=request.lease_token,
                    operation_id=request.operation_id,
                    coalesced=status == "coalesced",
                )
            )

    async def _run_queue(self) -> None:
        while True:
            # Let callbacks already scheduled for this event-loop turn settle;
            # participant state commonly arrives as a short burst.
            await asyncio.sleep(0)
            async with self._queue_lock:
                request = self._pending.pop(0) if self._pending else None
                if request is None:
                    self._active_request = None
                    self._worker_task = None
                    return
                self._active_request = request
            try:
                result = await self._execute(request)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                logger.exception(
                    "[expression] transition failed device_id=%s "
                    "requested=%s reason=%s",
                    self.device_id,
                    request.requested,
                    request.reason,
                )
                result = ExpressionSendResult(
                    requested=request.requested,
                    resolved=None,
                    status="error",
                    state=request.state,
                    source=request.source,
                    reason=request.reason,
                    previous_source=request.previous_source,
                    preempt_reason=request.preempt_reason,
                    lease_token=request.lease_token,
                    operation_id=request.operation_id,
                    error=str(exc) or type(exc).__name__,
                )
            if not request.future.done():
                if result.status not in {"played", "accepted"}:
                    self._record_result_operation(request, result=result)
                request.future.set_result(result)

    def _is_superseded(self, request: _ExpressionRequest) -> bool:
        if request.superseded.is_set():
            return True
        if request.cancel_generation != self._cancel_generation:
            return True
        return bool(
            (
                request.kind == "state"
                and request.state_generation != self._state_generation
            )
            or (
                request.kind == "explicit"
                and request.explicit_generation != self._explicit_generation
            )
        )

    def _superseded_result(
        self,
        request: _ExpressionRequest,
        *,
        resolved: str | None,
        frames: int,
        delivered: int,
        cancelled: bool = False,
    ) -> ExpressionSendResult:
        status = "cancelled" if cancelled else "coalesced"
        if request.request_id:
            self._record_operation(
                request_id=request.request_id,
                name=(
                    request.scene_override.name
                    if request.scene_override is not None
                    else resolved or request.requested
                ),
                title=(
                    request.title
                    or (
                        request.scene_override.title
                        if request.scene_override is not None
                        else resolved or request.requested
                    )
                ),
                source=request.source,
                reason=request.reason,
                status=status,
                fingerprint=request.fingerprint,
                operation_id=request.operation_id,
                state=request.state,
            )
        return ExpressionSendResult(
            requested=request.requested,
            resolved=resolved,
            status=status,
            frames=frames,
            delivered=delivered,
            state=request.state,
            source=request.source,
            reason=request.reason,
            previous_source=request.previous_source,
            preempt_reason=request.preempt_reason,
            lease_token=request.lease_token,
            operation_id=request.operation_id,
            coalesced=not cancelled,
        )

    async def _execute(self, request: _ExpressionRequest) -> ExpressionSendResult:
        result_title: str | None = None
        result_fingerprint: dict[str, Any] = {}

        def _result(
            *,
            resolved: str | None,
            status: str,
            frames: int = 0,
            delivered: int = 0,
            coalesced: bool = False,
            error: str | None = None,
            device_display_crc32: str | None = None,
        ) -> ExpressionSendResult:
            expected_crc = result_fingerprint.get("expected_display_crc32")
            normalized_device_crc = (
                str(device_display_crc32 or "").strip().lower() or None
            )
            return ExpressionSendResult(
                requested=request.requested,
                resolved=resolved,
                status=status,
                frames=frames,
                delivered=delivered,
                state=request.state,
                source=request.source,
                reason=request.reason,
                previous_source=request.previous_source,
                preempt_reason=request.preempt_reason,
                lease_token=request.lease_token,
                title=result_title,
                frame_fingerprint=result_fingerprint.get("frame_fingerprint"),
                final_frame_fingerprint=result_fingerprint.get(
                    "final_frame_fingerprint"
                ),
                final_frame_index=result_fingerprint.get("final_frame_index"),
                frame_count=result_fingerprint.get("frame_count"),
                timeline_ms=result_fingerprint.get("timeline_ms"),
                operation_id=request.operation_id,
                expected_display_crc32=expected_crc,
                device_display_crc32=normalized_device_crc,
                display_crc_match=(
                    normalized_device_crc == str(expected_crc).strip().lower()
                    if normalized_device_crc and expected_crc
                    else None
                ),
                coalesced=coalesced,
                error=error,
            )

        if self._is_superseded(request):
            return self._superseded_result(
                request,
                resolved=None,
                frames=0,
                delivered=0,
                cancelled=request.cancel_generation != self._cancel_generation,
            )
        catalog = self._catalog_loader()
        scene = request.scene_override or (
            catalog.resolve_state(request.state)
            if request.state is not None
            else catalog.resolve_scene(request.requested)
        )
        if scene is None:
            return _result(
                resolved=None,
                status="not_found",
                error=f"unknown expression: {request.requested}",
            )
        result_title = scene.title
        request.title = scene.title
        if request.state is not None:
            scene = _state_entry_frames(scene, request.state)

        request_id = request.request_id or f"rtc-expr-{uuid.uuid4().hex[:12]}"
        request.request_id = request_id
        scene_messages = build_expression_pb_frames(
            scene,
            request_id=request_id,
            duration_ms=request.duration_ms,
            replace=True,
            voice_mouth=(request.kind == "state" and request.state == "speaking"),
        )
        messages = join_expression_pb_chains(
            scene_messages,
            request_id=request_id,
        )
        if not messages:
            return _result(
                resolved=scene.name,
                status="not_found",
                error=f"expression has no frames: {scene.name}",
            )
        result_fingerprint = fingerprint_expression_messages(messages)
        request.fingerprint = result_fingerprint
        if (
            request.state is not None
            and not request.force
            and self._displayed is not None
            and self._displayed.name.casefold() == scene.name.casefold()
            and self._displayed.frame_fingerprint
            == result_fingerprint["frame_fingerprint"]
        ):
            return _result(
                resolved=scene.name,
                status="unchanged",
            )

        delivered = 0
        final_idx = int(messages[-1].get("idx") or 0)
        from deskbot_server.constants import PB_WAIT_ACK
        from deskbot_server.servo_protocol import pb_sequence_completion_budget_ms
        from deskbot_server.ws.pb_ack_waiter import (
            pb_ack_gate,
            pb_wait_ack_timeout_sec,
            pb_wait_played_timeout_sec,
        )
        playback_ms = pb_sequence_completion_budget_ms(messages)

        try:
            async with _ordered_session_chain(self.device_session):
                ack_started = False
                try:
                    if self.device_id and PB_WAIT_ACK:
                        try:
                            # A fast Agent tool return transfers the played
                            # wait to a background task, so ACK ownership must
                            # not be tied to this queue-worker task.
                            await pb_ack_gate.begin_req(
                                self.device_id,
                                request_id,
                                owner_cleanup=False,
                            )
                            ack_started = True
                        except RuntimeError as exc:
                            return _result(
                                resolved=scene.name,
                                status="request_active",
                                frames=len(messages),
                                delivered=0,
                                error=str(exc),
                            )

                    for message in messages:
                        if self._is_superseded(request):
                            return self._superseded_result(
                                request,
                                resolved=scene.name,
                                frames=len(messages),
                                delivered=delivered,
                                cancelled=(
                                    request.cancel_generation
                                    != self._cancel_generation
                                ),
                            )
                        if _message_declares_binary(message):
                            raise ValueError(
                                "RTC expression PB must not declare binary payloads"
                            )
                        wire = json.dumps(
                            message,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        sent = await self._send_frame_interruptibly(
                            request,
                            wire,
                        )
                        if not sent or self._is_superseded(request):
                            return self._superseded_result(
                                request,
                                resolved=scene.name,
                                frames=len(messages),
                                delivered=delivered,
                                cancelled=(
                                    request.cancel_generation
                                    != self._cancel_generation
                                ),
                            )
                        delivered += 1

                    if not (self.device_id and PB_WAIT_ACK):
                        self._record_operation(
                            request_id=request_id,
                            name=scene.name,
                            title=scene.title,
                            source=request.source,
                            reason=request.reason,
                            status="accepted",
                            fingerprint=result_fingerprint,
                            operation_id=request.operation_id,
                            state=request.state,
                        )
                        if request.lease_token is not None:
                            await self._arm_lease_timer(request.lease_token)
                        return _result(
                            resolved=scene.name,
                            status="accepted",
                            frames=len(messages),
                            delivered=delivered,
                            error="PB terminal ACK waiting is disabled",
                        )

                    accepted_result = await self._wait_ack_interruptibly(
                        request,
                        pb_ack_gate.wait_accepted_result(
                            self.device_id,
                            request_id,
                            final_idx,
                            timeout=pb_wait_ack_timeout_sec(),
                        ),
                    )
                    if accepted_result is None:
                        return self._superseded_result(
                            request,
                            resolved=scene.name,
                            frames=len(messages),
                            delivered=delivered,
                            cancelled=(
                                request.cancel_generation != self._cancel_generation
                            ),
                        )
                    if not accepted_result.ok:
                        status = accepted_result.status
                        if status == "not_started" and ack_started:
                            status = "disconnected"
                        return _result(
                            resolved=scene.name,
                            status=status,
                            frames=len(messages),
                            delivered=delivered,
                            error=(
                                "device did not accept the complete expression PB chain"
                            ),
                        )

                    self._accepted_generation += 1
                    accepted_generation = self._accepted_generation
                    self._record_operation(
                        request_id=request_id,
                        name=scene.name,
                        title=scene.title,
                        source=request.source,
                        reason=request.reason,
                        status="accepted",
                        fingerprint=result_fingerprint,
                        operation_id=request.operation_id,
                        state=request.state,
                    )

                    # The lease starts when the complete chain is accepted,
                    # not after its terminal played ACK. This makes the lease
                    # cover the actual playback interval instead of adding a
                    # second full playback interval afterwards.
                    if request.lease_token is not None:
                        await self._arm_lease_timer(request.lease_token)

                    if not request.wait_for_played:
                        completion = asyncio.create_task(
                            self._finish_played_ack(
                                request_id=request_id,
                                final_idx=final_idx,
                                playback_ms=playback_ms,
                                scene_name=scene.name,
                                scene_title=scene.title,
                                source=request.source,
                                reason=request.reason,
                                fingerprint=result_fingerprint,
                                operation_id=request.operation_id,
                                state=request.state,
                                accepted_generation=accepted_generation,
                                ack_started=ack_started,
                                lease_token=request.lease_token,
                            ),
                            name=f"deskbot-expression-played:{request_id}",
                        )
                        self._completion_tasks.add(completion)
                        completion.add_done_callback(self._completion_tasks.discard)
                        ack_started = False
                        return _result(
                            resolved=scene.name,
                            status="accepted",
                            frames=len(messages),
                            delivered=delivered,
                        )

                    played_result = await self._wait_ack_interruptibly(
                        request,
                        pb_ack_gate.wait_played_result(
                            self.device_id,
                            request_id,
                            final_idx,
                            timeout=pb_wait_played_timeout_sec(playback_ms),
                        ),
                    )
                    if played_result is None:
                        return self._superseded_result(
                            request,
                            resolved=scene.name,
                            frames=len(messages),
                            delivered=delivered,
                            cancelled=(
                                request.cancel_generation != self._cancel_generation
                            ),
                        )
                    if not played_result.ok:
                        status = played_result.status
                        if status == "not_started" and ack_started:
                            status = "disconnected"
                        self._record_operation(
                            request_id=request_id,
                            name=scene.name,
                            title=scene.title,
                            source=request.source,
                            reason=request.reason,
                            status=status,
                            fingerprint=result_fingerprint,
                            operation_id=request.operation_id,
                            state=request.state,
                        )
                        return _result(
                            resolved=scene.name,
                            status=status,
                            frames=len(messages),
                            delivered=delivered,
                            error="device did not complete the expression PB chain",
                        )
                    device_display_crc32 = played_result.display_crc32
                finally:
                    if ack_started:
                        await pb_ack_gate.end_req(self.device_id, request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "[expression] USB send failed device_id=%s expression=%s "
                "error=%s",
                self.device_id,
                scene.name,
                type(exc).__name__,
            )
            return _result(
                resolved=scene.name,
                status="device_unavailable",
                frames=len(messages),
                delivered=delivered,
                error=str(exc) or type(exc).__name__,
            )
        self._record_displayed(
            name=scene.name,
            title=scene.title,
            source=request.source,
            reason=request.reason,
            status="played",
            fingerprint=result_fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=device_display_crc32,
        )
        self._record_operation(
            request_id=request_id,
            name=scene.name,
            title=scene.title,
            source=request.source,
            reason=request.reason,
            status="played",
            fingerprint=result_fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=device_display_crc32,
        )
        logger.info(
            "[expression] played device_id=%s source=%s requested=%s "
            "resolved=%s state=%s frames=%d reason=%s previous_source=%s "
            "preempt_reason=%s lease=%s fingerprint=%s final=%s",
            self.device_id,
            request.source,
            request.requested,
            scene.name,
            request.state or "",
            delivered,
            request.reason,
            request.previous_source or "",
            request.preempt_reason or "",
            (request.lease_token or "")[:8],
            result_fingerprint["frame_fingerprint"][:12],
            result_fingerprint["final_frame_fingerprint"][:12],
        )
        return _result(
            resolved=scene.name,
            status="played",
            frames=len(messages),
            delivered=delivered,
            device_display_crc32=device_display_crc32,
        )

    async def _finish_played_ack(
        self,
        *,
        request_id: str,
        final_idx: int,
        playback_ms: int,
        scene_name: str,
        scene_title: str,
        source: str,
        reason: str,
        fingerprint: dict[str, Any],
        operation_id: str | None,
        state: str | None,
        accepted_generation: int,
        ack_started: bool,
        lease_token: str | None,
    ) -> None:
        from deskbot_server.ws.pb_ack_waiter import (
            pb_ack_gate,
            pb_wait_played_timeout_sec,
        )

        try:
            result = await pb_ack_gate.wait_played_result(
                self.device_id,
                request_id,
                final_idx,
                timeout=pb_wait_played_timeout_sec(playback_ms),
            )
            if not result.ok:
                self._record_operation(
                    request_id=request_id,
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status=result.status,
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                )
                logger.warning(
                    "[expression] background playback terminal=%s "
                    "device_id=%s req=%s expression=%s",
                    result.status,
                    self.device_id,
                    request_id,
                    scene_name,
                )
                if lease_token is not None:
                    await self.release_lease(
                        lease_token,
                        reason=f"background_{result.status}",
                    )
            elif self._accepted_generation == accepted_generation:
                self._record_displayed(
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status="played",
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                    device_display_crc32=result.display_crc32,
                )
            if result.ok:
                self._record_operation(
                    request_id=request_id,
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status="played",
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                    device_display_crc32=result.display_crc32,
                )
        finally:
            if ack_started:
                await pb_ack_gate.end_req(self.device_id, request_id)

    async def _wait_ack_interruptibly(
        self,
        request: _ExpressionRequest,
        wait_result: Any,
    ) -> Any | None:
        """Wait for one ACK phase unless a newer expression supersedes it."""

        ack_task = (
            wait_result
            if isinstance(wait_result, asyncio.Task)
            else asyncio.create_task(wait_result)
        )
        superseded_task = asyncio.create_task(request.superseded.wait())
        try:
            done, _pending = await asyncio.wait(
                {ack_task, superseded_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if superseded_task in done and self._is_superseded(request):
                if not ack_task.done():
                    ack_task.cancel()
                await asyncio.gather(ack_task, return_exceptions=True)
                return None
            return await ack_task
        finally:
            if not ack_task.done():
                ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)
            if not superseded_task.done():
                superseded_task.cancel()
            await asyncio.gather(superseded_task, return_exceptions=True)

    async def _send_frame_interruptibly(
        self,
        request: _ExpressionRequest,
        wire: str,
    ) -> bool:
        """Stop awaiting a stale state/tool frame as soon as it is superseded."""

        send_task = asyncio.create_task(_send_json_pb(self.device_session, wire))
        superseded_task = asyncio.create_task(request.superseded.wait())
        try:
            done, _pending = await asyncio.wait(
                {send_task, superseded_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if superseded_task in done and self._is_superseded(request):
                if not send_task.done():
                    send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
                return False
            await send_task
            return not self._is_superseded(request)
        finally:
            if not send_task.done():
                send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            if not superseded_task.done():
                superseded_task.cancel()
            await asyncio.gather(superseded_task, return_exceptions=True)


def _message_declares_binary(message: dict[str, Any]) -> bool:
    try:
        from deskbot_server.pb.servo_pcm import pb_expected_binary_lengths

        return bool(pb_expected_binary_lengths(message))
    except (ImportError, TypeError, ValueError):
        audio = message.get("audio")
        return bool(
            isinstance(audio, dict)
            and int(audio.get("next_bin_len") or 0) > 0
        )


async def _send_json_pb(session: object, wire: str) -> None:
    sender = getattr(session, "send_pb_wire", None)
    if not callable(sender):
        sender = getattr(session, "send", None)
    if not callable(sender):
        raise RuntimeError("USB session has no PB sender")
    result = sender(wire)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise RuntimeError("USB session rejected PB write")


_rtc_expression_runtimes: dict[str, RtcExpressionRuntime] = {}


def register_rtc_expression_runtime(runtime: RtcExpressionRuntime) -> None:
    if runtime.device_id:
        _rtc_expression_runtimes[runtime.device_id] = runtime


def unregister_rtc_expression_runtime(runtime: RtcExpressionRuntime) -> None:
    if _rtc_expression_runtimes.get(runtime.device_id) is runtime:
        _rtc_expression_runtimes.pop(runtime.device_id, None)


def get_rtc_expression_runtime(device_id: object) -> RtcExpressionRuntime | None:
    return _rtc_expression_runtimes.get(str(device_id or "").strip())


# USB-owned terminology for new callers.  Compatibility aliases above remain
# while older worker/core integration code is migrated incrementally.
register_expression_runtime = register_rtc_expression_runtime
unregister_expression_runtime = unregister_rtc_expression_runtime
get_expression_runtime = get_rtc_expression_runtime


async def play_rtc_expression(
    device_id: object,
    name: object,
    *,
    duration_ms: object = None,
) -> dict[str, Any]:
    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        requested = str(name or "").strip()
        return ExpressionSendResult(
            requested=requested,
            resolved=None,
            status="device_unavailable",
            source="agent",
            reason="tool_play_expression",
            error="RTC expression runtime is not attached to the USB device",
        ).as_tool_result()
    result = await runtime.play_expression(
        name,
        duration_ms=duration_ms,
        reason="tool_play_expression",
        source="agent",
        priority=20,
        lease_ms=duration_ms,
        wait_for_played=False,
    )
    return result.as_tool_result()


async def play_web_expression_frames(
    device_id: object,
    frames: Iterable[dict[str, Any]],
    *,
    name: object = None,
    title: object = None,
    hold_ms: object = None,
    reason: str = "web_preview",
    operation_id: object = None,
) -> ExpressionSendResult:
    """Route Web preview frames through the USB expression arbiter."""

    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        return ExpressionSendResult(
            requested="adhoc_web",
            resolved=None,
            status="device_unavailable",
            source="web",
            reason=reason,
            operation_id=str(operation_id or "").strip() or None,
            error="expression runtime is not attached to the USB device",
        )
    return await runtime.play_frames(
        frames,
        name=name,
        title=title,
        source="web",
        priority=30,
        reason=reason,
        hold_ms=hold_ms,
        persist_until_preempted=hold_ms is None,
        wait_for_played=True,
        operation_id=operation_id,
    )


async def play_web_expression_scene(
    device_id: object,
    name: object,
    *,
    hold_ms: object = None,
    reason: str = "web_scene",
    operation_id: object = None,
) -> ExpressionSendResult:
    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        return ExpressionSendResult(
            requested=str(name or "").strip(),
            resolved=None,
            status="device_unavailable",
            source="web",
            reason=reason,
            operation_id=str(operation_id or "").strip() or None,
            error="expression runtime is not attached to the USB device",
        )
    return await runtime.play_scene(
        name,
        source="web",
        priority=30,
        reason=reason,
        hold_ms=hold_ms,
        persist_until_preempted=hold_ms is None,
        wait_for_played=True,
        operation_id=operation_id,
    )


__all__ = [
    "CANONICAL_EXPRESSION_STATES",
    "EXPRESSION_STATE_ALIASES",
    "ExpressionCatalog",
    "ExpressionScene",
    "ExpressionSendResult",
    "RtcExpressionRuntime",
    "build_expression_catalog",
    "build_expression_pb_frames",
    "join_expression_pb_chains",
    "expression_tool_catalog",
    "fingerprint_expression_messages",
    "fingerprint_pb_display_pairs",
    "get_expression_runtime",
    "get_rtc_expression_runtime",
    "load_expression_catalog",
    "normalize_expression_state",
    "play_web_expression_frames",
    "play_web_expression_scene",
    "play_rtc_expression",
    "register_expression_runtime",
    "register_rtc_expression_runtime",
    "unregister_expression_runtime",
    "unregister_rtc_expression_runtime",
]
