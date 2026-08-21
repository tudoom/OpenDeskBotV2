"""舵机调试配置持久化（``data/local/servo.json``）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from deskbot_server.constants import SERVO_CFG_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path
from deskbot_server.servo_protocol import (
    SERVO_HARDWARE_ENVELOPE,
    SERVO_MAX_PLAN_DURATION_MS,
    SERVO_MAX_PLAN_STEPS,
    SERVO_MIN_SEGMENT_DURATION_MS,
)

DEFAULT_SERVO_LIMITS: dict[str, int] = {
    "xMin": 10,
    "xMax": 170,
    "yMin": 70,
    "yMax": 110,
    "xReverse": 0,
    "yReverse": 0,
}

DEFAULT_SERVO_PERSPECTIVE = "viewer"
_VALID_PERSPECTIVES = frozenset({"viewer", "robot"})
_PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_MAX_PRESETS = 128
_MAX_PRESET_STEPS = SERVO_MAX_PLAN_STEPS
_MAX_PRESET_TOTAL_MS = SERVO_MAX_PLAN_DURATION_MS


def _json_int(value: object, field: str) -> int:
    """Match the firmware's integer-only JSON contract."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value

# 观众视角下 left/right 类预设 id 与本体存储 id 的对调（预设 steps 仍为本体逻辑坐标）
VIEWER_LR_SWAP: dict[str, str] = {
    "look_left": "look_right",
    "look_right": "look_left",
    "look_upper_left": "look_upper_right",
    "look_upper_right": "look_upper_left",
    "look_lower_left": "look_lower_right",
    "look_lower_right": "look_lower_left",
}


def normalize_perspective(raw: object) -> str:
    p = str(raw or DEFAULT_SERVO_PERSPECTIVE).strip().lower()
    return p if p in _VALID_PERSPECTIVES else DEFAULT_SERVO_PERSPECTIVE


def servo_perspective() -> str:
    """``servo.json`` 中的 left/right 语义：``viewer``（默认）或 ``robot``。"""
    try:
        cfg = load_servo_cfg_file()
    except (OSError, ValueError):
        cfg = None
    if cfg:
        return normalize_perspective(cfg.get("perspective"))
    return DEFAULT_SERVO_PERSPECTIVE


def resolve_move_for_perspective(
    move_id: str,
    *,
    perspective: Optional[str] = None,
) -> str:
    """按视角解析 move/preset id（``viewer`` 时对调 left/right 类预设）。"""
    pid = str(move_id or "").strip()
    if not pid:
        return pid
    pers = normalize_perspective(perspective) if perspective is not None else servo_perspective()
    if pers != "viewer":
        return pid
    want = pid.lower()
    for src, dst in VIEWER_LR_SWAP.items():
        if src.lower() == want:
            return dst
    return pid


def _clamp_axis(v: int, lo: int, hi: int) -> int:
    a = min(int(lo), int(hi))
    b = max(int(lo), int(hi))
    return max(a, min(b, int(v)))


