"""场景编排持久化（``data/local/scene_playbooks.json``，顶层数组）。

每条编排由 ``chunks[]`` 组成，与 PB 协议一致：每包（``pb_single`` / 一轮 ``pb_chunk``）
可独立携带口播、表情、舵机，按顺序串行下发。
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from typing import Any, Optional

from deskbot_server.constants import SCENE_PLAYBOOKS_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import global_config_dir, local_data_dir, resolve_json_path

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
    # 动作条目的时长下限对齐舵机协议段下限（50ms）：早期姿态网格存过
    # 40ms 级别的步，存储层放行会在保存/运行的 expand 校验处炸整表。
    from deskbot_server.servo_protocol import SERVO_MIN_SEGMENT_DURATION_MS

    ms = max(SERVO_MIN_SEGMENT_DURATION_MS, _normalize_ms(raw.get("ms")))
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


# ── 内置表演 ──────────────────────────────────────────────────
# 随包 data/global/scene_playbooks.json 是内置表演的正本（含录制的姿态时间线）；
# 这里再放一份口播+表情+动作的五段基础表演，作为 global 文件缺失时的兜底，
# 也是 builtin 名单的一部分。内置表演不可删，用户改过的保留、缺失的补齐。
BUILTIN_PLAYBOOKS_VERSION = 2
_RETIRED_BUILTIN_NAMES = ("demo_greet",)  # 旧「演示问候」：升级时移除
_BUILTIN_VERSION_SIDECAR = "scene_playbooks.builtin_version"

_BASE_BUILTIN_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "name": "disco",
        "title": "小歪蹦迪",
        "chunks": [
            {
                "id": "c1",
                "text": "来点音乐！",
                "expr": {
                    "scene": "happy",
                    "ms": 1200
                },
                "servo": {
                    "preset": "dj",
                    "ms": 2000
                }
            },
            {
                "id": "c2",
                "text": "",
                "expr": {
                    "scene": "happy",
                    "ms": 1500
                },
                "servo": {
                    "preset": "clockwise",
                    "ms": 1500
                }
            },
            {
                "id": "c3",
                "text": "",
                "expr": {
                    "scene": "surprised",
                    "ms": 1500
                },
                "servo": {
                    "preset": "counterclockwise",
                    "ms": 1500
                }
            },
            {
                "id": "c4",
                "text": "呼，累了累了，收工！",
                "expr": {
                    "scene": "sleep",
                    "ms": 2200
                },
                "servo": {
                    "preset": "center",
                    "ms": 600
                }
            }
        ]
    },
    {
        "name": "want_attention",
        "title": "求关注",
        "chunks": [
            {
                "id": "c1",
                "text": "喂，你已经好久没理我了。",
                "expr": {
                    "scene": "sad",
                    "ms": 2200
                },
                "servo": {
                    "preset": "look_left",
                    "ms": 700
                }
            },
            {
                "id": "c2",
                "text": "",
                "expr": {
                    "scene": "shy",
                    "ms": 1000
                },
                "servo": {
                    "preset": "look_right",
                    "ms": 700
                }
            },
            {
                "id": "c3",
                "text": "看我一眼嘛，就一眼。",
                "expr": {
                    "scene": "shy",
                    "ms": 2000
                },
                "servo": {
                    "preset": "nod_head",
                    "ms": 900
                }
            },
            {
                "id": "c4",
                "text": "",
                "expr": {
                    "scene": "happy",
                    "ms": 1200
                },
                "servo": {
                    "preset": "center",
                    "ms": 500
                }
            }
        ]
    },
    {
        "name": "new_year",
        "title": "新年祝福",
        "chunks": [
            {
                "id": "c1",
                "text": "",
                "expr": {
                    "scene": "surprised",
                    "ms": 900
                },
                "servo": {
                    "preset": "look_up",
                    "ms": 600
                }
            },
            {
                "id": "c2",
                "text": "新年到啦！祝你新的一年顺顺利利，想要的都有！",
                "expr": {
                    "scene": "happy",
                    "ms": 3000
                },
                "servo": {
                    "preset": "clockwise",
                    "ms": 1600
                }
            },
            {
                "id": "c3",
                "text": "红包可以不用给我，摸摸头就行啦。",
                "expr": {
                    "scene": "shy",
                    "ms": 2200
                },
                "servo": {
                    "preset": "nod_head",
                    "ms": 900
                }
            }
        ]
    },
    {
        "name": "birthday",
        "title": "生日祝福",
        "chunks": [
            {
                "id": "c1",
                "text": "",
                "expr": {
                    "scene": "surprised",
                    "ms": 800
                },
                "servo": {
                    "preset": "look_upper_left",
                    "ms": 500
                }
            },
            {
                "id": "c2",
                "text": "生日快乐！",
                "expr": {
                    "scene": "happy",
                    "ms": 1800
                },
                "servo": {
                    "preset": "dj",
                    "ms": 1800
                }
            },
            {
                "id": "c3",
                "text": "祝你今天开开心心，蛋糕记得给我留一口！",
                "expr": {
                    "scene": "happy",
                    "ms": 2600
                },
                "servo": {
                    "preset": "nod_head_fast",
                    "ms": 900
                }
            },
            {
                "id": "c4",
                "text": "",
                "expr": {
                    "scene": "shy",
                    "ms": 1200
                },
                "servo": {
                    "preset": "center",
                    "ms": 500
                }
            }
        ]
    },
    {
        "name": "good_morning",
        "title": "早上好",
        "chunks": [
            {
                "id": "c1",
                "text": "",
                "expr": {
                    "scene": "wake",
                    "ms": 1400
                },
                "servo": {
                    "preset": "look_down",
                    "ms": 700
                }
            },
            {
                "id": "c2",
                "text": "早呀，昨晚睡得好吗？",
                "expr": {
                    "scene": "happy",
                    "ms": 2000
                },
                "servo": {
                    "preset": "look_up",
                    "ms": 600
                }
            },
            {
                "id": "c3",
                "text": "新的一天，我们一起加油！",
                "expr": {
                    "scene": "happy",
                    "ms": 2000
                },
                "servo": {
                    "preset": "nod_head_slow",
                    "ms": 1100
                }
            }
        ]
    }
]


def _seed_default_playbooks() -> list[dict[str, Any]]:
    return [dict(row) for row in builtin_playbooks()]


def builtin_playbooks() -> list[dict[str, Any]]:
    """内置表演全集：随包 global 文件（可读时）∪ 基础五段。

    内容以 global 文件为准（同名时用它的版本），顺序固定为：基础五段按 _BASE_BUILTIN_PLAYBOOKS 的先后排最前，
    global 文件里其余录制的动作按文件顺序跟在后面。顺序不能跟着 global 文件走：老安装的机器上那份文件
    可能是没有五段的旧版（2026-09-11 用户机器上是 8 月 31 日的），一跟它排名五段就掉到最后。
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        path = global_config_dir() / os.path.basename(SCENE_PLAYBOOKS_FILE)
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except Exception:  # noqa: BLE001
        raw = []
    for row in list(raw if isinstance(raw, list) else []) + _BASE_BUILTIN_PLAYBOOKS:
        name = str((row or {}).get("name") or "").strip()
        if not name or name in seen or name in _RETIRED_BUILTIN_NAMES:
            continue
        seen.add(name)
        rows.append(row)
    base_rank = {str(r.get("name") or ""): i for i, r in enumerate(_BASE_BUILTIN_PLAYBOOKS)}
    rows.sort(key=lambda r: base_rank.get(str(r.get("name") or ""), len(base_rank)))
    return rows


