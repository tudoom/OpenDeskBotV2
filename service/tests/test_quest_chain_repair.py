"""一条线的编排：模板升级保留主人加的小目标并接回原位；拖动排序后当前按「第一个没结果的」重算；
最后一个有结果就走完；「从这个开始」把前面没结果的记为跳过；等 N 天没结果自动往下走。"""

from __future__ import annotations

import json
from datetime import timedelta

from tests.quest_helpers import bind, clear_care_scene, demo_playbook, quest_env  # noqa: F401


def _mark(svc, pb, tid, status, note="ok"):
    svc.update_task_result("local", pb, tid, status, note)


def test_template_upgrade_keeps_user_goals_in_place(quest_env):
    from deskbot_server import quest_playbooks_store as store

    tmpl = store.load_default_playbook_template()
    old = json.loads(json.dumps(tmpl))
    old["template_version"] = 1
    # 主人在「第一次打招呼」之后插了一个自己的小目标，还加了一条日常关心（老版本住在主线里）
    old["tasks"].append({"id": "t9", "title": "问问今天吃了什么", "goal": "知道主人吃了什么", "on_success": [{"id": "g_learn_name", "score": 1}], "activation_score": 1})
    old["tasks"].append({"id": "p1", "kind": "care", "title": "活动颈椎", "goal": "提醒写代码间隙活动颈椎", "activation_score": 1})
    old["tasks"].append({"id": "g_quiet_company", "title": "安静陪伴", "goal": "主人不想说话时安静陪着", "activation_score": 1})  # v6 的备用分支，v7 已并进别处
    for t in old["tasks"]:
        if t["id"] == "g_greet":
            t["on_success"] = [{"id": "t9", "score": 1}]
    store.write_playbook("xiaoy", store.normalize_playbook("xiaoy", old))
    assert store.ensure_default_playbook() is True
    pb = store.load_playbook("xiaoy")
    by = {t["id"]: t for t in pb["tasks"]}
    assert pb["template_version"] == tmpl["template_version"]
    assert "t9" in by and "p1" in by and by["p1"]["kind"] == "care"  # 主人加的都还在
    assert "g_quiet_company" not in by  # 模板退役的分支不再当成主人加的
    assert [r["id"] for r in by["g_greet"]["on_success"]] == ["t9"] and [r["id"] for r in by["t9"]["on_success"]] == ["g_learn_name"]  # 接回原位
    assert [r["id"] for r in by["g_greet"]["on_failure"]] == [r["id"] for r in {t["id"]: t for t in store.normalize_playbook("xiaoy", tmpl)["tasks"]}["g_greet"]["on_failure"]]
    # 模板自己的小目标按新模板换（老版本里没有的新小目标出现了）
    assert set(t["id"] for t in tmpl["tasks"]) <= set(by)
    # 再跑一次不再改
    assert store.ensure_default_playbook() is False
    # 压成一条线后，主人加的那一步仍在原位（打招呼 → 自定义 → 称呼）
    from deskbot_server.application import quest_service as svc

    svc.ensure_linear_playbook("xiaoy")
    pb = store.load_playbook("xiaoy")
    assert store.is_linear(pb) and [t["id"] for t in store.story_sequence(pb)][:3] == ["g_greet", "t9", "g_learn_name"]


def test_reorder_moves_cursor_and_results_stick(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet"]
    _mark(svc, "demo", "g_greet", "success")
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_learn_name"]
    # 把最后一步拖到最前：它成了当前，原来的当前退回等待中，已有的结果不动
    qc.reorder_tasks("demo", ["g_learn_name_soften", "g_greet", "g_learn_name"])
    by = {t["id"]: t for t in qc.overview()["tasks"]}
    assert [t["id"] for t in qc.overview()["tasks"]] == ["g_learn_name_soften", "g_greet", "g_learn_name"]
    assert by["g_learn_name_soften"]["status"] == "running" and by["g_learn_name"]["status"] == "not_started" and by["g_greet"]["status"] == "success"
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_learn_name_soften"]
    # 它有了结果 → 跳过已有结果的 g_greet，轮到 g_learn_name
    qc.mark_task("g_learn_name_soften", "success")
    by = {t["id"]: t for t in qc.overview()["tasks"]}
    assert by["g_learn_name"]["status"] == "running"
    qc.mark_task("g_learn_name", "failed")
    assert qc.overview()["finished"] is True and svc.get_current_tasks() == []


def test_linear_finish_wording_and_start_from(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.web.app import create_app

    clear_care_scene()
    ov = qc.overview()  # 默认场景：一条线五步
    assert [t["order"] for t in ov["tasks"]] == [1, 2, 3, 4, 5] and ov["tasks"][0]["status"] == "running"
    out = qc.start_from("g_learn_daily")
    assert out["skipped"] == ["g_greet", "g_learn_name", "g_daily_mood"]
    by = {t["id"]: t for t in qc.overview()["tasks"]}
    assert by["g_learn_daily"]["status"] == "running" and by["g_daily_mood"]["status"] == "skipped" and "跳到了后面" in by["g_daily_mood"]["result"]
    for tid in ("g_learn_daily", "g_learn_hobby"):
        qc.mark_task(tid, "success")
    card = qc.overview()["playbooks"][0]
    assert card["finished"] is True and card["settled"] is True and card["task_count"] == 5
    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/quest").get_data(as_text=True)
    assert "都达成或跳过了" in html and "qs-handle" in html and "从这个开始" in html and "/order`" in html
    assert "未达成后" not in html and "这次没走到" not in html and "可重复</label>" not in html


def test_step_never_times_out_only_explicit_results_move_on(quest_env, monkeypatch):
    """2026-09-10：不再有「等几天没结果就往下走」——没人标结果就一直是这一步（定时检测会再提它）。"""
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    pb = demo_playbook()
    svc.save_playbook("demo", pb)
    bind("demo")
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet"]
    real = svc.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: real + timedelta(days=30))
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["g_greet"]
    rows = {r["task_id"]: r for r in svc.get_instances("local", "demo")}
    assert rows["g_greet"]["status"] == "running" and "max_wait_days" not in svc.get_task_definition("demo", "g_greet")
