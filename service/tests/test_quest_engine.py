"""剧本任务引擎：剧本管理 / 校验 / 状态机 / 分数流转 / 绑定。"""

from __future__ import annotations

import pytest

from tests.quest_helpers import bind, clear_care_scene, demo_playbook, quest_env  # noqa: F401


def _svc():
    from deskbot_server.application import quest_service

    return quest_service


# ── 剧本管理与校验 ────────────────────────────────────────────


def test_playbook_create_list_get_delete(quest_env):
    svc = _svc()
    assert svc.list_playbooks() == []
    pb = svc.create_playbook("demo")
    assert pb == {"name": "demo", "tasks": []}
    assert svc.list_playbooks() == ["demo"]
    svc.save_playbook("demo", demo_playbook())
    got = svc.get_playbook("demo")
    assert len(got["tasks"]) == 3
    svc.delete_playbook("demo")
    assert svc.list_playbooks() == []
    assert svc.get_playbook("demo") is None


def test_playbook_create_invalid_name(quest_env):
    from deskbot_server.quest_playbooks_store import QuestError

    with pytest.raises(QuestError):
        _svc().create_playbook("bad name/../x")
    with pytest.raises(QuestError):
        _svc().create_playbook("Bad-UPPER")
    with pytest.raises(QuestError, match="已存在"):
        _svc().create_playbook("demo")
        _svc().create_playbook("demo")


def test_validate_duplicate_id_unknown_ref_and_cycle(quest_env):
    from deskbot_server.quest_playbooks_store import QuestError, has_cycle, validate_playbook

    pb = demo_playbook()
    pb["tasks"][1]["id"] = "g_greet"
    with pytest.raises(QuestError, match="重复"):
        _svc().save_playbook("demo", pb)

    pb = demo_playbook()
    pb["tasks"][0]["on_success"] = [{"id": "ghost", "score": 1}]
    with pytest.raises(QuestError, match="不存在"):
        _svc().save_playbook("demo", pb)

    pb = demo_playbook()
    pb["tasks"][2]["on_success"] = [{"id": "g_greet", "score": 1}]  # soften → greet 成环
    with pytest.raises(QuestError, match="成环"):
        _svc().save_playbook("demo", pb)

    # 自环也是环
    pb = demo_playbook()
    pb["tasks"][2]["on_failure"] = [{"id": "g_learn_name_soften", "score": 1}]
    assert any("成环" in e for e in validate_playbook(pb))
    assert has_cycle([("a", "a")], {"a"})
    assert not has_cycle([("a", "b"), ("b", "c"), ("a", "c")], {"a", "b", "c"})

    # 字段校验
    errs = validate_playbook({"name": "demo", "tasks": [{"id": "x"}]})
    assert any("goal" in e for e in errs)
    errs = validate_playbook({"name": "demo", "tasks": [{"id": "x", "goal": "g", "activation_score": -1}]})
    assert any("activation_score" in e for e in errs)
    errs = validate_playbook({"name": "demo", "tasks": [{"id": "x", "goal": "g", "initial_status": "done"}]})
    assert any("initial_status" in e for e in errs)
    assert validate_playbook("nope") == ["剧本必须是 JSON 对象"]


def test_default_playbook_template_is_valid_dag(quest_env):
    from deskbot_server.quest_playbooks_store import (
        DEFAULT_PLAYBOOK_NAME,
        ensure_default_playbook,
        load_default_playbook_template,
        validate_playbook,
    )

    template = load_default_playbook_template()
    assert template and template["name"] == DEFAULT_PLAYBOOK_NAME
    assert validate_playbook(template) == []
    assert any(t.get("initial_status") == "running" for t in template["tasks"])
    assert all("主人" in t["goal"] or "小歪" in t["goal"] for t in template["tasks"])
    assert ensure_default_playbook() is True
    assert ensure_default_playbook() is False  # 幂等
    assert DEFAULT_PLAYBOOK_NAME in _svc().list_playbooks()


