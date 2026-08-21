from __future__ import annotations

import os

import pytest


def test_can_start_sees_credentials_written_after_process_start(
    tmp_path, monkeypatch
):
    """The console saves keys from another process; :9000 must notice.

    The web console (:5050) writes newly entered provider keys into `.env`,
    but the core process (:9000) that owns the Agent SDK keeps whatever
    os.environ held at startup.  Without a reload the 30s recovery loop
    re-reads the same stale blank values forever, so the natural first-run
    order — install, launch, fill in the keys, talk — can never bring voice
    up.  Guard the reload rather than the symptom.
    """
    from deskbot_server import env as env_module
    from deskbot_server import rtc_agent_sdk

    env_file = tmp_path / ".env"
    env_file.write_text("LLM_BASE_URL=https://example.invalid/v1\n", "utf-8")
    monkeypatch.setattr(env_module, "ENV_FILE", env_file)
    monkeypatch.setattr(env_module, "_last_signature", (0, 0), raising=False)
    monkeypatch.setattr(env_module, "_file_managed_values", {}, raising=False)

    for name in (
        "ASR_API_KEY",
        "VOLCENGINE_ASR_API_KEY",
        "DOUBAO_TTS_API_KEY",
        "VOLCENGINE_TTS_API_KEY",
        "VOLCENGINE_APP_ID",
        "VOLCENGINE_ACCESS_TOKEN",
        "DOUBAO_ASR_ACCESS_TOKEN",
        "DOUBAO_TTS_ACCESS_TOKEN",
        "ARK_API_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    manager = rtc_agent_sdk.RtcAgentSdkManager.__new__(
        rtc_agent_sdk.RtcAgentSdkManager
    )
    manager._last_error = ""
    manager._speech_adapter = ""
    manager.settings = _settings_stub()

    # Nothing configured yet: the recovery loop correctly refuses to start.
    assert manager._can_start() is False
    assert "Missing RTC configuration" in manager._last_error

    # The console writes the keys the user just entered.
    env_file.write_text(
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_MODEL=test-model\n"
        "LLM_API_KEY=llm-test-key\n"
        "ASR_API_KEY=asr-test-key\n"
        "DOUBAO_TTS_API_KEY=tts-test-key\n",
        "utf-8",
    )

    # The very next retry must pick them up, with no process restart.  The
    # gate can still stop later on for unrelated reasons (no LiveKit is
    # listening in a test run), so assert on the credential verdict itself
    # rather than on the overall result.
    manager._can_start()
    assert "Missing RTC configuration" not in manager._last_error
    assert os.environ.get("ASR_API_KEY") == "asr-test-key"
    assert os.environ.get("DOUBAO_TTS_API_KEY") == "tts-test-key"
    assert os.environ.get("LLM_API_KEY") == "llm-test-key"


def _settings_stub():
    from types import SimpleNamespace

    return SimpleNamespace(
        rtc=SimpleNamespace(
            livekit_url="ws://127.0.0.1:7880",
            token_endpoint="http://127.0.0.1:18790/rtc/token",
            agent_name="deskbot",
        ),
        llm=SimpleNamespace(base_url="", model_name=""),
    )
