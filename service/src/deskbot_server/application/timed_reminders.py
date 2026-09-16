"""定时提醒（2026-09-14 起替代独立的「定时任务」）：Agent 的 ``schedule_task`` 工具落到主动陪伴的 care 场景。

一条定时提醒就是「定时提醒」场景里的一条 care 小目标：``schedule_time``（HH:MM）+ ``schedule_days``
（每周几，空 = 每天）或 ``schedule_date``（一次性，只在那天提一次，提过写 ``done_at``）。到点由主动陪伴
循环让小歪主动开口（``quest_proactive`` 的定时分支），可以附带一段表演（``perform.scene``）。

工具协议（文字链路 JSON / RTC 原生 schema 同一套）：
  create: {task, time?: "HH:MM", date?: "YYYY-MM-DD", delay_minutes?, repeat?: daily|once|weekly, days?: [0-6], scene?}
  list:   → reminders[]
  update: {id, task?, time?, date?, days?, scene?}
  delete: {id | task}（需主人确认——由 llm_tool_runner 的确认账本把关）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from deskbot_server.application import quest_console, quest_service
from deskbot_server.application.quest_service import QuestError
from deskbot_server.device_preferences import preferred_timezone_name
from deskbot_server.quest_playbooks_store import (
    KIND_CARE,
    SCHEDULE_DATE_RE,
    SCHEDULE_TIME_RE,
    ensure_default_playbook,
)

logger = logging.getLogger("deskbot-server")

TOOL_NAME = "schedule_task"
REPEAT_DAILY = "daily"
REPEAT_ONCE = "once"
REPEAT_WEEKLY = "weekly"
_REPEATS = (REPEAT_DAILY, REPEAT_ONCE, REPEAT_WEEKLY)
_WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")


def _local_now() -> datetime:
    try:
        tz = ZoneInfo(preferred_timezone_name())
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


def _care_scene() -> str:
    ensure_default_playbook()
    scene = quest_service.care_playbook()
    if not scene:
        raise QuestError("「定时提醒」场景不可用")
    return scene


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _days(raw: Any) -> list[int]:
    if isinstance(raw, str):
        raw = [p for p in raw.replace("，", ",").split(",") if p.strip()]
    if not isinstance(raw, list):
        return []
    out: set[int] = set()
    for d in raw:
        n = _int(d, -1)
        if 0 <= n <= 6:
            out.add(n)
    return sorted(out)


def _view(task: dict[str, Any]) -> dict[str, Any]:
    perform = task.get("perform") if isinstance(task.get("perform"), dict) else {}
    date = str(task.get("schedule_date") or "")
    days = list(task.get("schedule_days") or [])
    if date:
        repeat = REPEAT_ONCE
    elif days:
        repeat = REPEAT_WEEKLY
    else:
        repeat = REPEAT_DAILY
    out: dict[str, Any] = {
        "id": str(task.get("id") or ""),
        "title": str(task.get("title") or ""),
        "time": str(task.get("schedule_time") or ""),
        "repeat": repeat,
        "status": str(task.get("status") or ""),
    }
    if date:
        out["date"] = date
    if days:
        out["days"] = days
    if perform.get("mode") == "scene" and perform.get("scene"):
        out["scene"] = str(perform["scene"])
    if task.get("done_at"):
        out["done"] = True
    if not task.get("schedule_time"):
        out["repeat"] = "when_needed"  # 看情况提的（不是钟点）
    return out


def _describe(view: dict[str, Any]) -> str:
    t = view.get("time") or ""
    if view.get("repeat") == REPEAT_ONCE:
        return f"{view.get('date')} {t} 提醒一次"
    if view.get("repeat") == REPEAT_WEEKLY:
        return "每周" + "、".join(_WEEKDAY_LABELS[d] for d in view.get("days") or []) + f" {t}"
    if view.get("repeat") == REPEAT_DAILY:
        return f"每天 {t}"
    return "看情况提"


def _resolve_when(raw: dict[str, Any]) -> tuple[str, str, list[int], str]:
    """→ (schedule_time, schedule_date, schedule_days, repeat)。相对延迟折成本地日期 + 钟点。"""
    delay = raw.get("delay_minutes")
    if delay in (None, "") and raw.get("delay_seconds") not in (None, ""):
        delay = max(1, _int(raw.get("delay_seconds")) // 60)
    time_raw = str(raw.get("time") or raw.get("schedule_time") or "").strip()
    date_raw = str(raw.get("date") or raw.get("schedule_date") or "").strip()
    repeat = str(raw.get("repeat") or raw.get("task_kind") or "").strip().lower()
    if repeat == "recurring":
        repeat = REPEAT_DAILY
    days = _days(raw.get("days") if raw.get("days") is not None else raw.get("schedule_days"))
    if delay not in (None, ""):
        minutes = max(1, _int(delay, 1))
        at = _local_now() + timedelta(minutes=minutes)
        return at.strftime("%H:%M"), at.strftime("%Y-%m-%d"), [], REPEAT_ONCE
    if time_raw and not SCHEDULE_TIME_RE.match(time_raw):
        raise QuestError("time 要写成 HH:MM，比如 21:00")
    if date_raw and not SCHEDULE_DATE_RE.match(date_raw):
        raise QuestError("date 要写成 YYYY-MM-DD，比如 2026-09-20")
    if not time_raw:
        raise QuestError("定时提醒需要 time（HH:MM）或 delay_minutes（几分钟后）")
    if date_raw or repeat == REPEAT_ONCE:
        if not date_raw:
            now = _local_now()
            hh, mm = (int(x) for x in time_raw.split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at <= now:
                at += timedelta(days=1)  # 今天已经过了这个点：明天
            date_raw = at.strftime("%Y-%m-%d")
        return time_raw, date_raw, [], REPEAT_ONCE
    if days or repeat == REPEAT_WEEKLY:
        if not days:
            raise QuestError("每周提醒要给 days（周一=0 … 周日=6）")
        return time_raw, "", days, REPEAT_WEEKLY
    if repeat and repeat not in _REPEATS:
        raise QuestError("repeat 只能是 daily / once / weekly")
    return time_raw, "", [], REPEAT_DAILY


def _find(scene: str, raw: dict[str, Any]) -> dict[str, Any]:
    pb = quest_service.require_playbook(scene)
    tasks = [t for t in (pb.get("tasks") or []) if quest_service.is_care(t) and not t.get("proposed")]
    wanted_id = str(raw.get("id") or raw.get("task_id") or "").strip()
    if wanted_id:
        for t in tasks:
            if str(t.get("id")) == wanted_id:
                return t
        raise QuestError(f"没有 id 为 {wanted_id} 的定时提醒")
    wanted = str(raw.get("task") or raw.get("title") or "").strip()
    if not wanted:
        raise QuestError("需要 id 或 task（提醒内容）来定位这条提醒")
    for t in tasks:
        if str(t.get("title") or "") == wanted:
            return t
    hits = [t for t in tasks if wanted in str(t.get("title") or "") or wanted in str(t.get("goal") or "")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise QuestError(f"没有叫「{wanted}」的定时提醒；先 list 看看")
    raise QuestError("有多条提醒都匹配「" + wanted + "」，请用 id 指定：" + "、".join(f"{t['id']}={t.get('title')}" for t in hits))


def create_reminder(raw: dict[str, Any]) -> dict[str, Any]:
    scene = _care_scene()
    text = str(raw.get("task") or raw.get("description") or raw.get("title") or "").strip()
    if not text:
        raise QuestError("schedule_task 需要 task：提醒什么")
    time_s, date_s, days, repeat = _resolve_when(raw)
    scene_name = str(raw.get("scene") or "").strip()
    title = text[:40]
    goal = f"到点提醒主人：{text}"[:400]
    fields: dict[str, Any] = {
        "kind": KIND_CARE,
        "title": title,
        "goal": goal,
        "strategy": "到点直接、简短地提醒，用主人的称呼；不用铺垫，说完等主人回应。",
        "success_condition": "把提醒说到主人就算完成；主人说知道了 / 好就算达成。",
        "trigger_hint": "",
        "schedule_time": time_s,
        "schedule_date": date_s,
        "schedule_days": days,
        "repeat_interval_sec": 86400,
        "max_attempts": 0,
    }
    if scene_name:
        fields["perform"] = {"mode": "scene", "scene": scene_name}
    view = quest_console.add_task(scene, fields)
    out = _view({**view, "status": view.get("status")})
    out["describe"] = _describe(out)
    logger.info("[timed_reminders] created id=%s %s", out["id"], out["describe"])
    return out


def list_reminders() -> list[dict[str, Any]]:
    scene = _care_scene()
    views = quest_console._task_views(scene)  # noqa: SLF001 —— 同一份页面视图
    return [_view(v) for v in views if v.get("kind") == KIND_CARE and not v.get("proposed")]


def update_reminder(raw: dict[str, Any]) -> dict[str, Any]:
    scene = _care_scene()
    task = _find(scene, raw)
    patch: dict[str, Any] = {}
    text = str(raw.get("task") or raw.get("description") or "").strip()
    if text:
        patch["title"] = text[:40]
        patch["goal"] = f"到点提醒主人：{text}"[:400]
    if any(raw.get(k) not in (None, "") for k in ("time", "schedule_time", "date", "schedule_date", "delay_minutes", "days", "schedule_days", "repeat")):
        merged = {
            "time": raw.get("time") or raw.get("schedule_time") or task.get("schedule_time"),
            "date": raw.get("date") if raw.get("date") is not None else (raw.get("schedule_date") if raw.get("schedule_date") is not None else task.get("schedule_date")),
            "days": raw.get("days") if raw.get("days") is not None else (raw.get("schedule_days") if raw.get("schedule_days") is not None else task.get("schedule_days")),
            "repeat": raw.get("repeat") or "",
            "delay_minutes": raw.get("delay_minutes"),
        }
        time_s, date_s, days, _repeat = _resolve_when(merged)
        patch.update({"schedule_time": time_s, "schedule_date": date_s, "schedule_days": days})
    if "scene" in raw:
        scene_name = str(raw.get("scene") or "").strip()
        patch["perform"] = {"mode": "scene", "scene": scene_name} if scene_name else {"mode": "speak"}
    if not patch:
        raise QuestError("没有要改的内容（task / time / date / days / scene）")
    view = quest_console.update_task(scene, str(task["id"]), patch)
    out = _view(view)
    out["describe"] = _describe(out)
    return out


def delete_reminder(raw: dict[str, Any]) -> dict[str, Any]:
    scene = _care_scene()
    task = _find(scene, raw)
    quest_console.delete_task(scene, str(task["id"]))
    return {"id": str(task["id"]), "title": str(task.get("title") or ""), "deleted": True}


def execute_schedule_task_tool(raw: dict[str, Any]) -> dict[str, Any]:
    """``schedule_task`` 工具入口：异常转成可读 error（llm_tool_runner 会再包一层）。"""
    action = str(raw.get("action") or raw.get("op") or "create").strip().lower()
    try:
        if action in ("create", "add", "new"):
            out = create_reminder(raw)
            return {"tool": TOOL_NAME, "ok": True, "action": "create", **out, "note": "已存进「主动陪伴 → 定时提醒」，到点小歪会主动开口"}
        if action in ("list", "get"):
            rows = list_reminders()
            return {"tool": TOOL_NAME, "ok": True, "action": "list", "reminders": rows, "count": len(rows)}
        if action in ("update", "edit", "modify"):
            out = update_reminder(raw)
            return {"tool": TOOL_NAME, "ok": True, "action": "update", **out}
        if action in ("delete", "remove", "del"):
            out = delete_reminder(raw)
            return {"tool": TOOL_NAME, "ok": True, "action": "delete", **out}
    except QuestError as exc:
        return {"tool": TOOL_NAME, "ok": False, "action": action, "error": str(exc)}
    return {"tool": TOOL_NAME, "ok": False, "action": action, "error": f"未知 action: {action}（create / list / update / delete）"}


__all__ = [
    "REPEAT_DAILY",
    "REPEAT_ONCE",
    "REPEAT_WEEKLY",
    "TOOL_NAME",
    "create_reminder",
    "delete_reminder",
    "execute_schedule_task_tool",
    "list_reminders",
    "update_reminder",
]
