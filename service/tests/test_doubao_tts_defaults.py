from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml


def _clear_tts_env(monkeypatch) -> None:
    from deskbot_server.tts.doubao import TTS_API_KEY_ENV_NAMES

    for name in (
        *TTS_API_KEY_ENV_NAMES,
        "DOUBAO_TTS_SPEAKER",
        "DOUBAO_TTS_RESOURCE_ID",
        "DOUBAO_TTS_MODEL",
        "DOUBAO_TTS_WS_URL",
        "DOUBAO_TTS_SAMPLE_RATE",
        "TTS_SAMPLE_RATE",
        "DOUBAO_TTS_FORMAT",
        "DOUBAO_TTS_ENABLE_TIMESTAMP",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_is_volcengine_doubao_streaming_tts_2(monkeypatch):
    from deskbot_server.tts import doubao

    _clear_tts_env(monkeypatch)
    monkeypatch.setattr(doubao, "load_dotenv", lambda: False)

    cfg = doubao.load_doubao_tts_config()

    assert cfg.api_key == ""
    assert cfg.ws_url == "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    assert cfg.resource_id == "seed-tts-2.0"
    assert cfg.model == "seed-tts-2.0-standard"
    assert cfg.speaker == "zh_female_vv_uranus_bigtts"
    assert cfg.sample_rate == 16000
    assert cfg.audio_format == "pcm"
    assert cfg.enable_timestamp is True
    assert cfg.ws_headers()["X-Api-Resource-Id"] == "seed-tts-2.0"

    public = cfg.masked()
    assert public["provider"] == "doubao"
    assert public["provider_name"] == "火山引擎豆包流式 TTS 2.0"
    assert public["protocol"] == "volcengine_v3_bidirectional_websocket"
    assert public["version"] == "2.0"
    assert public["streaming"] is True
    assert public["api_key"] == ""
    assert public["api_key_set"] is False


def test_existing_key_alias_is_preserved_but_canonical_key_wins(monkeypatch):
    from deskbot_server.tts import doubao

    _clear_tts_env(monkeypatch)
    monkeypatch.setattr(doubao, "load_dotenv", lambda: False)
    monkeypatch.setenv("SEED_TTS_API_KEY", "legacy-local-key")
    assert doubao.load_doubao_tts_config().api_key == "legacy-local-key"

    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "canonical-local-key")
    assert doubao.load_doubao_tts_config().api_key == "canonical-local-key"


