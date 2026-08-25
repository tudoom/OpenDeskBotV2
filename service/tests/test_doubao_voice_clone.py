from __future__ import annotations

import base64
import json


def test_clone_doubao_voice_posts_v3_payload(monkeypatch):
    from deskbot_server.tts.voice_clone import (
        DoubaoVoiceCloneConfig,
        clone_doubao_voice,
        custom_speaker_id_from_name,
    )

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status_code":0,"speaker_id":"brufik_wo_de_sheng_yin","status":1}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("deskbot_server.tts.voice_clone.safe_provider_urlopen", fake_urlopen)
    cfg = DoubaoVoiceCloneConfig(app_key="app-id", access_key="access-token")

    result = clone_doubao_voice(
        cfg,
        audio_bytes=b"RIFF....WAVE",
        audio_format="wav",
        language=0,
        display_name="我的声音",
        custom_speaker_id=custom_speaker_id_from_name("我的声音"),
        prompt_text="这是一段训练文本",
    )

    assert captured["url"] == "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
    assert captured["headers"]["X-api-app-key"] == "app-id"
    assert captured["headers"]["X-api-access-key"] == "access-token"
    assert captured["headers"]["X-api-resource-id"] == "seed-icl-2.0"
    assert captured["payload"]["speaker_id"] == "custom_speaker_id"
    assert captured["payload"]["custom_speaker_id"] == "brufik_wo_de_sheng_yin"
    assert captured["payload"]["language"] == 0
    assert captured["payload"]["display_name"] == "我的声音"
    assert captured["payload"]["audio"]["format"] == "wav"
    assert captured["payload"]["audio"]["text"] == "这是一段训练文本"
    assert captured["payload"]["audio"]["data"] == base64.b64encode(b"RIFF....WAVE").decode("ascii")
    assert result.speaker_id == "brufik_wo_de_sheng_yin"
    assert result.ready is False
    assert result.status == 1
    assert result.status_label == "训练中"


def test_get_doubao_voice_clone_status_normalizes_speaker_status(monkeypatch):
    from deskbot_server.tts.voice_clone import DoubaoVoiceCloneConfig, get_doubao_voice_clone_status

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return (
                b'{"speaker_status":[{"speaker_id":"S_ready","status":4,'
                b'"model_type":5,"available_training_times":8}]}'
            )

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("deskbot_server.tts.voice_clone.safe_provider_urlopen", fake_urlopen)
    cfg = DoubaoVoiceCloneConfig(app_key="app-id", access_key="access-token")

    result = get_doubao_voice_clone_status(cfg, "S_ready")

    assert captured["url"] == "https://openspeech.bytedance.com/api/v3/tts/get_voice"
    assert captured["headers"]["X-api-resource-id"] == "seed-icl-2.0"
    assert captured["payload"] == {"speaker_id": "S_ready"}
    assert result.speaker_id == "S_ready"
    assert result.status == 4
    assert result.status_label == "可用"
    assert result.ready is True
    assert result.model_type == 5


def test_clone_authenticates_with_the_shared_tts_key():
    """复刻不再需要单独的 App ID / Access Token。

    voice_clone / get_voice 接受与双向流式 TTS 相同的 X-Api-Key：带该头会走
    到资源校验（55000000），不带则被 45000000 "app key not found in header"
    挡在鉴权层。此前 headers() 硬性要求 App ID + Access Token，而只配了统一
    TTS 密钥的账号两者皆空，声音复刻整块功能因此始终被界面拦住无法使用。
    """
    from deskbot_server.tts.voice_clone import DoubaoVoiceCloneConfig

    cfg = DoubaoVoiceCloneConfig(api_key="tts-key", resource_id="seed-icl-2.0")
    headers = cfg.headers()
    assert headers["X-Api-Key"] == "tts-key"
    assert headers["X-Api-Resource-Id"] == "seed-icl-2.0"
    assert "X-Api-App-Key" not in headers
    assert "X-Api-Access-Key" not in headers

    # 只有旧凭证的账号继续按旧方式鉴权。
    legacy = DoubaoVoiceCloneConfig(app_key="app-id", access_key="access-token")
    legacy_headers = legacy.headers()
    assert legacy_headers["X-Api-App-Key"] == "app-id"
    assert "X-Api-Key" not in legacy_headers

    # 两者都没有才报错，且提示指向统一密钥。
    try:
        DoubaoVoiceCloneConfig().headers()
    except ValueError as exc:
        assert "DOUBAO_TTS_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing credentials must raise")


def test_status_query_uses_the_custom_speaker_convention(monkeypatch):
    """后付费自定义音色的查询体与训练接口同构。

    官方约定 speaker_id 必须是固定字面量 "custom_speaker_id"，真实代号放在
    custom_speaker_id 里；把代号直接填进 speaker_id 会被判成 55000000
    "resource ID is mismatched with speaker related resource"，于是训练早已
    完成的音色在界面上永远显示"未知"。
    """
    from deskbot_server.tts import voice_clone

    seen = {}

    def fake_post(url, payload, headers, *, timeout):
        seen["payload"] = payload
        # 顶层带权威 status/model_type；speaker_status 每行没有 status，
        # 且首行可能是 1.0 变体，解析必须以顶层为准。
        return {
            "speaker_id": payload.get("custom_speaker_id") or payload.get("speaker_id"),
            "status": 4,
            "model_type": 5,
            "speaker_status": [{"model_type": 1, "model_version": 1}],
        }

    monkeypatch.setattr(voice_clone, "_post_json", fake_post)
    cfg = voice_clone.DoubaoVoiceCloneConfig(api_key="tts-key")

    result = voice_clone.get_doubao_voice_clone_status(cfg, "brufik_demo_voice")
    assert seen["payload"]["speaker_id"] == "custom_speaker_id"
    assert seen["payload"]["custom_speaker_id"] == "brufik_demo_voice"
    assert result.status == 4
    assert result.model_type == 5
    assert result.ready is True

    # 官方分配的音色代号仍按原样查询。
    voice_clone.get_doubao_voice_clone_status(cfg, "S_official123")
    assert seen["payload"] == {"speaker_id": "S_official123"}
