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
    with patch("deskbot_server.web_tools._ark_search_config", return_value=None), patch(
        "deskbot_server.web_tools._http_get",
        return_value=(200, "application/json", payload),
    ):
        out = websearch("测试")
    assert out["ok"] is True
    assert out["provider"] == "duckduckgo"
    assert out["results"]


_ARK_RESPONSE = {
    "status": "completed",
    "usage": {"tool_usage": {"web_search": 1}},
    "output": [
        {"type": "reasoning", "id": "r1", "summary": []},
        {
            "type": "web_search_call",
            "id": "ws1",
            "status": "completed",
            "action": {"type": "search", "query": "北京 今天 天气"},
        },
        {
            "type": "message",
            "id": "m1",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "今天北京晴，最高 30℃。",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "title": "首都之窗",
                            "url": "https://www.beijing.gov.cn/x",
                            "site_name": "北京市人民政府",
                            "publish_time": "2026年09月02日",
                            "summary": "实时天气预报 北京 晴",
                        },
                        {
                            "type": "url_citation",
                            "title": "重复来源",
                            "url": "https://www.beijing.gov.cn/x",
                        },
                    ],
                }
            ],
        },
    ],
}


def test_websearch_prefers_ark_web_search_plugin():
    from deskbot_server.web_tools import websearch

    captured: dict = {}

    def _fake_post(url, payload, api_key):
        captured.update(url=url, payload=payload, api_key=api_key)
        return _ARK_RESPONSE

    with patch(
        "deskbot_server.web_tools._ark_search_config",
        return_value=("https://ark.cn-beijing.volces.com/api/v3/responses", "k", "doubao-x"),
    ), patch("deskbot_server.web_tools._post_ark_web_search", side_effect=_fake_post), patch(
        "deskbot_server.web_tools._http_get",
        side_effect=AssertionError("DuckDuckGo fallback must not run"),
    ):
        out = websearch("北京今天天气", max_results=5)

    assert out["ok"] is True
    assert out["provider"] == "ark_web_search"
    assert out["abstract"] == "今天北京晴，最高 30℃。"
    assert out["queries"] == ["北京 今天 天气"]
    assert out["search_calls"] == 1
    # 去重后只剩一条引用，且字段齐全
    assert [r["url"] for r in out["results"]] == ["https://www.beijing.gov.cn/x"]
    assert out["results"][0]["site_name"] == "北京市人民政府"
    assert captured["url"].endswith("/responses")
    assert captured["payload"]["model"] == "doubao-x"
    assert captured["payload"]["tools"][0]["type"] == "web_search"
    assert captured["payload"]["tools"][0]["limit"] == 5
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_websearch_retries_without_thinking_on_http_400():
    from deskbot_server.web_tools import websearch

    payloads: list[dict] = []

    def _fake_post(url, payload, api_key):
        payloads.append(payload)
        if "thinking" in payload:
            raise RuntimeError("HTTP 400: thinking not supported")
        return _ARK_RESPONSE

    with patch(
        "deskbot_server.web_tools._ark_search_config",
        return_value=("https://ark.cn-beijing.volces.com/api/v3/responses", "k", "doubao-x"),
    ), patch("deskbot_server.web_tools._post_ark_web_search", side_effect=_fake_post):
        out = websearch("北京今天天气")
    assert out["provider"] == "ark_web_search"
    assert len(payloads) == 2 and "thinking" not in payloads[1]


def test_websearch_falls_back_to_duckduckgo_when_ark_fails():
    from deskbot_server.web_tools import websearch

    payload = '{"AbstractText":"兜底摘要","Heading":"标题","RelatedTopics":[]}'.encode()
    with patch(
        "deskbot_server.web_tools._ark_search_config",
        return_value=("https://ark.cn-beijing.volces.com/api/v3/responses", "k", "doubao-x"),
    ), patch(
        "deskbot_server.web_tools._post_ark_web_search",
        side_effect=RuntimeError("HTTP 429: rate limited"),
    ), patch(
        "deskbot_server.web_tools._http_get",
        return_value=(200, "application/json", payload),
    ):
        out = websearch("测试")
    assert out["ok"] is True
    assert out["provider"] == "duckduckgo"
    assert "429" in out["ark_error"]
    assert out["results"][0]["snippet"] == "兜底摘要"


