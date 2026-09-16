"""2026-09-14：定时任务撤掉，定时提醒 = 主动陪伴 care 场景里带钟点的小目标；Agent 的 schedule_task 直接写入。
另含「叫小」断句修复。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.quest_helpers import care_scene_with, quest_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")


def _local(h: int, m: int, day: int = 8) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


# ---------------- 断句：「叫小」也算没说完 ----------------


def test_dangling_address_cue_counts_as_incomplete():
    from deskbot_server.application.speech_turn import looks_incomplete

    for text in ("叫小", "叫我小", "叫老", "叫我", "叫", "称呼我", "叫小。"):
        assert looks_incomplete(text), text
    for text in ("叫小明", "叫我小朋友", "小", "你好", "今天天气不错"):
        assert not looks_incomplete(text), text
    src = (ROOT / "service/src/deskbot_server/rtc_livekit_plugins.py").read_text(encoding="utf-8")
    sdk = (ROOT / "service/src/deskbot_server/rtc_agent_sdk.py").read_text(encoding="utf-8")
    assert 'kwargs.setdefault("max_endpointing_delay", 1.5)' in src and 'kwargs.setdefault("max_endpointing_delay", 1.5)' in sdk


# ---------------- 字段与触发规则 ----------------


def test_one_off_reminder_fields_and_pick_rules():
    from deskbot_server.application.quest_proactive import pick_scheduled_due
    from deskbot_server.quest_playbooks_store import normalize_task, validate_playbook

    t = normalize_task({"id": "a", "goal": "g", "kind": "care", "schedule_time": "09:00", "schedule_date": "2026-09-08"})
    assert t["schedule_date"] == "2026-09-08" and t["done_at"] == ""
    assert normalize_task({"id": "a", "goal": "g", "schedule_date": "9/8"})["schedule_date"] == ""
    errs = validate_playbook({"name": "d", "tasks": [{"id": "a", "goal": "g", "schedule_date": "2026-13-40"}]})
    assert any("schedule_date" in e for e in errs)

    once = {"task_id": "rent", "schedule_time": "09:00", "schedule_days": [], "schedule_date": "2026-09-08", "status": "running", "paused_until": None}
    assert pick_scheduled_due([once], _local(9, 0), {})["task_id"] == "rent"
    assert pick_scheduled_due([once], _local(9, 0, day=9), {}) is None  # 只在那一天
    assert pick_scheduled_due([{**once, "done_at": "2026-09-08T01:00:00"}], _local(9, 5), {}) is None  # 提过了
    # 到点时正在勿扰：勿扰结束后两小时内补提，超过就错过
    quiet = {"task_id": "late", "schedule_time": "23:00", "schedule_days": [], "status": "running", "paused_until": None}
    resume = lambda at: at.replace(hour=8, minute=0) + timedelta(days=1) if at.hour >= 22 else None  # noqa: E731
    assert pick_scheduled_due([quiet], _local(8, 30, day=9), {}, quiet_resume_for=resume)["task_id"] == "late"
    assert pick_scheduled_due([quiet], _local(10, 30, day=9), {}, quiet_resume_for=resume) is None
    assert pick_scheduled_due([quiet], _local(8, 30, day=9), {}) is None  # 没给勿扰信息 → 按原来的两小时宽限


def test_scheduled_candidates_skip_done_and_loop_marks_one_off_done(quest_env, monkeypatch):
    import asyncio

    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry

    care_scene_with([
        {"id": "rent", "kind": "care", "title": "交房租", "goal": "提醒交房租", "schedule_time": "09:00", "schedule_date": "2026-09-08", "activation_score": 1},
        {"id": "old", "kind": "care", "title": "旧", "goal": "g", "schedule_time": "09:00", "schedule_date": "2026-09-01", "done_at": "2026-09-01T01:00:00", "activation_score": 1},
    ])
    cands = svc.scheduled_care_candidates()
    assert [c["task_id"] for c in cands] == ["rent"] and cands[0]["schedule_date"] == "2026-09-08"

    calls: list[dict] = []

    class Runner:
        last_skip_reason = ""

        async def attempt(self, dev, **kw):
            calls.append(kw)
            return True

    now_ts = _local(9, 3).timestamp()
    monkeypatch.setattr(qp, "local_datetime", lambda ts=None: datetime.fromtimestamp(ts if ts is not None else now_ts, TZ))
    lp = qp.QuestProactiveLoop(
        Runner(), asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
        activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True, quiet_hours_provider=lambda: False, gate=lambda s, d: (True, ""),
    )

    async def _go():
        assert await lp.tick(now=now_ts) == ""
        for _ in range(20):
            await asyncio.sleep(0.02)
            if not lp._inflight:
                break

    asyncio.run(_go())
    assert calls and calls[0]["task_id"] == "rent" and calls[0]["scheduled_hit"] == "09:00"
    defn = svc.get_task_definition("care", "rent")
    assert defn["done_at"]  # 一次性：提过就标记
    assert svc.scheduled_care_candidates() == []
    # 7 天后自动清掉
    assert svc.prune_done_reminders(now=datetime(2026, 9, 20)) == ["rent", "old"] or set(svc.prune_done_reminders(now=datetime(2026, 9, 20))) <= {"rent", "old"}
    assert all(t.get("id") not in ("old",) for t in svc.require_playbook("care")["tasks"])


# ---------------- Agent 工具：schedule_task → 定时提醒 ----------------


def test_schedule_task_tool_creates_lists_updates_and_deletes_reminders(quest_env, monkeypatch):
    from deskbot_server.application import timed_reminders as tr
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    care_scene_with([])
    monkeypatch.setattr(tr, "_local_now", lambda: datetime(2026, 9, 8, 21, 30, tzinfo=TZ))

    daily = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "讲睡前故事", "time": "21:00"}], device_id="dev-1")[0]
    assert daily["ok"] and daily["repeat"] == "daily" and daily["time"] == "21:00" and "定时提醒" in daily["note"]
    once = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "交房租", "time": "09:00", "date": "2026-09-20", "scene": "birthday"}], device_id="dev-1")[0]
    assert once["ok"] and once["repeat"] == "once" and once["date"] == "2026-09-20" and once["scene"] == "birthday"
    delay = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "提醒喝水", "delay_minutes": 2}], device_id="dev-1")[0]
    assert delay["ok"] and delay["repeat"] == "once" and delay["time"] == "21:32" and delay["date"] == "2026-09-08"
    tomorrow = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "开会", "time": "09:00", "repeat": "once"}], device_id="dev-1")[0]
    assert tomorrow["date"] == "2026-09-09"  # 今天 9 点已过 → 明天
    weekly = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "倒垃圾", "time": "20:00", "days": [0, 3]}], device_id="dev-1")[0]
    assert weekly["ok"] and weekly["repeat"] == "weekly" and weekly["days"] == [0, 3]
    bad = execute_llm_tools([{"tool": "schedule_task", "action": "create", "task": "x"}], device_id="dev-1")[0]
    assert bad["ok"] is False and "time" in bad["error"]

    listed = execute_llm_tools([{"tool": "schedule_task", "action": "list"}], device_id="dev-1")[0]
    assert listed["ok"] and listed["count"] == 5 and {r["title"] for r in listed["reminders"]} == {"讲睡前故事", "交房租", "提醒喝水", "开会", "倒垃圾"}
    # 落在 care 场景里、不需要主人批准（不是 proposed）、能被定时候选看到
    from deskbot_server.application import quest_service as svc

    tasks = svc.require_playbook("care")["tasks"]
    assert all(t["kind"] == "care" and not t["proposed"] and t["schedule_time"] for t in tasks)
    assert {c["task_id"] for c in svc.scheduled_care_candidates()} == {t["id"] for t in tasks}
    rent = next(t for t in tasks if t["title"] == "交房租")
    assert rent["perform"] == {"mode": "scene", "expression": "", "scene": "birthday"} and rent["schedule_date"] == "2026-09-20"

    updated = execute_llm_tools([{"tool": "schedule_task", "action": "update", "id": daily["id"], "time": "21:30"}], device_id="dev-1")[0]
    assert updated["ok"] and updated["time"] == "21:30"
    by_title = execute_llm_tools([{"tool": "schedule_task", "action": "update", "task": "倒垃圾", "days": [5]}], device_id="dev-1")[0]
    assert by_title["ok"] and by_title["days"] == [5]
    # 删除要主人确认（工具确认账本）；确认后才真的删
    pending = execute_llm_tools([{"tool": "schedule_task", "action": "delete", "id": once["id"]}], device_id="dev-1")[0]
    assert pending["ok"] is False and pending["confirmation_required"] is True
    done = execute_llm_tools([{"tool": "schedule_task", "action": "delete", "id": once["id"]}], device_id="dev-1", user_confirmed=True)[0]
    assert done["ok"] and done["deleted"] is True
    assert execute_llm_tools([{"tool": "schedule_task", "action": "list"}], device_id="dev-1")[0]["count"] == 4


def test_care_scene_title_migrates_and_settings_take_quiet_hours(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc
    from deskbot_server.device_preferences import load_preferences

    svc.save_playbook("care", {"name": "care", "title": "日常关心", "is_care_scene": True, "template_version": 999, "tasks": []})
    svc._care_title_checked = False  # noqa: SLF001
    assert svc.care_playbook() == "care" and svc.require_playbook("care")["title"] == "定时提醒"
    ov = qc.save_settings({"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"}})
    assert ov["quiet_hours"]["start"] == "23:00" and load_preferences()["quiet_hours"]["end"] == "07:00"
    assert "reminder_soon_sec" not in ov["settings"]
    assert "offline_reminder_policy" not in load_preferences()


# ---------------- 目录 / 提示词 / 页面契约 ----------------


def test_scheduled_task_subsystem_is_gone_and_tool_contract_updated():
    src = ROOT / "service/src/deskbot_server"
    for gone in ("scheduled_task_service.py", "application/scheduled_task_scheduler.py", "web/templates/app2c/reminders.html",
                 "web/templates/app2c/preferences.html", "web/templates/app2c/people.html"):
        assert not (src / gone).exists(), gone
    from deskbot_server.application import proactive_gate, turn_arbiter
    from deskbot_server.db import models
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    assert not hasattr(models, "ScheduledTask") and not hasattr(turn_arbiter, "PRIORITY_REMINDER")
    assert not hasattr(proactive_gate, "seconds_until_next_reminder")
    schema = {s["name"]: s for s in build_rtc_tool_schemas()}["schedule_task"]
    props = schema["parameters"]["properties"]
    assert set(props) == {"action", "id", "task", "time", "date", "delay_minutes", "repeat", "days", "scene"}
    assert props["action"]["enum"] == ["create", "list", "update", "delete"] and "cron" not in props
    from deskbot_server.llm.utils import llm_tools_prompt_appendix

    text = llm_tools_prompt_appendix()
    assert "定时提醒" in text and '"time":"21:00"' in text and "cron" not in text
    tabs = (src / "web/templates/app2c/_agent_tabs.html").read_text(encoding="utf-8")
    assert "app2c.reminders" not in tabs and "app2c.preferences" not in tabs and "app2c.people" not in tabs and "quest.page" in tabs
    quest = (src / "web/templates/app2c/quest.html").read_text(encoding="utf-8")
    assert "日常关心" not in quest and "只提醒一次（某一天）" in quest and "saveQuiet(" in quest and "reminder_soon_sec" not in quest
    params = (src / "web/templates/app2c/params.html").read_text(encoding="utf-8")
    assert "/proxy/deskbot/api/asr_auto_reply" in params  # 自动回复开关从行为偏好页搬到参数设置
    prefs = (src / "device_preferences.py").read_text(encoding="utf-8")
    assert "offline_reminder" not in prefs
