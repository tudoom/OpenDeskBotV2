from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from deskbot_server.ws.tls import (
    build_server_tls,
    secure_deployment_required,
    validate_web_proxy_tls,
)

_TLS_ENV = (
    "DESKBOT_ENV",
    "DESKBOT_SERVER_TLS_CERT",
    "DESKBOT_SERVER_TLS_KEY",
    "DESKBOT_TLS_TERMINATED_BY_PROXY",
    "DESKBOT_WEB_TLS_TERMINATED_BY_PROXY",
    "DESKBOT_WS_PUBLIC_BASE",
    "DESKBOT_ALLOW_INSECURE_LOCAL",
)


@pytest.fixture(autouse=True)
def _clear_tls_env(monkeypatch: pytest.MonkeyPatch):
    for name in _TLS_ENV:
        monkeypatch.delenv(name, raising=False)


def test_development_allows_loopback_plaintext():
    assert secure_deployment_required("127.0.0.1") is False
    tls = build_server_tls("127.0.0.1")
    assert tls.context is None
    assert tls.scheme == "ws"


def test_all_default_launchers_bind_web_console_to_loopback():
    service_root = Path(__file__).resolve().parents[1]
    web_main = (
        service_root / "src" / "deskbot_server" / "web" / "__main__.py"
    ).read_text(encoding="utf-8")
    start_script = (service_root / "start.sh").read_text(encoding="utf-8")

    assert 'or "127.0.0.1"' in web_main
    assert 'DESKBOT_WEB_HOST:-127.0.0.1' in start_script
    assert 'export DESKBOT_WEB_HOST="0.0.0.0"' not in start_script
    assert "生产环境必须持久配置至少 32 字符" in start_script
    assert 'DESKBOT_TLS_TERMINATED_BY_PROXY:-' in start_script
    assert 'DESKBOT_WEB_TLS_TERMINATED_BY_PROXY:-' in start_script
    assert (
        '[[ -n "${DESKBOT_SERVER_TLS_CERT:-}" && '
        '-n "${DESKBOT_SERVER_TLS_KEY:-}" ]]' in start_script
    )
    assert "secrets.token_urlsafe(48)" in start_script


def test_public_bind_is_secure_by_default_and_has_explicit_local_override(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(RuntimeError, match="requires a TLS certificate"):
        build_server_tls("0.0.0.0")

    monkeypatch.setenv("DESKBOT_ALLOW_INSECURE_LOCAL", "1")
    tls = build_server_tls("0.0.0.0")
    assert tls.context is None
    assert tls.scheme == "ws"


def test_production_rejects_public_plaintext(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DESKBOT_ENV", "production")
    with pytest.raises(RuntimeError, match="requires a TLS certificate"):
        build_server_tls("0.0.0.0")


def test_proxy_terminated_tls_requires_loopback_in_production(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DESKBOT_ENV", "production")
    monkeypatch.setenv("DESKBOT_TLS_TERMINATED_BY_PROXY", "1")

    with pytest.raises(RuntimeError, match="must bind to localhost/loopback"):
        build_server_tls("0.0.0.0")

    tls = build_server_tls("127.0.0.1")
    assert tls.context is None
    assert tls.scheme == "wss"
    assert tls.terminated_by_proxy is True


def test_proxy_flags_imply_secure_deployment_even_without_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DESKBOT_TLS_TERMINATED_BY_PROXY", "1")
    assert secure_deployment_required("127.0.0.1") is True
    tls = build_server_tls("127.0.0.1")
    assert tls.scheme == "wss"
    with pytest.raises(RuntimeError, match="DESKBOT_WEB_TLS"):
        validate_web_proxy_tls("127.0.0.1")

    monkeypatch.delenv("DESKBOT_TLS_TERMINATED_BY_PROXY")
    monkeypatch.setenv("DESKBOT_WEB_TLS_TERMINATED_BY_PROXY", "true")
    assert secure_deployment_required("::1") is True
    validate_web_proxy_tls("::1")
    with pytest.raises(RuntimeError, match="requires a TLS certificate"):
        build_server_tls("127.0.0.1")


def test_native_tls_requires_cert_and_key_together(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DESKBOT_SERVER_TLS_CERT", "cert.pem")
    assert secure_deployment_required("127.0.0.1") is False
    with pytest.raises(RuntimeError, match="must be set together"):
        build_server_tls("127.0.0.1")


def test_native_tls_key_without_cert_is_not_a_secure_signal(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DESKBOT_SERVER_TLS_KEY", "privkey.pem")
    assert secure_deployment_required("127.0.0.1") is False
    with pytest.raises(RuntimeError, match="must be set together"):
        build_server_tls("127.0.0.1")


def test_native_tls_pair_implies_secure_deployment_without_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DESKBOT_SERVER_TLS_CERT", "fullchain.pem")
    monkeypatch.setenv("DESKBOT_SERVER_TLS_KEY", "privkey.pem")

    assert secure_deployment_required("127.0.0.1") is True
    with pytest.raises(RuntimeError, match="DESKBOT_WEB_TLS"):
        validate_web_proxy_tls("127.0.0.1")

    monkeypatch.setenv("DESKBOT_WEB_TLS_TERMINATED_BY_PROXY", "1")
    validate_web_proxy_tls("127.0.0.1")


def test_native_tls_loads_cert_chain(
    monkeypatch: pytest.MonkeyPatch,
):
    loaded: list[tuple[str, str]] = []

    def fake_load(self, certfile, keyfile=None, password=None):
        loaded.append((certfile, keyfile))

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", fake_load)
    monkeypatch.setenv("DESKBOT_ENV", "production")
    monkeypatch.setenv("DESKBOT_SERVER_TLS_CERT", "fullchain.pem")
    monkeypatch.setenv("DESKBOT_SERVER_TLS_KEY", "privkey.pem")

    tls = build_server_tls("0.0.0.0")
    assert tls.context is not None
    assert tls.context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert tls.scheme == "wss"
    assert loaded == [("fullchain.pem", "privkey.pem")]


def test_production_web_console_requires_loopback_https_proxy(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DESKBOT_ENV", "production")
    with pytest.raises(RuntimeError, match="requires DESKBOT_WEB_TLS"):
        validate_web_proxy_tls("127.0.0.1")

    monkeypatch.setenv("DESKBOT_WEB_TLS_TERMINATED_BY_PROXY", "1")
    with pytest.raises(RuntimeError, match="must bind to localhost/loopback"):
        validate_web_proxy_tls("0.0.0.0")

    validate_web_proxy_tls("::1")


def test_browser_websocket_origin_supports_reverse_proxy_path(
    monkeypatch: pytest.MonkeyPatch,
):
    from flask import Flask

    from deskbot_server.web.helpers import camera_view_ws_base, device_pipeline_ws_base

    monkeypatch.setenv(
        "DESKBOT_WS_PUBLIC_BASE",
        "wss://deskbot.example.com/realtime",
    )
    app = Flask(__name__)
    with app.test_request_context("/", base_url="https://deskbot.example.com"):
        assert (
            camera_view_ws_base()
            == "wss://deskbot.example.com/realtime/camera_view"
        )
        assert (
            device_pipeline_ws_base()
            == "wss://deskbot.example.com/realtime/device_pipeline"
        )
