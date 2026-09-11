"""内置表演：随包五段（新年祝福 / 生日祝福 / 早上好 / 小歪蹦迪 / 求关注）+ 录制的姿态时间线；
旧「演示问候」升级时移除；用户改过的内置表演保留；内置不可删（页面隐藏删除）。"""

from __future__ import annotations

import json

import pytest

from tests.quest_helpers import quest_env  # noqa: F401

BASE_FIVE = {"new_year", "birthday", "good_morning", "disco", "want_attention"}


@pytest.fixture()
def store_env(quest_env, monkeypatch):
    """临时 data 目录：global 无 seed 文件 → 走 Python 兜底五段；local 由测试写。"""
    from deskbot_server import scene_playbooks_store as store

    yield store


def test_seed_file_and_python_fallback_agree_on_the_five():
    from deskbot_server import scene_playbooks_store as store

    seed = json.loads(open("data/global/scene_playbooks.json", encoding="utf-8").read())
    names = [r["name"] for r in seed]
    assert names[:5] == ["disco", "want_attention", "new_year", "birthday", "good_morning"]
    assert "demo_greet" not in names
    assert {r["name"] for r in store._BASE_BUILTIN_PLAYBOOKS} == BASE_FIVE
    # 每段都能规格化，且只用随包表情与舵机预设
    face = json.loads(open("data/global/deskbot-face.json", encoding="utf-8").read())
    emotions = {e["name"] for e in face["emotions"]}
    servo = json.loads(open("data/global/servo.json", encoding="utf-8").read())
    presets = {p["id"] for p in servo["presets"]}
    for row in store._BASE_BUILTIN_PLAYBOOKS:
        pb = store.normalize_playbook(row)
        assert pb["chunks"], row["name"]
        for chunk in pb["chunks"]:
            for anim in chunk.get("anims") or []:
                assert anim["anim"] in emotions, (row["name"], anim)
            for move in chunk.get("moves") or []:
                assert move["move"] in presets, (row["name"], move)
        assert any(c["text"] for c in pb["chunks"]), row["name"]  # 每段至少说一句


def test_fresh_install_seeds_builtins_and_marks_them(store_env):
    store = store_env
    rows = store.load_scene_playbooks_file()
    names = [r["name"] for r in rows]
    assert BASE_FIVE <= set(names) and "demo_greet" not in names
    assert all(r["builtin"] is True for r in rows)
    assert store._read_builtin_version() == store.BUILTIN_PLAYBOOKS_VERSION
    # 再读不再改文件（只同步一次）
    mtime = store._STORE.path().stat().st_mtime_ns
    store.load_scene_playbooks_file()
    assert store._STORE.path().stat().st_mtime_ns == mtime


def test_upgrade_removes_demo_greet_adds_missing_and_keeps_user_edits(store_env):
    store = store_env
    legacy = [
        {"name": "demo_greet", "title": "演示问候", "chunks": [{"id": "c1", "text": "你好", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "birthday", "title": "我的生日版", "chunks": [{"id": "c1", "text": "生日快乐呀老板", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "mine", "title": "自己编的", "chunks": [{"id": "c1", "text": "自定义", "servo": {"preset": "center", "ms": 500}}]},
    ]
    store._STORE.path().parent.mkdir(parents=True, exist_ok=True)
    store._STORE.path().write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    rows = store.load_scene_playbooks_file()
    by = {r["name"]: r for r in rows}
    assert "demo_greet" not in by
    assert by["birthday"]["title"] == "我的生日版" and by["birthday"]["builtin"] is True  # 改过的保留
    # 内置排最前，蹦迪、求关注是第一第二；用户自己的跟在后面
    assert [r["name"] for r in rows][:5] == ["disco", "want_attention", "new_year", "birthday", "good_morning"]
    assert [r["name"] for r in rows][-1] == "mine"
    assert by["mine"]["builtin"] is False and by["mine"]["title"] == "自己编的"
    assert {"new_year", "good_morning", "disco", "want_attention"} <= set(by)
    # 落盘了；再读不会把用户删掉的内置重新加回来（版本已写）
    on_disk = [r["name"] for r in json.loads(store._STORE.path().read_text(encoding="utf-8"))]
    assert "demo_greet" not in on_disk and "new_year" in on_disk
    trimmed = [r for r in json.loads(store._STORE.path().read_text(encoding="utf-8")) if r["name"] != "disco"]
    store._STORE.path().write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    assert "disco" not in {r["name"] for r in store.load_scene_playbooks_file()}


def test_five_stay_first_even_when_machine_global_seed_is_stale(store_env):
    """2026-09-11 用户机器：data/global/scene_playbooks.json 还是 8 月 31 日的旧版（只有录制的动作，没有五段），
    内置排名一跟着它走，蹦迪 / 求关注 / 新年 / 生日就掉到列表最后。顺序必须固定：五段最前，录制的跟在后面。"""
    store = store_env
    stale_global = [
        {"name": "focused", "title": "专注", "chunks": [{"id": "c1", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "shy", "title": "害羞", "chunks": [{"id": "c1", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "demo_greet", "title": "演示问候", "chunks": [{"id": "c1", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "birthday", "title": "全局版生日", "chunks": [{"id": "c1", "text": "全局版", "expr": {"scene": "happy", "ms": 800}}]},
    ]
    gdir = store.global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "scene_playbooks.json").write_text(json.dumps(stale_global, ensure_ascii=False), encoding="utf-8")
    names = [r["name"] for r in store.builtin_playbooks()]
    assert names == ["disco", "want_attention", "new_year", "birthday", "good_morning", "focused", "shy"]
    # 同名时内容仍以 global 文件为准，只是顺序固定
    assert next(r for r in store.builtin_playbooks() if r["name"] == "birthday")["title"] == "全局版生日"
    # 用户 local 文件是老顺序（录制的在前、五段在后）：读出来也是五段最前，用户自己建的最后
    local = [dict(r) for r in stale_global if r["name"] != "demo_greet"] + [
        {"name": "disco", "title": "小歪蹦迪", "chunks": [{"id": "c1", "expr": {"scene": "happy", "ms": 800}}]},
        {"name": "mine", "title": "自己编的", "chunks": [{"id": "c1", "servo": {"preset": "center", "ms": 500}}]},
    ]
    store._STORE.path().parent.mkdir(parents=True, exist_ok=True)
    store._STORE.path().write_text(json.dumps(local, ensure_ascii=False), encoding="utf-8")
    rows = store.load_scene_playbooks_file()
    assert [r["name"] for r in rows] == ["disco", "want_attention", "new_year", "birthday", "good_morning", "focused", "shy", "mine"]


def test_performances_page_marks_builtin_and_is_named_biaoyan(quest_env):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/playbooks").get_data(as_text=True)
    assert "<h1>表演</h1>" in html and 'data-label="表演"' in html and "组合表演" not in html
    assert 'v-if="pb.builtin"' in html and 'v-if="!pb.builtin" @click="removePlaybook' in html
