"""主动陪伴 2026-09-10 定稿的推进规则：
- 结果（达成 / 未达成 / 跳过）只由小歪的工具或主人在控制台明确标，没有次数 / 天数自动判定；
- 标了结果 → 「接下一个」标记 → 循环等大家安静 next_delay_sec（默认 10 s）后触发顺序里的下一个还没达成的；
- 每 check_interval_hours（默认 1 小时）定时检测：进行中的重新计时，没有进行中的按顺序重开第一个未达成的；
- 旧数据的 failed 读到即迁移（主线 → unmet，日常关心 → paused）；随包场景 = 用户定稿的文案与设置。
"""

from __future__ import annotations

import asyncio
import time

from tests.quest_helpers import (  # noqa: F401
    bind,
    care_scene_with,
    clear_care_scene,
    demo_playbook,
    quest_env,
)
from tests.test_quest_proactive import FakeHub, FakeRegistry, FakeRunner


def _rows(svc, playbook="demo"):
    return {r["task_id"]: r for r in svc.get_instances("local", playbook)}


def test_result_marks_write_chain_marker_and_never_auto_fail(quest_env):
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet"]
    assert svc.pending_chain() is None
    # 达成：写标记（带这个小目标自己的 next_delay_sec），顺序里的下一个进入进行中
    pb = svc.get_playbook("demo")
    pb["tasks"][0]["next_delay_sec"] = 25
    svc.save_playbook("demo", pb)
    out = svc.update_task_result("local", "demo", "g_greet", "success", "主人回了")
    assert out["task"]["status"] == "success"
    chain = svc.pending_chain()
    assert chain and chain["playbook"] == "demo" and chain["after"] == "g_greet" and chain["delay_sec"] == 25
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_learn_name"]
    # 未达成：状态 unmet，同样往下走；跳过：skipped
    svc.update_task_result("local", "demo", "g_learn_name", "unmet", "主人没说")
    assert _rows(svc)["g_learn_name"]["status"] == "unmet" and svc.pending_chain()["after"] == "g_learn_name"
    svc.execute_quest_tool({"tool": "skip_goal", "task_id": "g_learn_name_soften", "reason": "别问了"}, device_id="dev1")
    assert _rows(svc)["g_learn_name_soften"]["status"] == "skipped"
    # 全都有结果：finished；但有未达成 → 没 settled；未达成列表按顺序
    assert svc.playbook_finished("local", "demo") is True and svc.playbook_settled("local", "demo") is False
    assert svc.unmet_task_ids("local", "demo") == ["g_learn_name"]
    # 标记过期：主人一直在说话超过 CHAIN_MAX_AGE_SEC → 作废
    svc.save_proactive_state(chain={"playbook": "demo", "after": "g_greet", "at": time.time() - svc.CHAIN_MAX_AGE_SEC - 1, "delay_sec": 10})
    assert svc.pending_chain() is None and svc.load_proactive_state().get("chain") is None


def test_chain_next_task_reopens_unmet_in_order_and_ends_the_round(quest_env):
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    svc.get_current_tasks()  # 绑定后实例随第一次取任务初始化
    svc.update_task_result("local", "demo", "g_greet", "unmet", "没回")
    svc.update_task_result("local", "demo", "g_learn_name", "success", "叫我老板")
    svc.update_task_result("local", "demo", "g_learn_name_soften", "unmet", "没回")
    assert svc.pending_chain()["after"] == "g_learn_name_soften"
    # 最后一个之后没有要接的 → 这轮到头
    assert svc.chain_next_task("local", "demo", "g_learn_name_soften") is None
    # 定时检测：没有进行中的 → 按顺序重开第一个未达成的（达成的不碰）
    assert svc.begin_pass("local", "demo") == {"touched": None, "reopened": "g_greet"}
    assert _rows(svc)["g_greet"]["status"] == "running" and _rows(svc)["g_learn_name"]["status"] == "success"
    svc.update_task_result("local", "demo", "g_greet", "success", "回了")
    # 接下一个：跳过已达成的 g_learn_name，重开未达成的 g_learn_name_soften
    assert svc.chain_next_task("local", "demo", "g_greet") == "g_learn_name_soften"
    rows = _rows(svc)
    assert rows["g_learn_name_soften"]["status"] == "running" and rows["g_learn_name"]["status"] == "success"
    # 有进行中的时定时检测只重新计时
    before = rows["g_learn_name_soften"]["started_at"]
    time.sleep(0.01)
    assert svc.begin_pass("local", "demo") == {"touched": "g_learn_name_soften", "reopened": None}
    assert _rows(svc)["g_learn_name_soften"]["started_at"] >= before
    # 全部达成 / 跳过 → settled，定时检测无事可做
    svc.update_task_result("local", "demo", "g_learn_name_soften", "skipped", "不用了")
    assert svc.playbook_settled("local", "demo") is True
    assert svc.begin_pass("local", "demo") == {"touched": None, "reopened": None}
    assert svc.check_interval_hours("demo") == 1


