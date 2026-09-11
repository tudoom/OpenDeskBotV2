from __future__ import annotations

import pytest
from flask import jsonify


@pytest.fixture()
def proxy_client(monkeypatch, tmp_path):
    db_path = tmp_path / "proxy.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", "p" * 32)
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    reset_engine()
    init_engine(db_path)
    init_database()
    from deskbot_server.web.app import create_app

    try:
        yield create_app().test_client()
    finally:
        reset_engine()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/proxy/deskbot/api/debug_prefs"),
        ("post", "/proxy/deskbot/api/not-allowed"),
    ],
)
def test_proxy_rejects_paths_outside_explicit_allowlist(
    proxy_client, monkeypatch, method, path
):
    forwarded: list[str] = []
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.proxy_bp._forward",
        lambda _method, route, **_kwargs: forwarded.append(route),
    )
    response = getattr(proxy_client, method)(path)
    assert response.status_code == 404
    assert response.get_json()["error"] == "proxy_path_not_allowed"
    assert forwarded == []


def test_proxy_enforces_method_and_explicit_hardware_route(proxy_client, monkeypatch):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, path, *, device_id=None, **_kwargs):
        forwarded.append((method, path, device_id))
        return jsonify({"ok": True})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)
    assert proxy_client.get(
        "/proxy/deskbot/api/device_servo?device_id=hardware-a"
    ).status_code == 405
    assert proxy_client.post(
        "/proxy/deskbot/api/device_servo?dyaw=1"
    ).status_code == 400

    # Any currently connected route is addressable; no account assignment is involved.
    allowed = proxy_client.post(
        "/proxy/deskbot/api/device_servo?device_id=hardware-b"
    )
    assert allowed.status_code == 200
    assert forwarded == [("POST", "/api/device_servo", "hardware-b")]


def test_control_operation_query_is_get_only_and_has_no_owner_scope(
    proxy_client, monkeypatch
):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, path, *, device_id=None, **_kwargs):
        forwarded.append((method, path, device_id))
        return jsonify({"ok": True, "status": "completed"})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)
    allowed = proxy_client.get(
        "/proxy/deskbot/api/control_operation"
        "?device_id=hardware-a&operation_id=op-1"
    )
    denied = proxy_client.post(
        "/proxy/deskbot/api/control_operation"
        "?device_id=hardware-a&operation_id=op-1"
    )
    assert allowed.status_code == 200
    assert denied.status_code == 405
    assert forwarded == [("GET", "/api/control_operation", "hardware-a")]


def test_expression_runtime_query_is_allowlisted_and_device_scoped(
    proxy_client, monkeypatch
):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, path, *, device_id=None, **_kwargs):
        forwarded.append((method, path, device_id))
        return jsonify({"ok": True})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)
    missing = proxy_client.get("/proxy/deskbot/api/expression_runtime")
    allowed = proxy_client.get(
        "/proxy/deskbot/api/expression_runtime?device_id=hardware-a"
    )

    assert missing.status_code == 400
    assert allowed.status_code == 200
    assert forwarded == [("GET", "/api/expression_runtime", "hardware-a")]


@pytest.mark.parametrize("path", ["/api/servo_config", "/api/scene_playbooks"])
def test_pc_local_config_proxy_does_not_require_hardware_route(
    proxy_client, monkeypatch, path
):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, route, *, device_id=None, **_kwargs):
        forwarded.append((method, route, device_id))
        return jsonify({"ok": True, "scope": "local"})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)

    assert proxy_client.get(f"/proxy/deskbot{path}").status_code == 200
    assert proxy_client.post(f"/proxy/deskbot{path}", json={}).status_code == 200
    assert forwarded == [("GET", path, None), ("POST", path, None)]


def test_servo_contract_proxy_is_readonly_and_device_free(proxy_client, monkeypatch):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, route, *, device_id=None, **_kwargs):
        forwarded.append((method, route, device_id))
        return jsonify({"ok": True})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)

    assert proxy_client.get("/proxy/deskbot/api/servo_contract").status_code == 200
    denied = proxy_client.post("/proxy/deskbot/api/servo_contract", json={})
    assert denied.status_code == 405
    assert forwarded == [("GET", "/api/servo_contract", None)]


@pytest.mark.parametrize("path", ["/api/pb_scenes", "/api/face_catalog"])
def test_pc_local_catalog_proxy_does_not_require_hardware_route(
    proxy_client, monkeypatch, path
):
    forwarded: list[tuple[str, str, str | None]] = []

    def _forward(method, route, *, device_id=None, **_kwargs):
        forwarded.append((method, route, device_id))
        return jsonify({"ok": True})

    monkeypatch.setattr("deskbot_server.web.blueprints.proxy_bp._forward", _forward)

    assert proxy_client.get(f"/proxy/deskbot{path}").status_code == 200
    assert forwarded == [("GET", path, None)]
