from __future__ import annotations

from pathlib import Path

from deskbot_server.core.settings import AppSettings
from deskbot_server.llm.utils import parse_llm_reply

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_app_settings_from_config_defaults():
    cfg = {
        "server": {"port": 9000, "device_pb_only": True},
        "audio": {"input_codec": "opus"},
        "asr": {"text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0}},
        "llm": {"base_url": "https://example.com/v1", "model_name": "qwen-flash"},
        "tts": {"provider": "doubao", "sample_rate": 24000},
    }
    s = AppSettings.from_config(cfg)
    assert s.server.host == "127.0.0.1"
    assert s.server.port == 9000
    assert s.server.device_pb_only is True
    assert s.server.minimal_device_downlink is False
    assert not hasattr(s.server, "send_face_info_to_device")
    assert not hasattr(s.server, "ws_path")
    assert s.audio.input_codec == "opus"
    assert not hasattr(s, "vad")
    assert s.rtc.local_server_enabled is True
    assert s.rtc.livekit_url == "http://127.0.0.1:7880"
    assert s.rtc.token_endpoint == "http://127.0.0.1:18790/rtc/token"


def test_app_settings_can_use_explicit_external_livekit(monkeypatch):
    monkeypatch.setenv("DESKBOT_LOCAL_LIVEKIT_ENABLED", "0")
    monkeypatch.setenv("DESKBOT_LIVEKIT_URL", "https://rtc.example.test")
    monkeypatch.setenv(
        "DESKBOT_RTC_TOKEN_ENDPOINT", "https://rtc.example.test/rtc/token"
    )

    settings = AppSettings.from_config({})

    assert settings.rtc.local_server_enabled is False
    assert settings.rtc.livekit_url == "https://rtc.example.test"
    assert settings.rtc.token_endpoint.endswith("/rtc/token")


def test_app_settings_env_override_pb_only(monkeypatch):
    cfg = {"server": {"device_pb_only": True}}
    monkeypatch.setenv("DESKBOT_DEVICE_PB_ONLY", "0")

    s = AppSettings.from_config(cfg)

    assert s.server.device_pb_only is False


def test_app_settings_accepts_hidden_legacy_device_downlink_names(monkeypatch):
    cfg = {
        "server": {
            "asr_chat_device_pb_only": True,
            "asr_chat_minimal_device_downlink": True,
        }
    }
    monkeypatch.setenv("DESKBOT_ASR_CHAT_DEVICE_PB_ONLY", "0")

    s = AppSettings.from_config(cfg)

    assert s.server.device_pb_only is False
    assert s.server.minimal_device_downlink is True


def test_app_settings_face_info_feature_is_fully_retired(monkeypatch):
    # face_pos/face_info 下行链已整体移除：新旧 config 键与 env 一律被忽略，
    # ServerSettings 不再有该字段。
    cfg = {
        "server": {
            "device_pb_only": False,
            "send_face_info_to_asr_chat": True,
            "send_face_info_to_device": True,
        }
    }
    monkeypatch.setenv("DESKBOT_SEND_FACE_INFO", "1")
    monkeypatch.setenv("DESKBOT_SEND_FACE_INFO_TO_DEVICE", "1")

    s = AppSettings.from_config(cfg)

    assert not hasattr(s.server, "send_face_info_to_device")


def test_new_device_downlink_env_wins_over_legacy_env(monkeypatch):
    monkeypatch.setenv("DESKBOT_DEVICE_PB_ONLY", "1")
    monkeypatch.setenv("DESKBOT_ASR_CHAT_DEVICE_PB_ONLY", "0")

    s = AppSettings.from_config({})

    assert s.server.device_pb_only is True


def test_shipped_config_does_not_expose_retired_device_network_options():
    config_text = (SERVICE_ROOT / "config.yaml").read_text(encoding="utf-8")
    env_text = (SERVICE_ROOT / ".env.example").read_text(encoding="utf-8")
    platform_text = (SERVICE_ROOT / "scripts" / "platform.sh").read_text(encoding="utf-8")

    for retired in (
        "DESKBOT_WS_PATH",
        "DESKBOT_ASR_CHAT_",
        "DESKBOT_SEND_FACE_INFO=",
        "DESKBOT_DEVICE_BOOTSTRAP",
        "DESKBOT_ALLOW_INSECURE_PAIRING",
    ):
        assert retired not in env_text
    for retired in (
        "ws_path:",
        "asr_chat_device_pb_only:",
        "asr_chat_minimal_device_downlink:",
        "send_face_info_to_asr_chat:",
        "\nvad:",
    ):
        assert retired not in config_text
    assert "语音 /asr_chat" not in platform_text


def test_parse_llm_reply_json():
    raw = '{"need_reply": true, "tts": "你好", "moves": [], "anims": []}'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == "你好"
    assert parsed["need_reply"] is True
    assert parsed["moves"] == []
    assert parsed["anims"] == []


def test_parse_llm_reply_plain_text_fallback():
    parsed = parse_llm_reply("纯文本回复")
    assert parsed["reply"] == "纯文本回复"
    assert parsed["json_ok"] is False


def test_asr_model_dir_ready_nested_weight(tmp_path):
    from deskbot_server.asr_model_dir import asr_model_dir_ready

    nested = tmp_path / "iic" / "SenseVoiceSmall"
    nested.mkdir(parents=True)
    assert asr_model_dir_ready(tmp_path) is False
    (nested / "model_quant.onnx").write_bytes(b"x")
    assert asr_model_dir_ready(tmp_path) is True


def test_asr_model_dir_ignores_modelscope_partial_snapshot(tmp_path):
    from deskbot_server.asr_model_dir import asr_model_dir_ready

    staging = tmp_path / "._____temp"
    staging.mkdir()
    (staging / "model.pt").write_bytes(b"partial")

    assert asr_model_dir_ready(tmp_path) is False

    (tmp_path / "model.pt").write_bytes(b"committed")
    assert asr_model_dir_ready(tmp_path) is True
