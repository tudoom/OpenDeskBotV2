from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from deskbot_server.paths import DATA_DIR

_engine = None
_session_factory: scoped_session[Session] | None = None
_init_lock = threading.Lock()


def default_db_path() -> Path:
    raw = (os.environ.get("DESKBOT_DB_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (DATA_DIR / "opendesk.db").resolve()


def reset_engine() -> None:
    global _engine, _session_factory
    # scoped_session retains one Session (and potentially a checked-out SQLite
    # connection) per thread.  Remove it before disposing the pool; otherwise
    # Windows keeps the database file locked and test/reload cleanup fails.
    if _session_factory is not None:
        _session_factory.remove()
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_engine(db_path: Path | None = None):
    global _engine, _session_factory
    with _init_lock:
        if _engine is not None:
            return _engine
        path = db_path or default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            # Both the realtime service and Flask control plane open the same
            # SQLite file.  Do not keep idle file handles in per-process
            # pools: it prevents clean reload/test teardown on Windows and
            # makes cross-process replacement/migration harder to reason
            # about.  WAL still provides the intended concurrent access.
            poolclass=NullPool,
            pool_pre_ping=True,
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        _session_factory = scoped_session(
            sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
        )
        return _engine


def get_session() -> Session:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory()


def remove_session() -> None:
    if _session_factory is not None:
        _session_factory.remove()


def apply_wal_pragmas(conn) -> None:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA busy_timeout=30000"))
    conn.execute(text("PRAGMA synchronous=NORMAL"))
