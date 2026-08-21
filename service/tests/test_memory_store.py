from __future__ import annotations

import json

import pytest


@pytest.fixture()
def memory_file(monkeypatch, tmp_path):
    path = tmp_path / "local" / "memories.json"
    monkeypatch.setattr(
        "deskbot_server.memory_store.resolve_json_path",
        lambda _default: str(path),
    )
    return path


def test_legacy_memory_filename_is_migrated_and_normalized(memory_file):
    from deskbot_server.memory_store import list_memory_entries

    legacy = memory_file.with_name("user_memory.json")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "legacy-memory",
                        "text": "kept locally",
                        "created_at": 1,
                        "device_id": "old-hardware-scope",
                        "owner_user_id": "old-account-scope",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert list_memory_entries() == [
        {
            "id": "legacy-memory",
            "text": "kept locally",
            "created_at": 1.0,
        }
    ]
    assert memory_file.is_file()
    assert not legacy.exists()
    stored = json.loads(memory_file.read_text(encoding="utf-8"))
    assert stored["entries"] == list_memory_entries()


def test_existing_memories_file_is_never_overwritten_by_legacy(memory_file):
    from deskbot_server.memory_store import list_memory_entries

    memory_file.parent.mkdir(parents=True)
    memory_file.write_text(
        '{"entries":[{"id":"current","text":"new data","created_at":2}]}',
        encoding="utf-8",
    )
    legacy = memory_file.with_name("user_memory.json")
    legacy.write_text(
        '{"entries":[{"id":"legacy","text":"old data","created_at":1}]}',
        encoding="utf-8",
    )

    assert [row["id"] for row in list_memory_entries()] == ["current"]
    assert legacy.is_file()


def test_memory_crud_is_pc_local(memory_file):
    from deskbot_server.memory_store import (
        add_memory,
        delete_memory,
        get_memory,
        list_memory_entries,
        update_memory,
    )

    first = add_memory("likes cats")
    second = add_memory("lives in Shanghai")
    assert len(list_memory_entries()) == 2
    assert get_memory(first["id"])["text"] == "likes cats"
    assert update_memory(first["id"], "likes dogs")["text"] == "likes dogs"
    assert delete_memory(second["id"])
    assert get_memory(second["id"]) is None
    assert len(list_memory_entries()) == 1
    assert "device_id" not in first
    assert "owner_user_id" not in first


def test_memory_api_has_no_hardware_scope(memory_file):
    import inspect

    from deskbot_server.memory_store import (
        add_memory,
        get_memory,
        list_memory_entries,
    )

    assert "device_id" not in inspect.signature(add_memory).parameters
    assert "device_id" not in inspect.signature(get_memory).parameters
    saved = add_memory("shared memory")
    assert get_memory(saved["id"])["text"] == "shared memory"
    assert [row["id"] for row in list_memory_entries()] == [
        saved["id"]
    ]
    assert memory_file.is_file()
    assert list(memory_file.parent.glob("hardware-*")) == []


def test_concurrent_memory_adds_do_not_lose_updates(memory_file):
    from concurrent.futures import ThreadPoolExecutor

    from deskbot_server.memory_store import add_memory, list_memory_entries

    with ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(lambda index: add_memory(f"memory-{index}"), range(40)))

    stored = list_memory_entries(limit=200)
    assert len(rows) == len(stored) == 40
    assert {row["text"] for row in stored} == {
        f"memory-{index}" for index in range(40)
    }


def test_memory_limits(memory_file, monkeypatch):
    import deskbot_server.memory_store as store

    with pytest.raises(ValueError, match="4096"):
        store.add_memory("x" * 4097)

    monkeypatch.setattr(store, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(store, "_MAX_PROMPT_TEXT_BYTES", 10)
    store.add_memory("123456")
    store.add_memory("abcdef")
    rows = store.list_memories(limit=30)
    assert len(rows) == 1
    assert len(rows[0]["text"].encode("utf-8")) <= 10
