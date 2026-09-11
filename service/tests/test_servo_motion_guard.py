"""转头舵机烧毁后的运动保护（2026-09-09）：运动学锁步、预算器、限位迁移、待机降幅。"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ref_ticks(travel: int, v: int, ta: int) -> int:
    """浮点参考实现：梯形 D/V+Ta，三角 2*sqrt(D*Ta/V)。"""
    if travel <= 0:
        return 0
    if travel >= v * ta:
        return math.ceil(travel / v) + ta
    return math.ceil(2 * math.sqrt(travel * ta / v))


def test_travel_min_ticks_matches_trapezoid_reference():
    from deskbot_server.servo_protocol import servo_travel_min_ticks

    assert servo_travel_min_ticks(0) == 0
    for v in (1, 2):
        for d in range(1, 181):
            assert servo_travel_min_ticks(d, v, 5) == _ref_ticks(d, v, 5), (d, v)
    # 全幅 120°：最坏 1°/拍 125 拍 = 2.5 s；常规 2°/拍 65 拍 = 1.3 s。
    assert servo_travel_min_ticks(120, 1, 5) == 125
    assert servo_travel_min_ticks(120, 2, 5) == 65
    # 小行程走三角曲线，比匀速更慢一点（起步收尾都柔）。
    assert servo_travel_min_ticks(3, 2, 5) == 6


def test_budget_drops_decorative_moves_and_stretches_user_moves():
    from deskbot_server.application.servo_budget import BIG_MOVES_PER_WINDOW, ServoBudget

    now = [1000.0]
    b = ServoBudget(clock=lambda: now[0])
    # 第一步从中位 90 到 150 只有 60°，第二步 150→30 是 120° 的大幅动作：每组算 1 次。
    swing = [{"xm": 0, "ym": 0, "x": 150, "y": 90, "ms": 500}, {"xm": 0, "ym": 0, "x": 30, "y": 90, "ms": 500}]
    for _ in range(BIG_MOVES_PER_WINDOW):
        steps, d = b.apply("dev", swing, source="http_device_servo")
        assert d.allow and d.stretch == 1 and steps == swing, d
    # 超额：用户来源不丢但拉长，装饰来源直接丢
    steps, d = b.apply("dev", swing, source="http_device_servo")
    assert d.allow and d.stretch == 2 and steps[0]["ms"] == 1000 and d.reason == "big_moves"
    steps, d = b.apply("dev", swing, source="expression")
    assert not d.allow and steps == []
    # 语音工具动作先放柔：目标夹到中心 ±30°、每度 ≥33 ms，于是根本不构成大幅动作
    steps, d = b.apply("dev", swing, source="rtc_tool_move")
    assert d.allow and [st["x"] for st in steps] == [120, 60]
    assert steps[1]["ms"] >= 60 * 33
    # 待机张望放得更开（±40°、每度 ≥10 ms），80° 折返仍低于 90° 的大幅门槛，同样不占名额
    steps, d = b.apply("dev", swing, source="live_wander")
    assert d.allow and [st["x"] for st in steps] == [130, 50]
    assert steps[1]["ms"] >= 80 * 10
    # 10 秒后窗口清空
    now[0] += 11.0
    steps, d = b.apply("dev", swing, source="expression")
    assert d.allow and d.stretch == 1


def test_soften_steps_limits_offset_and_slows_down():
    from deskbot_server.application.servo_budget import soften_steps

    out = soften_steps([
        {"xm": 0, "ym": 0, "x": 150, "y": 110, "ms": 300},
        {"xm": 1, "ym": 1, "x": -80, "y": 0, "ms": 100},
        {"xm": 2, "ym": 2, "x": 0, "y": 0, "ms": 500},
    ])
    assert (out[0]["x"], out[0]["y"]) == (120, 110) and out[0]["ms"] >= 30 * 33
    assert out[1]["x"] == -30 and out[1]["ms"] >= 30 * 33
    assert out[2]["ms"] == 500  # 保持段不变


def test_heat_estimate_accumulates_and_cools():
    from deskbot_server.application.servo_budget import HEAT_SKIP_THRESHOLD, ServoBudget

    now = [0.0]
    b = ServoBudget(clock=lambda: now[0])
    swing = [{"xm": 0, "ym": 0, "x": 150, "y": 90, "ms": 500}, {"xm": 0, "ym": 0, "x": 30, "y": 90, "ms": 500}]
    for _ in range(3):
        b.note_motion("dev", swing)
    assert b.heat("dev") >= HEAT_SKIP_THRESHOLD and b.too_hot("dev")
    now[0] += 60.0
    assert b.heat("dev") == 0.0 and not b.too_hot("dev")


def test_budget_counts_relative_and_hold_steps_sensibly():
    from deskbot_server.application.servo_budget import ServoBudget

    b = ServoBudget(clock=lambda: 0.0)
    nod = [{"xm": 1, "ym": 1, "x": 0, "y": 15, "ms": 350}, {"xm": 2, "ym": 2, "x": 0, "y": 0, "ms": 500}]
    _steps, d = b.apply("dev", nod, source="expression")
    assert d.allow and d.stretch == 1 and d.big_moves == 0


def test_legacy_x_limits_migrate_to_new_defaults():
    from deskbot_server.servo_config_store import DEFAULT_SERVO_LIMITS, normalize_servo_document

    assert (DEFAULT_SERVO_LIMITS["xMin"], DEFAULT_SERVO_LIMITS["xMax"]) == (30, 150)
    legacy = {"xMin": 10, "xMax": 170, "yMin": 70, "yMax": 110, "xReverse": 0, "yReverse": 0,
              "presets": [{"id": "far", "label": "远", "steps": [{"x": 10, "y": 70, "xm": 0, "ym": 0, "ms": 500}]}]}
    out = normalize_servo_document(legacy, migrate_legacy=True)
    assert (out["xMin"], out["xMax"], out["yMin"]) == (30, 150, 78)
    assert out["presets"][0]["steps"][0]["x"] == 30
    # 用户自己设的 25 不是旧默认：夹到包络 20..160 内保持 25。
    custom = dict(legacy, xMin=25)
    assert normalize_servo_document(custom, migrate_legacy=True)["xMin"] == 25


def test_shipped_seeds_use_the_new_pan_range():
    for rel in ("service/data/local/servo.json", "service/data/global/servo.json"):
        doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        assert (doc["xMin"], doc["xMax"]) == (30, 150), rel
        for preset in doc["presets"]:
            for step in preset["steps"]:
                if step.get("xm", 0) == 0:
                    assert 30 <= step["x"] <= 150, (rel, preset["id"], step)


def test_idle_behaviour_wanders_less_and_smaller():
    from deskbot_server.application import live_behavior as lb

    assert lb.DEFAULT_WANDER_IDLE_SEC >= 20
    assert 1 <= lb.DEFAULT_WANDER_PER_MIN <= lb.WANDER_PER_MIN_MAX <= 10  # 每分钟几次，有上限
    import random

    moves = lb.wander_moves(rng=random.Random(7))
    xs = [m["x"] for m in moves if m.get("xm") == 0]
    assert xs and max(xs) <= 135 and min(xs) >= 45
    # 张望的幅度/速度不能被预算器再夹回去：档位上限 = 预算器给张望的上限
    from deskbot_server.application.servo_budget import (
        WANDER_MAX_OFFSET_DEG,
        WANDER_MS_PER_DEG,
        ServoBudget,
    )
    assert max(off for off, _ in lb.WANDER_LEVELS.values()) <= WANDER_MAX_OFFSET_DEG
    assert min(ms for _, ms in lb.WANDER_LEVELS.values()) >= WANDER_MS_PER_DEG
    # 2026-09-10 用户反馈太快：闲时张望要慢——正常档每度 ≥ 30 ms（≤ 33°/s），最快的档也 ≥ 20 ms
    assert lb.WANDER_LEVELS["normal"][1] >= 30 and lb.WANDER_LEVELS["gentle"][1] >= lb.WANDER_LEVELS["normal"][1] >= lb.WANDER_LEVELS["bold"][1] >= 20
    clock = {"t": 1000.0}
    budget = ServoBudget(clock=lambda: clock["t"])
    raw = lb.wander_moves("bold", rng=random.Random(11))
    bold, decision = budget.apply("dev-guard", raw, source="live_wander")
    assert decision.allow and bold == raw  # 预算器不再改张望的幅度和速度
    assert all(50 <= st["x"] <= 130 for st in bold if st.get("xm") == 0)
    # 用户把频率调到 10 次/分钟也不能被预算器悄悄丢掉：保持段不计段数，张望段数上限放宽
    for i in range(1, 10):
        clock["t"] = 1000.0 + 6.0 * i
        _steps, d = budget.apply("dev-guard", lb.wander_moves("bold", rng=random.Random(100 + i)), source="live_wander")
        assert d.allow and d.stretch == 1, (i, d)
    assert all(m.get("move") != "look_left" and m.get("move") != "look_right" for m in moves)


def test_firmware_implements_the_motion_guard():
    head_h = (ROOT / "hardware/firmware/head.h").read_text(encoding="utf-8")
    head_cpp = (ROOT / "hardware/firmware/head.cpp").read_text(encoding="utf-8")
    assert re.search(r"#define\s+X_MIN_LIMIT\s+20\b", head_h) and re.search(r"#define\s+X_MAX_LIMIT\s+160\b", head_h)
    assert "SERVO_REVERSAL_PAUSE_MS = 60" in head_h
    for marker in ("void profile_init(", "float profile_travel(", "int resolve_target_safe(", "void heat_add(",
                   "kServoHeatCooldownEnter = 600.0f", "kServoCoolPerSec = 25.0f", "DESKBOT_SERVO_IDLE_RELAX_MS 150u",
                   "SERVO_USB_BOTH_AXES_STEP_DEG_PER_TICK;", "head_servo_travel_min_ticks(uint32_t travel_deg"):
        assert marker in head_cpp, marker
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert "servo_heat_x" in usb and "servo_cooldowns" in usb
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 50)
