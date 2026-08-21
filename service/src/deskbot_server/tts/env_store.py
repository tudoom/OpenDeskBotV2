"""读写 .env 中的豆包 TTS 配置。

通用 .env 读写器已上移到 ``deskbot_server.env``（read_env_file /
update_env_keys）；本模块的同名再导出仅为兼容保留一版（deprecated），
新代码请直接 import ``deskbot_server.env``。
"""

from __future__ import annotations

from typing import Iterable

from deskbot_server.env import (
    load_dotenv,
    read_env_file,  # noqa: F401  (deprecated re-export)
)
from deskbot_server.env import update_env_keys as _update_env_keys
from deskbot_server.safe_fetch import (
    validate_provider_http_url,
    validate_provider_websocket_url,
)
from deskbot_server.tts.doubao import _is_masked_secret

DOUBAO_TTS_ENV_KEYS = (
    "DOUBAO_TTS_API_KEY",
    "DOUBAO_TTS_SPEAKER",
    "DOUBAO_TTS_RESOURCE_ID",
    "DOUBAO_TTS_MODEL",
    "DOUBAO_TTS_WS_URL",
    "DOUBAO_TTS_SAMPLE_RATE",
    "DOUBAO_TTS_FORMAT",
    "DOUBAO_TTS_ENABLE_TIMESTAMP",
    "DOUBAO_TTS_APP_ID",
    "DOUBAO_TTS_ACCESS_TOKEN",
    "DOUBAO_TTS_VOICE_CLONE_RESOURCE_ID",
    "DOUBAO_TTS_VOICE_CLONE_URL",
    "DOUBAO_TTS_VOICE_STATUS_URL",
)

_PAYLOAD_FIELD_BY_ENV_KEY = {
    "DOUBAO_TTS_API_KEY": "api_key",
    "DOUBAO_TTS_SPEAKER": "speaker",
    "DOUBAO_TTS_RESOURCE_ID": "resource_id",
    "DOUBAO_TTS_MODEL": "model",
    "DOUBAO_TTS_WS_URL": "ws_url",
    "DOUBAO_TTS_SAMPLE_RATE": "sample_rate",
    "DOUBAO_TTS_FORMAT": "audio_format",
    "DOUBAO_TTS_ENABLE_TIMESTAMP": "enable_timestamp",
    "DOUBAO_TTS_APP_ID": "app_id",
    "DOUBAO_TTS_ACCESS_TOKEN": "access_token",
    "DOUBAO_TTS_VOICE_CLONE_RESOURCE_ID": "voice_clone_resource_id",
    "DOUBAO_TTS_VOICE_CLONE_URL": "voice_clone_url",
    "DOUBAO_TTS_VOICE_STATUS_URL": "voice_status_url",
}


def update_env_keys(
    updates: dict[str, str],
    *,
    keys: Iterable[str] | None = None,
    comment: str = "# 配置",
    write_empty_keys: Iterable[str] | None = None,
) -> None:
    """Deprecated re-export：请改用 ``deskbot_server.env.update_env_keys``。

    仅为存量调用方保留一版；``keys`` 缺省仍是豆包 TTS 键集。
    """
    _update_env_keys(
        updates,
        keys=keys or DOUBAO_TTS_ENV_KEYS,
        comment=comment,
        write_empty_keys=write_empty_keys,
    )


def save_doubao_tts_env(payload: dict[str, str]) -> None:
    """保存豆包 TTS 配置到 .env 并刷新进程内环境变量。留空字段不覆盖已有值。"""
    updates: dict[str, str] = {}
    for env_key in DOUBAO_TTS_ENV_KEYS:
        payload_key = _PAYLOAD_FIELD_BY_ENV_KEY[env_key]
        if payload_key not in payload:
            continue
        raw = str(payload.get(payload_key) or "").strip()
        if env_key in ("DOUBAO_TTS_API_KEY", "DOUBAO_TTS_ACCESS_TOKEN") and _is_masked_secret(raw):
            continue
        if not raw:
            continue
        updates[env_key] = raw
    if updates.get("DOUBAO_TTS_WS_URL"):
        validate_provider_websocket_url(
            updates["DOUBAO_TTS_WS_URL"],
            resolve_dns=False,
        )
    for key in (
        "DOUBAO_TTS_VOICE_CLONE_URL",
        "DOUBAO_TTS_VOICE_STATUS_URL",
    ):
        if updates.get(key):
            validate_provider_http_url(updates[key])
    update_env_keys(updates, comment="# 豆包语音 TTS 2.0")
    load_dotenv()
