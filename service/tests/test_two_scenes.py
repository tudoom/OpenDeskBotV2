"""两个默认场景：主线（初识）+「日常关心」。主线同时只开一个；日常关心不带开关、随主动陪伴生效、不能删、不会「走完」；
散在别的场景里的日常关心自动搬进来（连进度一起）。"""

from __future__ import annotations

from tests.quest_helpers import bind, demo_playbook, quest_env  # noqa: F401


def _cards(client):
    return {c["name"]: c for c in client.get("/api/quest/overview").get_json()["playbooks"]}


def test_only_one_story_scene_enabled_and_care_always_on(quest_env):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    cards = _cards(client)
    assert [n for n, c in cards.items() if c["enabled"] and not c["is_care_scene"]] == ["xiaoy"]
    assert cards["care"]["enabled"] is True and cards["care"]["is_care_scene"] is True
    # 新建一条主线 → 它开启、原来的自动关掉（同时只开一个）
    other = client.post("/api/quest/playbooks", json={"title": "第二条主线", "template": "blank"}).get_json()["playbook"]["name"]
    cards = _cards(client)
    assert len([n for n, c in cards.items() if not c["is_care_scene"]]) == 2
    assert cards[other]["enabled"] is True and cards["xiaoy"]["enabled"] is False and cards["care"]["enabled"] is True
    ov = client.put("/api/quest/settings", json={"playbook": "xiaoy"}).get_json()
    assert ov["playbook"]["name"] == "xiaoy"
    cards = _cards(client)
    assert cards["xiaoy"]["enabled"] is True and cards[other]["enabled"] is False
    # 全部关掉 → 没有主线在推进；日常关心照旧
    client.put("/api/quest/settings", json={"playbook": ""})
    cards = _cards(client)
    assert not any(c["enabled"] for c in cards.values() if not c["is_care_scene"]) and cards["care"]["enabled"] is True
    # 卡片顺序：开启的主线在前，日常关心紧随其后，其余在后
    client.put("/api/quest/settings", json={"playbook": other})
    names = [c["name"] for c in client.get("/api/quest/overview").get_json()["playbooks"]]
    assert names[:2] == [other, "care"]


def test_care_scene_cannot_be_deleted_or_finished(quest_env):
    import pytest

    from deskbot_server.application import quest_service as svc
    from deskbot_server.quest_playbooks_store import QuestError

    assert svc.care_playbook() == "care"  # 随包生成
    with pytest.raises(QuestError):
        svc.delete_playbook("care")
    svc.ensure_instances("local", "care")
    for t in (svc.get_playbook("care") or {}).get("tasks") or []:
        svc.update_task_result("local", "care", t["id"], "success", "做到了")
    assert svc.playbook_finished("local", "care") is False


def test_stray_care_goals_migrate_with_progress(quest_env):
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import QuestInstance

    pb = demo_playbook()
    pb["tasks"].append({"id": "c_water", "kind": "care", "title": "喝水", "goal": "提醒喝水", "activation_score": 1, "initial_status": "running"})
    pb["tasks"][0]["on_success"] = [{"id": "c_water", "score": 1}]  # 旧数据里主线连到了日常关心
    svc.save_playbook("demo", pb)
    bind("demo")
    svc.ensure_instances("local", "demo")
    svc.record_attempt("local", "demo", "c_water")
    svc._care_migrated_names.discard("demo")
    assert svc.migrate_care_tasks_into_care_scene() == 1
    assert "c_water" not in {t["id"] for t in svc.get_playbook("demo")["tasks"]}
    assert svc.get_task_definition("demo", "g_greet")["on_success"] == []  # 连线清掉
    care = {t["id"]: t for t in svc.get_playbook("care")["tasks"]}
    assert care["c_water"]["kind"] == "care" and care["c_water"]["on_success"] == []
    session = get_session()
    try:
        rows = session.query(QuestInstance).filter(QuestInstance.task_id == "c_water").all()
        assert [(r.playbook, r.attempt_count) for r in rows] == [("care", 1)]  # 进度跟着搬
    finally:
        session.close()
    assert "c_water" in {t["id"] for t in qc.overview()["care_tasks"]}
    # 主线里改成日常关心 → 立刻搬走
    qc.update_task("demo", "g_learn_name", {"kind": "care"})
    assert svc.get_task_definition("demo", "g_learn_name") is None
    assert "g_learn_name" in {t["id"] for t in svc.get_playbook("care")["tasks"]}
