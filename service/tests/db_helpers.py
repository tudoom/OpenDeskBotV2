"""测试共用：一次性 SQLite（原先住在 test_scheduled_task.py 里，2026-09-14 定时任务撤掉后搬到这里）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        try:
            yield db_path
        finally:
            # Dispose SQLite's pooled handles before TemporaryDirectory tries
            # to remove the database on Windows.
            reset_engine()
