"""PC-local interaction preferences shared by every attached robot.

The file is updated under a cross-process lock and replaced atomically because
the web console and realtime service normally run as separate processes.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[misc, assignment]

from deskbot_server.core.clock import format_cn_wall_clock, zone
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import local_data_dir

PREFERENCES_FILENAME = "interaction_preferences.json"
SUPPORTED_OFFLINE_REMINDER_POLICIES = frozenset(
    {"retry_within_grace", "expire", "deliver_when_online"}
)

DEFAULT_PREFERENCES: dict[str, Any] = {
    "schema_version": 1,
    "revision": 0,
    "quiet_hours": {
        "enabled": True,
        "start": "22:00",
        "end": "08:00",
        "timezone": "Asia/Shanghai",
    },
    "offline_reminder_policy": "retry_within_grace",
    "offline_reminder_grace_seconds": 300,
    # Credentials and provider endpoints stay global; this local profile only
    # selects the voice used by robots attached to this PC.
    "tts": {
        "speaker": "",
        "resource_id": "",
    },
}

_TTS_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:+-]*$")


def _tts_identifier(value: object, *, field: str, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        raise ValueError(f"{field} 过长")
    if text and not _TTS_IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"{field} 包含不支持的字符")
    return text


def _normalise_root(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("preferences root must be an object")
    return _normalise(raw)


_STORE = JsonDocumentStore(
    lambda: local_data_dir() / PREFERENCES_FILENAME,
    normalize=_normalise_root,
    # A corrupt preference file must fail safe: no proactive behaviour.
    default=lambda: copy.deepcopy(DEFAULT_PREFERENCES),
    corrupt_to_default=True,
)


def _clock_minutes(value: object, *, field: str) -> int:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"{field} 必须使用 HH:MM 格式")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{field} 必须使用 HH:MM 格式") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"{field} 必须是有效时间")
    return hour * 60 + minute


def _normalise_timezone(value: object) -> str:
    name = str(value or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if ZoneInfo is None:
        if name != "Asia/Shanghai":
            raise ValueError("当前 Python 环境不支持自定义时区")
        return name
    try:
        ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"未知时区: {name}") from exc
    return name


def _normalise(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(raw or {})
    quiet_raw = source.get("quiet_hours")
    quiet = dict(quiet_raw) if isinstance(quiet_raw, Mapping) else {}
    tts_raw = source.get("tts")
    tts = dict(tts_raw) if isinstance(tts_raw, Mapping) else {}
    start = str(quiet.get("start") or "22:00").strip()
    end = str(quiet.get("end") or "08:00").strip()
    _clock_minutes(start, field="quiet_hours.start")
    _clock_minutes(end, field="quiet_hours.end")
    timezone = _normalise_timezone(quiet.get("timezone"))

    policy = str(
        source.get("offline_reminder_policy") or "retry_within_grace"
    ).strip()
    if policy not in SUPPORTED_OFFLINE_REMINDER_POLICIES:
        raise ValueError(f"不支持的离线提醒策略: {policy}")

    try:
        grace = int(source.get("offline_reminder_grace_seconds", 300))
        revision = int(source.get("revision", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("行为偏好的数值字段格式错误") from exc
    if grace < 0 or grace > 86_400:
        raise ValueError("offline_reminder_grace_seconds 必须在 0 到 86400 之间")

    return {
        "schema_version": 1,
        "revision": max(0, revision),
        "quiet_hours": {
            "enabled": bool(quiet.get("enabled", True)),
            "start": start,
            "end": end,
            "timezone": timezone,
        },
        "offline_reminder_policy": policy,
        "offline_reminder_grace_seconds": grace,
        "tts": {
            "speaker": _tts_identifier(tts.get("speaker"), field="tts.speaker"),
            "resource_id": _tts_identifier(
                tts.get("resource_id"),
                field="tts.resource_id",
                max_length=80,
            ),
        },
    }


def load_preferences() -> dict[str, Any]:
    return _STORE.load()


def update_preferences(
    patch: Mapping[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise ValueError("请求体必须是 JSON 对象")
    with _STORE.lock():
        current = load_preferences()
        if (
            expected_revision is not None
            and int(expected_revision) != int(current["revision"])
        ):
            raise RuntimeError("配置已被其他页面更新，请刷新后重试")
        merged = copy.deepcopy(current)
        for key in (
            "offline_reminder_policy",
            "offline_reminder_grace_seconds",
        ):
            if key in patch:
                merged[key] = patch[key]
        if "quiet_hours" in patch:
            quiet_patch = patch.get("quiet_hours")
            if not isinstance(quiet_patch, Mapping):
                raise ValueError("quiet_hours 必须是 JSON 对象")
            merged["quiet_hours"] = {
                **dict(merged.get("quiet_hours") or {}),
                **dict(quiet_patch),
            }
        if "tts" in patch:
            tts_patch = patch.get("tts")
            if not isinstance(tts_patch, Mapping):
                raise ValueError("tts 必须是 JSON 对象")
            merged["tts"] = {
                **dict(merged.get("tts") or {}),
                **dict(tts_patch),
            }
        merged["revision"] = int(current["revision"]) + 1
        return _STORE.save_unlocked(merged)


def preferred_timezone_name() -> str:
    """本机用户偏好时区名（时区权威；缺省 Asia/Shanghai）。"""

    return str(load_preferences()["quiet_hours"]["timezone"])


def preferred_now() -> datetime:
    """Aware "now" in the user's preferred timezone."""

    return datetime.now(zone(preferred_timezone_name()))


