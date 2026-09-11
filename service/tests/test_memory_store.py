from __future__ import annotations

import json

import pytest


@pytest.fixture()
def memory_home(monkeypatch, tmp_path):
    """Point both the retired JSON file and the Markdown store at tmp_path."""
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "deskbot_server.memory_md.local_data_dir",
        lambda: local,
    )
    monkeypatch.setattr(
        "deskbot_server.memory_store.resolve_json_path",
        lambda _default: str(local / "memories.json"),
    )
    return local


def test_legacy_json_library_is_migrated_into_the_master_file(memory_home):
    from deskbot_server import memory_md
    from deskbot_server.memory_store import list_memory_entries

    (memory_home / "memories.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"id": "aaa111", "text": "主人是左撇子", "created_at": 1.0},
                    {"id": "bbb222", "text": "怕吵", "created_at": 2.0},
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = list_memory_entries()
    assert [row["text"] for row in rows] == ["主人是左撇子", "怕吵"]
    assert all(row["scope"] == "master" for row in rows)
    assert memory_md.master_path().is_file()


def test_migration_never_overwrites_an_existing_master_file(memory_home):
    from deskbot_server import memory_md
    from deskbot_server.memory_store import list_memory_entries

    memory_md.write_master([{"id": "kept01", "text": "已经整理过的记忆"}])
    (memory_home / "memories.json").write_text(
        json.dumps(
            {"entries": [{"id": "old999", "text": "旧库内容", "created_at": 1.0}]}
        ),
        encoding="utf-8",
    )

    assert [row["id"] for row in list_memory_entries()] == ["kept01"]


def test_new_memories_land_in_todays_file_and_crud_round_trips(memory_home):
    from deskbot_server import memory_md
    from deskbot_server.memory_store import (
        add_memory,
        delete_memory,
        get_memory,
        list_memory_entries,
        update_memory,
    )

    first = add_memory("likes cats")
    second = add_memory("hates thunder")

    assert first["scope"] == "daily"
    assert first["date"] == memory_md.today_str()
    assert len(memory_md.read_daily(memory_md.today_str())) == 2

    assert get_memory(first["id"])["text"] == "likes cats"
    assert update_memory(first["id"], "likes dogs")["text"] == "likes dogs"
    assert delete_memory(second["id"])
    assert get_memory(second["id"]) is None
    assert len(list_memory_entries()) == 1

    # 记忆属于这台 PC，不带任何硬件维度。
    assert "device_id" not in first
    assert "owner_user_id" not in first


def test_master_entries_are_deletable_too(memory_home):
    from deskbot_server import memory_md
    from deskbot_server.memory_store import delete_memory, list_memory_entries

    memory_md.write_master(
        [{"id": "m00001", "text": "长期事实"}, {"id": "m00002", "text": "另一条"}]
    )

    assert delete_memory("m00001")
    assert [row["id"] for row in list_memory_entries()] == ["m00002"]


def test_memory_api_has_no_hardware_scope(memory_home):
    import inspect

    from deskbot_server.memory_store import add_memory, get_memory

    assert "device_id" not in inspect.signature(add_memory).parameters
    assert "device_id" not in inspect.signature(get_memory).parameters
    saved = add_memory("shared memory")
    assert get_memory(saved["id"])["text"] == "shared memory"


def test_concurrent_memory_adds_do_not_lose_updates(memory_home):
    from concurrent.futures import ThreadPoolExecutor

    from deskbot_server.memory_store import add_memory, list_memory_entries

    with ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(lambda index: add_memory(f"memory-{index}"), range(40)))

    stored = list_memory_entries(limit=200)
    assert len(rows) == len(stored) == 40
    assert {row["text"] for row in stored} == {
        f"memory-{index}" for index in range(40)
    }


def test_oversized_entries_are_refused(memory_home):
    from deskbot_server.memory_store import add_memory

    with pytest.raises(ValueError, match="4096"):
        add_memory("x" * 4097)


def test_new_memories_reach_the_prompt_core_immediately(memory_home):
    """值得记的事即时进入长期记忆，不等每晚整理。"""
    from deskbot_server import memory_md
    from deskbot_server.memory_store import add_memory, memory_prompt_text

    memory_md.write_master([{"id": "core01", "text": "主人对花生过敏"}])
    entry = add_memory("主人喜欢吃菠萝")

    # 双写：每日流水账留档，长期记忆立刻可见，id 一致。
    master_ids = {row["id"] for row in memory_md.read_master()}
    assert entry["id"] in master_ids
    daily_ids = {r["id"] for r in memory_md.read_daily(memory_md.today_str())}
    assert entry["id"] in daily_ids

    text = memory_prompt_text()
    assert "主人对花生过敏" in text
    assert "主人喜欢吃菠萝" in text
    # 已进核心的条目不在"最近几天"重复出现，避免同一事实喂两遍。
    assert text.count("主人喜欢吃菠萝") == 1


def test_deleting_a_realtime_memory_clears_both_copies(memory_home):
    """删除必须两处同清，否则每晚整理会把每日残留提炼回长期记忆。"""
    from deskbot_server import memory_md
    from deskbot_server.memory_store import add_memory, delete_memory

    entry = add_memory("临时的事")
    assert delete_memory(entry["id"])
    assert all(r["id"] != entry["id"] for r in memory_md.read_master())
    assert all(
        r["id"] != entry["id"]
        for r in memory_md.read_daily(memory_md.today_str())
    )


def test_daily_files_are_pruned_to_the_retention_window(memory_home):
    from deskbot_server import memory_md

    for day in range(1, 6):
        memory_md.write_daily(f"2026-01-{day:02d}", [{"id": f"d{day}", "text": "x"}])

    assert memory_md.prune_daily_files(keep=2) == 3
    assert memory_md.list_daily_dates() == ["2026-01-05", "2026-01-04"]
