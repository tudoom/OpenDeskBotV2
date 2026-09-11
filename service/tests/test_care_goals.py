"""日常关心（care）小目标：暂停语义、到期恢复、连续没回应歇一天、主线优先与每日名额、插入不替换。"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

from tests.quest_helpers import (  # noqa: F401
    bind,
    care_scene_with,
    clear_care_scene,
    demo_playbook,
    quest_env,
)

C_NECK = {
    "id": "c_neck", "kind": "care", "title": "活动颈椎", "goal": "写代码久了提醒起身",
    "strategy": "一句话提一下", "trigger_hint": "主人连续写代码一小时", "success_condition": "主人起身了",
    "repeat_interval_sec": 3600, "max_attempts": 2, "activation_score": 1, "initial_status": "running",
}


def _pb_with_care():
    """主线 demo + 「日常关心」场景里一条 c_neck（日常关心只住在那个场景里）。"""
    care_scene_with([dict(C_NECK)])
    return demo_playbook()


def test_care_semantics_in_engine(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.core import clock
    from deskbot_server.quest_playbooks_store import normalize_task

    # 老数据：带 proposed_after 键（小歪提的）默认归日常关心；普通任务默认主线
    assert normalize_task({"id": "a", "goal": "g", "proposed_after": ""})["kind"] == "care"
    assert normalize_task({"id": "a", "goal": "g"})["kind"] == "story"
    care = normalize_task({"id": "a", "goal": "g", "kind": "care"})
    assert care["repeatable"] is True and care["max_attempts"] == 2

    svc.save_playbook("demo", _pb_with_care())
    bind("demo")
    cur = svc.get_current_tasks()
    assert [t["task_id"] for t in cur] == ["g_greet", "c_neck"] and cur[1]["kind"] == "care"
    # 主人说不用（skip_goal）→ 无限期暂停，不传播，主线不受影响
    out = svc.execute_quest_tool({"tool": "skip_goal", "task_id": "c_neck", "reason": "别管我颈椎"}, device_id="dev1")
    assert out["ok"] is True and out["task"]["status"] == "paused" and out["propagated"] == []
    rows = {r["task_id"]: r for r in svc.get_instances("local", "care")}
    assert rows["c_neck"]["paused_until"] is None
    assert {r["task_id"]: r for r in svc.get_instances("local", "demo")}["g_greet"]["status"] == "running"
    real = clock.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(days=3))
    assert svc.reactivate_due_repeats("local", "care") == []  # 无限期：不自动恢复
    monkeypatch.setattr(svc, "utcnow", lambda: real)
    svc.restart_task("local", "care", "c_neck")
    # 连续 2 次没回应 → 歇一天，之后自动恢复
    svc.record_attempt("local", "care", "c_neck")
    assert svc.auto_fail_if_exhausted("local", "care", "c_neck") is None
    svc.record_attempt("local", "care", "c_neck")
    out = svc.auto_fail_if_exhausted("local", "care", "c_neck")
    assert out and out["task"]["status"] == "paused" and "歇一天" in out["task"]["result"]
    rows = {r["task_id"]: r for r in svc.get_instances("local", "care")}
    assert rows["c_neck"]["paused_until"] and rows["c_neck"]["attempt_count"] == 0
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(hours=23))
    assert svc.reactivate_due_repeats("local", "care") == []
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(hours=25))
    assert svc.reactivate_due_repeats("local", "care") == ["c_neck"]
    # 达成 → 冷却 1 小时后回来；场景走完判定忽略日常关心
    monkeypatch.setattr(svc, "utcnow", lambda: real)
    svc.update_task_result("local", "care", "c_neck", "success", "主人起身了")
    assert svc.playbook_finished("local", "demo") is False  # 主线 g_greet 还在
    svc.update_task_result("local", "demo", "g_greet", "failed", "没回应")  # 一条线：未达成也进入下一个
    assert svc.playbook_finished("local", "demo") is False
    svc.update_task_result("local", "demo", "g_learn_name", "failed", "没回应")
    svc.update_task_result("local", "demo", "g_learn_name_soften", "failed", "没回应")  # 最后一个有了结果 → 走完
    assert svc.playbook_finished("local", "demo") is True
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(hours=2))
    assert svc.reactivate_due_repeats("local", "care") == ["c_neck"]
    assert svc.playbook_finished("local", "demo") is True and svc.playbook_finished("local", "care") is False
    # 提示词里日常关心单列
    text = svc.quest_prompt_appendix()
    assert "可以顺带关心的事" in text and "[c_neck] 活动颈椎" in text and "主人连续写代码一小时" in text


def test_runner_prefers_story_and_caps_care_per_day(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from tests.test_quest_proactive import FakeHub, FakeRegistry, _stub_delivery

    pb = _pb_with_care()
    pb["tasks"][0]["max_attempts"] = 0
    svc.save_playbook("demo", pb)
    bind("demo")
    calls: list[str] = []
    _stub_delivery(monkeypatch, qp, calls)
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(),
        dp_broker=None, daily_limit_provider=lambda: 0, care_daily_limit_provider=lambda: 1, task_retry_sec=1800.0,
    )
    tasks = asyncio.run(asyncio.to_thread(svc.get_current_tasks, "dev1"))
    now = time.time()
    # 主线在前：先挑 g_greet；主线这轮提过 → 轮到日常关心；日常关心当天名额 1 次
    assert runner.pick_task(tasks, now=now)["task_id"] == "g_greet"
    runner._task_last_attempt["g_greet"] = now + 1
    assert runner.pick_task(tasks, now=now)["task_id"] == "c_neck"
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True  # g_greet（真实 attempt 用当前时间，主线未冷却）
    assert "[g_greet]" in calls[0]
    runner._task_last_attempt["g_greet"] = runner.last_spoke_at
    assert asyncio.run(runner.attempt("dev1")) is True and "[c_neck]" in calls[1] and "日常关心" in calls[1]
    assert runner.stats()["care_today_count"] == 1
    # 名额用完：主线这轮提过、日常关心也不再提
    runner._task_last_attempt["g_greet"] = runner.last_spoke_at + 100000  # 主线这轮已提过
    assert runner.pick_task(tasks, now=runner.last_spoke_at + 60) is None
    # 日常关心两次之间至少隔它自己的频率（1 小时），即便名额放开
    runner._care_today_count = 0
    assert runner.pick_task(tasks, now=runner.last_spoke_at + 1801) is None
    assert runner.pick_task(tasks, now=runner.last_spoke_at + 3601)["task_id"] == "c_neck"


def test_insert_after_keeps_order_and_care_lives_apart(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())  # 一条线：g_greet → g_learn_name → 换个角度
    bind("demo")
    new = qc.add_task("demo", {"title": "问问加班", "goal": "知道加不加班"}, after="g_greet")
    ov = qc.overview()
    assert [t["id"] for t in ov["tasks"]] == ["g_greet", new["id"], "g_learn_name", "g_learn_name_soften"]
    by = {t["id"]: t for t in ov["tasks"]}
    assert by[new["id"]]["order"] == 2 and by[new["id"]]["status"] == "not_started" and by["g_greet"]["status"] == "running"
    care = qc.add_task("demo", {"kind": "care", "title": "喝水", "goal": "提醒喝水"}, after="g_greet")
    assert care["kind"] == "care" and care["status"] == "running" and care["order"] == 0  # 日常关心不在线上
    assert care["id"] in {t["id"] for t in qc.overview()["care_tasks"]}  # 落在「日常关心」场景里
    assert [t["id"] for t in qc.overview()["tasks"]] == ["g_greet", new["id"], "g_learn_name", "g_learn_name_soften"]
    # 把主线任务改成日常关心：搬进日常关心场景，线上的顺序自动接上
    qc.update_task("demo", new["id"], {"kind": "care"})
    ov = qc.overview()
    assert [t["id"] for t in ov["tasks"]] == ["g_greet", "g_learn_name", "g_learn_name_soften"]
    assert {care["id"], new["id"]} <= {t["id"] for t in ov["care_tasks"]}


def test_throttle_rules_come_from_preferences(quest_env, monkeypatch):
    """每天几次 / 同一小目标再提间隔 / 提醒让路 / 日常关心歇多久 都可在高级里改，改了立即生效。"""
    from deskbot_server.application import proactive_gate as gate
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from deskbot_server.device_preferences import update_preferences
    from tests.test_quest_proactive import FakeHub, FakeRegistry

    assert svc.task_retry_sec() == 120.0 and svc.care_daily_limit() == 10
    assert svc.reminder_soon_sec() == 90.0 and svc.care_pause_sec() == 86400.0
    update_preferences({"quest": {"task_retry_sec": 300, "reminder_soon_sec": 0, "care_pause_sec": 3600}})
    assert (svc.task_retry_sec(), svc.reminder_soon_sec(), svc.care_pause_sec()) == (300.0, 0.0, 3600.0)
    # runner 不再固定 30 分钟：现读偏好（只管看情况的日常关心）；主线一轮只提一次，重开 / 定时检测重新计时后才再提
    runner = qp.QuestProactiveRunner(chat=SimpleNamespace(settings=None), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), dp_broker=None)
    tasks = [{"task_id": "a", "kind": "care", "ratio": 1.0, "repeat_interval_sec": 60}]
    runner._task_last_attempt["a"] = 1000.0
    assert runner.pick_task(tasks, now=1000.0 + 299) is None
    assert runner.pick_task(tasks, now=1000.0 + 301)["task_id"] == "a"
    assert runner.stats()["task_retry_sec"] == 300.0
    story = [{"task_id": "s", "kind": "story", "ratio": 1.0, "started_at_ts": 1000.0}]
    runner._task_last_attempt["s"] = 1001.0
    assert runner.pick_task(story, now=1000.0 + 99999) is None  # 这轮提过：不管隔多久都不再提
    story[0]["started_at_ts"] = 1002.0  # 重开 / 定时检测重新计时
    assert runner.pick_task(story, now=1000.0 + 5)["task_id"] == "s"
    # 提醒让路 = 0 → 不让
    monkeypatch.setattr(gate, "quiet_hours_active", lambda: False)
    monkeypatch.setattr(gate, "seconds_until_next_reminder", lambda: 5.0)
    assert gate.can_be_proactive(gate.SOURCE_QUEST) == (True, "")
    update_preferences({"quest": {"reminder_soon_sec": 60}})
    assert gate.can_be_proactive(gate.SOURCE_QUEST) == (False, "reminder_soon")
    # 日常关心歇多久：连续没回应后按偏好暂停 1 小时
    svc.save_playbook("demo", _pb_with_care())
    bind("demo")
    svc.ensure_instances("local", "care")
    svc.record_attempt("local", "care", "c_neck")
    svc.record_attempt("local", "care", "c_neck")
    out = svc.auto_fail_if_exhausted("local", "care", "c_neck")
    rows = {r["task_id"]: r for r in svc.get_instances("local", "care")}
    from datetime import datetime
    until = datetime.fromisoformat(rows["c_neck"]["paused_until"])
    finished = datetime.fromisoformat(rows["c_neck"]["finished_at"])
    assert out and abs((until - finished).total_seconds() - 3600) < 5