def test_saving_tts2_defaults_does_not_overwrite_existing_key(tmp_path, monkeypatch):
    from deskbot_server import env as dotenv_module
    from deskbot_server.tts import env_store
    from deskbot_server.tts.doubao import DEFAULT_MODEL, DEFAULT_RESOURCE_ID, DEFAULT_SPEAKER

    env_path = tmp_path / ".env"
    env_path.write_text("DOUBAO_TTS_API_KEY=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(dotenv_module, "ENV_FILE", env_path)
    monkeypatch.setattr(dotenv_module, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(env_store, "load_dotenv", lambda *args, **kwargs: False)
    for name in env_store.DOUBAO_TTS_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)

    env_store.save_doubao_tts_env(
        {
            "api_key": "",
            "speaker": DEFAULT_SPEAKER,
            "resource_id": DEFAULT_RESOURCE_ID,
            "model": DEFAULT_MODEL,
            "enable_timestamp": True,
        }
    )

    stored = env_store.read_env_file()
    assert stored["DOUBAO_TTS_API_KEY"] == "keep-me"
    assert stored["DOUBAO_TTS_SPEAKER"] == DEFAULT_SPEAKER
    assert stored["DOUBAO_TTS_RESOURCE_ID"] == "seed-tts-2.0"
    assert stored["DOUBAO_TTS_MODEL"] == "seed-tts-2.0-standard"
    assert stored["DOUBAO_TTS_ENABLE_TIMESTAMP"] == "True"


def test_streaming_protocol_sequence_and_audio_chunks(monkeypatch):
    from deskbot_server.tts import doubao
    from deskbot_server.tts.protocols import EventType, Message, MsgType

    sequence: list[str] = []

    class FakeSocket:
        def close(self) -> None:
            sequence.append("socket_close")

    class FakeWebSocket:
        async def close(self) -> None:
            sequence.append("websocket_close")

    fake_ws = FakeWebSocket()
    messages = [
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(type=MsgType.AudioOnlyServer, event=EventType.TTSResponse, payload=b"pcm-a"),
        Message(type=MsgType.AudioOnlyServer, event=EventType.TTSResponse, payload=b"pcm-b"),
        Message(type=MsgType.FullServerResponse, event=EventType.SessionFinished),
    ]

    def fake_connect_socket(url: str, *, timeout: float):
        sequence.append("connect_socket")
        assert url == doubao.DEFAULT_WS_URL
        assert timeout == 30
        return SimpleNamespace(scheme="wss", hostname="openspeech.bytedance.com"), FakeSocket()

    async def fake_ws_connect(url: str, **kwargs):
        sequence.append("websocket_connect")
        assert url == doubao.DEFAULT_WS_URL
        assert kwargs["additional_headers"]["X-Api-Key"] == "test-key"
        return fake_ws

    async def fake_start_connection(websocket) -> None:
        assert websocket is fake_ws
        sequence.append("start_connection")

    async def fake_wait_for_event(websocket, msg_type, event) -> None:
        assert websocket is fake_ws
        assert msg_type == MsgType.FullServerResponse
        assert event == EventType.ConnectionStarted
        sequence.append("connection_started")

    async def fake_start_session(websocket, payload: bytes, session_id: str) -> None:
        assert websocket is fake_ws
        body = json.loads(payload.decode("utf-8"))
        assert body["namespace"] == "BidirectionalTTS"
        assert body["req_params"]["model"] == "seed-tts-2.0-standard"
        assert body["req_params"]["audio_params"] == {
            "format": "pcm",
            "sample_rate": 16000,
            "enable_timestamp": True,
        }
        sequence.append("start_session")

    async def fake_task_request(websocket, payload: bytes, session_id: str) -> None:
        assert json.loads(payload.decode("utf-8"))["req_params"]["text"] == "你好"
        sequence.append("task_request")

    async def fake_finish_session(websocket, session_id: str) -> None:
        sequence.append("finish_session")

    async def fake_receive_message(websocket):
        message = messages.pop(0)
        sequence.append(f"receive_{message.event.name}")
        return message

    monkeypatch.setattr(doubao, "connect_provider_websocket_socket", fake_connect_socket)
    monkeypatch.setattr(doubao.websockets, "connect", fake_ws_connect)
    monkeypatch.setattr(doubao, "start_connection", fake_start_connection)
    monkeypatch.setattr(doubao, "wait_for_event", fake_wait_for_event)
    monkeypatch.setattr(doubao, "start_session", fake_start_session)
    monkeypatch.setattr(doubao, "task_request", fake_task_request)
    monkeypatch.setattr(doubao, "finish_session", fake_finish_session)
    monkeypatch.setattr(doubao, "receive_message", fake_receive_message)
    monkeypatch.setattr(doubao, "new_session_id", lambda: "session-1")

    result = asyncio.run(
        doubao.DoubaoTtsConnection(
            doubao.DoubaoTtsConfig(api_key="test-key")
        ).synthesize("你好")
    )

    assert result.pcm == b"pcm-apcm-b"
    assert messages == []
    assert sequence.index("start_connection") < sequence.index("start_session")
    assert sequence.index("start_session") < sequence.index("task_request")
    assert sequence.index("task_request") < sequence.index("finish_session")
    assert sequence.count("receive_TTSResponse") == 2


def test_default_sample_rate_is_16k_and_pb_sr_follows(monkeypatch):
    """默认 16k，且传统 ChatService→PB 路径的 sr 字段跟随同一配置值。"""
    from deskbot_server.core.settings import AppSettings
    from deskbot_server.tts import doubao

    _clear_tts_env(monkeypatch)
    monkeypatch.setattr(doubao, "load_dotenv", lambda: False)

    assert doubao.DEFAULT_SAMPLE_RATE == 16000
    assert doubao.load_doubao_tts_config().sample_rate == 16000
    # env 覆盖仍然有效。
    monkeypatch.setenv("DOUBAO_TTS_SAMPLE_RATE", "24000")
    assert doubao.load_doubao_tts_config().sample_rate == 24000

    # PB 下发（chat_flow 读 tts_cfg["sample_rate"]）跟随 settings，而非硬编码 24k。
    settings = AppSettings.from_config({"tts": {"provider": "doubao"}})
    assert settings.tts.sample_rate == 16000
    assert settings.tts_cfg["sample_rate"] == 16000

    service_root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load(
        (service_root / "config.yaml").read_text(encoding="utf-8")
    )
    settings_from_yaml = AppSettings.from_config(config)
    assert settings_from_yaml.tts.sample_rate == 16000
    assert settings_from_yaml.tts_cfg["sample_rate"] == 16000


def test_config_and_user_interfaces_name_the_tts2_default():
    service_root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((service_root / "config.yaml").read_text(encoding="utf-8"))
    assert config["tts"]["provider"] == "doubao"
    assert config["tts"]["sample_rate"] == 16000

    env_example = (service_root / ".env.example").read_text(encoding="utf-8")
    assert "TTS_PROVIDER=doubao" in env_example
    assert "DOUBAO_TTS_API_KEY=" in env_example
    assert "DOUBAO_TTS_MODEL=seed-tts-2.0-standard" in env_example

    templates = [
        service_root / "src/deskbot_server/web/templates/app2c/advanced.html",
        service_root / "src/deskbot_server/web/templates/app2c/voice.html",
        service_root / "src/deskbot_server/web/templates/debug_tts.html",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in templates)
    assert "豆包流式 TTS 2.0" in combined
    assert "Seed Speech API Key" not in combined
    assert "seed-tts-2.0-expressive" not in combined
