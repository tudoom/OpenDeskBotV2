"""定时的日常关心：每天 HH:MM 由主动陪伴循环按钟表触发（不等冷场、不看人脸、不占名额、勿扰让路、宽限两小时、一天一次）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.quest_helpers import (  # noqa: F401
    bind,
    care_scene_with,
    clear_care_scene,
    demo_playbook,
    quest_env,
)

TZ = ZoneInfo("Asia/Shanghai")


def _local(h, m, *, weekday_date="2026-09-08"):  # 2026-09-08 是周二（weekday 1）
    return datetime.fromisoformat(f"{weekday_date}T{h:02d}:{m:02d}:00").replace(tzinfo=TZ)


def test_schedule_fields_normalize_and_validate():
    from deskbot_server.quest_playbooks_store import normalize_task, validate_playbook

    t = normalize_task({"id": "a", "goal": "g", "kind": "care", "schedule_time": "16:00", "schedule_days": [0, 2, 9, "x", True]})
    assert t["schedule_time"] == "16:00" and t["schedule_days"] == [0, 2]
    assert normalize_task({"id": "a", "goal": "g", "schedule_time": "25:00"})["schedule_time"] == ""
    errs = validate_playbook({"name": "d", "tasks": [{"id": "a", "goal": "g", "schedule_time": "4pm", "schedule_days": [7]}]})
    assert any("schedule_time" in e for e in errs) and any("schedule_days" in e for e in errs)


def test_pick_scheduled_due_rules():
    from deskbot_server.application.quest_proactive import pick_scheduled_due

    c = {"task_id": "eyes", "schedule_time": "16:00", "schedule_days": [], "status": "running", "paused_until": None}
    assert pick_scheduled_due([c], _local(15, 59), {}) is None  # 没到点
    due = pick_scheduled_due([c], _local(16, 0), {})
    assert due and due["task_id"] == "eyes" and due["date"] == "2026-09-08"
    assert pick_scheduled_due([c], _local(17, 59), {}) is not None  # 宽限两小时内补提
    assert pick_scheduled_due([c], _local(18, 1), {}) is None  # 过了宽限 = 今天错过
    assert pick_scheduled_due([c], _local(16, 5), {"eyes": "2026-09-08"}) is None  # 今天叫过
    assert pick_scheduled_due([c], _local(16, 5), {"eyes": "2026-09-07"}) is not None  # 昨天叫过不算
    # 星期几：周二不在 [0, 2] 里 → 不提
    weekly = {**c, "schedule_days": [0, 2]}
    assert pick_scheduled_due([weekly], _local(16, 5), {}) is None
    assert pick_scheduled_due([{**c, "schedule_days": [1]}], _local(16, 5), {}) is not None
    # 主人说不用（无限期暂停）→ 不提；歇一天中 → 不提；歇完 → 提
    assert pick_scheduled_due([{**c, "status": "failed", "paused_until": None}], _local(16, 5), {}) is None
    later = (_local(16, 5) + timedelta(hours=3)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    assert pick_scheduled_due([{**c, "status": "failed", "paused_until": later}], _local(16, 5), {}) is None
    earlier = (_local(16, 5) - timedelta(hours=1)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    assert pick_scheduled_due([{**c, "status": "failed", "paused_until": earlier}], _local(16, 5), {}) is not None
    # 冷却中的（上次达成）也算候选，由 prepare 重开
    assert pick_scheduled_due([{**c, "status": "success"}], _local(16, 5), {}) is not None
    # 多条到点按时间早的先
    early = {**c, "task_id": "water", "schedule_time": "15:30"}
    assert pick_scheduled_due([c, early], _local(16, 5), {})["task_id"] == "water"


def test_candidates_and_prepare(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    care_scene_with([
        {"id": "eyes", "kind": "care", "title": "远眺", "goal": "看远处", "schedule_time": "16:00", "schedule_days": [1, 3], "activation_score": 1},
        {"id": "water", "kind": "care", "title": "喝水", "goal": "喝水", "activation_score": 1},
    ])
    bind("demo")
    svc.ensure_instances("local", "care")
    cands = svc.scheduled_care_candidates("dev1")
    assert [c["task_id"] for c in cands] == ["eyes"] and cands[0]["schedule_days"] == [1, 3] and cands[0]["status"] == "running"
    # 达成后冷却 → prepare 重开；主人说不用 → 不可提
    svc.update_task_result("local", "care", "eyes", "success", "看了")
    assert svc.scheduled_care_candidates("dev1")[0]["status"] == "success"
    assert svc.prepare_scheduled_task("dev1", "care", "eyes") is True
    assert {r["task_id"]: r for r in svc.get_instances("local", "care")}["eyes"]["status"] == "running"
    svc.execute_quest_tool({"tool": "skip_goal", "task_id": "eyes"}, device_id="dev1")
    assert svc.prepare_scheduled_task("dev1", "care", "eyes") is False
    # 提示词里定时的写法
    svc.restart_task("local", "care", "eyes")
    assert "每天 16:00 由系统按时叫你提" in svc.quest_prompt_appendix()


def test_loop_fires_scheduled_care_without_idle_or_face(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry, _settle

    svc.save_playbook("demo", demo_playbook())
    care_scene_with([{"id": "eyes", "kind": "care", "title": "远眺", "goal": "看远处", "schedule_time": "16:00", "activation_score": 1}])
    bind("demo")
    svc.ensure_instances("local", "care")
    calls: list[dict] = []

    class Runner:
        last_skip_reason = ""

        async def attempt(self, dev, **kw):
            calls.append(kw)
            return True

    now_ts = _local(16, 3).timestamp()
    monkeypatch.setattr(qp, "local_datetime", lambda ts=None: datetime.fromtimestamp(ts if ts is not None else now_ts, TZ))
    lp = qp.QuestProactiveLoop(
        Runner(), asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
        activity_ts_provider=lambda dev: now_ts - 5,  # 刚聊过：普通主动开口会说 recent_conversation
        # 没看到人也照提
        enabled_provider=lambda: True, quiet_hours_provider=lambda: False, gate=lambda s, d: (True, ""),
    )

    async def _go():
        assert await lp.tick(now=now_ts) == ""
        await _settle(lp)
        assert calls and calls[0]["task_id"] == "eyes" and calls[0]["ignore_limits"] is True and calls[0]["scheduled_hit"] == "16:00"
        # 今天不再重复；第二天同一时刻再提
        assert await lp.tick(now=now_ts + 60) == "recent_conversation"
        await _settle(lp)
        assert len(calls) == 1
        monkeypatch.setattr(qp, "local_datetime", lambda ts=None: datetime.fromtimestamp(ts if ts is not None else now_ts, TZ) + timedelta(days=1))
        lp._last_attempt.clear()
        assert await lp.tick(now=now_ts + 86400) == ""
        await _settle(lp)
        assert len(calls) == 2
        # 勿扰时段：到点也不提，标记不落下（勿扰结束后宽限内补提）
        lp2 = qp.QuestProactiveLoop(
            Runner(), asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
            activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True, quiet_hours_provider=lambda: True, gate=lambda s, d: (True, ""),
        )
        assert await lp2.tick(now=now_ts) == "quiet_hours" and lp2._scheduled_fired == {}
        await _settle(lp2)

    asyncio.run(_go())


def test_runner_text_marks_scheduled_hit_and_ui_contract(quest_env):
    from deskbot_server.application.quest_proactive import build_rtc_instruction, build_user_text
    from deskbot_server.web.app import create_app

    task = {"task_id": "eyes", "title": "远眺", "goal": "看远处", "kind": "care", "scheduled_hit": "16:00"}
    assert "到了主人定的时间（16:00）" in build_rtc_instruction(task) and "到了主人定的时间（16:00）" in build_user_text(task)
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    html = client.get("/quest").get_data(as_text=True)
    assert "定时（到点由系统叫它提）" in html and "哪几天" in html and "scheduleLabel" in html
    # 页面保存：定时 + 星期几；坏时间被拒
    ov = client.post("/api/quest/playbooks", json={"title": "作息", "template": "blank"}).get_json()
    base = f"/api/quest/playbooks/{ov['playbook']['name']}/tasks"
    r = client.post(base, json={"kind": "care", "title": "远眺", "goal": "看远处", "schedule_time": "16:00", "schedule_days": [0, 4]})
    assert r.status_code == 200
    t = r.get_json()["task"]
    assert t["schedule_time"] == "16:00" and t["schedule_days"] == [0, 4]
    assert t["id"] in {x["id"] for x in r.get_json()["care_tasks"]}  # 日常关心住进日常关心场景
    assert client.put(f"{base}/{t['id']}", json={"schedule_time": "4pm"}).status_code == 400
    # 小歪提议时带时间，同意时保留
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    out = execute_llm_tools([{"tool": "propose_goal", "title": "午睡", "goal": "提醒午睡", "schedule_time": "13:00"}], device_id="dev1")
    assert out[0]["ok"] is True
    ov = client.post(f"/api/quest/proposals/{out[0]['task_id']}/approve", json={}).get_json()
    assert {x["id"]: x for x in ov["care_tasks"]}[out[0]["task_id"]]["schedule_time"] == "13:00"