def builtin_names() -> frozenset[str]:
    return frozenset(str(r.get("name") or "") for r in builtin_playbooks())


def _sidecar_path():
    return local_data_dir() / _BUILTIN_VERSION_SIDECAR


def _read_builtin_version() -> int:
    try:
        return int(_sidecar_path().read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_builtin_version() -> None:
    try:
        path = _sidecar_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(BUILTIN_PLAYBOOKS_VERSION), encoding="utf-8")
    except OSError:
        pass


def sync_builtin_playbooks(rows: list[dict[str, Any]]) -> bool:
    """把内置表演补进用户文件：退役的删掉、缺的补上、已有的（可能被改过）原样保留。
    返回是否有改动。"""
    changed = False
    before = len(rows)
    rows[:] = [r for r in rows if str((r or {}).get("name") or "") not in _RETIRED_BUILTIN_NAMES]
    if len(rows) != before:
        changed = True
    present = {str((r or {}).get("name") or "") for r in rows}
    for row in builtin_playbooks():
        if row["name"] not in present:
            rows.append(json.loads(json.dumps(row, ensure_ascii=False)))
            present.add(row["name"])
            changed = True
    return changed


def _mark_builtin(rows: list[dict[str, Any]]) -> None:
    names = builtin_names()
    for row in rows:
        if isinstance(row, dict):
            row["builtin"] = str(row.get("name") or "") in names


