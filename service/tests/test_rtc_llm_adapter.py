from __future__ import annotations

from types import SimpleNamespace

import pytest

from deskbot_server import rtc_llm_adapter as adapter


def test_responses_base_url_normalizes_chat_and_responses_suffixes():
    assert (
        adapter.responses_base_url("https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert (
        adapter.responses_base_url("https://ark.cn-beijing.volces.com/api/v3/responses/")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert (
        adapter.responses_base_url("https://ark.cn-beijing.volces.com/api/v3")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )


def test_web_search_enabled_only_for_ark_and_not_when_disabled(monkeypatch):
    monkeypatch.delenv("DESKBOT_RTC_WEB_SEARCH", raising=False)
    assert adapter.web_search_enabled_for("https://ark.cn-beijing.volces.com/api/v3")
    assert not adapter.web_search_enabled_for("https://api.xiaomimimo.com/v1")
    assert not adapter.web_search_enabled_for("")
    monkeypatch.setenv("DESKBOT_RTC_WEB_SEARCH", "0")
    assert not adapter.web_search_enabled_for("https://ark.cn-beijing.volces.com/api/v3")


def test_ark_web_search_provider_tool_serializes_plugin_params():
    pytest.importorskip("livekit.plugins.openai")
    from livekit.agents import ProviderTool

    tool = adapter.build_ark_web_search_tool()
    assert isinstance(tool, ProviderTool)
    assert tool.id == "ark_web_search"
    body = tool.to_dict()
    assert body["type"] == "web_search"
    assert 1 <= body["max_keyword"] <= 10
    assert body["limit"] >= 1


def _fake_lampgo(*, provider_type="openai", base_url="https://ark.cn-beijing.volces.com/api/v3"):
    provider = SimpleNamespace(
        type=provider_type,
        get_extra=lambda key: {"api_key": "sk-test", "base_url": base_url}.get(key),
    )
    component = SimpleNamespace(
        options={"model": "doubao-x", "temperature": 0.3, "max_tokens": 160}
    )
    config = SimpleNamespace(provider_for=lambda component, path: provider)
    runtime = SimpleNamespace(voice_agent=SimpleNamespace(name="deskbot", llm=component))
    return config, runtime


def test_params_from_lampgo_mirrors_create_llm_options():
    pytest.importorskip("lampgo_livekit_agent")
    config, runtime = _fake_lampgo()
    params = adapter.params_from_lampgo(config, runtime)
    assert params == adapter.ArkLlmParams(
        model="doubao-x",
        api_key="sk-test",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        temperature=0.3,
        max_output_tokens=160,
    )
    config, runtime = _fake_lampgo(provider_type="volcengine")
    assert adapter.params_from_lampgo(config, runtime) is None


def test_install_patches_both_lampgo_aliases_and_falls_back_for_non_ark(monkeypatch):
    lampgo_llm = pytest.importorskip("lampgo_livekit_agent.llm")
    lampgo_worker = pytest.importorskip("lampgo_livekit_agent.worker")

    calls: list[str] = []
    monkeypatch.setattr(lampgo_llm, "create_llm", lambda *, config, runtime: calls.append("orig") or "ORIG")
    monkeypatch.setattr(lampgo_worker, "create_llm", lampgo_llm.create_llm)
    monkeypatch.setattr(adapter, "_installed", False)
    monkeypatch.delenv(adapter.ACTIVE_ENV, raising=False)

    assert adapter.install_lampgo_llm_adapter() is True
    assert lampgo_worker.create_llm is lampgo_llm.create_llm

    # 非方舟网关：原样退回原工厂，且不标记内置联网激活。
    config, runtime = _fake_lampgo(base_url="https://api.xiaomimimo.com/v1")
    assert lampgo_llm.create_llm(config=config, runtime=runtime) == "ORIG"
    assert calls == ["orig"]
    assert not adapter.ark_web_search_active()

    # 方舟网关：构造 Responses LLM 并标记激活。
    built: list[adapter.ArkLlmParams] = []
    monkeypatch.setattr(adapter, "build_ark_responses_llm", lambda p: built.append(p) or "ARK")
    config, runtime = _fake_lampgo()
    assert lampgo_llm.create_llm(config=config, runtime=runtime) == "ARK"
    assert built and built[0].model == "doubao-x"
    assert adapter.ark_web_search_active()

    # 构造失败必须退回原链路，不能让语音会话起不来。
    monkeypatch.setattr(
        adapter, "build_ark_responses_llm", lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert lampgo_llm.create_llm(config=config, runtime=runtime) == "ORIG"
    assert not adapter.ark_web_search_active()


def test_deskbot_tools_swap_core_websearch_for_builtin_when_active(monkeypatch):
    pytest.importorskip("livekit.agents")
    from deskbot_server.rtc_worker_tools import build_livekit_deskbot_tools

    monkeypatch.delenv(adapter.ACTIVE_ENV, raising=False)
    names = {getattr(t, "id", "") or getattr(t, "name", "") for t in build_livekit_deskbot_tools()}
    assert "ark_web_search" not in names

    monkeypatch.setenv(adapter.ACTIVE_ENV, "1")
    tools = build_livekit_deskbot_tools()
    ids = [getattr(t, "id", "") for t in tools]
    assert "ark_web_search" in ids
    fn_names = {
        getattr(getattr(t, "info", None), "name", "") for t in tools if hasattr(t, "info")
    }
    assert "websearch" not in fn_names
    assert "webfetch" in fn_names


def test_ark_responses_llm_disables_thinking_and_server_state(monkeypatch):
    pytest.importorskip("livekit.plugins.openai")
    import asyncio

    from livekit.agents.llm import ChatContext
    from livekit.plugins.openai.responses import llm as responses_llm

    # chat() 会为流创建任务并立即发请求；这里只验证请求参数，不真的联网。
    async def _no_network(self):
        self._response_completed = True

    monkeypatch.setattr(responses_llm.LLMStream, "_run_impl", _no_network)

    llm = adapter.build_ark_responses_llm(
        adapter.ArkLlmParams(
            model="doubao-x",
            api_key="sk-test",
            base_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            temperature=0.3,
            max_output_tokens=160,
        )
    )
    assert llm._opts.use_websocket is False
    assert llm._opts.store is False
    assert llm.provider == "ark.cn-beijing.volces.com"

    async def _capture() -> dict:
        ctx = ChatContext()
        ctx.add_message(role="user", content="hi")
        stream = llm.chat(chat_ctx=ctx, tools=[])
        try:
            return dict(stream._extra_kwargs)
        finally:
            await stream.aclose()
            await llm.aclose()

    extra = asyncio.run(_capture())
    assert extra["extra_body"] == {"thinking": {"type": "disabled"}}
    assert extra["store"] is False
    assert extra["max_output_tokens"] == 160
