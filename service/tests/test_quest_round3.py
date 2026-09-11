"""主动陪伴第三轮：主人回应过的尝试不算没回应；进行中按场景顺序；提示词里日常关心列全、按真实名额说；
定时的也给提醒让路；开口由头按情形写；提议过期作废；记数/再提间隔重启后接着算；首页露出提议。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tests.quest_helpers import (  # noqa: F401
    bind,
    care_scene_with,
    clear_care_scene,
    demo_playbook,
    quest_env,
)
from tests.test_quest_proactive import FakeHub, FakeRegistry, _settle, _stub_delivery

TZ = ZoneInfo("Asia/Shanghai")


def _runner(qp, monkeypatch, calls, **kw):
    _stub_delivery(monkeypatch, qp, calls)
    return qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(),
        dp_broker=None, daily_limit_provider=lambda: 0, task_retry_sec=0.0, **kw,
    )


def test_story_goal_is_raised_once_per_round(quest_env, monkeypatch):
    """2026-09-10：主线小目标一轮只提一次——提过就等小歪标结果；重开 / 定时检测重新计时后才会再提。"""
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    calls: list[str] = []
    runner = _runner(qp, monkeypatch, calls)
    row = lambda: {r["task_id"]: r for r in svc.get_instances("local", "demo")}["g_greet"]  # noqa: E731
    assert asyncio.run(runner.attempt("dev1")) is True and "[g_greet]" in calls[0]
    assert row()["attempt_count"] == 0  # 主线不数次数
    assert asyncio.run(runner.attempt("dev1")) is False and runner.last_skip_reason == "task_cooldown"  # 这轮提过
    assert row()["status"] == "running"
    # 定时检测：进行中的重新计时 → 又能提了
    assert svc.begin_pass("local", "demo") == {"touched": "g_greet", "reopened": None}
    assert asyncio.run(runner.attempt("dev1")) is True and len(calls) == 2


def test_current_task_follows_scene_order(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    pb = demo_playbook()
    pb["tasks"].append({"id": "a_last", "goal": "排在最后的一步", "activation_score": 1, "initial_status": "running"})
    svc.save_playbook("demo", pb)
    bind("demo")
    # 一条线：id 字母序、initial_status 都不算数，当前 = 顺序里第一个没结果的
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet"]
    svc.update_task_result("local", "demo", "g_greet", "success", "ok")
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_learn_name"]
    assert [(t["id"], t["order"]) for t in qc.overview()["tasks"]] == [("g_greet", 1), ("g_learn_name", 2), ("g_learn_name_soften", 3), ("a_last", 4)]


def test_prompt_lists_all_care_goals_with_real_daily_limit(quest_env):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.device_preferences import update_preferences

    care_scene_with([{"id": f"c{i}", "kind": "care", "title": f"关心{i}", "goal": f"第 {i} 件小事", "activation_score": 1} for i in range(5)])
    bind("")
    text = svc.quest_prompt_appendix()
    assert all(f"[c{i}]" in text for i in range(5)) and "一天合计最多 10 次" in text and "一天最多一次" not in text  # 默认 10 次（2026-09-10）
    update_preferences({"quest": {"care_daily_limit": 0}})
    assert "不限次数" in svc.quest_prompt_appendix()


def test_scheduled_care_yields_to_reminder_gate(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    care_scene_with([{"id": "eyes", "kind": "care", "title": "远眺", "goal": "看远处", "schedule_time": "16:00", "activation_score": 1}])
    bind("demo")
    calls: list[dict] = []

    class Runner:
        last_skip_reason = ""

        async def attempt(self, dev, **kw):
            calls.append(kw)
            return True

    now_ts = datetime.fromisoformat("2026-09-08T16:03:00").replace(tzinfo=TZ).timestamp()
    monkeypatch.setattr(qp, "local_datetime", lambda ts=None: datetime.fromtimestamp(ts if ts is not None else now_ts, TZ))
    gate = {"allowed": False}
    lp = qp.QuestProactiveLoop(
        Runner(), asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
        activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True, quiet_hours_provider=lambda: False, gate=lambda s, d: (gate["allowed"], "" if gate["allowed"] else "reminder_soon"),
    )

    async def _go():
        assert await lp.tick(now=now_ts) == "reminder_soon" and lp._scheduled_fired == {} and calls == []
        gate["allowed"] = True
        assert await lp.tick(now=now_ts + 5) == ""
        await _settle(lp)
        assert calls and calls[0]["task_id"] == "eyes"

    asyncio.run(_go())


def test_openers_match_reason():
    from deskbot_server.application.quest_proactive import build_rtc_instruction, build_user_text

    task = {"task_id": "t1", "title": "知道称呼", "goal": "g"}
    assert "不确定他在不在旁边" in build_user_text(task, idle_sec=120) and "人就在面前" not in build_user_text(task, idle_sec=120)
    assert build_user_text(task, reason="manual").split("\n", 1)[0].endswith("[t1] 知道称呼") and "触发一次" in build_user_text(task, reason="manual")
    assert "到了主人定的时间" in build_user_text({**task, "scheduled_hit": "16:00"}, reason="scheduled")
    assert build_rtc_instruction(task, idle_sec=300).startswith("主人约 5 分钟没有和你说话，不确定他在不在旁边")
    assert build_rtc_instruction(task, reason="manual").startswith("主人在控制台点了「触发一次」")


def test_stale_proposals_expire_and_unblock_new_ones(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    out = execute_llm_tools([{"tool": "propose_goal", "title": "午睡", "goal": "提醒午睡"}], device_id="dev1")
    assert out[0]["ok"] is True
    pid = out[0]["task_id"]
    assert svc.get_task_definition("care", pid)["proposed_at"]
    assert execute_llm_tools([{"tool": "propose_goal", "title": "x", "goal": "y"}], device_id="dev1")[0]["ok"] is False  # 一次只挂一个
    assert svc.expire_stale_proposals(now=svc.utcnow() + timedelta(days=6)) == []
    assert svc.expire_stale_proposals(now=svc.utcnow() + timedelta(days=7, minutes=1)) == [pid]
    assert svc.get_task_definition("care", pid) is None and qc.overview()["proposals"] == []
    assert execute_llm_tools([{"tool": "propose_goal", "title": "x", "goal": "y"}], device_id="dev1")[0]["ok"] is True


def test_runner_counters_survive_restart_but_not_a_new_day(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    pb = demo_playbook()
    pb["tasks"][0]["max_attempts"] = 0
    svc.save_playbook("demo", pb)
    bind("demo")
    calls: list[str] = []
    runner = _runner(qp, monkeypatch, calls)
    assert asyncio.run(runner.attempt("dev1")) is True
    saved = svc.load_proactive_state()["runner"]
    assert saved["today_count"] == 1 and saved["task_last_attempt"]["g_greet"] > 0 and saved["day"] == runner._today
    fresh = _runner(qp, monkeypatch, calls)
    assert fresh.stats()["today_count"] == 0
    fresh.restore_state()
    assert fresh.stats()["today_count"] == 1 and fresh.stats()["task_last_attempt"]["g_greet"] == saved["task_last_attempt"]["g_greet"]
    assert fresh.last_spoke_at == runner.last_spoke_at
    # 第二天：次数清零，再提间隔照旧
    monkeypatch.setattr(qp, "local_datetime", lambda ts=None: datetime.now(TZ) + timedelta(days=1))
    tomorrow = _runner(qp, monkeypatch, calls)
    tomorrow.restore_state()
    assert tomorrow.stats()["today_count"] == 0 and "g_greet" in tomorrow.stats()["task_last_attempt"]


def test_home_page_mentions_pending_proposal(quest_env):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/home").get_data(as_text=True)
    assert "小歪想加一个小目标" in html and "proposals:(r.proposals||[])" in html


def test_user_activity_module():
    from deskbot_server.application import user_activity as ua

    ua._reset_for_tests()
    assert ua.last_ts("dev1") == 0.0
    ua.note("dev1", 100.0)
    ua.note("", 200.0)
    assert ua.last_ts("dev1") == 100.0 and ua.last_ts("dev2") == 0.0
