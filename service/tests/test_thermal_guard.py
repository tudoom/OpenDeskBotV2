"""片内温度上报与告警（固件 0.0.52 + Core thermal_guard）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_levels_hysteresis_and_actions(monkeypatch):
    from deskbot_server.application import thermal_guard as tg

    monkeypatch.setenv("DESKBOT_TEMP_WARN_C", "70")
    monkeypatch.setenv("DESKBOT_TEMP_CRIT_C", "85")
    now = [1000.0]
    g = tg.ThermalGuard(clock=lambda: now[0])
    crit_calls: list[str] = []
    g.on_crit.append(crit_calls.append)
    g.snapshot_provider = lambda dev: {"rtc_session": True, "camera_fps": 1, "servo_heat_x": 0, "transport": "usb_cdc"}

    assert g.snapshot("dev")["level"] == "unknown"
    assert g.observe("dev", 55, transport="usb_cdc")["level"] == "ok"
    assert g.observe("dev", 72)["level"] == "warn"
    assert g.snapshot("dev")["alerts"] == 1
    # 滞回：回到 68 仍是 warn，回到 64 才解除
    assert g.observe("dev", 68)["level"] == "warn"
    assert g.observe("dev", 64)["level"] == "ok"
    # 严重档触发减负动作，且只触发一次（持续高温不重复）
    assert g.observe("dev", 88)["level"] == "crit"
    assert crit_calls == ["dev"]
    g.observe("dev", 90)
    assert crit_calls == ["dev"]
    snap = g.snapshot("dev")
    assert snap["max_c"] == 90 and snap["temp_c"] == 90 and "严重" in snap["message"]
    assert g.all_alerts() and g.all_alerts()[0]["device_id"] == "dev"
    # 回落到 81 仍 crit（85-5=80 以上），79 降为 warn，64 解除
    assert g.observe("dev", 81)["level"] == "crit"
    assert g.observe("dev", 79)["level"] == "warn"
    assert g.observe("dev", 64)["level"] == "ok"
    # 无效值不改变状态
    assert g.observe("dev", None)["level"] == "ok"
    assert g.observe("dev", 0)["temp_c"] == 64


def test_hints_name_the_likely_heat_source():
    from deskbot_server.application.thermal_guard import ThermalGuard

    hints = ThermalGuard._hints({"rtc_session": True, "camera_fps": 2, "servo_heat_x": 350, "transport": "wifi_tcp"})  # noqa: SLF001
    text = "；".join(hints)
    assert "麦克风" in text and "相机" in text and "舵机" in text and "WiFi" in text
    assert "没有明显" in "；".join(ThermalGuard._hints({}))  # noqa: SLF001


def test_history_keeps_one_point_per_minute():
    from deskbot_server.application.thermal_guard import ThermalGuard

    now = [0.0]
    g = ThermalGuard(clock=lambda: now[0])
    for i in range(10):
        now[0] = i * 10.0
        g.observe("dev", 50 + i)
    assert len(g.snapshot("dev")["history"]) == 2  # 0s 与 60s 两个点


def test_session_telemetry_carries_chip_temperature():
    from deskbot_server.infrastructure.serial import session as sess

    assert "chip_temp_c" in sess._USB_TELEMETRY_FIELDS  # noqa: SLF001
    assert "chip_temp_c" in sess.HelloInfo.__dataclass_fields__
    seen: list[tuple[str, dict]] = []
    sess.add_telemetry_observer(lambda s, u: seen.append((getattr(s, "device_id", ""), u)))

    class _S:
        port = "p"
        device_id = "dev"
        transport = "usb_cdc"
        _chip_temp_c = 0
        _usb_poll_max_gap_ms = 0
    s = _S()
    sess.DeviceSession._update_usb_telemetry(s, {"chip_temp_c": 61, "usb_poll_max_gap_ms": 3})  # noqa: SLF001
    assert s._chip_temp_c == 61 and s._usb_poll_max_gap_ms == 3  # noqa: SLF001
    assert seen and seen[-1] == ("dev", {"chip_temp_c": 61, "usb_poll_max_gap_ms": 3})


def test_firmware_reports_chip_temperature_everywhere():
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert "int usb_transport_chip_temp_c()" in usb and "temperatureRead()" in usb
    assert usb.count('\\"chip_temp_c\\":%d') >= 2  # hello + heartbeat
    trace = (ROOT / "hardware/firmware/task_trace.cpp").read_text(encoding="utf-8")
    assert "temp=%dC" in trace and "usb_transport_chip_temp_c()" in trace
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 52)
