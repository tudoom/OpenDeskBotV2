"""片内温度告警（2026-09-09「设备有点烫」）。

板上没有电流/电压采样，ESP32-S3 的片内温度是唯一能看见"板子在发热"的量。固件
≥0.0.52 在 hello 与心跳里带 ``chip_temp_c``；这里按阈值分级并做三件事：

* 记录每台设备的当前温度 / 最高温度 / 告警等级，供 ``/api/devices`` 展示；
* 越过阈值时打 WARNING 日志 + 遥测事件，并附一份"排查快照"（链路、舵机热量、
  相机节拍），告诉用户先查什么；
* 严重档（默认 85℃）自动减负：暂停待机动作、停掉相机预览租约。

阈值可用 DESKBOT_TEMP_WARN_C / DESKBOT_TEMP_CRIT_C 覆盖；回落 5℃ 才解除（滞回）。
片内温度通常比外壳高 10–20℃，满负荷时 60–70℃ 属正常，超过 80℃ 才需要动手。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from deskbot_server.telemetry import emit

logger = logging.getLogger("deskbot-server")

DEFAULT_WARN_C = 65   # 设备 70℃ 自己断电，PC 提前 5℃ 提示
DEFAULT_CRIT_C = 85
HYSTERESIS_C = 5
CRIT_HOLD_SEC = 600.0
LEVELS = ("ok", "warn", "crit", "cutoff")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def thresholds() -> tuple[int, int]:
    warn = _env_int("DESKBOT_TEMP_WARN_C", DEFAULT_WARN_C)
    crit = max(warn + 1, _env_int("DESKBOT_TEMP_CRIT_C", DEFAULT_CRIT_C))
    return warn, crit


@dataclass
class _DeviceThermal:
    temp_c: int = 0
    at: float = 0.0
    max_c: int = 0
    max_at: float = 0.0
    level: str = "ok"
    since: float = 0.0
    transport: str = ""
    history: list[tuple[float, int]] = field(default_factory=list)  # (ts, temp) 最近 2 小时，每分钟一点
    alerts: int = 0
    shutdown: dict[str, Any] | None = None


class ThermalGuard:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._devices: dict[str, _DeviceThermal] = {}
        # 动作钩子由 main.py 注入，便于测试替换
        self.on_crit: list[Callable[[str], None]] = []
        self.snapshot_provider: Callable[[str], dict[str, Any]] | None = None

    # ── 观测 ──
    def observe(self, device_id: str, temp_c: int | float | None, *, transport: str = "") -> dict[str, Any]:
        dev = str(device_id or "").strip()
        try:
            temp = int(round(float(temp_c)))
        except (TypeError, ValueError):
            return self.snapshot(dev)
        if not dev or temp <= 0:
            return self.snapshot(dev)
        now = self._clock()
        warn, crit = thresholds()
        with self._lock:
            st = self._devices.setdefault(dev, _DeviceThermal())
            st.temp_c, st.at = temp, now
            if transport:
                st.transport = transport
            if temp > st.max_c:
                st.max_c, st.max_at = temp, now
            if not st.history or now - st.history[-1][0] >= 60.0:
                st.history.append((now, temp))
                cutoff = now - 7200.0
                st.history = [h for h in st.history if h[0] >= cutoff]
            previous = st.level
            if temp >= crit:
                level = "crit"
            elif previous in ("crit", "cutoff") and temp >= crit - HYSTERESIS_C:
                level = "crit"  # 滞回：严重档要回落 5℃ 才降级
            elif temp >= warn:
                level = "warn"
            elif previous in ("warn", "crit", "cutoff") and temp >= warn - HYSTERESIS_C:
                level = "warn"
            else:
                level = "ok"
            changed = level != previous
            if changed:
                st.level, st.since = level, now
                if level != "ok":
                    st.alerts += 1
        if changed:
            self._announce(dev, level, previous, temp)
        return self.snapshot(dev)

    def _diagnosis(self, dev: str) -> dict[str, Any]:
        info: dict[str, Any] = {}
        if self.snapshot_provider is not None:
            try:
                info = dict(self.snapshot_provider(dev) or {})
            except Exception:  # noqa: BLE001
                info = {}
        return info

    def _announce(self, dev: str, level: str, previous: str, temp: int) -> None:
        warn, crit = thresholds()
        diag = self._diagnosis(dev)
        if level == "ok":
            logger.info("[thermal] device_id=%s 温度回落到 %d℃（解除 %s）", dev, temp, previous)
            emit("device.thermal.clear", device_id=dev, temp_c=temp, previous=previous)
            return
        hints = self._hints(diag)
        log = logger.error if level == "crit" else logger.warning
        log("[thermal] device_id=%s 片内温度 %d℃ 达到 %s 档（阈值 warn=%d crit=%d）；排查：%s | 快照 %s",
            dev, temp, level, warn, crit, "；".join(hints) or "见快照", diag)
        emit("device.thermal.alert", device_id=dev, temp_c=temp, level=level, hints=hints, **{k: v for k, v in diag.items() if isinstance(v, (int, float, str, bool))})
        if level == "crit":
            for fn in list(self.on_crit):
                try:
                    fn(dev)
                except Exception:  # noqa: BLE001
                    logger.debug("[thermal] crit hook failed", exc_info=True)

    @staticmethod
    def _hints(diag: dict[str, Any]) -> list[str]:
        hints: list[str] = []
        if diag.get("rtc_session"):
            hints.append("语音会话在线：麦克风持续编码上行，是芯片满负荷的主因，可先结束会话看温度是否回落")
        if int(diag.get("camera_fps") or 0) > 0:
            hints.append(f"相机预览在流（{diag.get('camera_fps')} fps）：离开首页即停")
        heat = max(int(diag.get("servo_heat_x") or 0), int(diag.get("servo_heat_y") or 0))
        if heat >= 300:
            hints.append(f"舵机热预算 {heat}：近期动作过多或顶住机械限位，检查头部是否卡住")
        if str(diag.get("transport") or "") == "wifi_tcp":
            hints.append("WiFi 链路在用：射频持续发送也会发热")
        if not hints:
            hints.append("没有明显的高负载来源：检查供电线和外壳散热，必要时拔掉 USB 冷却")
        return hints

    def note_shutdown(self, device_id: str, message: dict[str, Any]) -> None:
        """设备报告即将深睡断电：记成 cutoff 档（下一次温度上报时自动重新分级）。"""
        dev = str(device_id or "").strip()
        now = self._clock()
        with self._lock:
            st = self._devices.setdefault(dev, _DeviceThermal())
            try:
                st.temp_c = int(message.get("temp_c") or st.temp_c)
            except (TypeError, ValueError):
                pass
            st.at = now
            st.max_c = max(st.max_c, st.temp_c)
            st.level, st.since = "cutoff", now
            st.alerts += 1
            st.shutdown = {
                "temp_c": st.temp_c,
                "cutoff_c": message.get("cutoff_c"),
                "sleep_s": message.get("sleep_s"),
                "trips": message.get("trips"),
                "at": now,
            }
        emit("device.thermal.shutdown", device_id=dev, temp_c=st.temp_c, cutoff_c=message.get("cutoff_c"), sleep_s=message.get("sleep_s"), trips=message.get("trips"))

    # ── 读取 ──
    def snapshot(self, device_id: str) -> dict[str, Any]:
        dev = str(device_id or "").strip()
        warn, crit = thresholds()
        with self._lock:
            st = self._devices.get(dev)
            if st is None:
                return {"level": "unknown", "temp_c": None, "warn_c": warn, "crit_c": crit}
            return {
                "level": st.level,
                "temp_c": st.temp_c,
                "at": st.at,
                "max_c": st.max_c,
                "max_at": st.max_at,
                "since": st.since,
                "alerts": st.alerts,
                "warn_c": warn,
                "crit_c": crit,
                "history": list(st.history[-120:]),
                "shutdown": dict(st.shutdown) if st.shutdown else None,
                "message": self._message(st, warn, crit),
            }

    @staticmethod
    def _message(st: _DeviceThermal, warn: int, crit: int) -> str:
        if st.level == "cutoff":
            sd = st.shutdown or {}
            return (f"芯片 {st.temp_c}℃ 达到断电阈值 {sd.get('cutoff_c')}℃，设备已自动断电休眠 "
                    f"{int(sd.get('sleep_s') or 0) // 60} 分钟（第 {sd.get('trips')} 次）；请检查供电、散热和是否有堵转")
        if st.level == "crit":
            return f"芯片 {st.temp_c}℃，已达严重档（≥{crit}℃）：已暂停待机动作和相机预览，请检查供电与散热"
        if st.level == "warn":
            return f"芯片 {st.temp_c}℃，偏高（≥{warn}℃）：先结束语音会话或关掉首页画面看是否回落"
        return f"芯片 {st.temp_c}℃"

    def all_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            devs = [d for d, st in self._devices.items() if st.level in ("warn", "crit", "cutoff")]
        return [dict(self.snapshot(d), device_id=d) for d in devs]


_guard: ThermalGuard | None = None


def thermal_guard() -> ThermalGuard:
    global _guard
    if _guard is None:
        _guard = ThermalGuard()
    return _guard


def on_session_telemetry(session: Any, updates: dict[str, Any]) -> None:
    """注册到 DeviceSession 的心跳遥测观察者（心跳温度 + 断电通知）。"""
    shutdown = updates.get("thermal_shutdown")
    if isinstance(shutdown, dict):
        thermal_guard().note_shutdown(str(getattr(session, "device_id", "") or ""), shutdown)
        return
    temp = updates.get("chip_temp_c")
    if temp is None:
        return
    thermal_guard().observe(str(getattr(session, "device_id", "") or ""), temp, transport=str(getattr(session, "transport", "") or ""))


__all__ = ["DEFAULT_CRIT_C", "DEFAULT_WARN_C", "HYSTERESIS_C", "CRIT_HOLD_SEC", "ThermalGuard", "on_session_telemetry", "thermal_guard", "thresholds"]
