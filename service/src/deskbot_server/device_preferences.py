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
SUPPORTED_IDLE_MOTION_LEVELS = ("gentle", "normal", "bold")

DEFAULT_PREFERENCES: dict[str, Any] = {
    "schema_version": 1,
    "revision": 0,
    "quiet_hours": {
        "enabled": True,
        "start": "22:00",
        "end": "08:00",
        "timezone": "Asia/Shanghai",
    },
    # 行为：空闲待机张望——开关、档位（轻柔/正常）、每分钟几次（自然分钟内随机）、空闲多久后开始。
    "behavior": {
        "idle_live": True,
        "idle_motion": "normal",
        "wander_per_min": 2,
        "wander_idle_sec": 60,
    },
    # 参数设置里以 PC 为准的设备开关：静音默认关闭；设备连上时由 Core 推下去，
    # 设备自己 NVS 里的残留值不作数（2026-09-14 用户要求：重装电脑端就回到默认）。
    "power": {
        "mic_muted": False,
    },
    # Credentials and provider endpoints stay global; this local profile only
    # selects the voice used by robots attached to this PC.
    "tts": {
        "speaker": "",
        "resource_id": "",
    },
    # 主动陪伴（Quest 剧本）：playbook None = 从未选择（运行时按随包默认剧本），
    # "" = 用户明确关掉剧本；proactive_enabled 是"冷场主动开口"的总开关；
    # idle_sec = 冷场多久才开口；daily_limit = 每天最多主动开口次数（0 = 不限）。
    "quest": {
        "playbook": None,
        "proactive_enabled": True,
        # 2026-09-10 用户定稿的开机默认：冷场 1 分钟就开口、一天最多 16 次
        "idle_sec": 60,
        "daily_limit": 16,
        # 日常关心（小歪自提的长期小事）每天最多占的主动开口次数
        "care_daily_limit": 10,
        # 看情况的日常关心两次之间至少隔多久（主线不用：一轮只提一次，结果由小歪标）；页面上不再展示
        "task_retry_sec": 120,
        # 提醒还有多少秒到点时，主动陪伴先让路
        # 日常关心连续没回应后歇多久
        "care_pause_sec": 86400,
    },
}

_TTS_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:+-]*$")
_QUEST_PLAYBOOK_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _normalise_quest(raw: object) -> dict[str, Any]:
    quest = dict(raw) if isinstance(raw, Mapping) else {}
    playbook = quest.get("playbook")
    if playbook is not None:
        playbook = str(playbook or "").strip()
        if playbook and not _QUEST_PLAYBOOK_RE.fullmatch(playbook):
            raise ValueError("quest.playbook 只允许小写字母/数字/下划线/中划线")
    return {
        "playbook": playbook,
        "proactive_enabled": bool(quest.get("proactive_enabled", True)),
        "idle_sec": _bounded_int(quest.get("idle_sec", 60), field="quest.idle_sec", lo=30, hi=3600),
        "daily_limit": _bounded_int(quest.get("daily_limit", 16), field="quest.daily_limit", lo=0, hi=100),
        "care_daily_limit": _bounded_int(quest.get("care_daily_limit", 10), field="quest.care_daily_limit", lo=0, hi=100),
        "task_retry_sec": _bounded_int(quest.get("task_retry_sec", 120), field="quest.task_retry_sec", lo=0, hi=86400),
        "care_pause_sec": _bounded_int(quest.get("care_pause_sec", 86400), field="quest.care_pause_sec", lo=600, hi=604800),
    }


def _bounded_int(value: object, *, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if number < lo or number > hi:
        raise ValueError(f"{field} 必须在 {lo}~{hi} 之间")
    return number


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

    try:
        revision = int(source.get("revision", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("行为偏好的数值字段格式错误") from exc

    behavior_raw = source.get("behavior")
    behavior = dict(behavior_raw) if isinstance(behavior_raw, Mapping) else {}
    power_raw = source.get("power")
    power = dict(power_raw) if isinstance(power_raw, Mapping) else {}
    idle_motion = str(behavior.get("idle_motion") or "normal").strip().lower()
    if idle_motion not in SUPPORTED_IDLE_MOTION_LEVELS:
        raise ValueError(f"不支持的张望档位: {idle_motion}")

    return {
        "schema_version": 1,
        "revision": max(0, revision),
        "quiet_hours": {
            "enabled": bool(quiet.get("enabled", True)),
            "start": start,
            "end": end,
            "timezone": timezone,
        },
        "behavior": {
            "idle_live": bool(behavior.get("idle_live", True)),
            "idle_motion": idle_motion,
            "wander_per_min": _bounded_int(behavior.get("wander_per_min", 2), field="behavior.wander_per_min", lo=0, hi=10),
            "wander_idle_sec": _bounded_int(behavior.get("wander_idle_sec", 60), field="behavior.wander_idle_sec", lo=10, hi=600),
        },
        "tts": {
            "speaker": _tts_identifier(tts.get("speaker"), field="tts.speaker"),
            "resource_id": _tts_identifier(
                tts.get("resource_id"),
                field="tts.resource_id",
                max_length=80,
            ),
        },
        "quest": _normalise_quest(source.get("quest")),
        "power": {"mic_muted": bool(power.get("mic_muted", False))},
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
        if "behavior" in patch:
            # 行为（人脸跟随 / 空闲张望打盹）：之前没有这一段，偏好页与主动陪伴页的改动都不落盘
            behavior_patch = patch.get("behavior")
            if not isinstance(behavior_patch, Mapping):
                raise ValueError("behavior 必须是 JSON 对象")
            merged["behavior"] = {
                **dict(merged.get("behavior") or {}),
                **dict(behavior_patch),
            }
        if "power" in patch:
            power_patch = patch.get("power")
            if not isinstance(power_patch, Mapping):
                raise ValueError("power 必须是 JSON 对象")
            merged["power"] = {
                **dict(merged.get("power") or {}),
                **dict(power_patch),
            }
        if "quest" in patch:
            quest_patch = patch.get("quest")
            if not isinstance(quest_patch, Mapping):
                raise ValueError("quest 必须是 JSON 对象")
            merged["quest"] = {
                **dict(merged.get("quest") or {}),
                **dict(quest_patch),
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