def test_task_crud_and_cycle_guard(quest_env):
    svc = _svc()
    svc.create_playbook("demo")
    t = svc.add_task("demo", {"id": "g_a", "goal": "A"})
    assert t["activation_score"] == 1
    assert t["title"] == "notitle"
    assert t["pos"]["x"] == 120
    svc.add_task("demo", {"id": "g_b", "goal": "B"})
    svc.update_task("demo", "g_a", {"on_success": [{"id": "g_b", "score": 5}]})
    assert svc.get_playbook("demo")["tasks"][0]["on_success"] == [{"id": "g_b", "score": 5}]
    svc.delete_task("demo", "g_b")
    assert svc.get_playbook("demo")["tasks"][0]["on_success"] == []  # 删任务清理引用
    from deskbot_server.quest_playbooks_store import QuestError

    svc.add_task("demo", {"id": "g_b", "goal": "B", "on_success": [{"id": "g_a", "score": 1}]})
    with pytest.raises(QuestError, match="成环"):
        svc.update_task("demo", "g_a", {"on_success": [{"id": "g_b", "score": 1}]})


# ── 实例与状态机 ──────────────────────────────────────────────


def test_ensure_and_reset_instances(quest_env):
    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    r = svc.ensure_instances("dev1", "demo")
    assert r == {"created": 3, "activated": 1, "total": 3}
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_greet"]["status"] == "running"
    assert by_id["g_greet"]["started_at"] is not None
    assert by_id["g_learn_name"]["status"] == "not_started"
    assert svc.ensure_instances("dev1", "demo")["created"] == 0
    svc.restart_task("dev1", "demo", "g_learn_name")
    svc.reset_instances("dev1", "demo")
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "not_started"
    assert by_id["g_learn_name"]["current_score"] == 0


def test_update_task_result_propagates_and_activates(quest_env):
    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    svc.ensure_instances("dev1", "demo")
    r = svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    assert r["task"]["status"] == "success"
    assert r["task"]["finished_at"] is not None
    assert r["task"]["result"] == "用户回应了问候"
    assert r["propagated"] == [{"task_id": "g_learn_name", "current_score": 10, "status": "running"}]
    assert r["activated"][0]["task_id"] == "g_learn_name"
    # 失败口传播
    r = svc.update_task_result("dev1", "demo", "g_learn_name", "failed", "用户表示不想说")  # 旧名 failed 折成 unmet
    assert r["task"]["status"] == "unmet"
    assert r["activated"][0]["task_id"] == "g_learn_name_soften"
    soften = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}["g_learn_name_soften"]
    assert soften["status"] == "running" and soften["current_score"] == 6


def test_update_task_result_guards(quest_env):
    from deskbot_server.quest_playbooks_store import QuestError

    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    svc.ensure_instances("dev1", "demo")
    with pytest.raises(QuestError, match="未激活"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "success", "x")
    svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    with pytest.raises(QuestError, match="已有结果"):
        svc.update_task_result("dev1", "demo", "g_greet", "failed", "y")
    with pytest.raises(QuestError, match="status"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "done", "z")
    with pytest.raises(QuestError, match="结果"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "success", "")
    with pytest.raises(QuestError, match="实例不存在"):
        svc.update_task_result("nobody", "demo", "g_greet", "success", "x")


def test_terminal_target_ignores_propagation(quest_env):
    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    svc.ensure_instances("dev1", "demo")
    svc.restart_task("dev1", "demo", "g_learn_name")
    svc.update_task_result("dev1", "demo", "g_learn_name", "failed", "手动")
    r = svc.update_task_result("dev1", "demo", "g_greet", "success", "ok")
    assert r["propagated"] == [] and r["activated"] == []


