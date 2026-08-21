from __future__ import annotations

import asyncio
import socket
from unittest.mock import patch

import pytest


def test_webfetch_ok():
    from deskbot_server.web_tools import webfetch

    class _Resp:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def read(self, n=-1):
            return b"hello"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("deskbot_server.web_tools.safe_urlopen", return_value=_Resp()):
        out = webfetch("https://example.com")
    assert out["ok"] is True
    assert "hello" in out["text"]


def test_websearch_returns_structure():
    from deskbot_server.web_tools import websearch

    payload = '{"AbstractText":"测试摘要","Heading":"标题","RelatedTopics":[]}'.encode()
    with patch(
        "deskbot_server.web_tools._http_get",
        return_value=(200, "application/json", payload),
    ):
        out = websearch("测试")
    assert out["ok"] is True
    assert out["results"]


def test_provider_urls_block_private_and_plaintext_by_default(monkeypatch):
    from deskbot_server.safe_fetch import (
        validate_provider_http_url,
        validate_provider_websocket_url,
    )

    monkeypatch.delenv("DESKBOT_PROVIDER_PRIVATE_ORIGINS", raising=False)
    monkeypatch.delenv("DESKBOT_ALLOW_INSECURE_PROVIDER_HTTP", raising=False)
    monkeypatch.delenv("DESKBOT_ALLOW_INSECURE_PROVIDER_WS", raising=False)

    with pytest.raises(ValueError):
        validate_provider_http_url("http://127.0.0.1:11434/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_provider_http_url("http://example.com/v1")
    with pytest.raises(ValueError):
        validate_provider_websocket_url("wss://169.254.169.254/tts")
    with pytest.raises(ValueError, match="WSS"):
        validate_provider_websocket_url("ws://example.com/tts")


def test_operator_can_allow_exact_private_provider_origin(monkeypatch):
    from deskbot_server.safe_fetch import (
        validate_provider_http_url,
        validate_provider_websocket_url,
    )

    monkeypatch.setenv(
        "DESKBOT_PROVIDER_PRIVATE_ORIGINS",
        "http://127.0.0.1:11434,ws://127.0.0.1:8765",
    )

    assert (
        validate_provider_http_url("http://127.0.0.1:11434/v1").hostname
        == "127.0.0.1"
    )
    assert (
        validate_provider_websocket_url("ws://127.0.0.1:8765/tts").hostname
        == "127.0.0.1"
    )
    with pytest.raises(ValueError):
        validate_provider_http_url("http://127.0.0.1:11435/v1")


def test_doubao_wss_pins_single_validated_dns_result(monkeypatch):
    from deskbot_server.tts import doubao

    monkeypatch.delenv("DESKBOT_PROVIDER_PRIVATE_ORIGINS", raising=False)
    dns_calls: list[tuple[str, int]] = []
    connected: list[tuple[str, int]] = []

    def fake_getaddrinfo(host, port, *, type):
        assert type == socket.SOCK_STREAM
        dns_calls.append((host, port))
        # A second lookup would simulate rebinding to loopback. The pinned
        # path must never ask for this second answer.
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    class FakeSocket:
        def __init__(self):
            self.closed = False
            self.blocking = True

        def setblocking(self, value):
            self.blocking = value

        def close(self):
            self.closed = True

    pinned_sock = FakeSocket()

    def fake_connect_sockaddrs(rows, timeout):
        assert timeout == 30
        connected.extend(row[4] for row in rows)
        return pinned_sock

    captured: dict = {}
    fake_ws = object()

    async def fake_connect(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return fake_ws

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "deskbot_server.safe_fetch.socket.getaddrinfo",
        fake_getaddrinfo,
    )
    monkeypatch.setattr(
        "deskbot_server.safe_fetch._connect_sockaddrs",
        fake_connect_sockaddrs,
    )
    monkeypatch.setattr(doubao.websockets, "connect", fake_connect)
    monkeypatch.setattr(doubao, "start_connection", noop)
    monkeypatch.setattr(doubao, "wait_for_event", noop)

    cfg = doubao.DoubaoTtsConfig(
        api_key="test-key",
        speaker="test-speaker",
        ws_url="wss://provider.example.test/tts",
    )
    connection = doubao.DoubaoTtsConnection(cfg)
    asyncio.run(connection._ensure_ready())

    assert dns_calls == [("provider.example.test", 443)]
    assert connected == [("93.184.216.34", 443)]
    assert captured["uri"] == cfg.ws_url
    assert captured["sock"] is pinned_sock
    assert captured["server_hostname"] == "provider.example.test"
    assert pinned_sock.blocking is False


def test_doubao_wss_closes_pinned_socket_when_handshake_setup_fails(
    monkeypatch,
):
    from urllib.parse import urlsplit

    from deskbot_server.tts import doubao

    class FakeSocket:
        closed = False

        def close(self):
            self.closed = True

    pinned_sock = FakeSocket()

    def fake_pinned_socket(_url, *, timeout):
        assert timeout == 30
        return urlsplit("wss://provider.example.test/tts"), pinned_sock

    async def fail_connect(_uri, **_kwargs):
        raise OSError("TLS handshake failed")

    monkeypatch.setattr(
        doubao,
        "connect_provider_websocket_socket",
        fake_pinned_socket,
    )
    monkeypatch.setattr(doubao.websockets, "connect", fail_connect)
    connection = doubao.DoubaoTtsConnection(
        doubao.DoubaoTtsConfig(
            api_key="test-key",
            speaker="test-speaker",
            ws_url="wss://provider.example.test/tts",
        )
    )

    with pytest.raises(OSError, match="TLS handshake failed"):
        asyncio.run(connection._ensure_ready())
    assert pinned_sock.closed is True
