"""Operator API for global ASR configuration and connectivity testing."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os

from flask import Blueprint, jsonify, request

from deskbot_server.asr.env_store import get_asr_config_status, save_asr_env
from deskbot_server.config import load_config
from deskbot_server.core.ports.asr import AsrError
from deskbot_server.core.settings import AppSettings
from deskbot_server.infrastructure.asr.factory import build_asr_adapter
from deskbot_server.web.blueprints.app_bp import _consume_settings_test_quota

bp = Blueprint("asr_setup", __name__)


def _runtime_settings() -> AppSettings:
    config_path = os.environ.get("DESKBOT_SERVER_CONFIG")
    return AppSettings.from_config(load_config(config_path))


@bp.get("/api/setup/asr")
def setup_asr_get():
    return jsonify(
        {
            "ok": True,
            "scope": "global",
            "config": get_asr_config_status(_runtime_settings()),
        }
    )


@bp.post("/api/setup/asr")
@bp.patch("/api/setup/asr")
def setup_asr_update():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象。"}), 400
    try:
        config = save_asr_env(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "保存 ASR 配置失败。"}), 500
    return jsonify({"ok": True, "config": config})


def _test_pcm(payload: dict) -> tuple[bytes, int]:
    try:
        sample_rate = int(payload.get("sample_rate") or 16000)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_rate 必须是整数。") from exc
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError("sample_rate 必须在 8000 到 48000 Hz 之间。")
    encoded = str(payload.get("pcm_base64") or "").strip()
    if not encoded:
        # A one-second silent clip verifies DNS/TLS/authentication/model access
        # without storing or shipping a user's microphone sample.
        return b"\x00\x00" * sample_rate, sample_rate
    if len(encoded) > 14 * 1024 * 1024:
        raise ValueError("ASR 测试音频过大。")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("pcm_base64 不是有效的 Base64。") from exc
    if not pcm or len(pcm) % 2:
        raise ValueError("测试音频必须是非空的 s16le PCM。")
    return pcm, sample_rate


def _asr_error_status(exc: AsrError) -> int:
    if exc.code == "asr_rate_limited":
        return 429
    if exc.code == "asr_provider_unavailable":
        return 503
    if exc.code in {"asr_invalid_response", "asr_error"}:
        return 502
    return 400


@bp.post("/api/setup/asr/test")
def setup_asr_test():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象。"}), 400
    quota, limit_error = _consume_settings_test_quota()
    if limit_error:
        return limit_error
    try:
        pcm, sample_rate = _test_pcm(payload)
        adapter = build_asr_adapter(_runtime_settings())
        text = asyncio.run(adapter.transcribe(pcm, sample_rate))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "quota": quota}), 400
    except AsrError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "provider_status": exc.status_code,
                    "quota": quota,
                }
            ),
            _asr_error_status(exc),
        )
    except Exception:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ASR 测试发生未预期错误，请查看脱敏后的服务日志。",
                    "code": "asr_test_failed",
                    "quota": quota,
                }
            ),
            500,
        )
    return jsonify(
        {
            "ok": True,
            "text": text,
            "message": (
                "云 ASR 连接成功，测试静音没有识别文本。"
                if not text and not payload.get("pcm_base64")
                else "ASR 测试成功。"
            ),
            "quota": quota,
        }
    )
