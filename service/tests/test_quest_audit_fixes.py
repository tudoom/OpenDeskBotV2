"""主动陪伴排查后的修复：
定时 / 手动触发不占每日名额；定时的日常关心不在冷场时顺带提；没选主线时循环照跑（日常关心生效）；
「今天叫过」落盘、重启不重复叫；User.md 只记一次性的主线里程碑；日常关心改成主线会搬进在用的主线。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.quest_helpers import (  # noqa: F401
    bind,
    care_scene_with,
    clear_care_scene,
    demo_playbook,
    quest_env,
)


def test_ignore_limits_attempts_do_not_consume_daily_quota(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry, _stub_delivery

    clear_care_scene()
    pb = demo_playbook()
    pb["tasks"][0]["max_attempts"] = 0
    svc.save_playbook("demo", pb)
    bind("demo")
    calls: list[str] = []
    _stub_delivery(monkeypatch, qp, calls)
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(),
        dp_broker=None, daily_limit_provider=lambda: 1, task_retry_sec=0.0,
    )
    # 主人点「触发一次」/ 定时到点：说了，但不算进每日名额；时间照记
    assert asyncio.run(runner.attempt("dev1", ignore_limits=True, task_id="g_greet", scheduled_hit="16:00")) is True
    assert runner.stats()["today_count"] == 0 and runner.stats()["last_spoke_at"] and "到了主人定的时间（16:00）" in calls[0]
    assert runner.stats()["task_last_attempt"]["g_greet"] > 0
    # 循环自己开口才占名额（主线一轮只提一次：重开一下让它这轮还没提过）
    svc.restart_task("local", "demo", "g_learn_name")
    svc.restart_task("local", "demo", "g_greet")
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True and runner.stats()["today_count"] == 1
    svc.restart_task("local", "demo", "g_greet")
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is False and runner.last_skip_reason == "daily_limit"


def test_pick_task_never_picks_scheduled_care_off_schedule():
    from deskbot_server.application import quest_proactive as qp

    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=None, registry=None, dp_broker=None,
        care_daily_limit_provider=lambda: 5, task_retry_sec=0.0,
    )
    timed = {"task_id": "eyes", "kind": "care", "schedule_time": "16:00", "repeat_interval_sec": 3600}
    loose = {"task_id": "water", "kind": "care", "schedule_time": "", "repeat_interval_sec": 3600}
    assert runner.pick_task([timed], now=1000.0) is None  # 定时的只在到点由循环叫
    assert runner.pick_task([timed, loose], now=1000.0)["task_id"] == "water"


def test_loop_runs_without_a_story_scene_so_care_still_works(quest_env):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from deskbot_server.device_preferences import update_preferences

    assert svc.care_playbook() == "care"
    bind("")
    assert svc.bound_playbook() is None and qp._default_enabled() is True  # 没选主线，日常关心照样生效
    update_preferences({"quest": {"proactive_enabled": False}})
    assert qp._default_enabled() is False


def test_scheduled_fired_survives_restart(quest_env):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry

    assert svc.load_scheduled_fired() == {}
    lp = qp.QuestProactiveLoop(SimpleNamespace(last_skip_reason=""), asr_chat_hub=FakeHub(), registry=FakeRegistry())
    lp._scheduled_fired["eyes"] = "2026-09-08"
    lp._persist_scheduled_fired()
    assert svc.load_scheduled_fired() == {"eyes": "2026-09-08"}

    async def _go():
        fresh = qp.QuestProactiveLoop(SimpleNamespace(last_skip_reason=""), asr_chat_hub=FakeHub(), registry=FakeRegistry())
        assert fresh._scheduled_fired == {}
        fresh.start()  # 重启后的新循环从盘上把「今天叫过」读回来
        try:
            assert fresh._scheduled_fired == {"eyes": "2026-09-08"}
        finally:
            await fresh.stop()

    asyncio.run(_go())


def test_user_doc_only_records_one_off_story_milestones(quest_env, monkeypatch):
    from deskbot_server import agent_docs
    from deskbot_server.application import quest_service as svc

    docs: dict[str, str] = {"User.md": "# 主人\n"}
    monkeypatch.setattr(agent_docs, "read_doc", lambda name: docs.get(agent_docs.resolve(name) or name, ""))
    monkeypatch.setattr(agent_docs, "write_doc", lambda name, text: docs.__setitem__(agent_docs.resolve(name) or name, text) or {"name": name, "bytes": len(text)})
    care_scene_with([{"id": "c_water", "kind": "care", "title": "喝水", "goal": "提醒喝水", "activation_score": 1}])
    pb = demo_playbook()
    pb["tasks"][0]["repeatable"] = True
    svc.save_playbook("demo", pb)
    bind("demo")
    svc.ensure_instances("local", "demo")
    svc.ensure_instances("local", "care")
    svc.update_task_result("local", "care", "c_water", "success", "主人去喝水了")  # 日常关心：不写
    svc.update_task_result("local", "demo", "g_greet", "success", "回应了")  # 可重复的主线：不写
    assert docs["User.md"] == "# 主人\n"
    svc.update_task_result("local", "demo", "g_learn_name", "success", "叫十三")  # 一次性的主线里程碑：写一行
    assert "陪伴小目标「" in docs["User.md"] and "叫十三" in docs["User.md"]


def test_changing_care_goal_to_story_moves_it_into_the_active_story(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc
    from deskbot_server.quest_playbooks_store import QuestError

    ov = qc.overview()
    assert "g_care_break" in {t["id"] for t in ov["care_tasks"]}
    view = qc.update_task("care", "g_care_break", {"kind": "story"})
    assert view["kind"] == "story" and view["order"] == 6 and view["status"] == "not_started"  # 接在主线末尾，等前面走完
    assert svc.get_task_definition("care", "g_care_break") is None
    assert svc.get_task_definition("xiaoy", "g_care_break")["repeatable"] is False
    assert svc.get_instances("local", "care") == [] or all(r["task_id"] != "g_care_break" for r in svc.get_instances("local", "care"))
    ov = qc.overview()
    assert "g_care_break" in {t["id"] for t in ov["tasks"]} and "g_care_break" not in {t["id"] for t in ov["care_tasks"]}
    # 没有在用的主线 → 明确报错，不会把主线小目标留在日常关心里
    care_scene_with([{"id": "c_x", "kind": "care", "title": "x", "goal": "y", "activation_score": 1}])
    bind("")
    with pytest.raises(QuestError, match="主线"):
        qc.update_task("care", "c_x", {"kind": "story"})
    assert svc.get_task_definition("care", "c_x")["kind"] == "care"


def test_manual_trigger_does_not_count_as_a_miss(quest_env, monkeypatch):
    """控制台点「触发一次」只记时间，不记「没回应」次数：测两下不该把日常关心测暂停。"""
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry, _stub_delivery

    care_scene_with([{"id": "c_water", "kind": "care", "title": "喝水", "goal": "提醒喝水", "max_attempts": 2, "activation_score": 1}])
    bind("")
    calls: list[str] = []
    _stub_delivery(monkeypatch, qp, calls)
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(),
        dp_broker=None, daily_limit_provider=lambda: 0, task_retry_sec=0.0, activity_ts_provider=lambda dev: 0.0,
    )
    row = lambda: {r["task_id"]: r for r in svc.get_instances("local", "care")}["c_water"]  # noqa: E731
    for _ in range(3):
        assert asyncio.run(runner.attempt("dev1", ignore_limits=True, task_id="c_water")) is True
    assert row()["attempt_count"] == 0 and row()["status"] == "running" and "触发一次" in calls[-1]
    # 循环自己提的才算：两次没回应 → 歇一天（清掉「刚提过」的间隔，让循环能马上再挑它）
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True and row()["attempt_count"] == 1
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True and row()["status"] == "paused" and row()["paused_until"]
    # 页面：歇一天显示到期时间，不是笼统的「已暂停」
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/quest").get_data(as_text=True)
    assert "pauseLabel(t)" in html and "歇到" in html and "触发一次」不算" in html