def test_strategy_override_and_rename(quest_env):
    from deskbot_server.quest_playbooks_store import QuestError

    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    svc.ensure_instances("dev1", "demo")
    assert svc.get_effective_strategy("dev1", "demo", "g_greet") == "轻松自然地打招呼"
    r = svc.update_task_strategy("dev1", "demo", "g_greet", "用户喜欢简洁，问候要短")
    assert r["strategy"] == "用户喜欢简洁，问候要短"
    assert svc.get_effective_strategy("dev1", "demo", "g_greet") == "用户喜欢简洁，问候要短"
    with pytest.raises(QuestError, match="strategy"):
        svc.update_task_strategy("dev1", "demo", "g_greet", "  ")
    # 改名：连线引用联动 + 实例改名且运行态保留
    svc.restart_task("dev1", "demo", "g_learn_name")
    t = svc.update_task("demo", "g_learn_name", {"id": "g_learn_user_name"})
    assert t["id"] == "g_learn_user_name"
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["on_success"] == [{"id": "g_learn_user_name", "score": 10}]
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert "g_learn_name" not in by_id
    assert by_id["g_learn_user_name"]["status"] == "running"
    with pytest.raises(QuestError, match="已存在"):
        svc.update_task("demo", "g_greet", {"id": "g_learn_user_name"})
    with pytest.raises(QuestError, match="非法"):
        svc.update_task("demo", "g_greet", {"id": "bad id"})
    # 删任务清理实例与引用
    svc.delete_task("demo", "g_learn_name_soften")
    assert len(svc.get_instances("dev1", "demo")) == 2
    assert svc.get_playbook("demo")["tasks"][1]["on_failure"] == []


# ── 绑定与运行视角 ────────────────────────────────────────────


