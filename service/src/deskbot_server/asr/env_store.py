"""Safe ASR configuration persistence and redacted status reporting."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from deskbot_server.asr_model_dir import asr_model_dir_ready
from deskbot_server.config import load_config
from deskbot_server.core.settings import AppSettings
from deskbot_server.env import load_dotenv, update_env_keys
from deskbot_server.env import looks_masked as _looks_masked
from deskbot_server.infrastructure.asr.factory import (
    CLOUD_ASR_PROVIDER,
    DOUBAO_ASR_PROVIDER,
    LOCAL_ASR_PROVIDER,
    SUPPORTED_ASR_PROVIDERS,
    normalize_asr_provider,
)
from deskbot_server.infrastructure.asr.openai_compat import (
    resolve_openai_compatible_asr_config,
    validate_openai_compatible_asr_config,
)
from deskbot_server.infrastructure.asr.volcengine_streaming import (
    resolve_volcengine_streaming_asr_config,
)
from deskbot_server.paths import PROJECT_ROOT

ASR_ENV_KEYS = (
    "ASR_PROVIDER",
    "ASR_API_KEY",
    "ASR_ENDPOINT",
    "ASR_MODEL",
    "ASR_LANGUAGE",
    "ASR_TIMEOUT_SECONDS",
    "ASR_MAX_AUDIO_BYTES",
    "ASR_MODEL_DIR",
    "VOLCENGINE_ASR_API_KEY",
    "VOLCENGINE_ASR_RESOURCE_ID",
    "VOLCENGINE_ASR_ENDPOINT",
)

_ENV_KEY_BY_FIELD = {
    "provider": "ASR_PROVIDER",
    "api_key": "ASR_API_KEY",
    "endpoint": "ASR_ENDPOINT",
    "model": "ASR_MODEL",
    "language": "ASR_LANGUAGE",
    "timeout_seconds": "ASR_TIMEOUT_SECONDS",
    "max_audio_bytes": "ASR_MAX_AUDIO_BYTES",
    "model_dir": "ASR_MODEL_DIR",
    "resource_id": "VOLCENGINE_ASR_RESOURCE_ID",
    "ws_url": "VOLCENGINE_ASR_ENDPOINT",
}


def _reject_control_characters(field: str, value: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} 不能包含换行或其他控制字符。")


def _settings_from_disk() -> AppSettings:
    config_path = os.environ.get("DESKBOT_SERVER_CONFIG")
    return AppSettings.from_config(load_config(config_path))


def _local_model_path(settings: AppSettings) -> Path:
    raw = (
        os.environ.get("ASR_MODEL_DIR")
        or settings.asr.model_dir
        or str(PROJECT_ROOT / "models" / "SenseVoiceSmall")
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def get_asr_config_status(
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    """Return an operator-facing status without ever returning the API key."""

    load_dotenv(force_reload=True)
    current = settings or _settings_from_disk()
    provider = normalize_asr_provider(
        os.environ.get("ASR_PROVIDER") or current.asr.provider
    )
    common: dict[str, Any] = {
        "provider": provider,
        "providers": list(SUPPORTED_ASR_PROVIDERS),
        "configured": False,
        "configuration_status": "unconfigured",
        "message": "",
        "api_key_set": False,
    }
    if provider == DOUBAO_ASR_PROVIDER:
        resolved = resolve_volcengine_streaming_asr_config(current.asr)
        api_key = str(getattr(resolved, "api_key", "") or "").strip()
        legacy_app_id = str(getattr(resolved, "app_id", "") or "").strip()
        legacy_access_token = str(
            getattr(resolved, "access_token", "") or ""
        ).strip()
        credentials_set = bool(
            api_key or (legacy_app_id and legacy_access_token)
        )
        common.update(
            {
                "endpoint": resolved.ws_url,
                "ws_url": resolved.ws_url,
                "model": getattr(resolved, "model", "bigmodel"),
                "resource_id": resolved.resource_id,
                "language": resolved.language,
                "timeout_seconds": resolved.timeout_seconds,
                "max_audio_bytes": resolved.max_audio_bytes,
                "api_key_set": credentials_set,
                "model_dir": "",
                "local_dependencies_installed": None,
                "local_model_ready": None,
            }
        )
        try:
            from deskbot_server.safe_fetch import (
                validate_provider_websocket_url,
            )

            validate_provider_websocket_url(
                resolved.ws_url,
                resolve_dns=False,
            )
        except Exception as exc:
            common["message"] = str(exc)
            return common
        if not credentials_set:
            common["message"] = (
                "请填写火山豆包流式 ASR 的 API Key；凭证仅保存在 PC。"
            )
            return common
        common.update(
            {
                "configured": True,
                "configuration_status": "configured",
                "message": "火山豆包流式 ASR 已配置。",
            }
        )
        return common

    if provider == CLOUD_ASR_PROVIDER:
        resolved = resolve_openai_compatible_asr_config(current.asr)
        common.update(
            {
                "endpoint": resolved.endpoint,
                "model": resolved.model,
                "language": resolved.language,
                "timeout_seconds": resolved.timeout_seconds,
                "max_audio_bytes": resolved.max_audio_bytes,
                "api_key_set": bool(resolved.api_key),
                "model_dir": "",
                "local_dependencies_installed": None,
                "local_model_ready": None,
            }
        )
        try:
            validate_openai_compatible_asr_config(
                resolved,
                require_api_key=False,
            )
        except Exception as exc:
            common["message"] = str(exc)
            return common
        if not resolved.api_key:
            common["message"] = (
                "请填写独立的 ASR_API_KEY；LLM 的 ARK_API_KEY 不会用于语音识别。"
            )
            return common
        common.update(
            {
                "configured": True,
                "configuration_status": "configured",
                "message": "云 ASR 已配置。",
            }
        )
        return common

    if provider == LOCAL_ASR_PROVIDER:
        model_path = _local_model_path(current)
        dependencies_installed = bool(
            importlib.util.find_spec("funasr")
            and importlib.util.find_spec("funasr_onnx")
        )
        model_ready = asr_model_dir_ready(model_path)
        common.update(
            {
                "endpoint": "",
                "model": "",
                "language": (
                    os.environ.get("ASR_LANGUAGE")
                    or current.asr.language
                    or "zh"
                ),
                "timeout_seconds": None,
                "max_audio_bytes": None,
                "model_dir": str(model_path),
                "local_dependencies_installed": dependencies_installed,
                "local_model_ready": model_ready,
            }
        )
        if not dependencies_installed:
            common["message"] = (
                "本地 ASR 可选依赖未安装；执行 pip install -e '.[local-asr]'。"
            )
            return common
        if not model_ready:
            common["message"] = (
                "SenseVoice 模型未就绪；请自行准备模型并设置 ASR_MODEL_DIR。"
            )
            return common
        common.update(
            {
                "configured": True,
                "configuration_status": "configured",
                "message": "本地 FunASR 已配置。",
            }
        )
        return common

    common["message"] = (
        f"不支持的 ASR_PROVIDER={provider!r}；"
        f"可选值为 {', '.join(SUPPORTED_ASR_PROVIDERS)}。"
    )
    return common


def save_asr_env(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist partial ASR settings.

    An omitted or blank ``api_key`` preserves the existing secret.
    ``clear_api_key=true`` is required to remove it.
    """

    if not isinstance(payload, dict):
        raise ValueError("ASR 配置必须是 JSON 对象。")
    updates: dict[str, str] = {}
    explicit_empty: set[str] = set()

    if "provider" in payload:
        provider = normalize_asr_provider(str(payload.get("provider") or ""))
        if provider not in SUPPORTED_ASR_PROVIDERS:
            raise ValueError(
                f"不支持的 ASR provider：{provider or '(空)'}。"
            )
        updates["ASR_PROVIDER"] = provider

    if bool(payload.get("clear_api_key")):
        for key in (
            "ASR_API_KEY",
            "VOLCENGINE_ASR_API_KEY",
        ):
            updates[key] = ""
            explicit_empty.add(key)
    elif "api_key" in payload:
        api_key = str(payload.get("api_key") or "").strip()
        if api_key and not _looks_masked(api_key):
            _reject_control_characters("ASR API Key", api_key)
            if len(api_key) > 4096:
                raise ValueError("ASR API Key 长度不能超过 4096 个字符。")
            updates["ASR_API_KEY"] = api_key

    for field in (
        "endpoint",
        "model",
        "language",
        "model_dir",
        "resource_id",
        "ws_url",
    ):
        if field not in payload:
            continue
        env_key = _ENV_KEY_BY_FIELD[field]
        value = str(payload.get(field) or "").strip()
        _reject_control_characters(field, value)
        updates[env_key] = value
        if not value:
            explicit_empty.add(env_key)

    if updates.get("ASR_ENDPOINT"):
        from deskbot_server.safe_fetch import validate_provider_http_url

        validate_provider_http_url(updates["ASR_ENDPOINT"])
    if updates.get("VOLCENGINE_ASR_ENDPOINT"):
        from deskbot_server.safe_fetch import validate_provider_websocket_url

        validate_provider_websocket_url(
            updates["VOLCENGINE_ASR_ENDPOINT"],
            resolve_dns=False,
        )

    if "ASR_MODEL" in updates and (
        not updates["ASR_MODEL"] or len(updates["ASR_MODEL"]) > 200
    ):
        raise ValueError("ASR 模型名称不能为空且不能超过 200 个字符。")
    if "ASR_LANGUAGE" in updates and len(updates["ASR_LANGUAGE"]) > 32:
        raise ValueError("ASR 语言代码不能超过 32 个字符。")
    if (
        "VOLCENGINE_ASR_RESOURCE_ID" in updates
        and len(updates["VOLCENGINE_ASR_RESOURCE_ID"]) > 256
    ):
        raise ValueError("火山语音 Resource ID 不能超过 256 个字符。")

    if "timeout_seconds" in payload:
        try:
            timeout = float(payload.get("timeout_seconds"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ASR timeout 必须是数字。") from exc
        if timeout < 1 or timeout > 300:
            raise ValueError("ASR timeout 必须在 1 到 300 秒之间。")
        updates["ASR_TIMEOUT_SECONDS"] = str(timeout)

    if "max_audio_bytes" in payload:
        try:
            max_bytes = int(payload.get("max_audio_bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ASR 音频大小上限必须是整数。") from exc
        if max_bytes < 1024 or max_bytes > 100 * 1024 * 1024:
            raise ValueError("ASR 音频大小上限必须在 1 KiB 到 100 MiB 之间。")
        updates["ASR_MAX_AUDIO_BYTES"] = str(max_bytes)

    if updates:
        update_env_keys(
            updates,
            keys=ASR_ENV_KEYS,
            comment="# 语音识别 ASR",
            write_empty_keys=explicit_empty,
        )
        load_dotenv(force_reload=True)
    return get_asr_config_status()
