from __future__ import annotations

import hashlib
import sqlite3

import pytest


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield db_path
    finally:
        reset_engine()


def test_free_api_key_is_pc_local_and_authenticates(temp_db):
    from deskbot_server.auth.api_key_service import (
        FREE_DAILY_QUOTA_BYTES,
        FREE_FILE_KEY_ID,
        authenticate_api_key,
        read_free_api_key_config,
    )

    assert (temp_db.parent / ".free_api_key").is_file()
    cfg = read_free_api_key_config()
    assert cfg is not None
    assert cfg.daily_quota_bytes == FREE_DAILY_QUOTA_BYTES
    auth = authenticate_api_key(cfg.api_key)
    assert auth is not None
    assert auth.api_key_id == FREE_FILE_KEY_ID
    assert auth.is_free is True
    assert not hasattr(auth, "user_id")


def test_legacy_database_api_key_is_not_an_authentication_source(temp_db):
    from deskbot_server.auth.api_key_service import (
        authenticate_api_key,
        record_usage_checked,
    )
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ApiKey, _new_id

    raw = "odk_legacy_database_key"
    row_id = _new_id()
    session = get_session()
    try:
        session.add(
            ApiKey(
                id=row_id,
                name="legacy managed key",
                key_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                key_prefix=raw[:12],
                is_free=False,
                daily_quota_bytes=1024,
                is_active=True,
            )
        )
        session.commit()
    finally:
        session.close()

    assert authenticate_api_key(raw) is None
    with pytest.raises(PermissionError, match="invalid_api_key"):
        record_usage_checked(row_id, "asr", 1)


def test_usage_is_aggregated_for_the_pc_not_an_account(temp_db):
    from deskbot_server.auth.api_key_service import (
        FREE_FILE_KEY_ID,
        authenticate_api_key,
        get_local_usage_summary,
        get_local_usage_today,
        read_free_api_key_config,
        record_usage_checked,
    )

    cfg = read_free_api_key_config()
    assert cfg is not None
    auth = authenticate_api_key(cfg.api_key)
    assert auth is not None
    record_usage_checked(auth.api_key_id, "asr", 1024)
    record_usage_checked(auth.api_key_id, "llm", 512)

    today = get_local_usage_today()
    summary = get_local_usage_summary(days=7)
    assert today["asr_bytes"] == 1024
    assert today["llm_bytes"] == 512
    assert summary["totals"]["total_bytes"] == 1536
    assert len(summary["key_stats"]) == 1
    assert summary["key_stats"][0]["api_key_id"] == FREE_FILE_KEY_ID


def test_startup_removes_legacy_key_and_usage_but_keeps_free_usage(temp_db):
    from datetime import date

    from sqlalchemy import event, select, text

    from deskbot_server.auth.api_key_service import FREE_FILE_KEY_ID
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import get_session, init_engine, reset_engine
    from deskbot_server.db.models import ApiKey, UsageDaily, _new_id

    legacy_key_id = _new_id()
    today = date.today()
    session = get_session()
    try:
        session.add(
            ApiKey(
                id=legacy_key_id,
                name="legacy managed key",
                key_hash=f"legacy-{legacy_key_id}",
                key_prefix="odk_legacy",
                is_free=False,
                daily_quota_bytes=1024,
                is_active=True,
            )
        )
        session.add(
            UsageDaily(
                id=_new_id(),
                api_key_id=legacy_key_id,
                usage_date=today,
                asr_bytes=7,
            )
        )
        session.add(
            UsageDaily(
                id=_new_id(),
                api_key_id=FREE_FILE_KEY_ID,
                usage_date=today,
                llm_bytes=11,
            )
        )
        session.commit()
        # Force the API-key parent table through the legacy rebuild path.
        session.execute(text("ALTER TABLE api_keys ADD COLUMN user_id TEXT"))
        session.commit()
    finally:
        session.close()

    backup_path = temp_db.with_suffix(".db.pre-local-scope.bak")
    # A previous interrupted direct-to-final backup must not suppress the
    # recoverable snapshot for this destructive migration.
    backup_path.write_bytes(b"incomplete backup")

    reset_engine()
    engine = init_engine(temp_db)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    init_database()

    session = get_session()
    try:
        assert session.get(ApiKey, legacy_key_id) is None
        assert session.get(ApiKey, FREE_FILE_KEY_ID) is not None
        remaining_usage = list(session.scalars(select(UsageDaily)))
        assert len(remaining_usage) == 1
        assert remaining_usage[0].api_key_id == FREE_FILE_KEY_ID
        assert remaining_usage[0].llm_bytes == 11
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        session.close()
    assert backup_path.is_file()
    with sqlite3.connect(str(backup_path)) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute(
            "SELECT 1 FROM api_keys WHERE id = ?",
            (legacy_key_id,),
        ).fetchone() == (1,)


def test_free_key_quota_is_enforced_atomically(temp_db):
    from deskbot_server.auth.api_key_service import (
        QuotaExceededError,
        authenticate_api_key,
        read_free_api_key_config,
        record_usage_checked,
        write_free_api_key_file,
    )

    cfg = read_free_api_key_config()
    assert cfg is not None
    write_free_api_key_file(cfg.api_key, daily_quota_bytes=100)
    auth = authenticate_api_key(cfg.api_key)
    assert auth is not None

    record_usage_checked(auth.api_key_id, "asr", 90)
    with pytest.raises(QuotaExceededError):
        record_usage_checked(auth.api_key_id, "tts", 11)


def test_file_free_key_rotation_revokes_the_previous_value(temp_db):
    from deskbot_server.auth.api_key_service import (
        authenticate_api_key,
        read_free_api_key_config,
        write_free_api_key_file,
    )

    old = read_free_api_key_config()
    assert old is not None
    new_raw = "odk_free_localReplacementKey"
    write_free_api_key_file(new_raw)
    assert authenticate_api_key(new_raw) is not None
    assert authenticate_api_key(old.api_key) is None
