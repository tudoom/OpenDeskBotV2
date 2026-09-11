from __future__ import annotations

import pytest


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    from deskbot_server import device_data

    data_dir = tmp_path / "data"
    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield db_path
    finally:
        reset_engine()


def test_local_web_console_routes_do_not_require_login(temp_db):
    from deskbot_server.web.app import create_app

    client = create_app().test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/home").status_code == 200
    assert client.get("/debug/devices").status_code == 200


def test_http_provider_gate_rejects_missing_api_key():
    from deskbot_server.ws.api_key_gate import http_require_api_key

    with pytest.raises(PermissionError, match="api_key_required"):
        http_require_api_key({}, {})


def test_http_provider_gate_accepts_the_pc_free_api_key(temp_db):
    from deskbot_server.auth.api_key_service import (
        FREE_FILE_KEY_ID,
        read_free_api_key_raw,
    )
    from deskbot_server.ws.api_key_gate import http_require_api_key

    raw = read_free_api_key_raw()
    assert raw
    auth = http_require_api_key({}, {"X-API-Key": raw})
    assert auth.api_key_id == FREE_FILE_KEY_ID
    assert not hasattr(auth, "user_id")


def test_api_key_checks_routing_id_but_never_device_ownership(temp_db):
    from deskbot_server.auth.api_key_service import (
        authenticate_api_key,
        read_free_api_key_raw,
    )
    from deskbot_server.ws.api_key_gate import http_require_device_route

    raw = read_free_api_key_raw()
    assert raw
    auth = authenticate_api_key(raw)
    assert auth is not None

    http_require_device_route(auth, "hardware-a", require_device=True)
    http_require_device_route(auth, "hardware-b", require_device=True)
    with pytest.raises(PermissionError, match="device_id_required"):
        http_require_device_route(auth, "", require_device=True)
    with pytest.raises(PermissionError, match="api_key_required"):
        http_require_device_route(None, "hardware-a", require_device=True)
