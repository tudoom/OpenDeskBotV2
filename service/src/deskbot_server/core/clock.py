"""Single wall-clock/timezone authority for the whole service.

约定（存储口径）：数据库与 JSON 里的 datetime 一律按 UTC 落库（SQLite 的
DateTime 会丢掉 tzinfo，因此写入前必须先 ``as_utc``，回读到的 naive 值一律
按 UTC 解释）；只在序列化/展示边界经 ``to_local`` 转成目标时区。

用户偏好时区的权威在 ``deskbot_server.device_preferences``（quiet_hours
.timezone）；本模块只提供与具体偏好无关的时区原语与东八区兜底。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[misc, assignment]

UTC = timezone.utc

DEFAULT_TZ_NAME = "Asia/Shanghai"

#: 东八区固定偏移兜底（无 zoneinfo 数据时仍可用；上海无夏令时，偏移恒定）。
_FIXED_CST = timezone(timedelta(hours=8), name="Asia/Shanghai")

_CN_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def zone(name: str | None = None) -> tzinfo:
    """Resolve an IANA timezone name with a fixed +08:00 fallback."""

    tz_name = str(name or "").strip() or DEFAULT_TZ_NAME
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return _FIXED_CST


#: 东八区（北京时间）常量；调度 cron 语义与旧数据迁移都以它为准。
CST: tzinfo = zone(DEFAULT_TZ_NAME)


def utcnow() -> datetime:
    """Aware UTC now —— 所有落库时间戳的唯一来源。"""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Interpret *value* as UTC: naive 视为 UTC 墙钟，aware 则换算。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_local(value: datetime, tz: tzinfo | str | None = None) -> datetime:
    """Convert a stored (naive=UTC) or aware datetime to a local timezone."""

    target = zone(tz) if (tz is None or isinstance(tz, str)) else tz
    return as_utc(value).astimezone(target)


def local_now(tz: tzinfo | str | None = None) -> datetime:
    """Aware "now" in the given timezone (default Asia/Shanghai)."""

    target = zone(tz) if (tz is None or isinstance(tz, str)) else tz
    return datetime.now(target)


def format_local(
    value: datetime | None,
    tz: tzinfo | str | None = None,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S",
) -> str | None:
    """Serialize a stored datetime at the display boundary (naive=UTC)."""

    if value is None:
        return None
    return to_local(value, tz).strftime(fmt)


def format_cn_wall_clock(value: datetime) -> str:
    """``2026-08-19 12:00:00 星期三`` —— 给 LLM 注入"当前时间"的统一格式。"""

    return value.strftime("%Y-%m-%d %H:%M:%S") + " " + _CN_WEEKDAYS[value.weekday()]