def test_binding_and_current_tasks(quest_env):
    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    # 从未设置 → 默认剧本，但只在默认剧本已落盘时生效（bound_playbook 只读不写）
    assert svc.bound_playbook() is None
    from deskbot_server.quest_playbooks_store import ensure_default_playbook

    assert ensure_default_playbook() is True
    assert svc.bound_playbook() == "xiaoy"
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet", "g_care_break", "g_care_neck"]  # 主线在前，日常关心在后
    # 明确关掉主线 → None；日常关心仍在（随主动陪伴生效）
    bind("")
    assert svc.bound_playbook() is None
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_care_break", "g_care_neck"]
    assert svc.get_tool_calls()[0]["available_task_ids"] == ["g_care_break", "g_care_neck"]
    # 绑定 demo：自动初始化实例，起点任务 running
    bind("demo")
    cur = svc.get_current_tasks("any-robot")
    assert [t["task_id"] for t in cur] == ["g_greet", "g_care_break", "g_care_neck"]
    assert cur[0]["playbook"] == "demo" and cur[0]["title"] == "初次问候"
    assert len(svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo")) == 3
    svc.get_current_tasks()
    assert len(svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo")) == 3  # 幂等
    # 一条线：同时只有一个进行中；g_greet 有了结果就轮到 g_learn_name；策略覆盖生效
    svc.update_task_result(svc.LOCAL_PROFILE_DEVICE, "demo", "g_greet", "success", "回应了")
    svc.update_task_strategy(svc.LOCAL_PROFILE_DEVICE, "demo", "g_learn_name", "用户喜欢简洁")
    cur = svc.get_current_tasks()
    assert [t["task_id"] for t in cur] == ["g_learn_name", "g_care_break", "g_care_neck"]
    assert cur[0]["strategy"] == "用户喜欢简洁" and cur[0]["next_title"] == "换个角度"
    calls = svc.get_tool_calls()
    assert [c["name"] for c in calls] == ["update_task_result", "update_task_strategy", "skip_goal", "propose_goal"]
    assert set(calls[0]["available_task_ids"]) == {"g_learn_name", "g_care_break", "g_care_neck"}
    # 沙箱设备不受绑定影响、不参与运行视角
    assert svc.get_current_tasks(svc.DESIGN_SANDBOX_DEVICE) == []
    assert svc.profile_device_id("robot-1") == svc.LOCAL_PROFILE_DEVICE
    assert svc.profile_device_id(svc.DESIGN_SANDBOX_DEVICE) == svc.DESIGN_SANDBOX_DEVICE
    # 绑定不存在的剧本被拒绝；删除已绑定剧本自动解绑
    from deskbot_server.quest_playbooks_store import QuestError

    with pytest.raises(QuestError, match="不存在"):
        bind("ghost")
    svc.delete_playbook("demo")
    assert svc.bound_playbook() is None
    assert svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo") == []


def test_preferences_quest_block_defaults_and_validation(quest_env):
    from deskbot_server.device_preferences import load_preferences, update_preferences

    prefs = load_preferences()
    assert prefs["quest"] == {
        "playbook": None,
        "proactive_enabled": True,
        "idle_sec": 60,
        "daily_limit": 16,
        "care_daily_limit": 10,
        "task_retry_sec": 120,
        "care_pause_sec": 86400,
    }
    saved = update_preferences({"quest": {"proactive_enabled": False}})
    assert saved["quest"]["proactive_enabled"] is False
    assert saved["quest"]["playbook"] is None  # 其它字段保留
    assert _svc().proactive_enabled() is False
    with pytest.raises(ValueError):
        update_preferences({"quest": {"playbook": "Bad Name"}})
    with pytest.raises(ValueError):
        update_preferences({"quest": "nope"})
    with pytest.raises(ValueError):
        update_preferences({"quest": {"idle_sec": 5}})
    with pytest.raises(ValueError):
        update_preferences({"quest": {"daily_limit": -1}})
    update_preferences({"quest": {"idle_sec": 300, "daily_limit": 0}})
    assert _svc().proactive_idle_sec() == 300.0 and _svc().proactive_daily_limit() == 0
    assert _svc().proactive_enabled() is False


# ── 新字段 / 尝试上限 / 可重复 / 剧场结束 / 默认剧场升级 ──────────


def test_normalize_task_new_fields_and_perform_fallback(quest_env):
    from deskbot_server.quest_playbooks_store import normalize_task, validate_playbook

    t = normalize_task({"id": "a", "goal": "g"})
    assert t["trigger_hint"] == "" and t["perform"] == {"mode": "speak", "expression": "", "scene": ""}
    assert t["max_attempts"] == 0 and t["next_delay_sec"] == 10 and t["repeatable"] is False and t["repeat_interval_sec"] == 86400
    # 达成条件只有一段：老数据里单独的「未达成条件」并进来；日常关心不用 next_delay_sec
    merged = normalize_task({"id": "b", "goal": "g", "success_condition": "说了名字", "failure_condition": "不想说"})
    assert merged["success_condition"] == "说了名字（未达成的情况：不想说）" and "failure_condition" not in merged
    assert normalize_task({"id": "c", "goal": "g", "next_delay_sec": 9999})["next_delay_sec"] == 600
    assert normalize_task({"id": "d", "goal": "g", "kind": "care", "next_delay_sec": 30})["next_delay_sec"] == 0
    assert t["proposed"] is False
    t = normalize_task({"id": "a", "goal": "g", "perform": {"mode": "scene", "scene": ""}, "max_attempts": 0,
                        "repeatable": 1, "repeat_interval_sec": 5, "trigger_hint": " x "})
    assert t["perform"]["mode"] == "speak"  # 没选表演 → 退回只说话
    assert t["max_attempts"] == 0 and t["repeatable"] is True and t["repeat_interval_sec"] == 60
    assert t["trigger_hint"] == "x"
    errs = validate_playbook({"name": "d", "tasks": [{"id": "a", "goal": "g", "perform": {"mode": "dance"}, "max_attempts": -1}]})
    assert any("perform.mode" in e for e in errs) and any("max_attempts" in e for e in errs)


def test_attempts_auto_fail_and_repeat_reactivation(quest_env, monkeypatch):
    from datetime import timedelta

    svc = _svc()
    pb = demo_playbook()
    pb["tasks"][0]["max_attempts"] = 2
    pb["tasks"][2]["repeatable"] = True
    pb["tasks"][2]["repeat_interval_sec"] = 3600
    svc.save_playbook("demo", pb)
    svc.ensure_instances("local", "demo")
    # 主线不数次数：提多少次都不会自动按未达成处理，结果只由小歪 / 主人标
    assert svc.record_attempt("local", "demo", "g_greet") == 1
    assert svc.record_attempt("local", "demo", "g_greet") == 2
    assert svc.pause_care_if_missed("local", "demo", "g_greet") is None
    assert {r["task_id"]: r for r in svc.get_instances("local", "demo")}["g_greet"]["status"] == "running"
    # 有了结果后重新开始：清零尝试次数
    svc.update_task_result("local", "demo", "g_greet", "unmet", "没聊成")
    assert svc.restart_task("local", "demo", "g_greet")["attempt_count"] == 0
    # 可重复：终态后过了间隔才重开
    svc.restart_task("local", "demo", "g_learn_name_soften")
    svc.update_task_result("local", "demo", "g_learn_name_soften", "success", "ok")
    assert svc.reactivate_due_repeats("local", "demo") == []
    real = svc.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(hours=2))
    assert svc.reactivate_due_repeats("local", "demo") == ["g_learn_name_soften"]
    rows = {r["task_id"]: r for r in svc.get_instances("local", "demo")}
    assert rows["g_learn_name_soften"]["status"] == "running"


def test_playbook_finished_and_propose_task(quest_env):
    from deskbot_server.quest_playbooks_store import QuestError

    svc = _svc()
    svc.save_playbook("demo", demo_playbook())
    svc.ensure_linear_playbook("demo")  # 老的分支编排压成一条线
    svc.ensure_instances("local", "demo")
    assert svc.playbook_finished("local", "demo") is False
    svc.update_task_result("local", "demo", "g_greet", "failed", "no")  # 未达成也进入下一个
    assert svc.playbook_finished("local", "demo") is False
    svc.update_task_result("local", "demo", "g_learn_name", "failed", "no")
    svc.update_task_result("local", "demo", "g_learn_name_soften", "failed", "no")  # 最后一个有了结果 → 走完
    assert svc.playbook_finished("local", "demo") is True
    # 提一个小目标（提议住在日常关心场景）：proposed、不进入运行、不算入"走完"判断
    bind("demo")
    clear_care_scene()  # 只看主线：走完了就没有进行中的小目标
    t = svc.propose_task("care", {"title": "关心作息", "goal": "别熬夜"})
    assert t["id"] == "p1" and t["proposed"] is True and t["initial_status"] == "not_started"
    assert svc.playbook_finished("local", "demo") is True
    assert svc.get_current_tasks() == []
    with pytest.raises(QuestError, match="等主人"):
        svc.propose_task("care", {"title": "x", "goal": "y"})
    with pytest.raises(QuestError, match="goal"):
        svc.propose_task("care", {"title": "x"})


def test_default_playbook_upgrades_by_template_version(quest_env):
    from deskbot_server.quest_playbooks_store import (
        ensure_default_playbook,
        load_playbook,
        write_playbook,
    )

    write_playbook("xiaoy", {"name": "xiaoy", "title": "旧的", "tasks": []})
    assert ensure_default_playbook() is True  # 没有版本号 → 升级
    pb = load_playbook("xiaoy")
    assert pb["title"] == "与主人的初识" and pb["template_version"] == 9 and len(pb["tasks"]) == 5 and pb["check_interval_hours"] == 1
    from deskbot_server.quest_playbooks_store import is_linear

    assert is_linear(pb) and [t["id"] for t in pb["tasks"]][:2] == ["g_greet", "g_learn_name"]
    care = load_playbook("care")
    assert care and care["is_care_scene"] is True and [t["id"] for t in care["tasks"]] == ["g_care_break", "g_care_neck"]
    assert ensure_default_playbook() is False  # 已是最新
    # 单选链：每个小目标最多一个达成后 / 未达成后
    for t in pb["tasks"]:
        assert len(t["on_success"]) <= 1 and len(t["on_failure"]) <= 1