def test_loop_waits_for_quiet_after_a_result_then_triggers_next(quest_env, monkeypatch):
    """标了结果 → 大家安静 next_delay_sec 后接下一个；安静不够就等；到点了走 attempt(chain=True)。"""
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.quest_proactive import QuestProactiveLoop

    clear_care_scene()
    pb = demo_playbook()
    pb["tasks"][0]["next_delay_sec"] = 10
    svc.save_playbook("demo", pb)
    bind("demo")
    svc.get_current_tasks()
    calls: list[dict] = []

    class ChainRunner(FakeRunner):
        async def attempt(self, device_id, *, ignore_limits=False, task_id=None, scheduled_hit="", chain=False):
            calls.append({"task_id": task_id, "chain": chain})
            return True

    now = {"t": 10_000.0}
    activity = {"t": 10_000.0}
    loop = QuestProactiveLoop(
        ChainRunner(), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), idle_sec=120.0,
        activity_ts_provider=lambda dev: activity["t"], enabled_provider=lambda: True,
        quiet_hours_provider=lambda: False, device_provider=_dev("dev1"),
    )
    monkeypatch.setattr(loop, "_gate", lambda *a, **k: (True, ""))
    svc.update_task_result("local", "demo", "g_greet", "success", "回了")  # 标记时间 = 现在（真实钟）
    # 刚标完、主人还在说话（安静 3 s）→ 等
    activity["t"] = now["t"] - 3
    assert asyncio.run(loop.tick(now=now["t"])) == "chain_wait"
    assert calls == []
    # 安静够 10 s → 触发下一个（g_learn_name），标记消费掉
    activity["t"] = now["t"] - 11
    assert asyncio.run(loop.tick(now=now["t"])) == ""
    asyncio.run(_drain(loop))
    assert calls == [{"task_id": "g_learn_name", "chain": True}] and svc.pending_chain() is None
    assert _rows(svc)["g_learn_name"]["status"] == "running"
    # 换了场景：标记作废，走常规流程
    svc.update_task_result("local", "demo", "g_learn_name", "unmet", "没说")
    svc.save_proactive_state(chain={**svc.pending_chain(), "playbook": "other"})
    loop._inflight.clear()
    activity["t"] = now["t"] - 11
    reason = asyncio.run(loop.tick(now=now["t"] + 60))
    assert reason == "recent_conversation" and svc.pending_chain() is None


def test_loop_runs_a_pass_every_check_interval(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.quest_proactive import QuestProactiveLoop

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    svc.get_current_tasks()
    pb = svc.get_playbook("demo")
    pb["check_interval_hours"] = 2
    svc.save_playbook("demo", pb)
    assert svc.check_interval_hours("demo") == 2
    runner = FakeRunner()
    loop = QuestProactiveLoop(
        runner, asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), idle_sec=120.0,
        activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True,
        quiet_hours_provider=lambda: False, device_provider=_dev("dev1"),
    )
    monkeypatch.setattr(loop, "_gate", lambda *a, **k: (False, "reminder_soon"))  # 只看定时检测，不真的开口
    t0 = 100_000.0
    asyncio.run(loop.tick(now=t0))  # 第一次看见：从现在起算
    assert loop.next_pass_at("demo") == t0 + 7200 and svc.load_proactive_state()["passes"]["demo"] == t0
    svc.update_task_result("local", "demo", "g_greet", "unmet", "没回")
    svc.update_task_result("local", "demo", "g_learn_name", "unmet", "没回")
    svc.update_task_result("local", "demo", "g_learn_name_soften", "unmet", "没回")
    svc.clear_chain()
    asyncio.run(loop.tick(now=t0 + 7199))
    assert _rows(svc)["g_greet"]["status"] == "unmet"  # 还没到点
    asyncio.run(loop.tick(now=t0 + 7201))
    assert _rows(svc)["g_greet"]["status"] == "running" and loop.snapshot()["pass_at"]["demo"] == t0 + 7201
    # 重启后接着算：pass 时间从状态文件恢复
    loop2 = QuestProactiveLoop(
        runner, asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), idle_sec=120.0,
        activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True,
        quiet_hours_provider=lambda: False, device_provider=_dev("dev1"),
    )
    assert loop2.next_pass_at("demo") == t0 + 7201 + 7200


