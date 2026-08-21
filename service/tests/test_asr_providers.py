from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.error

import pytest

from deskbot_server.core.ports.asr import (
    AsrAuthenticationError,
    AsrConfigurationError,
    AsrInputError,
    AsrProviderResponseError,
)
from deskbot_server.core.settings import AppSettings


@pytest.fixture()
def app_settings(monkeypatch):
    for name in (
        "ASR_PROVIDER",
        "ASR_API_KEY",
        "ASR_ENDPOINT",
        "ASR_MODEL",
        "ASR_LANGUAGE",
        "ASR_TIMEOUT_SECONDS",
        "ASR_MAX_AUDIO_BYTES",
        "ASR_MODEL_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.load_dotenv",
        lambda: False,
    )
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.factory.load_dotenv",
        lambda: False,
    )
    return AppSettings.from_config(
        {
            "asr": {
                "provider": "openai_compatible",
                "endpoint": "https://asr.example.com/v1/audio/transcriptions",
                "model": "whisper-1",
                "language": "zh",
                "timeout_seconds": 12,
                "max_audio_bytes": 4096,
                "text_filter": {
                    "min_text_len": 2,
                    "min_chinese_ratio": 0,
                },
            }
        }
    )


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 1024 * 1024 + 1
        return self.payload


def test_unconfigured_runtime_defaults_to_doubao_streaming(monkeypatch):
    from deskbot_server.infrastructure.asr.factory import build_asr_adapter

    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.factory.load_dotenv",
        lambda: False,
    )
    settings = AppSettings.from_config({})
    router = build_asr_adapter(settings)

    assert settings.asr.provider == "doubao_streaming"
    assert router._provider() == "doubao_streaming"


def test_cloud_asr_missing_key_is_actionable_and_does_not_open_network(
    app_settings,
    monkeypatch,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    opened = False

    def fail_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("network must not be opened without a key")

    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.safe_provider_urlopen",
        fail_open,
    )
    with pytest.raises(AsrConfigurationError, match="ASR_API_KEY") as caught:
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))
    assert caught.value.code == "asr_not_configured"
    assert opened is False


def test_cloud_asr_posts_openai_compatible_multipart_without_logging_key(
    app_settings,
    monkeypatch,
    caplog,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "asr-secret-do-not-log")
    captured = {}

    def fake_open(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(json.dumps({"text": "  你好，世界  "}).encode())

    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.safe_provider_urlopen",
        fake_open,
    )
    with caplog.at_level("INFO"):
        text = asyncio.run(
            OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(
                b"\x01\x00" * 100,
                16000,
            )
        )

    assert text == "你好，世界"
    request = captured["request"]
    assert request.full_url == "https://asr.example.com/v1/audio/transcriptions"
    assert captured["timeout"] == 12
    assert request.get_header("Authorization") == "Bearer asr-secret-do-not-log"
    assert b'name="model"\r\n\r\nwhisper-1' in request.data
    assert b'name="language"\r\n\r\nzh' in request.data
    assert b"RIFF" in request.data
    assert "asr-secret-do-not-log" not in caplog.text


@pytest.mark.parametrize(
    ("status", "error_type", "code", "retryable"),
    [
        (401, AsrAuthenticationError, "asr_auth_failed", False),
        (403, AsrAuthenticationError, "asr_auth_failed", False),
        (429, Exception, "asr_rate_limited", True),
        (503, Exception, "asr_provider_unavailable", True),
    ],
)
def test_cloud_asr_classifies_provider_http_errors(
    app_settings,
    monkeypatch,
    status,
    error_type,
    code,
    retryable,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "valid-looking-key")

    def fake_open(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "provider body must not surface",
            {},
            io.BytesIO(b'{"error":{"message":"secret echo"}}'),
        )

    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.safe_provider_urlopen",
        fake_open,
    )
    with pytest.raises(error_type) as caught:
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert "secret echo" not in str(caught.value)


def test_cloud_asr_rejects_insecure_endpoint_and_oversized_audio(
    app_settings,
    monkeypatch,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "test-key")
    monkeypatch.setenv("ASR_ENDPOINT", "http://asr.example.com/transcriptions")
    with pytest.raises(AsrConfigurationError, match="HTTPS"):
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))

    monkeypatch.setenv("ASR_ENDPOINT", "https://asr.example.com/transcriptions")
    monkeypatch.setenv("ASR_MAX_AUDIO_BYTES", "1024")
    with pytest.raises(AsrInputError, match="ASR_MAX_AUDIO_BYTES"):
        asyncio.run(
            OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(
                b"\0" * 1026,
                16000,
            )
        )


def test_cloud_asr_rejects_non_openai_json_response(app_settings, monkeypatch):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "test-key")
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.safe_provider_urlopen",
        lambda *_args, **_kwargs: _Response(b'{"result":"wrong shape"}'),
    )
    with pytest.raises(AsrProviderResponseError, match="text"):
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ASR_TIMEOUT_SECONDS", "not-a-number", "必须是数字"),
        ("ASR_TIMEOUT_SECONDS", "nan", "有限数字"),
        ("ASR_MAX_AUDIO_BYTES", "12.5", "必须是整数"),
    ],
)
def test_cloud_asr_invalid_numeric_env_is_configuration_error(
    app_settings,
    monkeypatch,
    name,
    value,
    message,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "test-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(AsrConfigurationError, match=message):
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ASR_MODEL", "whisper-1\r\nInjected: yes"),
        ("ASR_LANGUAGE", "zh\nfake-log"),
        ("ASR_LANGUAGE", "x" * 33),
        # Windows rejects NUL before the adapter can validate it. Unit
        # Separator is an equivalent embedded control character that every
        # supported platform permits in the test process environment.
        ("ASR_API_KEY", "key\x1fsuffix"),
    ],
)
def test_cloud_asr_rejects_control_characters_and_oversized_language(
    app_settings,
    monkeypatch,
    name,
    value,
):
    from deskbot_server.infrastructure.asr.openai_compat import (
        OpenAiCompatibleAsrAdapter,
    )

    monkeypatch.setenv("ASR_API_KEY", "test-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(AsrConfigurationError):
        asyncio.run(OpenAiCompatibleAsrAdapter(app_settings.asr).transcribe(b"\0\0", 16000))


def test_default_router_does_not_import_or_load_funasr(app_settings):
    from deskbot_server.infrastructure.asr.factory import build_asr_adapter

    sys.modules.pop("deskbot_server.infrastructure.asr.funasr", None)
    router = build_asr_adapter(app_settings)
    assert router._provider() == "openai_compatible"
    assert "deskbot_server.infrastructure.asr.funasr" not in sys.modules


def test_unknown_provider_fails_on_first_transcription_not_service_start(
    app_settings,
    monkeypatch,
):
    from deskbot_server.infrastructure.asr.factory import build_asr_adapter

    monkeypatch.setenv("ASR_PROVIDER", "made-up-provider")
    router = build_asr_adapter(app_settings)
    with pytest.raises(AsrConfigurationError, match="不支持"):
        asyncio.run(router.transcribe(b"\0\0", 16000))


def test_asr_env_update_preserves_blank_secret_and_requires_explicit_clear(
    monkeypatch,
):
    from deskbot_server.asr import env_store

    calls = []
    monkeypatch.setattr(
        env_store,
        "update_env_keys",
        lambda updates, **kwargs: calls.append((updates, kwargs)),
    )
    monkeypatch.setattr(env_store, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr(
        env_store,
        "get_asr_config_status",
        lambda: {"configured": True},
    )

    env_store.save_asr_env({"api_key": "", "model": "whisper-1"})
    assert calls[-1][0] == {"ASR_MODEL": "whisper-1"}

    env_store.save_asr_env({"api_key": "********", "endpoint": ""})
    assert calls[-1][0] == {"ASR_ENDPOINT": ""}
    assert "ASR_ENDPOINT" in calls[-1][1]["write_empty_keys"]

    env_store.save_asr_env({"clear_api_key": True})
    assert calls[-1][0] == {
        "ASR_API_KEY": "",
        "VOLCENGINE_ASR_API_KEY": "",
    }
    assert set(calls[-1][1]["write_empty_keys"]) == set(calls[-1][0])

    with pytest.raises(ValueError, match="控制字符"):
        env_store.save_asr_env({"api_key": "secret\nASR_MODEL=injected"})


def test_asr_configuration_status_never_returns_secret(app_settings, monkeypatch):
    from deskbot_server.asr.env_store import get_asr_config_status

    monkeypatch.setattr(
        "deskbot_server.asr.env_store.load_dotenv",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.openai_compat.load_dotenv",
        lambda: False,
    )
    monkeypatch.setenv("ASR_API_KEY", "private-asr-key")
    status = get_asr_config_status(app_settings)
    assert status["configured"] is True
    assert status["api_key_set"] is True
    assert "api_key" not in status
    assert "private-asr-key" not in json.dumps(status, ensure_ascii=False)


@pytest.fixture()
def asr_api_db(monkeypatch, tmp_path):
    db_path = tmp_path / "asr-api.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    from deskbot_server import device_data

    monkeypatch.setattr(device_data, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", tmp_path / "data" / "local")
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield db_path
    finally:
        reset_engine()


def test_asr_setup_api_is_redacted_and_available_to_local_console(
    asr_api_db,
    monkeypatch,
):
    from deskbot_server.web.app import create_app
    from deskbot_server.web.blueprints import asr_bp

    monkeypatch.setattr(
        asr_bp,
        "get_asr_config_status",
        lambda _settings: {
            "provider": "openai_compatible",
            "api_key_set": True,
            "configured": True,
        },
    )

    client = create_app().test_client()
    response = client.get("/api/setup/asr")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"ok", "scope", "config"}
    assert payload["config"]["api_key_set"] is True
    assert "api_key" not in payload["config"]
    advanced = client.get("/advanced").get_data(as_text=True)
    assert "语音识别 ASR" in advanced
    assert "不会复用大模型对话的 LLM_API_KEY" in advanced
    assert "saveAsrConfig" in advanced
    assert "clearAsrKey" in advanced


def test_asr_setup_api_updates_and_tests_without_echoing_secret(
    asr_api_db,
    monkeypatch,
):
    from deskbot_server.web.app import create_app
    from deskbot_server.web.blueprints import asr_bp

    saved = []
    monkeypatch.setattr(
        asr_bp,
        "save_asr_env",
        lambda payload: (
            saved.append(dict(payload))
            or {
                "provider": "openai_compatible",
                "api_key_set": True,
                "configured": True,
            }
        ),
    )

    class Adapter:
        async def transcribe(self, pcm, sample_rate):
            assert pcm
            assert sample_rate == 16000
            return "连接成功"

    monkeypatch.setattr(asr_bp, "build_asr_adapter", lambda _settings: Adapter())
    monkeypatch.setattr(asr_bp, "_runtime_settings", lambda: object())

    client = create_app().test_client()
    updated = client.patch(
        "/api/setup/asr",
        json={"api_key": "new-private-key", "model": "whisper-1"},
    )
    assert updated.status_code == 200
    body = updated.get_json()
    assert body["config"]["api_key_set"] is True
    assert "new-private-key" not in json.dumps(body)
    assert saved == [{"api_key": "new-private-key", "model": "whisper-1"}]

    tested = client.post("/api/setup/asr/test", json={})
    assert tested.status_code == 200
    assert tested.get_json()["text"] == "连接成功"


def test_doubao_asr_status_is_default_and_redacts_api_key(monkeypatch):
    from deskbot_server.asr import env_store

    monkeypatch.setattr(env_store, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.volcengine_streaming.load_dotenv",
        lambda: False,
    )
    for name in (
        "ASR_PROVIDER",
        "ASR_ENDPOINT",
        "VOLCENGINE_ASR_ENDPOINT",
        "VOLCENGINE_ASR_RESOURCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASR_API_KEY", "private-volcengine-asr-key")
    settings = AppSettings.from_config({})

    status = env_store.get_asr_config_status(settings)

    assert status["provider"] == "doubao_streaming"
    assert status["configured"] is True
    assert status["api_key_set"] is True
    assert status["resource_id"] == "volc.seedasr.sauc.duration"
    assert status["ws_url"].endswith("/api/v3/sauc/bigmodel_async")
    assert "api_key" not in status
    assert "private-volcengine-asr-key" not in json.dumps(status)


def test_asr_save_uses_canonical_volcengine_keys_immediately(
    monkeypatch,
):
    from deskbot_server.asr import env_store
    from deskbot_server.infrastructure.asr.volcengine_streaming import (
        resolve_volcengine_streaming_asr_config,
    )

    monkeypatch.setattr(env_store, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.asr.volcengine_streaming.load_dotenv",
        lambda: False,
    )
    for name in (
        "ASR_PROVIDER",
        "ASR_API_KEY",
        "ASR_ENDPOINT",
        "VOLCENGINE_ASR_API_KEY",
        "VOLCENGINE_ASR_ENDPOINT",
        "VOLCENGINE_ASR_RESOURCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AppSettings.from_config({})
    persisted = []

    def apply_updates(updates, **_kwargs):
        persisted.append(dict(updates))
        for key, value in updates.items():
            monkeypatch.setenv(key, value)

    monkeypatch.setattr(env_store, "update_env_keys", apply_updates)
    monkeypatch.setattr(
        env_store,
        "get_asr_config_status",
        lambda: {"configured": True},
    )

    env_store.save_asr_env(
        {
            "provider": "doubao_streaming",
            "api_key": "new-volcengine-asr-key",
            "resource_id": "volc.seedasr.sauc.duration",
            "ws_url": (
                "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
            ),
        }
    )
    resolved = resolve_volcengine_streaming_asr_config(settings.asr)

    assert persisted[-1] == {
        "ASR_PROVIDER": "doubao_streaming",
        "ASR_API_KEY": "new-volcengine-asr-key",
        "VOLCENGINE_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
        "VOLCENGINE_ASR_ENDPOINT": (
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        ),
    }
    assert resolved.api_key == "new-volcengine-asr-key"
    assert resolved.resource_id == "volc.seedasr.sauc.duration"
    assert resolved.endpoint.endswith("/api/v3/sauc/bigmodel_async")
