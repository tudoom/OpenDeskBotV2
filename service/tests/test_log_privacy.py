from __future__ import annotations

import logging
from pathlib import Path


def test_content_is_redacted_from_default_logs(monkeypatch, caplog):
    from deskbot_server.log_privacy import safe_log_content

    sentinel = "SENSITIVE-SPEECH-提醒妈妈下午三点吃药"
    monkeypatch.delenv("DESKBOT_LOG_CONTENT", raising=False)
    with caplog.at_level(logging.INFO, logger="deskbot-server"):
        logging.getLogger("deskbot-server").info(
            "asr content=%s",
            safe_log_content(sentinel),
        )

    rendered = caplog.text
    assert sentinel not in rendered
    assert "<redacted len=" in rendered
    assert "sha256=" in rendered


def test_content_logging_requires_explicit_opt_in(monkeypatch):
    from deskbot_server.log_privacy import safe_log_content

    monkeypatch.setenv("DESKBOT_LOG_CONTENT", "1")
    assert "explicit-debug-content" in safe_log_content(
        "explicit-debug-content",
        limit=80,
    )


def test_high_risk_runtime_logs_do_not_use_raw_content_formats():
    root = Path(__file__).parents[1] / "src" / "deskbot_server"
    paths = [
        root / "ws" / "asr_chat.py",
        root / "ws" / "http_api.py",
        root / "application" / "chat_flow.py",
        root / "application" / "scheduled_task_scheduler.py",
        root / "infrastructure" / "tts" / "doubao_phoneme.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for unsafe in (
        " text=%r",
        " desc=%r",
        " llm_text=%r",
        " TTS=%r",
    ):
        assert unsafe not in combined
