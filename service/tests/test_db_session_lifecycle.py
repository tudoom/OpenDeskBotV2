from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

_EXCLUDED = {
    Path("auth/service.py"),
    Path("auth/api_key_service.py"),
    Path("db/engine.py"),
}


def _calls_get_session(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "get_session"
        for child in ast.walk(node)
    )


def _finally_closes_session(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Try):
            continue
        for final_node in child.finalbody:
            for call in ast.walk(final_node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "close"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "session"
                ):
                    return True
    return False


def test_every_non_excluded_get_session_function_closes_in_finally():
    package = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
    )
    missing: list[str] = []
    for path in package.rglob("*.py"):
        relative = path.relative_to(package)
        if relative in _EXCLUDED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _calls_get_session(node) and not _finally_closes_session(node):
                missing.append(f"{relative.as_posix()}:{node.lineno}:{node.name}")
    assert missing == []


class _ReadSession:
    def __init__(self, *, scalar_result=None, error: Exception | None = None):
        self.scalar_result = scalar_result
        self.error = error
        self.closed = False
        self.rolled_back = False

    def scalar(self, _query):
        if self.error is not None:
            raise self.error
        return self.scalar_result

    def execute(self, _statement):
        if self.error is not None:
            raise self.error
        return None

    def in_transaction(self):
        return False

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_scheduled_task_read_closes_on_early_return_and_query_error(monkeypatch):
    import deskbot_server.scheduled_task_service as service

    missing = _ReadSession(scalar_result=None)
    monkeypatch.setattr(service, "get_session", lambda: missing)
    assert service.get_scheduled_task("missing") is None
    assert missing.closed is True

    failed = _ReadSession(error=RuntimeError("query failed"))
    monkeypatch.setattr(service, "get_session", lambda: failed)
    with pytest.raises(RuntimeError, match="query failed"):
        service.get_scheduled_task("broken")
    assert failed.closed is True


def test_settings_quota_closes_on_limit_exception(monkeypatch):
    import deskbot_server.application.settings_test_limit as limits

    session = _ReadSession()
    monkeypatch.setattr(limits, "get_session", lambda: session)
    monkeypatch.setattr(
        limits,
        "_get_row",
        lambda *_args: SimpleNamespace(count=limits.SETTINGS_TEST_DAILY_LIMIT),
    )

    with pytest.raises(limits.SettingsTestLimitExceeded):
        limits.check_and_consume_settings_test()
    assert session.closed is True


class _ClaimSession:
    def __init__(self):
        self.closed = False
        self.added = []

    def scalar(self, _query):
        return None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        return None

    def close(self):
        self.closed = True


class _FinishSession:
    def __init__(self):
        self.closed = False
        self.row = SimpleNamespace(
            status="running",
            result_json=None,
            completed_at=None,
        )

    def scalar(self, _query):
        return self.row

    def commit(self):
        return None

    def in_transaction(self):
        return False

    def rollback(self):
        return None

    def close(self):
        self.closed = True


def test_tool_claim_session_closes_before_side_effect_and_finish(monkeypatch):
    import deskbot_server.application.llm_tool_runner as runner
    import deskbot_server.db.engine as engine

    claim_session = _ClaimSession()
    finish_session = _FinishSession()
    sessions = iter((claim_session, finish_session))
    monkeypatch.setattr(engine, "get_session", lambda: next(sessions))

    def fake_add_memory(text):
        assert claim_session.closed is True
        assert finish_session.closed is False
        return {"id": "memory-1", "text": text}

    monkeypatch.setattr(runner, "add_memory", fake_add_memory)
    result = runner.execute_llm_tools(
        [
            {
                "tool": "memory_add",
                "text": "喜欢猫",
                "operation_id": "op-memory-1",
            }
        ],
        device_id="deskbot-a",
    )

    assert result[0]["ok"] is True
    assert result[0]["operation_id"] == "op-memory-1"
    assert claim_session.closed is True
    assert finish_session.closed is True
    assert finish_session.row.status == "completed"


