"""结构化遥测：一行一个 JSON 事件，进程内保留最近 N 条。

core.log 是给人读的；这里回答"WiFi 会话平均活多久""抓拍首帧多慢""工具执行
花了多少毫秒"这类需要数字的问题。不引入外部监控：事件写到日志目录旁的
``telemetry.jsonl``（轮转），并经 ``GET /api/telemetry/recent`` 给控制台看。

用法::

    from deskbot_server.telemetry import emit
    emit("session.ready", device_id=..., transport="wifi_tcp")

字段只放可序列化的标量/短字符串；绝不放音频、图片、凭证。
"""

from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_RECENT_MAX = 500
_recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=_RECENT_MAX)
_lock = threading.Lock()
_logger: logging.Logger | None = None
_SECRET_HINTS = ("key", "token", "secret", "password", "authorization")


def _telemetry_dir() -> Path:
    """与 core.log 同目录；没有文件日志时退回数据目录下的 logs/。"""
    explicit = os.environ.get("DESKBOT_SERVER_LOG_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve().parent
    for handler in logging.getLogger("deskbot-server").handlers + logging.getLogger().handlers:
        base = getattr(handler, "baseFilename", None)
        if base:
            return Path(base).parent
    try:
        from deskbot_server.device_data import DATA_DIR

        return Path(DATA_DIR) / "logs"
    except Exception:  # noqa: BLE001
        return Path.cwd() / "data" / "logs"


def _get_logger() -> logging.Logger | None:
    global _logger
    if _logger is not None:
        return _logger
    try:
        directory = _telemetry_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / "telemetry.jsonl", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg = logging.getLogger("deskbot-telemetry")
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.handlers[:] = [handler]
        _logger = lg
    except Exception:  # noqa: BLE001 —— 遥测落盘失败不能影响主流程；内存环仍可用
        _logger = None
    return _logger


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SECRET_HINTS):
            out[key] = "***"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value if not isinstance(value, str) else value[:300]
        else:
            out[key] = str(value)[:300]
    return out


def emit(event: str, **fields: Any) -> dict[str, Any]:
    """记录一个事件；返回写入的记录（便于测试与调用方复用）。"""
    record = {"ts": round(time.time(), 3), "event": str(event), **_scrub(fields)}
    with _lock:
        _recent.append(record)
    lg = _get_logger()
    if lg is not None:
        try:
            lg.info(json.dumps(record, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
    return record


def recent(limit: int = 100, *, event_prefix: str | None = None) -> list[dict[str, Any]]:
    """最近的事件（新在前）。"""
    with _lock:
        items: Iterable[dict[str, Any]] = list(_recent)
    if event_prefix:
        items = [r for r in items if str(r.get("event", "")).startswith(event_prefix)]
    rows = list(items)[-max(1, int(limit)) :]
    rows.reverse()
    return rows


def reset_for_tests() -> None:
    with _lock:
        _recent.clear()


class Timer:
    """``with Timer() as t: ...; t.ms`` —— 给工具执行/抓拍这类耗时点用。"""

    def __enter__(self) -> "Timer":
        self._t0 = time.monotonic()
        self.ms = 0
        return self

    def __exit__(self, *_exc: object) -> None:
        self.ms = int((time.monotonic() - self._t0) * 1000)


__all__ = ["Timer", "emit", "recent", "reset_for_tests"]
