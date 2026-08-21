from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile
    from pathlib import Path

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
            reset_engine()


def test_settings_test_limit_is_one_pc_local_daily_counter(temp_db):
    from deskbot_server.application.settings_test_limit import (
        SETTINGS_TEST_DAILY_LIMIT,
        SettingsTestLimitExceeded,
        check_and_consume_settings_test,
        get_settings_test_quota,
    )

    for i in range(SETTINGS_TEST_DAILY_LIMIT):
        snap = check_and_consume_settings_test()
        assert snap.count == i + 1
        assert snap.remaining == SETTINGS_TEST_DAILY_LIMIT - (i + 1)

    with pytest.raises(SettingsTestLimitExceeded) as exc:
        check_and_consume_settings_test()
    assert exc.value.limit == SETTINGS_TEST_DAILY_LIMIT

    quota = get_settings_test_quota()
    assert quota.count == SETTINGS_TEST_DAILY_LIMIT
    assert quota.remaining == 0


def test_api_test_llm_returns_429_when_quota_exhausted(temp_db, monkeypatch):
    from deskbot_server.application.settings_test_limit import SETTINGS_TEST_DAILY_LIMIT
    from deskbot_server.web.app import create_app

    app = create_app()
    client = app.test_client()

    def fake_completion(*_args, **_kwargs):
        return "ok", {"model": "test", "display_name": "test", "usage": {}}

    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app2c_bp.chat_completion",
        fake_completion,
    )

    url = "/api/setup/llm/test"
    body = {
        "model_name": "qwen-flash",
        "protocol": "openai",
        "api_key": "sk-test",
        "prompt": "hi",
    }
    for _ in range(SETTINGS_TEST_DAILY_LIMIT):
        resp = client.post(url, json=body)
        assert resp.status_code == 200

    blocked = client.post(url, json=body)
    assert blocked.status_code == 429
    data = blocked.get_json()
    assert data["ok"] is False
    assert "上限" in data["error"]


def test_settings_test_limit_is_atomic_under_concurrency(temp_db):
    from deskbot_server.application.settings_test_limit import (
        SETTINGS_TEST_DAILY_LIMIT,
        SettingsTestLimitExceeded,
        check_and_consume_settings_test,
        get_settings_test_quota,
    )

    def consume_once(_index: int) -> bool:
        try:
            check_and_consume_settings_test()
            return True
        except SettingsTestLimitExceeded:
            return False

    attempts = SETTINGS_TEST_DAILY_LIMIT + 12
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(consume_once, range(attempts)))

    assert sum(accepted) == SETTINGS_TEST_DAILY_LIMIT
    quota = get_settings_test_quota()
    assert quota.count == SETTINGS_TEST_DAILY_LIMIT
    assert quota.remaining == 0
