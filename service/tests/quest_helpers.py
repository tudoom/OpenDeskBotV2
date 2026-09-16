"""剧本任务测试共用夹具：临时剧本目录 + 临时 sqlite + 临时偏好目录。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def quest_env(tmp_path, monkeypatch):
    """临时剧本目录 + 临时数据库 + 临时本地档案目录（偏好文件）。"""
    db_path = tmp_path / "quest.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    from deskbot_server import device_data, quest_playbooks_store
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    quest_playbooks_store.configure_playbooks_dir(tmp_path / "playbooks")
    reset_engine()
    init_engine(db_path)
    init_database()
    from deskbot_server.application import quest_service as _qs

    _qs._care_migrated_names.clear()
    _qs._linearized_names.clear()
    _qs._status_migrated_keys.clear()
    try:
        yield tmp_path
    finally:
        quest_playbooks_store.configure_playbooks_dir(None)
        reset_engine()


def demo_playbook(name: str = "demo") -> dict:
    """三段剧本：起点问候 →(成功 10 分)→ 了解姓名 →(失败 6 分)→ 换角度。"""
    return {
        "name": name,
        "tasks": [
            {
                "id": "g_greet",
                "title": "初次问候",
                "goal": "主动向用户问好",
                "strategy": "轻松自然地打招呼",
                "activation_score": 100,
                "initial_status": "running",
                "success_condition": "用户回应了问候",
                "failure_condition": "用户没有回应",
                "on_success": [{"id": "g_learn_name", "score": 10}],
                "on_failure": [],
                "pos": {"x": 120, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_learn_name",
                "title": "了解姓名",
                "goal": "知道用户的名字",
                "strategy": "自然地问，记住并下次称呼",
                "activation_score": 10,
                "initial_status": "not_started",
                "success_condition": "用户明确告诉了我他的名字",
                "failure_condition": "用户表示不想说",
                "on_success": [],
                "on_failure": [{"id": "g_learn_name_soften", "score": 6}],
                "pos": {"x": 460, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_learn_name_soften",
                "title": "换个角度",
                "goal": "换个角度接近用户",
                "strategy": "不再问名字，聊用户感兴趣的日常",
                "activation_score": 6,
                "initial_status": "not_started",
                "success_condition": "用户愿意闲聊了",
                "failure_condition": "用户始终冷淡",
                "on_success": [],
                "on_failure": [],
                "pos": {"x": 460, "y": 380, "width": 200, "height": 96},
            },
        ],
    }


def clear_care_scene() -> None:
    """把随包「定时提醒」场景清空，让只关心主线的用例不受它影响。"""
    from deskbot_server.application.quest_service import save_playbook

    save_playbook("care", {"name": "care", "title": "定时提醒", "is_care_scene": True, "template_version": 999, "tasks": []})


def care_scene_with(tasks: list[dict]) -> None:
    """把给定的定时提醒写进「定时提醒」场景（替换原有）。"""
    from deskbot_server.application.quest_service import save_playbook

    save_playbook("care", {"name": "care", "title": "定时提醒", "is_care_scene": True, "template_version": 999, "tasks": tasks})


def bind(name: str | None) -> None:
    from deskbot_server.application.quest_service import set_bound_playbook

    set_bound_playbook(name)
