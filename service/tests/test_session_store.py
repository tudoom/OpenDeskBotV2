from __future__ import annotations

import json

import pytest


@pytest.fixture()
def session_env(tmp_path, monkeypatch):
    import deskbot_server.session_store as store
    from deskbot_server import device_data

    data_dir = tmp_path / "data"
    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    return store, data_dir / "local" / "session"


def test_create_append_and_reload_local_session(session_env):
    store, root = session_env
    created = store.create_session(title="hello", now=100)
    updated = store.append_turn(
        created["session_id"],
        "Hi",
        "Hello",
        now=101,
        request_id="request-1",
    )

    assert [row["role"] for row in updated["messages"]] == ["user", "assistant"]
    assert store.get_current_session()["session_id"] == created["session_id"]
    assert (root / f"{created['session_id']}.json").is_file()
    assert "device_id" not in updated
    assert "owner_user_id" not in updated


def test_hardware_id_cannot_select_a_second_session_namespace(session_env):
    store, root = session_env
    session = store.create_session(title="shared", now=100)

    # Old callers may still carry transport metadata, but it is not persisted.
    payload = json.loads((root / f"{session['session_id']}.json").read_text("utf-8"))
    assert "device_id" not in payload
    assert "owner_user_id" not in payload
    assert list(root.parent.glob("hardware-*")) == []


def test_idle_timeout_rotates_the_single_current_session(session_env):
    store, _root = session_env
    first = store.create_session(title="first", now=100)
    same = store.ensure_active_session(user_text="still here", now=101)
    assert same["session_id"] == first["session_id"]

    rotated = store.ensure_active_session(
        user_text="new conversation",
        now=100 + store.SESSION_IDLE_SECONDS + 1,
    )
    assert rotated["session_id"] != first["session_id"]
    assert store.get_current_session()["session_id"] == rotated["session_id"]


def test_llm_history_excludes_unplayed_assistant_messages(session_env):
    store, _root = session_env
    session = store.create_session(now=100)
    store.append_turn(
        session["session_id"],
        "first",
        "not heard",
        now=101,
        assistant_delivery_status="failed",
    )
    store.append_turn(
        session["session_id"],
        "second",
        "heard",
        now=102,
        assistant_delivery_status="played",
    )
    assert store.session_history_for_llm(session["session_id"]) == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "heard"},
    ]


def test_delivery_receipt_updates_matching_assistant_turn(session_env):
    store, _root = session_env
    session = store.create_session(now=100)
    store.append_turn(
        session["session_id"],
        "question",
        "answer",
        request_id="req-1",
        assistant_delivery_status="enqueued",
        now=101,
    )
    assert store.update_assistant_delivery(
        session["session_id"], "req-1", "played", now=102
    )
    rows = store.load_session(session["session_id"])["messages"]
    assert rows[-1]["delivery_status"] == "played"


def test_session_tool_uses_local_namespace_without_device_argument(session_env):
    store, _root = session_env
    session = store.create_session(title="tool session")
    current = store.execute_session_tool({"action": "current"})
    listed = store.execute_session_tool({"action": "list"})
    loaded = store.execute_session_tool(
        {"action": "get", "session_id": session["session_id"]}
    )
    assert current["session"]["session_id"] == session["session_id"]
    assert listed["sessions"][0]["session_id"] == session["session_id"]
    assert loaded["session"]["session_id"] == session["session_id"]


def test_clear_delete_and_count(session_env):
    store, _root = session_env
    first = store.create_session(title="first", now=100)
    second = store.create_session(title="second", now=101)
    assert store.count_sessions() == 2
    assert store.clear_current_session() is True
    assert store.get_current_session() is None
    assert store.delete_session(first["session_id"]) is True
    assert store.delete_session(first["session_id"]) is False
    assert store.count_sessions() == 1
    assert store.load_session(second["session_id"]) is not None


def test_invalid_session_id_is_rejected(session_env):
    store, _root = session_env
    with pytest.raises(ValueError, match="invalid session_id"):
        store.load_session("../escape")
