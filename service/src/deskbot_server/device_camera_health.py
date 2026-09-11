"""设备相机健康：从设备日志行与最近一帧时间推断，供控制台准确报状态。

设备只在 hello 里带一次 ``camera_ready``，跑久后相机驱动重建失败（内部 RAM
碎片化）时页面只能看到"等待画面 → 8 秒 → 重连"的循环。这里持续消费固件的
``[CAMERA]`` 日志行，把"初始化反复失败 / 内存不足推迟 / 已恢复"记成可查询的
状态，和最近一帧的时间一起挂到 ``/api/devices`` 的 ``camera_health``。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

_SETUP_FAILED_RE = re.compile(r"\[CAMERA\] supervisor setup failed attempt=(\d+)")
_SETUP_DEFERRED_RE = re.compile(r"\[CAMERA\] setup deferred: internal largest_block=(\d+)")
_RESET_SKIPPED_RE = re.compile(r"\[CAMERA\] driver reset skipped: internal largest_block=(\d+)")
_RECOVERED_RE = re.compile(r"\[CAMERA\] (?:supervisor recovery complete|ready)")
_VALIDATION_FAILED = "[CAMERA] setup validation failed"

# 连续失败到这个次数就判定"初始化失败"，而不是偶发抖动。
_FAILING_AFTER = 2

_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}


def _entry(device_id: str) -> dict[str, Any]:
    return _state.setdefault(
        device_id,
        {
            "setup_failures": 0,
            "last_error": "",
            "last_error_at": 0.0,
            "largest_block": None,
            "recovered_at": 0.0,
        },
    )


def observe_device_log(device_id: str, text: str) -> None:
    """喂一行固件日志；只认相机相关行，其它直接返回。"""
    dev = str(device_id or "").strip()
    line = str(text or "")
    if not dev or "[CAMERA]" not in line:
        return
    now = time.time()
    with _lock:
        entry = _entry(dev)
        if _RECOVERED_RE.search(line):
            entry["setup_failures"] = 0
            entry["last_error"] = ""
            entry["recovered_at"] = now
            return
        m = _SETUP_FAILED_RE.search(line)
        if m:
            entry["setup_failures"] += 1
            entry["last_error"] = "setup_failed"
            entry["last_error_at"] = now
            return
        m = _SETUP_DEFERRED_RE.search(line) or _RESET_SKIPPED_RE.search(line)
        if m:
            entry["largest_block"] = int(m.group(1))
            entry["last_error"] = "low_memory"
            entry["last_error_at"] = now
            entry["setup_failures"] = max(entry["setup_failures"], _FAILING_AFTER)
            return
        if _VALIDATION_FAILED in line:
            entry["last_error"] = "validation_failed"
            entry["last_error_at"] = now


def observe_frame(device_id: str) -> None:
    """一帧真实画面到达即视为相机健康：清掉历史失败计数。

    相机初始化失败是"拿不到帧"，所以有帧就是最硬的恢复证据——不再依赖
    固件那行 INFO 级的 ``[CAMERA] ready``（设备默认只上送 WARN，实际到不了）。
    """
    dev = str(device_id or "").strip()
    if not dev:
        return
    with _lock:
        entry = _entry(dev)
        entry["setup_failures"] = 0
        entry["last_error"] = ""
        entry["recovered_at"] = time.time()


def reset_device(device_id: str) -> None:
    """设备重新连接（新会话）时清零：新固件/重启后状态从头算。"""
    with _lock:
        _state.pop(str(device_id or "").strip(), None)


def snapshot(device_id: str, *, last_frame_at: float | None = None) -> dict[str, Any]:
    """返回页面可直接消费的状态：ok / failing / unknown + 人话说明。"""
    dev = str(device_id or "").strip()
    now = time.time()
    with _lock:
        entry = dict(_state.get(dev) or {})
    failures = int(entry.get("setup_failures") or 0)
    last_error = str(entry.get("last_error") or "")
    frame_age = (now - float(last_frame_at)) if last_frame_at else None
    # 30 秒内有新帧就是 ok，优先于任何历史失败：否则设备重启后相机已正常、
    # 画面持续到达，页面却因为几分钟前的两次失败一直显示"需重启机器人"。
    if frame_age is not None and frame_age < 30:
        status, message = ("ok", "")
    elif failures >= _FAILING_AFTER and last_error:
        if last_error == "low_memory":
            status, message = (
                "failing",
                "设备相机初始化失败：内部内存不足（最大连续块 "
                f"{int(entry.get('largest_block') or 0) // 1024}KB），重连无效，需重启机器人",
            )
        else:
            status, message = ("failing", "设备相机初始化反复失败，重连无效，需重启机器人")
    elif last_error and (now - float(entry.get("last_error_at") or 0)) < 120:
        status, message = ("degraded", "设备相机刚报过错误，正在恢复")
    else:
        status, message = ("unknown", "")
    return {
        "status": status,
        "message": message,
        "setup_failures": failures,
        "last_error": last_error or None,
        "last_frame_age_s": round(frame_age, 1) if frame_age is not None else None,
        "largest_block": entry.get("largest_block"),
    }


__all__ = ["observe_device_log", "observe_frame", "reset_device", "snapshot"]
