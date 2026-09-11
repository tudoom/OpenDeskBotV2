"""OpenAI-compatible cloud speech-to-text adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from deskbot_server.core.concurrency import asr_infer_slot
from deskbot_server.core.ports.asr import (
    AsrAuthenticationError,
    AsrConfigurationError,
    AsrInputError,
    AsrProviderResponseError,
    AsrProviderUnavailableError,
    AsrRateLimitError,
)
from deskbot_server.core.settings import AsrSettings
from deskbot_server.env import load_dotenv
from deskbot_server.safe_fetch import (
    safe_provider_urlopen,
    validate_provider_http_url,
)
from deskbot_server.util import pcm_to_wav_bytes

logger = logging.getLogger("deskbot-server")

DEFAULT_TRANSCRIPTION_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_TRANSCRIPTION_MODEL = "whisper-1"
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class OpenAiCompatibleAsrConfig:
    endpoint: str
    model: str
    api_key: str
    language: str
    timeout_seconds: float
    max_audio_bytes: int


def _runtime_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return float(fallback)
    try:
        value = float(raw)
    except ValueError as exc:
        raise AsrConfigurationError(f"{name} 必须是数字。") from exc
    if not math.isfinite(value):
        raise AsrConfigurationError(f"{name} 必须是有限数字。")
    return value


def _runtime_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(fallback)
    try:
        return int(raw)
    except ValueError as exc:
        raise AsrConfigurationError(f"{name} 必须是整数。") from exc


def _has_unsafe_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def resolve_openai_compatible_asr_config(
    settings: AsrSettings,
) -> OpenAiCompatibleAsrConfig:
    """Resolve current cloud settings.

    Environment variables are intentionally read for every request so an API
    key saved by the management UI becomes effective without restarting the
    USB service.  ASR uses its own credential and never falls back to an LLM
    or TTS key.
    """

    # :5050 persists settings to .env while :9000 owns the live ChatService.
    # The signature-aware loader makes atomic cross-process updates visible on
    # the next utterance without replacing unrelated process environment.
    load_dotenv()
    return OpenAiCompatibleAsrConfig(
        endpoint=(
            os.environ.get("ASR_ENDPOINT")
            or settings.endpoint
            or DEFAULT_TRANSCRIPTION_ENDPOINT
        ).strip(),
        model=(
            os.environ.get("ASR_MODEL")
            or settings.model
            or DEFAULT_TRANSCRIPTION_MODEL
        ).strip(),
        api_key=(os.environ.get("ASR_API_KEY") or "").strip(),
        language=(os.environ.get("ASR_LANGUAGE") or settings.language or "zh").strip(),
        timeout_seconds=_runtime_float(
            "ASR_TIMEOUT_SECONDS",
            settings.timeout_seconds,
        ),
        max_audio_bytes=_runtime_int(
            "ASR_MAX_AUDIO_BYTES",
            settings.max_audio_bytes,
        ),
    )


def validate_openai_compatible_asr_config(
    config: OpenAiCompatibleAsrConfig,
    *,
    require_api_key: bool = True,
) -> None:
    if require_api_key and not config.api_key:
        raise AsrConfigurationError(
            "云 ASR 尚未配置。请在 PC 服务的高级设置中填写 ASR_API_KEY，"
            "或在 service/.env 中配置后重试。"
        )
    if not config.endpoint:
        raise AsrConfigurationError(
            "云 ASR endpoint 为空，请配置 ASR_ENDPOINT。"
        )
    if len(config.endpoint) > 2048 or _has_unsafe_control(config.endpoint):
        raise AsrConfigurationError(
            "ASR_ENDPOINT 长度不能超过 2048，且不能包含控制字符。"
        )
    try:
        validate_provider_http_url(config.endpoint)
    except (TypeError, ValueError) as exc:
        raise AsrConfigurationError(
            "ASR_ENDPOINT 必须是受信任的 HTTPS 地址；"
            "自托管内网服务需由管理员显式放行精确 origin。"
        ) from exc
    if not config.model:
        raise AsrConfigurationError(
            "云 ASR 模型为空，请配置 ASR_MODEL（例如 whisper-1）。"
        )
    if len(config.model) > 200:
        raise AsrConfigurationError("ASR_MODEL 长度不能超过 200 个字符。")
    if _has_unsafe_control(config.model):
        raise AsrConfigurationError("ASR_MODEL 不能包含换行或其他控制字符。")
    if len(config.language) > 32 or _has_unsafe_control(config.language):
        raise AsrConfigurationError(
            "ASR_LANGUAGE 长度不能超过 32，且不能包含控制字符。"
        )
    if _has_unsafe_control(config.api_key):
        raise AsrConfigurationError("ASR_API_KEY 不能包含换行或控制字符。")
    if config.timeout_seconds < 1 or config.timeout_seconds > 300:
        raise AsrConfigurationError(
            "ASR_TIMEOUT_SECONDS 必须在 1 到 300 秒之间。"
        )
    if config.max_audio_bytes < 1024 or config.max_audio_bytes > 100 * 1024 * 1024:
        raise AsrConfigurationError(
            "ASR_MAX_AUDIO_BYTES 必须在 1 KiB 到 100 MiB 之间。"
        )


def _multipart_body(
    wav_bytes: bytes,
    *,
    model: str,
    language: str,
) -> tuple[bytes, str]:
    boundary = f"----deskbot-asr-{uuid.uuid4().hex}"
    marker = boundary.encode("ascii")
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend(
            [
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    add_field("model", model)
    add_field("response_format", "json")
    if language and language.lower() != "auto":
        add_field("language", language)
    chunks.extend(
        [
            b"--" + marker + b"\r\n",
            (
                'Content-Disposition: form-data; name="file"; '
                'filename="deskbot-audio.wav"\r\n'
            ).encode("ascii"),
            b"Content-Type: audio/wav\r\n\r\n",
            wav_bytes,
            b"\r\n",
            b"--" + marker + b"--\r\n",
        ]
    )
    return b"".join(chunks), boundary


def _http_error(status: int) -> Exception:
    if status in (401, 403):
        return AsrAuthenticationError(
            "云 ASR 鉴权失败，请检查 ASR_API_KEY 是否属于该语音服务且仍然有效。",
            status_code=status,
        )
    if status in (408, 409, 425, 429):
        return AsrRateLimitError(
            f"云 ASR 暂时繁忙或触发限流（HTTP {status}），请稍后重试。",
            status_code=status,
        )
    if status == 404:
        return AsrConfigurationError(
            "云 ASR 返回 HTTP 404，请检查 ASR_ENDPOINT 和 ASR_MODEL。",
            status_code=status,
        )
    if status in (400, 413, 415, 422):
        return AsrInputError(
            f"云 ASR 拒绝了音频请求（HTTP {status}），"
            "请检查模型、音频大小和语言配置。",
            status_code=status,
        )
    if status >= 500:
        return AsrProviderUnavailableError(
            f"云 ASR 服务暂时不可用（HTTP {status}），请稍后重试。",
            status_code=status,
        )
    return AsrProviderResponseError(
        f"云 ASR 返回未预期的 HTTP 状态 {status}。",
        status_code=status,
    )


class OpenAiCompatibleAsrAdapter:
    """Upload mono PCM as WAV to an OpenAI-compatible transcription API."""

    def __init__(self, settings: AsrSettings) -> None:
        self._settings = settings

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        if not pcm_bytes:
            return ""
        if sample_rate < 8000 or sample_rate > 192000:
            raise AsrInputError(
                f"不支持的 ASR 采样率 {sample_rate} Hz；请使用 8 kHz 到 192 kHz。"
            )
        config = resolve_openai_compatible_asr_config(self._settings)
        validate_openai_compatible_asr_config(config)
        if len(pcm_bytes) > config.max_audio_bytes:
            raise AsrInputError(
                f"本轮音频大小 {len(pcm_bytes)} B 超过 ASR_MAX_AUDIO_BYTES "
                f"限制 {config.max_audio_bytes} B。"
            )
        async with asr_infer_slot():
            return await asyncio.to_thread(
                self._transcribe_sync,
                pcm_bytes,
                sample_rate,
                config,
            )

    @staticmethod
    def _transcribe_sync(
        pcm_bytes: bytes,
        sample_rate: int,
        config: OpenAiCompatibleAsrConfig,
    ) -> str:
        wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate)
        if len(wav_bytes) > config.max_audio_bytes + 128:
            raise AsrInputError(
                "WAV 封装后的音频超过云 ASR 请求大小限制，请缩短单次录音。"
            )
        body, boundary = _multipart_body(
            wav_bytes,
            model=config.model,
            language=config.language,
        )
        request = urllib.request.Request(
            config.endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "deskbot-server/0.1",
            },
        )
        logger.info(
            "[ASR] cloud transcription request provider=openai_compatible "
            "model=%s pcm_bytes=%d sample_rate=%d",
            config.model,
            len(pcm_bytes),
            sample_rate,
        )
        try:
            with safe_provider_urlopen(
                request,
                timeout=config.timeout_seconds,
            ) as response:
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            finally:
                raise _http_error(int(exc.code)) from None
        except (TimeoutError, socket.timeout) as exc:
            raise AsrProviderUnavailableError(
                "云 ASR 请求超时，请检查 PC 网络或增大 ASR_TIMEOUT_SECONDS。"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                message = (
                    "云 ASR 请求超时，请检查 PC 网络或增大 "
                    "ASR_TIMEOUT_SECONDS。"
                )
            else:
                message = "无法连接云 ASR，请检查 PC 网络、DNS 和 ASR_ENDPOINT。"
            raise AsrProviderUnavailableError(message) from exc
        except OSError as exc:
            raise AsrProviderUnavailableError(
                "无法连接云 ASR，请检查 PC 网络和 TLS 证书。"
            ) from exc

        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise AsrProviderResponseError(
                "云 ASR 响应超过 1 MiB 安全上限，已拒绝处理。"
            )
        try:
            payload: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AsrProviderResponseError(
                "云 ASR 返回的不是有效 JSON；请确认 endpoint 兼容 "
                "OpenAI transcription API。"
            ) from exc
        if not isinstance(payload, dict):
            raise AsrProviderResponseError("云 ASR JSON 响应必须是对象。")
        text = payload.get("text")
        if not isinstance(text, str):
            raise AsrProviderResponseError(
                "云 ASR 响应缺少字符串字段 text；请检查 provider 兼容性。"
            )
        return text.strip()
