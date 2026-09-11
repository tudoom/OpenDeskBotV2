"""表情目录与 PB 帧构建（纯数据/纯函数层）。

从 expression_runtime 拆出：目录解析、场景归一化、PB 帧构建、显示语义 CRC 与
指纹都是无运行态的纯函数，运行时（租约/仲裁/USB 下发）在 expression_runtime。
名字经 expression_runtime 原样再导出，既有调用方与测试不必改。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from deskbot_server.face_expr_scenes_store import (
    builtin_cartoon_scenes,
    decode_scene_assets,
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
    # 卡通（位图）表情：帧内 image 图元引用的 JPEG 资产（已解码），下发时作 PB 二进制附件
    assets: tuple[bytes, ...] = ()


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
            # 文件里的 emotions + 代码里注册的内置卡通套图（system 场景 cartoon_default_*）：
            # 控制台校验映射时看得到它们，运行时目录也必须解析得到，否则映射到内置卡通的
            # 倾听/说话状态会退回矢量脸并带口型（2026-09-07 真机复现）。
            seen = {
                str(row.get("name") or "").strip().casefold()
                for row in raw
                if isinstance(row, dict)
            }
            extra = [
                row for row in builtin_cartoon_scenes()
                if str(row.get("name") or "").strip().casefold() not in seen
            ]
            return list(raw) + extra
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
                assets=tuple(decode_scene_assets(raw)),
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


BITMAP_LOOP_FILL_MS = 9000
_BITMAP_LOOP_MAX_FRAMES = 60


def _repeat_frames_to_fill(frames: list[dict[str, Any]], fill_ms: int) -> list[dict[str, Any]]:
    """把帧周期重复到总时长 ≥ fill_ms（帧数封顶），保持每帧原始 ms。"""
    cycle_ms = sum(max(1, int(f.get("ms") or 500)) for f in frames)
    if cycle_ms <= 0:
        return frames
    out: list[dict[str, Any]] = []
    total = 0
    while total < fill_ms and len(out) + len(frames) <= _BITMAP_LOOP_MAX_FRAMES:
        out.extend(copy.deepcopy(f) for f in frames)
        total += cycle_ms
    return out or frames


def build_expression_pb_frames(
    scene: ExpressionScene,
    *,
    request_id: str,
    duration_ms: object = None,
    replace: bool = True,
    level: int = PB_LEVEL_TASK,
    voice_mouth: bool = False,
    out_binaries: list[list[bytes]] | None = None,
) -> list[dict[str, Any]]:
    """Build an ordered PB chain for one validated scene.

    位图（卡通）表情会带 JPEG 二进制附件：调用方传 ``out_binaries`` 接收每条消息对应的
    附件列表；不传则沿用旧约束——出现二进制即报错（RTC 口型等路径不允许位图）。
    """

    frames = [copy.deepcopy(frame) for frame in scene.frames]
    if scene.assets:
        # 位图脸不能做口型叠加：固件在新 PB 到来时会丢掉上一帧的 JPEG 图元
        # （pb_drop_inherited_images），mouth_only 会变成黑脸只剩一张嘴。说话状态用
        # 自己的"说话动画"位图表情循环播放代替。
        voice_mouth = False
    if scene.assets and duration_ms is None and len(frames) > 1:
        # 位图（卡通）表情：固件对 PB 只播一遍就停在最后一帧，把帧周期重复铺满
        # 一条 ≈9s 的消息（≤10s chunk，附件只带一次），配合运行时到期续发形成循环。
        frames = _repeat_frames_to_fill(frames, BITMAP_LOOP_FILL_MS)
    pairs = design_frames_to_pb_chain(
        frames,
        runtime_req=request_id,
        assets=list(scene.assets),
    )
    if out_binaries is None:
        if any(bool(binaries) for _message, binaries in pairs):
            raise ValueError("RTC expression scene unexpectedly produced binary PB")
    else:
        out_binaries.clear()
        out_binaries.extend([list(binaries) for _message, binaries in pairs])
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
        assets=scene.assets,
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