def preferred_time_prompt() -> str:
    """给 LLM 注入"当前时间"的统一文案（按用户偏好时区）。"""

    name = preferred_timezone_name()
    label = "北京时间，东八区" if name == "Asia/Shanghai" else name
    return f"{format_cn_wall_clock(datetime.now(zone(name)))}（{label}）"


def _quiet_local_now(quiet: Mapping[str, Any], now: datetime | None) -> datetime:
    """Project ``now`` (or the wall clock) into the configured quiet timezone."""
    tz = zone(quiet["timezone"])
    if now is None:
        return datetime.now(tz=tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _quiet_window_active(quiet: Mapping[str, Any], local_now: datetime) -> bool:
    minute = local_now.hour * 60 + local_now.minute
    start = _clock_minutes(quiet["start"], field="quiet_hours.start")
    end = _clock_minutes(quiet["end"], field="quiet_hours.end")
    if start == end:
        # Identical boundaries mean an all-day window rather than a zero-width
        # one; anything else would make the toggle a no-op.
        return True
    if start < end:
        return start <= minute < end
    # Cross-midnight window, e.g. 22:00-08:00.
    return minute >= start or minute < end


def quiet_hours_active(*, now: datetime | None = None) -> bool:
    prefs = load_preferences()
    quiet = prefs["quiet_hours"]
    if not quiet["enabled"]:
        return False
    return _quiet_window_active(quiet, _quiet_local_now(quiet, now))


def quiet_hours_resume_at(*, now: datetime | None = None) -> datetime | None:
    """Return when the currently active quiet window ends, else ``None``.

    The result is the next wall-clock occurrence of ``quiet_hours.end`` in the
    configured timezone (timezone-aware), which handles cross-midnight windows
    such as 22:00-08:00: a check at 23:00 resolves to 08:00 of the next day.
    An all-day window (``start == end``) resolves to the next daily ``end``
    boundary so callers naturally re-evaluate once per day.
    """
    prefs = load_preferences()
    quiet = prefs["quiet_hours"]
    if not quiet["enabled"]:
        return None
    local_now = _quiet_local_now(quiet, now)
    if not _quiet_window_active(quiet, local_now):
        return None
    end = _clock_minutes(quiet["end"], field="quiet_hours.end")
    candidate = local_now.replace(
        hour=end // 60, minute=end % 60, second=0, microsecond=0
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate
