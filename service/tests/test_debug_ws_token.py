from __future__ import annotations

import pytest

from deskbot_server.auth.debug_ws_token import (
    DEBUG_WS_SUBPROTOCOL,
    debug_ws_subprotocols,
    extract_debug_token_from_headers,
    extract_debug_token_from_query,
    issue_debug_ws_token,
    select_debug_ws_subprotocol,
    verify_debug_ws_token,
)


@pytest.fixture(autouse=True)
def local_secret(monkeypatch):
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    monkeypatch.setenv("DESKBOT_DEBUG_WS_TOKEN_SECONDS", "600")


def test_issue_and_verify_pc_local_debug_token():
    info = issue_debug_ws_token()
    assert info.token
    assert info.expires_in == 600
    assert verify_debug_ws_token(info.token) is True
    assert not hasattr(info, "user_id")


def test_tampered_and_expired_tokens_are_rejected(monkeypatch):
    import time

    base = time.time()
    monkeypatch.setattr(time, "time", lambda: base)
    info = issue_debug_ws_token()
    assert verify_debug_ws_token(info.token + "x") is False

    monkeypatch.setenv("DESKBOT_DEBUG_WS_TOKEN_SECONDS", "60")
    monkeypatch.setattr(time, "time", lambda: base + 61)
    assert verify_debug_ws_token(info.token) is False


def test_query_token_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("DESKBOT_ALLOW_DEBUG_TOKEN_IN_QUERY", raising=False)
    assert extract_debug_token_from_query({"debug_token": " token "}) is None
    monkeypatch.setenv("DESKBOT_ALLOW_DEBUG_TOKEN_IN_QUERY", "1")
    assert extract_debug_token_from_query({"debug_token": " token "}) == "token"


def test_header_and_websocket_protocol_extract_token_without_identity():
    info = issue_debug_ws_token()
    assert extract_debug_token_from_headers(
        {"X-Deskbot-Debug-Token": f" {info.token} "}
    ) == info.token

    protocols = debug_ws_subprotocols(info.token)
    assert protocols[0] == DEBUG_WS_SUBPROTOCOL
    assert extract_debug_token_from_headers(
        {"Sec-WebSocket-Protocol": ", ".join(protocols)}
    ) == info.token
    assert select_debug_ws_subprotocol(None, protocols) == DEBUG_WS_SUBPROTOCOL


def test_short_or_missing_server_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        issue_debug_ws_token()
