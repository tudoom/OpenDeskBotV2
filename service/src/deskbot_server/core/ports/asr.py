from __future__ import annotations

from typing import Protocol


class AsrError(RuntimeError):
    """Base class for actionable ASR failures.

    ``code`` is safe to expose through the pipeline API.  Exception messages
    must likewise be suitable for users and logs: provider response bodies and
    credentials are deliberately not retained here.
    """

    code = "asr_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


class AsrConfigurationError(AsrError):
    code = "asr_not_configured"


class AsrInputError(AsrError):
    code = "asr_invalid_audio"


class AsrAuthenticationError(AsrError):
    code = "asr_auth_failed"


class AsrRateLimitError(AsrError):
    code = "asr_rate_limited"
    retryable = True


class AsrProviderUnavailableError(AsrError):
    code = "asr_provider_unavailable"
    retryable = True


class AsrProviderResponseError(AsrError):
    code = "asr_invalid_response"


class AsrPort(Protocol):
    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str: ...

    def is_valid_text(self, text: str) -> bool: ...
