"""USB 过流防线：Y 轴限位留余量、旧配置迁移、固件到位释放。

2026-09-08：低头到逻辑 70 是顶在外壳上的堵转位，保持 8–20 秒就把 Mac 的
USB 口拉过流断电（黑匣子里全是上电复位）。修法分三层：包络 yMin 78、旧
servo.json 读盘时夹进新包络、固件到位后停脉冲。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_envelope_keeps_the_head_off_the_mechanical_stop():
    from deskbot_server.servo_config_store import DEFAULT_SERVO_LIMITS
    from deskbot_server.servo_protocol import SERVO_HARDWARE_ENVELOPE, SERVO_LEGACY_Y_MIN

    assert SERVO_HARDWARE_ENVELOPE["yMin"] == 78
    assert SERVO_LEGACY_Y_MIN == 70
    assert DEFAULT_SERVO_LIMITS["yMin"] == SERVO_HARDWARE_ENVELOPE["yMin"]


def test_shipped_servo_seeds_are_inside_the_envelope():
    for rel in ("service/data/local/servo.json", "service/data/global/servo.json"):
        doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        assert doc["yMin"] == 78, rel
        for preset in doc["presets"]:
            for step in preset["steps"]:
                if step.get("ym", 0) == 0:
                    assert step["y"] >= 78, (rel, preset["id"], step)


def test_legacy_servo_json_is_clamped_not_rejected():
    from deskbot_server.servo_config_store import normalize_servo_document

    legacy = {
        "xMin": 30, "xMax": 150, "yMin": 70, "yMax": 110, "xReverse": 0, "yReverse": 0,
        "perspective": "viewer",
        "presets": [
            {"id": "look_down", "label": "低头", "steps": [{"x": 90, "y": 70, "xm": 0, "ym": 0, "ms": 500}]},
            {"id": "nod", "label": "点头", "steps": [{"x": 0, "y": -30, "xm": 1, "ym": 1, "ms": 350}]},
        ],
    }
    out = normalize_servo_document(legacy, migrate_legacy=True)
    assert out["yMin"] == 78
    assert out["presets"][0]["steps"][0]["y"] == 78
    # 相对步不动：它本来就按当前位置走，固件再夹一次。
    assert out["presets"][1]["steps"][0]["y"] == -30
    # 用户主动保存越界值仍然是错误（页面会提示），只有读盘迁移才夹。
    import pytest

    with pytest.raises(ValueError, match="yMin must be between 78 and 110"):
        normalize_servo_document(legacy)


def test_store_load_migrates_the_users_file(monkeypatch, tmp_path):
    from deskbot_server import servo_config_store as store

    path = tmp_path / "servo.json"
    path.write_text(json.dumps({
        "xMin": 30, "xMax": 150, "yMin": 70, "yMax": 110, "xReverse": 0, "yReverse": 0,
        "presets": [{"id": "look_down", "label": "低头", "steps": [{"x": 90, "y": 70, "xm": 0, "ym": 0, "ms": 500}]}],
    }), encoding="utf-8")
    monkeypatch.setattr(store._STORE, "path", lambda: path)  # noqa: SLF001
    cfg = store.load_servo_cfg_file()
    assert cfg["yMin"] == 78
    assert cfg["presets"][0]["steps"][0]["y"] == 78
    assert store.servo_limits()["yMin"] == 78


def test_wander_stays_inside_the_safe_envelope():
    """打盹已去掉（2026-09-10）；张望左右/左上/右上/抬头随机，但只抬头不低头，幅度不出硬件包络。"""
    import random

    from deskbot_server.application.live_behavior import wander_moves
    from deskbot_server.servo_protocol import SERVO_HARDWARE_ENVELOPE

    for level in ("gentle", "normal", "bold"):
        for seed in range(40):
            for step in wander_moves(level, rng=random.Random(seed)):
                if step.get("xm") == 0:
                    assert SERVO_HARDWARE_ENVELOPE["xMin"] <= step["x"] <= SERVO_HARDWARE_ENVELOPE["xMax"]
                    # 低头（y 往 78 走）是最容易堵转的方向：张望只抬头，且留出固件 3° 边缘余量
                    assert 84 <= step["y"] <= SERVO_HARDWARE_ENVELOPE["yMax"] - 3


def test_firmware_limits_and_idle_relax_are_in_lockstep():
    head_h = (ROOT / "hardware/firmware/head.h").read_text(encoding="utf-8")
    head_cpp = (ROOT / "hardware/firmware/head.cpp").read_text(encoding="utf-8")
    assert re.search(r"#define\s+Y_MIN_LIMIT\s+78\b", head_h)
    # 到位释放：默认 800ms 停脉冲，可用 servo_relax 控制消息关掉并落 NVS。
    assert "DESKBOT_SERVO_IDLE_RELAX_MS 150u" in head_cpp
    assert "static void head_servo_relax()" in head_cpp
    assert "static void head_servo_energize()" in head_cpp
    assert '\\"servo_relax_ack\\"' in head_cpp
    assert "bool head_handle_control_json" in head_h
    rom = (ROOT / "hardware/firmware/deskbot_rom.ino").read_text(encoding="utf-8")
    assert "head_handle_control_json(frame.payload, frame.payload_length)" in rom
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert '\\"servo_idle_relax_ms\\"' in usb