def order_builtins_first(rows: list[dict[str, Any]]) -> None:
    """内置表演排在最前：先基础五段（蹦迪、求关注、新年、生日、早上好），再随包录制的动作；
    用户自己建的按原顺序跟在后面。就地重排（顺序见 builtin_playbooks，不受本机 global 文件顺序影响）。"""
    rank = {row["name"]: i for i, row in enumerate(builtin_playbooks())}
    rows.sort(key=lambda r: (rank.get(str((r or {}).get("name") or ""), len(rank)), 0))


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
        rows = normalize_scene_playbooks(_seed_default_playbooks())
        migrate_legacy_pose_moves(rows)
        _STORE.save(rows)
        _write_builtin_version()
        order_builtins_first(rows)
        _mark_builtin(rows)
        return rows
    rows = _STORE.load()
    # 读取时就地迁移遗留 pose_* 预设名：调用方（页面/运行）拿到的直接是
    # 可保存、可运行的坐标动作形态。
    if isinstance(rows, list):
        migrate_legacy_pose_moves(rows)
        # 内置表演升级（只做一次）：退役的删、缺的补，用户改过的不动
        if _read_builtin_version() < BUILTIN_PLAYBOOKS_VERSION:
            if sync_builtin_playbooks(rows):
                try:
                    norm = normalize_scene_playbooks(rows)
                    migrate_legacy_pose_moves(norm)
                    _STORE.save(norm)
                    rows[:] = norm
                except ValueError:
                    pass
            _write_builtin_version()
        order_builtins_first(rows)
        _mark_builtin(rows)
    return rows


_LEGACY_POSE_RE = re.compile(r"^pose_x(\d{1,3})_y(\d{1,3})$", re.I)


def migrate_legacy_pose_moves(rows: list[dict[str, Any]]) -> int:
    """把早期姿态网格工具留下的 ``pose_x{X}_y{Y}`` 预设名就地转为坐标动作。

    这些名字是机器生成的字面坐标，早已不在 servo.json 里；作为未知预设会
    卡死整表保存。仅当该名字确实不在预设目录中时才转换，返回转换条数。
    """
    from deskbot_server.servo_config_store import servo_preset_catalog

    available = {
        str(preset.get("id") or "").strip().casefold()
        for preset in servo_preset_catalog()
        if isinstance(preset, dict) and str(preset.get("id") or "").strip()
    }
    converted = 0
    for playbook in rows:
        if not isinstance(playbook, dict):
            continue
        for chunk in playbook.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            for move in chunk.get("moves") or []:
                if not isinstance(move, dict):
                    continue
                name = str(move.get("move") or "").strip()
                match = _LEGACY_POSE_RE.match(name)
                if not match or name.casefold() in available:
                    continue
                from deskbot_server.servo_protocol import (
                    SERVO_MIN_SEGMENT_DURATION_MS,
                )

                move.update(
                    move="__custom__",
                    x=int(match.group(1)),
                    y=int(match.group(2)),
                    xm=0,
                    ym=0,
                    ms=max(
                        SERVO_MIN_SEGMENT_DURATION_MS,
                        _normalize_ms(move.get("ms")),
                    ),
                )
                converted += 1
    return converted


def save_scene_playbooks_file(rows: list[dict[str, Any]]) -> None:
    norm = normalize_scene_playbooks(rows)
    migrate_legacy_pose_moves(norm)
    missing = collect_missing_servo_presets(norm)
    if missing:
        raise ValueError("unknown servo preset(s): " + ", ".join(missing))
    from deskbot_server.pb.llm_plan import expand_llm_moves

    for playbook in norm:
        for chunk in playbook.get("chunks") or []:
            expand_llm_moves(list(chunk.get("moves") or []))
    _STORE.save(norm)


PROMPT_MAX_SCENES = 20


def scene_catalog(limit: int = PROMPT_MAX_SCENES) -> list[dict[str, str]]:
    """可用表演目录：[{name, title}]，按文件顺序取前 limit 段（提示词与控制台共用）。"""
    try:
        rows = load_scene_playbooks_file() or []
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "title": str(row.get("title") or name).strip()})
        if len(out) >= max(1, int(limit)):
            break
    return out


def scene_catalog_prompt() -> str:
    """提示词附录：可用表演 + 调用时机。没有表演时空串（不注入）。"""
    items = scene_catalog()
    if not items:
        return ""
    listing = "、".join(f"{s['title']}({s['name']})" for s in items)
    return (
        f"可用的表演（name 见括号）：{listing}。"
        '主人要求表演、庆祝、打招呼等固定桥段时调用 perform_scene：{"tool":"perform_scene","name":"表演name"}；'
        "日常闲聊不要滥用，一轮最多演一段。"
    )


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
