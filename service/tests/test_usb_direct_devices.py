from __future__ import annotations

import pytest


@pytest.fixture()
def usb_direct_env(tmp_path, monkeypatch):
    db_path = tmp_path / "usb-direct.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", "d" * 32)
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield
    finally:
        reset_engine()


def _usb_snapshot(*device_ids: str):
    return {
        "ok": True,
        "observed_at": "2026-07-29T09:00:00+00:00",
        "error": None,
        "devices": [
            {
                "device_id": device_id,
                "online": True,
                "transport": "usb_cdc",
                "session_generation": index + 1,
                "interaction_state": "IDLE",
                "last_seen": "2026-07-29 17:00:00",
            }
            for index, device_id in enumerate(device_ids)
        ],
    }


def _live_snapshot(*device_ids: str):
    return {
        "ok": True,
        "observed_at": "2026-07-29T09:00:00+00:00",
        "error": None,
        "devices": {
            device_id: {
                "online": True,
                "transport": "usb_cdc",
                "session_generation": index + 1,
                "interaction_state": "IDLE",
                "last_seen": "2026-07-29 17:00:00",
            }
            for index, device_id in enumerate(device_ids)
        },
    }


def _mock_snapshots(monkeypatch, *device_ids: str) -> None:
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app_bp.fetch_attached_usb_snapshot",
        lambda **_kwargs: _usb_snapshot(*device_ids),
    )
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app_bp.fetch_live_device_snapshot",
        lambda **_kwargs: _live_snapshot(*device_ids),
    )


def test_usb_hello_registers_transport_without_identity(usb_direct_env):
    from deskbot_server.hardware_catalog import (
        ensure_local_device,
        get_device_by_device_id,
    )

    registered = ensure_local_device("deskbot_a1b2c3d4e5f6")
    stored = get_device_by_device_id("deskbot_a1b2c3d4e5f6")
    assert stored is not None
    assert registered.id == stored.id
    assert not hasattr(stored, "owner_user_id")
    assert not hasattr(stored, "lifecycle_state")
    assert stored.last_seen_at is not None


def test_console_discovers_and_auto_selects_single_usb_route(
    usb_direct_env, monkeypatch
):
    from deskbot_server.web.app import create_app

    device_id = "deskbot_001122334455"
    _mock_snapshots(monkeypatch, device_id)
    payload = create_app().test_client().get("/app/api/devices").get_json()
    assert [row["device_id"] for row in payload["devices"]] == [device_id]
    assert payload["devices"][0]["online"] is True
    assert payload["current_device_id"] == device_id


def test_browser_cannot_forge_or_delete_usb_routes(usb_direct_env, monkeypatch):
    from deskbot_server.hardware_catalog import (
        ensure_local_device,
        get_device_by_device_id,
    )
    from deskbot_server.web.app import create_app

    connected = "deskbot_112233445566"
    forged = "deskbot_deadbeef0001"
    ensure_local_device(connected)
    _mock_snapshots(monkeypatch, connected)
    client = create_app().test_client()

    added = client.post("/app/api/devices", json={"device_id": forged})
    removed = client.delete(f"/app/api/devices/{connected}")
    assert added.status_code == 405
    assert get_device_by_device_id(forged) is None
    assert removed.status_code == 404
    assert get_device_by_device_id(connected) is not None


def test_multiple_usb_routes_can_be_selected_without_data_partition(
    usb_direct_env, monkeypatch
):
    from deskbot_server.web.app import create_app

    first = "deskbot_010203040506"
    second = "deskbot_102030405060"
    _mock_snapshots(monkeypatch, first, second)
    client = create_app().test_client()
    discovered = client.get("/app/api/devices")
    assert {row["device_id"] for row in discovered.get_json()["devices"]} == {
        first,
        second,
    }
    selected = client.post("/app/api/devices/select", json={"device_id": second})
    assert selected.status_code == 200
    assert selected.get_json()["current_device_id"] == second