def test_tool_safety_schema_migration_removes_identity_columns_and_keeps_rows():
    from sqlalchemy import create_engine, inspect, text

    from deskbot_server.db.init_db import _migrate_tool_safety_schema

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE devices ("
                "device_id VARCHAR(128) PRIMARY KEY, "
                "owner_user_id VARCHAR(36) NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tool_operations ("
                "id VARCHAR(36) PRIMARY KEY, "
                "device_id VARCHAR(128) NOT NULL, "
                "owner_user_id VARCHAR(36), "
                "initiator_scope VARCHAR(32), "
                "initiator_subject_id VARCHAR(128), "
                "tool_name VARCHAR(64) NOT NULL, "
                "operation_id VARCHAR(128) NOT NULL, "
                "payload_hash VARCHAR(64), "
                "session_id VARCHAR(36), "
                "status VARCHAR(16), "
                "result_json TEXT, "
                "created_at DATETIME, "
                "completed_at DATETIME"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tool_confirmations ("
                "id VARCHAR(36) PRIMARY KEY, "
                "device_id VARCHAR(128) NOT NULL, "
                "owner_user_id VARCHAR(36), "
                "initiator_scope VARCHAR(32), "
                "initiator_subject_id VARCHAR(128), "
                "tool_name VARCHAR(64) NOT NULL, "
                "payload_hash VARCHAR(64) NOT NULL, "
                "session_id VARCHAR(36), "
                "created_at DATETIME, "
                "expires_at DATETIME, "
                "consumed_at DATETIME"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices (device_id, owner_user_id) "
                "VALUES ('deskbot-legacy', 'owner-legacy')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tool_operations "
                "(id, device_id, owner_user_id, initiator_scope, "
                "initiator_subject_id, tool_name, operation_id, payload_hash, "
                "session_id, status, result_json, created_at, completed_at) "
                "VALUES ('op-row', 'deskbot-legacy', 'owner-legacy', 'user', "
                "'subject-legacy', 'miot', 'operation-1', 'operation-hash', "
                "'session-1', 'failed', 'kept-result', "
                "'2026-07-01 01:02:03+00:00', "
                "'2026-07-01 01:02:04+00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tool_confirmations "
                "(id, device_id, owner_user_id, initiator_scope, "
                "initiator_subject_id, tool_name, payload_hash, session_id, "
                "created_at, expires_at, consumed_at) "
                "VALUES ('confirmation-row', 'deskbot-legacy', 'owner-legacy', "
                "'user', 'subject-legacy', 'miot', 'hash-1', 'session-2', "
                "'2026-07-01 01:03:03+00:00', "
                "'2026-07-01 01:04:03+00:00', "
                "'2026-07-01 01:03:30+00:00')"
            )
        )

    _migrate_tool_safety_schema(engine)
    _migrate_tool_safety_schema(engine)

    operation_columns = {
        column["name"] for column in inspect(engine).get_columns("tool_operations")
    }
    confirmation_columns = {
        column["name"]
        for column in inspect(engine).get_columns("tool_confirmations")
    }
    operation_indexes = {
        index["name"] for index in inspect(engine).get_indexes("tool_operations")
    }
    confirmation_indexes = {
        index["name"]
        for index in inspect(engine).get_indexes("tool_confirmations")
    }
    assert {"payload_hash", "session_id", "status"}.issubset(operation_columns)
    assert {"payload_hash", "session_id", "expires_at"}.issubset(
        confirmation_columns
    )
    assert {
        "device_id",
        "owner_user_id",
        "initiator_scope",
        "initiator_subject_id",
    }.isdisjoint(operation_columns)
    assert {
        "device_id",
        "owner_user_id",
        "initiator_scope",
        "initiator_subject_id",
    }.isdisjoint(confirmation_columns)
    assert {
        "ix_tool_operations_payload_hash",
        "ix_tool_operations_session_id",
    }.issubset(operation_indexes)
    assert {
        "ix_tool_confirmations_payload_hash",
        "ix_tool_confirmations_session_id",
    }.issubset(confirmation_indexes)
    assert not any("owner" in str(name) or "initiator" in str(name) for name in operation_indexes)
    assert not any("owner" in str(name) or "initiator" in str(name) for name in confirmation_indexes)

    with engine.connect() as connection:
        operation = connection.execute(
            text(
                "SELECT tool_name, operation_id, payload_hash, session_id, "
                "status, result_json, created_at, completed_at "
                "FROM tool_operations WHERE id = 'op-row'"
            )
        ).mappings().one()
        confirmation = connection.execute(
            text(
                "SELECT tool_name, payload_hash, session_id, created_at, "
                "expires_at, consumed_at "
                "FROM tool_confirmations WHERE id = 'confirmation-row'"
            )
        ).mappings().one()
    assert dict(operation) == {
        "tool_name": "miot",
        "operation_id": "operation-1",
        "payload_hash": "operation-hash",
        "session_id": "session-1",
        "status": "failed",
        "result_json": "kept-result",
        "created_at": "2026-07-01 01:02:03+00:00",
        "completed_at": "2026-07-01 01:02:04+00:00",
    }
    assert dict(confirmation) == {
        "tool_name": "miot",
        "payload_hash": "hash-1",
        "session_id": "session-2",
        "created_at": "2026-07-01 01:03:03+00:00",
        "expires_at": "2026-07-01 01:04:03+00:00",
        "consumed_at": "2026-07-01 01:03:30+00:00",
    }


def test_obsolete_device_usage_table_is_detected_and_dropped():
    from sqlalchemy import create_engine, inspect, text

    from deskbot_server.db.init_db import (
        _drop_obsolete_identity_tables,
        _legacy_schema_present,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE usage_daily_device ("
                "id VARCHAR(36) PRIMARY KEY, "
                "device_id VARCHAR(128) NOT NULL"
                ")"
            )
        )

    assert _legacy_schema_present(engine) is True
    _drop_obsolete_identity_tables(engine)
    assert "usage_daily_device" not in inspect(engine).get_table_names()
    assert _legacy_schema_present(engine) is False