def test_ark_search_config_only_for_volcengine_hosts(monkeypatch):
    from deskbot_server import web_tools
    from deskbot_server.llm.runtime import ResolvedLlmConfig

    monkeypatch.delenv("ARK_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ARK_WEB_SEARCH_MODEL", raising=False)

    def _cfg(base, key="sk-test"):
        return ResolvedLlmConfig(
            model="m", api_key=key, api_base=base, protocol="ark", source="test", display_name="t"
        )

    monkeypatch.setattr(
        "deskbot_server.llm.runtime.resolve_llm_config",
        lambda: _cfg("https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
    )
    assert web_tools._ark_search_config() == (
        "https://ark.cn-beijing.volces.com/api/v3/responses",
        "sk-test",
        "m",
    )
    monkeypatch.setattr(
        "deskbot_server.llm.runtime.resolve_llm_config",
        lambda: _cfg("https://api.xiaomimimo.com/v1"),
    )
    assert web_tools._ark_search_config() is None
    monkeypatch.setattr(
        "deskbot_server.llm.runtime.resolve_llm_config",
        lambda: _cfg("https://ark.cn-beijing.volces.com/api/v3", key=""),
    )
    assert web_tools._ark_search_config() is None


def test_ark_search_config_prefers_dedicated_key_over_llm(monkeypatch):
    """模型配置页「联网检索」：单独的方舟 Key 优先；大模型是方舟时复用；都没有则 None。"""
    from deskbot_server import web_tools
    from deskbot_server.llm.runtime import ResolvedLlmConfig

    monkeypatch.delenv("ARK_WEB_SEARCH_MODEL", raising=False)
    mimo = ResolvedLlmConfig(
        model="mimo", api_key="sk-mimo", api_base="https://api.xiaomimimo.com/v1",
        protocol="openai", source="test", display_name="t",
    )
    monkeypatch.setattr("deskbot_server.llm.runtime.resolve_llm_config", lambda: mimo)
    monkeypatch.delenv("ARK_WEB_SEARCH_API_KEY", raising=False)
    assert web_tools._ark_search_config() is None
    assert web_tools.ark_search_status()["api_key_source"] == "none"

    monkeypatch.setenv("ARK_WEB_SEARCH_API_KEY", "sk-search")
    assert web_tools._ark_search_config() == (
        web_tools.DEFAULT_ARK_RESPONSES_URL, "sk-search", web_tools.DEFAULT_ARK_WEB_SEARCH_MODEL,
    )
    status = web_tools.ark_search_status()
    assert status["api_key_source"] == "own" and status["llm_is_ark"] is False
    assert "sk-search" not in str(status)
    monkeypatch.setenv("ARK_WEB_SEARCH_MODEL", "doubao-seed-2-0-pro")
    assert web_tools._ark_search_config()[2] == "doubao-seed-2-0-pro"

    ark = ResolvedLlmConfig(
        model="doubao-seed-2-0-lite-260428", api_key="sk-ark", api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark", source="test", display_name="t",
    )
    monkeypatch.setattr("deskbot_server.llm.runtime.resolve_llm_config", lambda: ark)
    monkeypatch.delenv("ARK_WEB_SEARCH_API_KEY", raising=False)
    assert web_tools._ark_search_config()[1] == "sk-ark"
    status = web_tools.ark_search_status()
    assert status["api_key_source"] == "llm" and status["llm_is_ark"] is True
    # 单独填的 Key 比复用的优先
    monkeypatch.setenv("ARK_WEB_SEARCH_API_KEY", "sk-search")
    assert web_tools._ark_search_config()[1] == "sk-search"


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
