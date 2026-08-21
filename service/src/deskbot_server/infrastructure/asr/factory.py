"""ASR provider selection and lazy adapter construction."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from deskbot_server.asr.text_filter import is_asr_text_acceptable
from deskbot_server.core.ports.asr import AsrConfigurationError
from deskbot_server.core.settings import AppSettings
from deskbot_server.env import load_dotenv
from deskbot_server.infrastructure.asr.openai_compat import (
    OpenAiCompatibleAsrAdapter,
)
from deskbot_server.infrastructure.asr.volcengine_streaming import (
    VolcengineStreamingAsrAdapter,
)

DOUBAO_ASR_PROVIDER = "doubao_streaming"
CLOUD_ASR_PROVIDER = "openai_compatible"
LOCAL_ASR_PROVIDER = "funasr"
DEFAULT_ASR_PROVIDER = DOUBAO_ASR_PROVIDER
SUPPORTED_ASR_PROVIDERS = (
    DOUBAO_ASR_PROVIDER,
    CLOUD_ASR_PROVIDER,
    LOCAL_ASR_PROVIDER,
)


def normalize_asr_provider(value: str | None) -> str:
    provider = str(value or DEFAULT_ASR_PROVIDER).strip().lower().replace("-", "_")
    if provider in {
        "doubao",
        "doubao_asr",
        "doubao_streaming",
        "volcengine",
        "volcengine_asr",
        "volcano",
    }:
        return DOUBAO_ASR_PROVIDER
    if provider in {
        "cloud",
        "openai",
        "openai_compat",
        "openai_compatible",
        "transcription",
    }:
        return CLOUD_ASR_PROVIDER
    if provider in {"local", "sensevoice", "sense_voice", "funasr"}:
        return LOCAL_ASR_PROVIDER
    return provider


class AsrProviderRouter:
    """Select the configured provider without importing local ASR by default."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._doubao = VolcengineStreamingAsrAdapter(settings.asr)
        self._cloud = OpenAiCompatibleAsrAdapter(settings.asr)
        self._local: Any = None
        self._local_key: tuple[str, str, str] | None = None
        self._local_lock = asyncio.Lock()

    def _provider(self) -> str:
        load_dotenv()
        return normalize_asr_provider(
            os.environ.get("ASR_PROVIDER") or self._settings.asr.provider
        )

    def _current_local_key(self) -> tuple[str, str, str]:
        return (
            str(os.environ.get("ASR_MODEL_DIR") or self._settings.asr.model_dir),
            str(os.environ.get("ASR_LANGUAGE") or self._settings.asr.language),
            str(
                os.environ.get("DESKBOT_ASR_ONNX_THREADS")
                or self._settings.asr.onnx_intra_op_threads
            ),
        )

    async def _local_adapter(self) -> Any:
        wanted_key = self._current_local_key()
        if self._local is not None and self._local_key == wanted_key:
            return self._local
        async with self._local_lock:
            wanted_key = self._current_local_key()
            if self._local is not None and self._local_key == wanted_key:
                return self._local
            try:
                from deskbot_server.infrastructure.asr.funasr import FunAsrAdapter

                self._local = await asyncio.to_thread(
                    FunAsrAdapter,
                    self._settings,
                )
                self._local_key = wanted_key
            except (ImportError, ModuleNotFoundError) as exc:
                raise AsrConfigurationError(
                    "本地 FunASR 未安装。请在 service 目录执行 "
                    "pip install -e '.[local-asr]'，准备 SenseVoice 模型后重试；"
                    "也可切换到 doubao_streaming 或 openai_compatible。"
                ) from exc
            except ValueError as exc:
                raise AsrConfigurationError(str(exc)) from exc
        return self._local

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        provider = self._provider()
        if provider == DOUBAO_ASR_PROVIDER:
            return await self._doubao.transcribe(pcm_bytes, sample_rate)
        if provider == CLOUD_ASR_PROVIDER:
            return await self._cloud.transcribe(pcm_bytes, sample_rate)
        if provider == LOCAL_ASR_PROVIDER:
            adapter = await self._local_adapter()
            return await adapter.transcribe(pcm_bytes, sample_rate)
        raise AsrConfigurationError(
            f"不支持的 ASR_PROVIDER={provider!r}；"
            f"可选值为 {', '.join(SUPPORTED_ASR_PROVIDERS)}。"
        )

    def is_valid_text(self, text: str) -> bool:
        text_filter = self._settings.asr.text_filter
        return is_asr_text_acceptable(
            text,
            min_len=text_filter.min_text_len,
            min_chinese_ratio=text_filter.min_chinese_ratio,
        )


def build_asr_adapter(settings: AppSettings) -> AsrProviderRouter:
    return AsrProviderRouter(settings)
