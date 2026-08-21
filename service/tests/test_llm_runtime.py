from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest
from PIL import Image


def _test_jpeg(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(output, format="JPEG")
    return output.getvalue()


class _FakeHttpResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_build_chat_model_keeps_openai_compatible_model_id():
    from deskbot_server.llm.runtime import build_chat_model

    assert build_chat_model("openai", "ep-202607020001") == "ep-202607020001"
    assert build_chat_model("openai", "openai/ep-202607020001") == "ep-202607020001"


def test_resolve_system_llm_config_prefers_ark_env(monkeypatch):
    from deskbot_server import env as dotenv_module
    from deskbot_server.llm.runtime import resolve_system_llm_config

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROTOCOL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("ARK_MODEL", raising=False)
    monkeypatch.delenv("VOLCENGINE_LLM_MODEL", raising=False)
    monkeypatch.setenv("ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example.test/api/v3")
    monkeypatch.setattr(dotenv_module, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr(
        "deskbot_server.llm.runtime.load_config",
        lambda: {"llm": {"model_name": "ep-202607020001"}},
    )

    cfg = resolve_system_llm_config()

    assert cfg.api_key == "ark-test-key"
    assert cfg.api_base == "https://ark.example.test/api/v3"
    assert cfg.model == "ep-202607020001"
    assert cfg.protocol == "ark"


def test_resolve_system_llm_config_defaults_to_mimo(monkeypatch):
    from deskbot_server import env as dotenv_module
    from deskbot_server.llm.runtime import (
        DEFAULT_LLM_MODEL,
        MIMO_OPENAI_BASE_URL,
        resolve_system_llm_config,
    )

    for name in (
        "LLM_PROTOCOL",
        "LLM_MODEL",
        "ARK_MODEL",
        "VOLCENGINE_LLM_MODEL",
        "LLM_BASE_URL",
        "ARK_BASE_URL",
        "VOLCENGINE_LLM_BASE_URL",
        "VOLCENGINE_API_BASE",
        "DOUBAO_LLM_BASE_URL",
        "DASHSCOPE_BASE_URL",
        "ARK_API_KEY",
        "VOLCENGINE_API_KEY",
        "DOUBAO_API_KEY",
        "DASHSCOPE_API_KEY",
        "QWEN_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "mimo-test-key")
    monkeypatch.setattr(dotenv_module, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr("deskbot_server.llm.runtime.load_config", lambda: {"llm": {}})

    cfg = resolve_system_llm_config()

    assert cfg.model == DEFAULT_LLM_MODEL == "mimo-v2.5"
    assert cfg.protocol == "openai"
    assert cfg.api_base == MIMO_OPENAI_BASE_URL == "https://api.xiaomimimo.com/v1"
    assert cfg.api_key == "mimo-test-key"


def test_chat_completion_stream_invokes_tts_extractor(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_acompletion

    seen_deltas: list[str] = []

    def fake_stream(messages, cfg, *, temperature, json_mode, on_delta=None, timeout=60):
        assert json_mode is True
        chunks = ['{"tts":"', "你好", '","tools":[]}']
        for c in chunks:
            seen_deltas.append(c)
            if on_delta is not None:
                on_delta(c)
        return "".join(chunks), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    monkeypatch.setattr(
        "deskbot_server.llm.runtime._request_chat_completion_stream",
        fake_stream,
    )
    cfg = ResolvedLlmConfig(
        model="qwen-flash",
        api_key="test-key",
        api_base="https://dashscope.example/v1",
        protocol="dashscope",
        source="test",
        display_name="test",
    )
    tts_seen: list[str] = []

    async def _run():
        async def on_tts(text: str) -> None:
            tts_seen.append(text)

        content, meta = await chat_acompletion(
            [{"role": "user", "content": "hi"}],
            config=cfg,
            on_tts_ready=on_tts,
        )
        return content, meta

    import asyncio

    content, meta = asyncio.run(_run())
    assert content == '{"tts":"你好","tools":[]}'
    assert tts_seen == ["你好"]
    assert meta["usage"]["total_tokens"] == 3


def test_chat_completion_posts_to_openai_compatible_endpoint(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(
            {
                "choices": [{"message": {"content": '{"tts":"你好"}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            }
        )

    monkeypatch.setattr("deskbot_server.llm.runtime.safe_provider_urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="openai/ep-202607020001",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="openai",
        source="test",
        display_name="火山方舟",
    )

    content, meta = chat_completion(
        [{"role": "user", "content": "你好"}],
        config=cfg,
        json_mode=True,
        temperature=0.2,
    )

    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer ark-test-key"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "model": "ep-202607020001",
        "messages": [{"role": "user", "content": "你好"}],
            "temperature": 0.2,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
    }
    assert content == '{"tts":"你好"}'
    assert meta["model"] == "ep-202607020001"
    assert meta["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_chat_completion_uses_mimo_api_key_header(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion

    seen = {}

    def fake_urlopen(req, timeout):
        del timeout
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeHttpResponse(
            {"choices": [{"message": {"content": "你好"}}]}
        )

    monkeypatch.setattr(
        "deskbot_server.llm.runtime.safe_provider_urlopen",
        fake_urlopen,
    )
    cfg = ResolvedLlmConfig(
        model="mimo-v2.5",
        api_key="mimo-test-key",
        api_base="https://api.xiaomimimo.com/v1",
        protocol="openai",
        source="test",
        display_name="MiMo",
    )

    content, _meta = chat_completion(
        [{"role": "user", "content": "你好"}],
        config=cfg,
        json_mode=False,
    )

    assert content == "你好"
    assert seen["headers"]["api-key"] == "mimo-test-key"
    assert "authorization" not in seen["headers"]


def test_missing_key_message_mentions_volcengine_env():
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion

    cfg = ResolvedLlmConfig(
        model="ep-202607020001",
        api_key="",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="openai",
        source="test",
        display_name="火山方舟",
    )

    with pytest.raises(ValueError) as exc:
        chat_completion([{"role": "user", "content": "hi"}], config=cfg)

    assert "ARK_API_KEY" in str(exc.value)
    assert "VOLCENGINE_API_KEY" in str(exc.value)
    assert "pip install" not in str(exc.value).lower()


def test_ark_responses_completion_posts_to_responses_endpoint(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"tts":"你好"}'}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
            }
        )

    monkeypatch.setattr("deskbot_server.llm.runtime.safe_provider_urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="ep-test-model",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )

    content, meta = chat_completion(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        config=cfg,
        json_mode=True,
        temperature=0.2,
    )

    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert seen["body"]["model"] == "ep-test-model"
    assert seen["body"]["stream"] is False
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["body"]["text"] == {"format": {"type": "json_object"}}
    assert seen["body"]["input"] == [
        {"role": "system", "content": [{"type": "input_text", "text": "你是助手"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "你好"}]},
    ]
    assert content == '{"tts":"你好"}'
    assert meta["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_multimodal_blocks_map_to_chat_completions_and_ark_responses():
    from deskbot_server.llm.runtime import (
        ResolvedLlmConfig,
        _build_completion_payload,
    )
    from deskbot_server.llm.vision_input import (
        build_openai_vision_content,
        make_transient_vision_image,
    )

    jpeg = _test_jpeg((40, 120, 200))
    content = build_openai_vision_content(
        "请看这一帧",
        make_transient_vision_image(jpeg),
    )
    messages = [{"role": "user", "content": content}]
    openai_cfg = ResolvedLlmConfig(
        model="vision-model",
        api_key="test-key",
        api_base="https://api.example.test/v1",
        protocol="openai",
        source="test",
        display_name="vision",
    )
    ark_cfg = ResolvedLlmConfig(
        model="vision-endpoint",
        api_key="test-key",
        api_base="https://ark.example.test/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="vision",
    )

    chat_payload = _build_completion_payload(
        messages,
        openai_cfg,
        temperature=0.2,
        json_mode=True,
        stream=False,
    )
    assert chat_payload["messages"][0]["content"] == content
    assert chat_payload["thinking"] == {"type": "disabled"}

    ark_payload = _build_completion_payload(
        messages,
        ark_cfg,
        temperature=0.2,
        json_mode=True,
        stream=False,
    )
    assert ark_payload["input"][0]["content"] == [
        {"type": "input_text", "text": "请看这一帧"},
        {
            "type": "input_image",
            "image_url": content[1]["image_url"]["url"],
        },
    ]


def test_multimodal_payload_rejects_non_jpeg_data_url():
    from deskbot_server.llm.runtime import (
        ResolvedLlmConfig,
        _build_completion_payload,
    )
    from deskbot_server.llm.vision_input import VisionImageValidationError

    cfg = ResolvedLlmConfig(
        model="vision-model",
        api_key="test-key",
        api_base="https://api.example.test/v1",
        protocol="openai",
        source="test",
        display_name="vision",
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]

    with pytest.raises(VisionImageValidationError, match="image/jpeg"):
        _build_completion_payload(
            messages,
            cfg,
            temperature=0.2,
            json_mode=True,
            stream=False,
        )


def test_provider_vision_rejection_has_actionable_error(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion
    from deskbot_server.llm.vision_input import (
        LlmVisionUnsupportedError,
        build_openai_vision_content,
        make_transient_vision_image,
    )

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":"image input unsupported"}'),
        )

    monkeypatch.setattr(
        "deskbot_server.llm.runtime.safe_provider_urlopen",
        fake_urlopen,
    )
    cfg = ResolvedLlmConfig(
        model="text-only-model",
        api_key="test-key",
        api_base="https://api.example.test/v1",
        protocol="openai",
        source="test",
        display_name="text-only",
    )
    jpeg = _test_jpeg((80, 30, 160))
    messages = [
        {
            "role": "user",
            "content": build_openai_vision_content(
                "看图",
                make_transient_vision_image(jpeg),
            ),
        }
    ]

    with pytest.raises(LlmVisionUnsupportedError) as exc:
        chat_completion(messages, config=cfg)
    assert "高级设置" in str(exc.value)
    assert "支持视觉" in str(exc.value)


def test_ark_responses_stream_parses_output_text_delta(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_acompletion

    class _FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int = 4096) -> bytes:
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return (
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"{\\"tts\\":\\""}\n\n'
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"\\u4f60\\u597d"}\n\n'
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"\\"}"}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}\n\n'
            )

    def fake_urlopen(req, timeout):
        return _FakeStreamResponse()

    monkeypatch.setattr("deskbot_server.llm.runtime.safe_provider_urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="ep-test-model",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )

    import asyncio

    content, meta = asyncio.run(
        chat_acompletion(
            [{"role": "user", "content": "hi"}],
            config=cfg,
            stream=True,
        )
    )

    assert content == '{"tts":"你好"}'
    assert meta["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_resolve_first_token_timeout_disables_ark_responses_default(monkeypatch):
    from deskbot_server.llm.runtime import (
        LLM_FIRST_TOKEN_TIMEOUT_SECONDS,
        resolve_first_token_timeout,
    )

    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT", raising=False)
    assert resolve_first_token_timeout("ark_responses") == 0.0
    assert resolve_first_token_timeout("openai") == LLM_FIRST_TOKEN_TIMEOUT_SECONDS


def test_chat_completion_rejects_private_or_plaintext_provider_urls():
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_completion

    for api_base in (
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/latest",
        "http://example.com/v1",
    ):
        cfg = ResolvedLlmConfig(
            model="test-model",
            api_key="test-key",
            api_base=api_base,
            protocol="openai",
            source="test",
            display_name="unsafe",
        )
        with pytest.raises(ValueError):
            chat_completion([{"role": "user", "content": "hi"}], config=cfg)


def test_resolve_first_token_timeout_honors_env(monkeypatch):
    from deskbot_server.llm.runtime import resolve_first_token_timeout

    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT", "20")
    assert resolve_first_token_timeout("ark_responses") == 20.0


def test_delayed_provider_error_is_not_hidden_by_first_token_threshold(monkeypatch):
    from deskbot_server.llm.runtime import ResolvedLlmConfig, chat_acompletion

    def fake_stream(
        _messages,
        _cfg,
        *,
        temperature,
        json_mode,
        on_delta=None,
        timeout=60,
    ):
        del temperature, json_mode, on_delta, timeout
        time.sleep(0.03)
        raise RuntimeError("LLM SSE 读取失败 HTTP 401: Invalid API Key")

    monkeypatch.setattr(
        "deskbot_server.llm.runtime._request_chat_completion_stream",
        fake_stream,
    )
    cfg = ResolvedLlmConfig(
        model="mimo-v2.5",
        api_key="invalid-test-key",
        api_base="https://api.xiaomimimo.com/v1",
        protocol="openai",
        source="test",
        display_name="MiMo",
    )

    import asyncio

    with pytest.raises(RuntimeError, match="HTTP 401: Invalid API Key"):
        asyncio.run(
            chat_acompletion(
                [{"role": "user", "content": "hi"}],
                config=cfg,
                stream=True,
                first_token_timeout=0.005,
            )
        )


def test_wrap_plain_text_llm_answer():
    from deskbot_server.infrastructure.llm.openai_compat import _wrap_plain_text_llm_answer

    wrapped = _wrap_plain_text_llm_answer("明天是7月16号，星期四。")
    assert wrapped is not None
    assert '"tts": "明天是7月16号，星期四。"' in wrapped
    assert _wrap_plain_text_llm_answer('{"tts":"已有 JSON"}') is None