def _limits_with_reverse(
    limits: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    lim = dict(DEFAULT_SERVO_LIMITS)
    if limits:
        lim.update({k: int(limits[k]) for k in DEFAULT_SERVO_LIMITS if k in limits})
    else:
        lim.update(servo_limits())
    return lim


def logical_step_to_protocol(
    step: dict[str, Any],
    limits: dict[str, int],
) -> dict[str, int]:
    """逻辑坐标 step → PB 协议坐标（与调试页 ``_servoStepToProtocol`` 一致）。"""
    xm = _json_int(step.get("xm", 0), "step xm")
    ym = _json_int(step.get("ym", 0), "step ym")
    if xm not in (0, 1, 2) or ym not in (0, 1, 2):
        raise ValueError("step xm/ym must be 0 (absolute), 1 (relative) or 2 (hold)")
    lx = _json_int(step.get("x", 0), "step x")
    ly = _json_int(step.get("y", 0), "step y")
    ms = _json_int(step.get("ms", 0), "step ms")
    x_rev = int(limits.get("xReverse", 0)) == 1
    y_rev = int(limits.get("yReverse", 0)) == 1

    if xm == 2:
        x = 0
    elif xm == 0:
        clx = _clamp_axis(lx, limits["xMin"], limits["xMax"])
        x = (limits["xMin"] + limits["xMax"] - clx) if x_rev else clx
    else:
        dx = lx
        if x_rev:
            dx = -dx
        x = int(dx)

    if ym == 2:
        y = 0
    elif ym == 0:
        cly = _clamp_axis(ly, limits["yMin"], limits["yMax"])
        y = (limits["yMin"] + limits["yMax"] - cly) if y_rev else cly
    else:
        dy = ly
        if y_rev:
            dy = -dy
        y = int(dy)

    return {
        "xm": xm,
        "ym": ym,
        "x": x,
        "y": y,
        "ms": ms,
        "x_min": min(int(limits["xMin"]), int(limits["xMax"])),
        "x_max": max(int(limits["xMin"]), int(limits["xMax"])),
        "y_min": min(int(limits["yMin"]), int(limits["yMax"])),
        "y_max": max(int(limits["yMin"]), int(limits["yMax"])),
    }


def servo_limits() -> dict[str, int]:
    """读取本机 ``servo.json`` 限位；缺失字段回退默认值。"""
    out = dict(DEFAULT_SERVO_LIMITS)
    try:
        cfg = load_servo_cfg_file()
    except (OSError, ValueError):
        cfg = None
    if cfg:
        for key in out:
            if key in cfg:
                out[key] = int(cfg[key])
    return out


def clamp_servo_step(
    step: dict[str, Any],
    *,
    limits: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    """逻辑 step → 协议 step（限位 clamp + xReverse/yReverse）。"""
    lim = _limits_with_reverse(limits)
    return logical_step_to_protocol(step, lim)


def normalize_servo_step(
    raw: object,
    *,
    limits: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ValueError("preset step must be an object")
    for key in ("xm", "ym"):
        try:
            mode = _json_int(raw.get(key, 0), f"step {key}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid step {key}") from exc
        if mode not in (0, 1, 2):
            raise ValueError(
                f"step {key} must be 0 (absolute), 1 (relative) or 2 (hold)"
            )
        out_mode = mode
        if key == "xm":
            xm = out_mode
        else:
            ym = out_mode
    out: dict[str, int] = {}
    for key, mode, lo_key, hi_key in (
        ("x", xm, "xMin", "xMax"),
        ("y", ym, "yMin", "yMax"),
    ):
        try:
            value = _json_int(raw.get(key, 0), f"step {key}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid step {key}") from exc
        if mode == 2:
            value = 0
        elif limits is None:
            value = max(-180, min(180, value))
        elif mode == 0:
            lo = min(int(limits[lo_key]), int(limits[hi_key]))
            hi = max(int(limits[lo_key]), int(limits[hi_key]))
            if value < lo or value > hi:
                raise ValueError(f"absolute step {key}={value} outside [{lo}, {hi}]")
        else:
            span = abs(int(limits[hi_key]) - int(limits[lo_key]))
            if value < -span or value > span:
                raise ValueError(f"relative step {key}={value} outside [-{span}, {span}]")
        out[key] = value
    out["xm"] = xm
    out["ym"] = ym
    try:
        ms = _json_int(raw.get("ms", 400), "step ms")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid step ms") from exc
    if ms < SERVO_MIN_SEGMENT_DURATION_MS or ms > _MAX_PRESET_TOTAL_MS:
        raise ValueError(
            f"step ms must be between {SERVO_MIN_SEGMENT_DURATION_MS} and "
            f"{_MAX_PRESET_TOTAL_MS}"
        )
    out["ms"] = ms
    return out


def normalize_servo_preset(
    raw: object,
    *,
    limits: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("preset must be an object")
    preset_id = str(raw.get("id") or "").strip()
    label = str(raw.get("label") or "").strip()
    if not preset_id:
        raise ValueError("preset missing id")
    if not _PRESET_ID_RE.fullmatch(preset_id):
        raise ValueError(
            "preset id must be 1-80 characters using letters, digits, '.', '_' or '-'"
        )
    if not label:
        raise ValueError(f"preset {preset_id!r} missing label")
    if len(label) > 80:
        raise ValueError(f"preset {preset_id!r} label is too long")
    desc = str(raw.get("desc") or "").strip()
    if len(desc) > 500:
        raise ValueError(f"preset {preset_id!r} description is too long")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"preset {preset_id!r} requires non-empty steps")
    if len(steps_raw) > _MAX_PRESET_STEPS:
        raise ValueError(
            f"preset {preset_id!r} exceeds {_MAX_PRESET_STEPS} steps"
        )
    steps = [normalize_servo_step(s, limits=limits) for s in steps_raw]
    if sum(step["ms"] for step in steps) > _MAX_PRESET_TOTAL_MS:
        raise ValueError(
            f"preset {preset_id!r} exceeds {_MAX_PRESET_TOTAL_MS} ms total"
        )
    if "exposeToModel" in raw and not isinstance(raw["exposeToModel"], bool):
        raise ValueError(f"preset {preset_id!r} exposeToModel must be a boolean")
    expose_to_model = raw.get(
        "exposeToModel", not preset_id.lower().startswith("pose_")
    )
    return {
        "id": preset_id,
        "label": label,
        "desc": desc,
        "exposeToModel": expose_to_model,
        "steps": steps,
    }


def normalize_servo_document(raw: object, *, require_presets: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("body must be a JSON object")
    out: dict[str, Any] = {}
    for key in ("xMin", "xMax", "yMin", "yMax"):
        if raw.get(key) is None:
            raise ValueError(f"missing {key}")
        try:
            val = _json_int(raw[key], key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {key}") from exc
        hw_lo = int(SERVO_HARDWARE_ENVELOPE[key[:-3] + "Min"])
        hw_hi = int(SERVO_HARDWARE_ENVELOPE[key[:-3] + "Max"])
        if val < hw_lo or val > hw_hi:
            raise ValueError(f"{key} must be between {hw_lo} and {hw_hi}")
        out[key] = val
    for key in ("xReverse", "yReverse"):
        if raw.get(key) is None:
            raise ValueError(f"missing {key}")
        reverse = _json_int(raw[key], key)
        if reverse not in (0, 1):
            raise ValueError(f"{key} must be 0 or 1")
        out[key] = reverse
    x_min = min(out["xMin"], out["xMax"])
    x_max = max(out["xMin"], out["xMax"])
    y_min = min(out["yMin"], out["yMax"])
    y_max = max(out["yMin"], out["yMax"])
    out["xMin"], out["xMax"] = x_min, x_max
    out["yMin"], out["yMax"] = y_min, y_max
    out["perspective"] = normalize_perspective(raw.get("perspective"))
    if "presets" in raw or require_presets:
        presets_raw = raw.get("presets", [])
        if presets_raw is None:
            presets_raw = []
        if not isinstance(presets_raw, list):
            raise ValueError("presets must be an array")
        if len(presets_raw) > _MAX_PRESETS:
            raise ValueError(f"presets exceeds {_MAX_PRESETS} entries")
        presets = [normalize_servo_preset(p, limits=out) for p in presets_raw]
        seen: set[str] = set()
        for preset in presets:
            folded = preset["id"].casefold()
            if folded in seen:
                raise ValueError(f"duplicate preset id: {preset['id']!r}")
            seen.add(folded)
        out["presets"] = presets
    return out


def servo_preset_catalog(*, model_visible_only: bool = False) -> list[dict[str, Any]]:
    """Return the normalized local motion catalog used by Web, RTC and LLM."""
    try:
        cfg = load_servo_cfg_file()
    except (OSError, ValueError):
        return []
    presets = list((cfg or {}).get("presets") or [])
    if model_visible_only:
        presets = [p for p in presets if bool(p.get("exposeToModel"))]
    return presets


def servo_model_preset_ids() -> tuple[str, ...]:
    return tuple(
        str(preset.get("id") or "")
        for preset in servo_preset_catalog(model_visible_only=True)
        if str(preset.get("id") or "")
    )


_STORE = JsonDocumentStore(
    lambda: resolve_json_path(SERVO_CFG_FILE),
    normalize=normalize_servo_document,
    # require_presets 语义在 save_servo_cfg_file 入口处理，写盘不再二次
    # normalize。
    normalize_save=lambda doc: doc,
)


def load_servo_cfg_file() -> Optional[dict[str, Any]]:
    path = _STORE.path()
    if not path.is_file():
        # Core/Web normally seed ``data/local`` during startup, but helpers
        # such as LLM plan expansion can be used before either entry point is
        # initialized.  Read the shipped template in that narrow case while
        # keeping every save directed at the PC-local path.
        path = Path(SERVO_CFG_FILE)
    return _STORE.load(path)


def save_servo_cfg_file(cfg: dict[str, Any]) -> None:
    norm = normalize_servo_document(cfg, require_presets="presets" in cfg)
    _STORE.save(norm)
