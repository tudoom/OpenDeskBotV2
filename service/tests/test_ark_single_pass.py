from __future__ import annotations

import pytest

from deskbot_server.llm import ark_single_pass as sp
from deskbot_server.llm.runtime import ResolvedLlmConfig


def _cfg(base="https://ark.cn-beijing.volces.com/api/v3", key="sk-test", protocol="openai"):
    return ResolvedLlmConfig(
        model="doubao-x", api_key=key, api_base=base, protocol=protocol, source="test", display_name="t"
    )


def test_single_pass_available_only_for_ark_with_key(monkeypatch):
    monkeypatch.delenv("DESKBOT_RTC_WEB_SEARCH", raising=False)
    assert sp.single_pass_available(_cfg())
    assert sp.single_pass_available(_cfg(base="https://ark.cn-beijing.volces.com/api/v3/chat/completions"))
    assert not sp.single_pass_available(_cfg(base="https://api.xiaomimimo.com/v1"))
    assert not sp.single_pass_available(_cfg(key=""))
    monkeypatch.setenv("DESKBOT_RTC_WEB_SEARCH", "0")
    assert not sp.single_pass_available(_cfg())


_RESPONSE = {
    "output": [
        {"type": "reasoning", "summary": []},
        {"type": "web_search_call", "status": "completed", "action": {"query": "北京 天气"}},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": '{"need_reply": true, "tts": "北京晴 30℃", "tools": []}'}],
        },
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5, "tool_usage": {"web_search": 1}},
}


def test_ark_single_pass_completion_builds_responses_payload(monkeypatch):
    captured: dict = {}

    def _fake_post(url, payload, api_key):
        captured.update(url=url, payload=payload, api_key=api_key)
        return _RESPONSE

    monkeypatch.setattr(sp, "_post", _fake_post)
    messages = [
        {"role": "system", "content": "你是小歪"},
        {"role": "user", "content": "今天北京天气怎么样"},
    ]
    text, meta = sp.ark_single_pass_completion(
        messages, config=_cfg(base="https://ark.cn-beijing.volces.com/api/v3/chat/completions"), temperature=0.5
    )
    assert '"tts": "北京晴 30℃"' in text
    assert meta["provider"] == "ark_single_pass"
    assert meta["web_search_calls"] == 1
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert captured["api_key"] == "sk-test"
    payload = captured["payload"]
    assert payload["model"] == "doubao-x"
    assert payload["stream"] is False
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.5
    assert payload["tools"][0]["type"] == "web_search"
    # 不能强制 JSON 格式，否则模型不再真正联网
    assert "text" not in payload
    roles = [item.get("role") for item in payload["input"]]
    assert roles == ["system", "user"]


def test_ark_single_pass_converts_native_function_call_into_tool_json(monkeypatch):
    """豆包会把提示词里的 JSON 工具当成原生 function_call 返回；折算回 tools 约定。"""
    from deskbot_server.llm.utils import parse_llm_reply

    response = {
        "output": [
            {
                "type": "function_call",
                "status": "completed",
                "name": "capture_camera",
                "arguments": '{"display": false}',
                "call_id": "call_1",
            }
        ],
        "usage": {},
    }
    monkeypatch.setattr(sp, "_post", lambda url, payload, api_key: response)
    text, meta = sp.ark_single_pass_completion([{"role": "user", "content": "画面里有什么"}], config=_cfg())
    parsed = parse_llm_reply(text)
    assert parsed["tools"] == [{"tool": "capture_camera", "display": False}]
    assert meta["web_search_calls"] == 0


def test_ark_single_pass_completion_raises_without_text(monkeypatch):
    monkeypatch.setattr(sp, "_post", lambda url, payload, api_key: {"output": [], "usage": {}})
    with pytest.raises(RuntimeError):
        sp.ark_single_pass_completion([{"role": "user", "content": "hi"}], config=_cfg())


def test_ark_single_pass_keeps_function_call_when_text_also_present(monkeypatch):
    """"我看一下画面" + function_call：过渡语和工具都要保留。"""
    from deskbot_server.llm.utils import parse_llm_reply

    response = {
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "我看一下画面"}]},
            {"type": "function_call", "status": "completed", "name": "capture_camera",
             "arguments": "{}", "call_id": "c1"},
        ],
        "usage": {},
    }
    monkeypatch.setattr(sp, "_post", lambda url, payload, api_key: response)
    text, _meta = sp.ark_single_pass_completion([{"role": "user", "content": "画面里有什么"}], config=_cfg())
    parsed = parse_llm_reply(text)
    assert [t["tool"] for t in parsed["tools"]] == ["capture_camera"]
    assert "我看一下画面" in str(parsed.get("reply") or "")

    # 文本本身已是 JSON（含 tools）时，把原生 function_call 并进去而不是覆盖
    response["output"][0]["content"][0]["text"] = '{"need_reply": true, "tts": "好", "tools": [{"tool": "websearch", "query": "x"}]}'
    text, _meta = sp.ark_single_pass_completion([{"role": "user", "content": "q"}], config=_cfg())
    assert [t["tool"] for t in parse_llm_reply(text)["tools"]] == ["websearch", "capture_camera"]