def test_legacy_failed_rows_migrate_by_kind(quest_env):
    from deskbot_server.application import quest_service as svc

    care_scene_with([{"id": "c_water", "kind": "care", "title": "喝水", "goal": "提醒喝水", "activation_score": 1}])
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    svc.get_current_tasks()
    for playbook, tid in (("demo", "g_greet"), ("care", "c_water")):
        row = svc._require_row("local", playbook, tid)
        svc._update_row(row.id, status="failed")
    svc._status_migrated_keys.clear()
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_learn_name"]
    assert _rows(svc)["g_greet"]["status"] == "unmet" and _rows(svc, "care")["c_water"]["status"] == "paused"


def test_shipped_defaults_match_user_decision(quest_env):
    """开机默认 = 用户 2026-09-10 定稿：初识 5 步（用户的打招呼文案）、日常关心两条、冷场 1 分钟、每天 16 次。"""
    from deskbot_server.application import quest_console as qc
    from deskbot_server.device_preferences import load_preferences
    from deskbot_server.quest_playbooks_store import (
        ensure_default_playbook,
        load_playbook,
        write_playbook,
    )

    # 老机器：xiaoy v8 带认脸、care v1 + 主人自己加的「活动颈椎」（同名）→ 升级后不重复、认脸下线
    write_playbook("xiaoy", {"name": "xiaoy", "template_version": 8, "check_interval_hours": 3, "tasks": [
        {"id": "g_greet", "goal": "旧", "activation_score": 1, "initial_status": "running"},
        {"id": "g_photo_memory", "goal": "认脸", "activation_score": 1},
    ]})
    write_playbook("care", {"name": "care", "is_care_scene": True, "template_version": 1, "tasks": [
        {"id": "g_care_break", "kind": "care", "goal": "旧", "activation_score": 1},
        {"id": "p1", "kind": "care", "title": "活动颈椎", "goal": "提醒写代码间隙活动颈椎", "activation_score": 1},
    ]})
    assert ensure_default_playbook() is True
    pb = load_playbook("xiaoy")
    ids = [t["id"] for t in pb["tasks"]]
    assert ids == ["g_greet", "g_learn_name", "g_daily_mood", "g_learn_daily", "g_learn_hobby"]
    assert pb["check_interval_hours"] == 3  # 主人改过的场景设置保留
    greet = pb["tasks"][0]
    assert greet["goal"] == "主动向主人打个招呼，做一个自我介绍，让主人认识自己"
    assert greet["success_condition"] == "小歪问候完就算达成" and greet["perform"]["expression"] == "happy"
    assert all(t["next_delay_sec"] == 10 and t["max_attempts"] == 0 and "failure_condition" not in t for t in pb["tasks"])
    care = load_playbook("care")
    assert [t["id"] for t in care["tasks"]] == ["g_care_break", "g_care_neck"]
    neck = care["tasks"][1]
    assert neck["title"] == "活动颈椎" and neck["strategy"] and neck["success_condition"] and neck["max_attempts"] == 2
    q = load_preferences()["quest"]
    assert q["idle_sec"] == 60 and q["daily_limit"] == 16 and q["care_daily_limit"] == 10
    quiet = load_preferences()["quiet_hours"]
    assert quiet["enabled"] is True and (quiet["start"], quiet["end"]) == ("22:00", "08:00")
    ov = qc.overview()
    assert ov["settings"]["idle_sec"] == 60 and ov["settings"]["daily_limit"] == 16 and "task_retry_sec" not in ov["settings"]
    assert ov["playbooks"][0]["check_interval_hours"] == 3


def test_page_shows_new_goal_editor_and_scene_setting(quest_env):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/quest").get_data(as_text=True)
    assert "<label>达成条件</label>" in html and "怎样算数" not in html
    assert "drawer.failure_condition" not in html and "drawer.max_wait_days" not in html
    assert "drawer.next_delay_sec" in html and "本目标有了结果后，大家安静多久触发下一个小目标" in html
    assert "目标检测间隔" in html and "检测目标完成情况，按顺序重新触发未达成目标" in html
    assert "同一个小目标再提间隔" not in html and "task_retry_sec" not in html
    assert "mark(t, 'unmet')" in html and "mark(t, 'skipped')" in html and "记为未达成" in html
    assert "5 个小目标一条线" in html and "认脸" not in html
    # 已有场景行右侧：AI 生成场景 + 新建场景；新建弹窗不再带「让小歪自己编」，AI 生成有自己的弹窗（复用动作页样式）
    head = html[html.index("目前已有场景"):html.index("目前已有场景") + 400]
    assert "openAiGen()" in head and "openNew()" in head and "AI 生成场景" in head
    assert html.count('@click="openNew()"') == 1 and "template==='generate'" not in html
    assert 'class="lab-modal"' in html and "偶尔讲个冷笑话" in html and "米家音箱" in html and "aiGenerateScene" in html


def _dev(device_id: str):
    async def _provider():
        return device_id

    return _provider


async def _drain(loop) -> None:
    workers = tuple(loop._workers)
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
