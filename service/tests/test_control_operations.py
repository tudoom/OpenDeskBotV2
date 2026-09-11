from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, update


@pytest.fixture()
def operations_db(tmp_path, monkeypatch):
    db_path = tmp_path / "operations.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DESKBOT_CONTROL_OPERATION_CLEANUP_INTERVAL_SEC", "86400")
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield
    finally:
        reset_engine()


def test_operation_id_is_pc_global_and_idempotent(operations_db):
    from deskbot_server.application.control_operations import accept_control_operation

    first, created = accept_control_operation(
        device_id="hardware-a",
        kind="device_tts",
        payload={"text": "hello"},
        operation_id="same-operation",
        request_id="request-a",
    )
    replay, replay_created = accept_control_operation(
        device_id="hardware-a",
        kind="device_tts",
        payload={"text": "hello"},
        operation_id="same-operation",
        request_id="request-b",
    )
    assert created is True
    assert replay_created is False
    assert replay.operation_id == first.operation_id
    assert replay.request_id == first.request_id
    assert not hasattr(replay, "owner_user_id")


def test_reusing_operation_id_for_another_routing_target_conflicts(operations_db):
    from deskbot_server.application.control_operations import (
        ControlOperationConflict,
        accept_control_operation,
    )

    accept_control_operation(
        device_id="hardware-a",
        kind="device_tts",
        payload={"text": "hello"},
        operation_id="bound-operation",
    )
    with pytest.raises(ControlOperationConflict):
        accept_control_operation(
            device_id="hardware-b",
            kind="device_tts",
            payload={"text": "hello"},
            operation_id="bound-operation",
        )


def test_operation_lifecycle_has_durable_terminal_result(operations_db):
    from deskbot_server.application.control_operations import (
        accept_control_operation,
        finish_control_operation,
        get_control_operation,
        heartbeat_control_operation,
        mark_control_operation_running,
    )

    accepted, _ = accept_control_operation(
        device_id="hardware-a",
        kind="device_servo",
        payload={"angle": 12},
        operation_id="lifecycle",
    )
    assert accepted.status == "accepted"

    running = mark_control_operation_running(operation_id="lifecycle")
    assert running is not None
    assert running.status == "running"
    assert running.lease_token
    assert heartbeat_control_operation(
        operation_id="lifecycle", lease_token=running.lease_token
    )

    done = finish_control_operation(
        operation_id="lifecycle",
        lease_token=running.lease_token,
        status="completed",
        result={"played": True},
    )
    assert done is not None
    assert done.status == "completed"
    assert done.result == {"played": True}
    assert get_control_operation(operation_id="lifecycle").status == "completed"


def test_stale_worker_lease_is_recovered_without_identity_scope(operations_db):
    from deskbot_server.application.control_operations import (
        accept_control_operation,
        get_control_operation,
        recover_stale_control_operations,
    )
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ControlOperation

    accept_control_operation(
        device_id="hardware-a",
        kind="device_tts",
        payload={"text": "late"},
        operation_id="stale",
    )
    session = get_session()
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.execute(
            update(ControlOperation)
            .where(ControlOperation.operation_id == "stale")
            .values(lease_expires_at=past)
        )
        session.commit()
    finally:
        session.close()

    assert recover_stale_control_operations(operation_id="stale") == 1
    recovered = get_control_operation(operation_id="stale")
    assert recovered is not None
    assert recovered.status == "timeout"
    assert recovered.error_code == "worker_lease_expired"


def test_control_operation_schema_contains_no_owner_or_user_columns(operations_db):
    from deskbot_server.db.engine import init_engine

    columns = {
        item["name"] for item in inspect(init_engine()).get_columns("control_operations")
    }
    assert "owner_user_id" not in columns
    assert "user_id" not in columns
    assert {"operation_id", "device_id", "kind", "status"} <= columns


def test_payload_hash_is_canonical_and_operation_id_is_validated():
    from deskbot_server.application.control_operations import (
        canonical_payload_hash,
        normalize_operation_id,
    )

    assert canonical_payload_hash({"a": 1, "b": 2}) == canonical_payload_hash(
        {"b": 2, "a": 1}
    )
    assert normalize_operation_id(" request:1 ") == "request:1"
    with pytest.raises(ValueError):
        normalize_operation_id("contains spaces")
