"""Volcengine Seed ASR 2.0 streaming WebSocket adapter.

The public ``AsrPort`` accepts a complete mono S16LE utterance.  This adapter
keeps that contract while sending the buffered PCM to Volcengine in 200 ms
streaming frames and returning the final cumulative transcript.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import math
import os
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    InvalidStatus,
    InvalidURI,
    WebSocketException,
)

from deskbot_server.asr.text_filter import is_asr_text_acceptable
from deskbot_server.core.concurrency import asr_infer_slot
from deskbot_server.core.ports.asr import (
    AsrAuthenticationError,
    AsrConfigurationError,
    AsrError,
    AsrInputError,
    AsrProviderResponseError,
    AsrProviderUnavailableError,
    AsrRateLimitError,
)
from deskbot_server.core.settings import AsrSettings
from deskbot_server.env import load_dotenv
from deskbot_server.safe_fetch import (
    connect_provider_websocket_socket,
    validate_provider_websocket_url,
)

logger = logging.getLogger("deskbot-server")
_wire_logger = logging.getLogger("deskbot-server.asr.volcengine.websocket-wire")
_wire_logger.setLevel(logging.CRITICAL + 1)
_wire_logger.propagate = False
_wire_logger.disabled = True
if not _wire_logger.handlers:
    _wire_logger.addHandler(logging.NullHandler())

DEFAULT_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 8.0
DEFAULT_FINAL_TIMEOUT_SECONDS = 8.0
PCM_SAMPLE_RATE = 16_000
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_CHANNELS = 1
PCM_FRAME_DURATION_MS = 200
PCM_FRAME_BYTES = (
    PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS * PCM_FRAME_DURATION_MS // 1000
)

MSG_FULL_CLIENT_REQUEST = 0x1
MSG_AUDIO_ONLY_REQUEST = 0x2
MSG_FULL_SERVER_RESPONSE = 0x9
MSG_SERVER_ERROR = 0xF

FLAG_NO_SEQUENCE = 0x0
FLAG_POSITIVE_SEQUENCE = 0x1
FLAG_FINAL_NO_SEQUENCE = 0x2
FLAG_NEGATIVE_SEQUENCE = 0x3

SERIALIZATION_NONE = 0x0
SERIALIZATION_JSON = 0x1
COMPRESSION_NONE = 0x0
COMPRESSION_GZIP = 0x1

SUCCESS_CODES = frozenset({0, 1000, 20_000_000})
MAX_SERVER_FRAME_BYTES = 2 * 1024 * 1024
MAX_SERVER_PAYLOAD_BYTES = 1024 * 1024
MAX_SERVER_FRAMES = 4096
_SAFE_LOG_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PREWARM_TTL_SECONDS = 60.0
# 静默期不无限重建握手：TTL 到期/服务端断开最多连续补充这么多次，之后
# 停止预热，等下一次真实识别结束后再重新调度。
PREWARM_MAX_IDLE_REFRESHES = 3


@dataclass(frozen=True, slots=True)
class VolcengineStreamingAsrConfig:
    endpoint: str
    api_key: str
    resource_id: str
    timeout_seconds: float
    max_audio_bytes: int
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    final_timeout_seconds: float = DEFAULT_FINAL_TIMEOUT_SECONDS

    @property
    def ws_url(self) -> str:
        """Compatibility alias used by the existing ASR settings UI."""

        return self.endpoint

    @property
    def model(self) -> str:
        return "bigmodel"

    @property
    def language(self) -> str:
        return "zh-CN"

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True, slots=True)
class ServerFrame:
    message_type: int
    flags: int
    serialization: int
    compression: int
    sequence: int | None
    error_code: int
    payload: bytes

    @property
    def is_final(self) -> bool:
        return self.flags in {FLAG_FINAL_NO_SEQUENCE, FLAG_NEGATIVE_SEQUENCE} or (
            self.sequence is not None and self.sequence < 0
        )


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


def _configured_websocket_endpoint(settings: AsrSettings) -> str:
    specific = str(os.environ.get("VOLCENGINE_ASR_ENDPOINT") or "").strip()
    if specific:
        return specific
    shared = str(os.environ.get("ASR_ENDPOINT") or settings.endpoint or "").strip()
    if shared.lower().startswith(("ws://", "wss://")):
        return shared
    return DEFAULT_ENDPOINT


def resolve_volcengine_streaming_asr_config(
    settings: AsrSettings,
) -> VolcengineStreamingAsrConfig:
    """Resolve credentials for each utterance so UI-saved values apply live."""

    load_dotenv()
    timeout_seconds = _runtime_float("ASR_TIMEOUT_SECONDS", settings.timeout_seconds)
    return VolcengineStreamingAsrConfig(
        endpoint=_configured_websocket_endpoint(settings),
        api_key=str(
            os.environ.get("VOLCENGINE_ASR_API_KEY") or os.environ.get("ASR_API_KEY") or ""
        ).strip(),
        resource_id=str(
            os.environ.get("VOLCENGINE_ASR_RESOURCE_ID")
            or os.environ.get("ASR_RESOURCE_ID")
            or DEFAULT_RESOURCE_ID
        ).strip(),
        timeout_seconds=timeout_seconds,
        max_audio_bytes=_runtime_int(
            "ASR_MAX_AUDIO_BYTES",
            settings.max_audio_bytes,
        ),
        connect_timeout_seconds=min(
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
            timeout_seconds,
        ),
        final_timeout_seconds=min(
            DEFAULT_FINAL_TIMEOUT_SECONDS,
            timeout_seconds,
        ),
    )


def validate_volcengine_streaming_asr_config(
    config: VolcengineStreamingAsrConfig,
    *,
    require_credentials: bool = True,
) -> None:
    if require_credentials and not config.api_key:
        raise AsrConfigurationError("火山豆包 ASR 尚未配置 API Key，请设置 ASR_API_KEY 后重试。")
    if config.api_key and (len(config.api_key) > 4096 or _has_unsafe_control(config.api_key)):
        raise AsrConfigurationError("ASR_API_KEY 长度或字符格式无效。")
    if (
        not config.resource_id
        or len(config.resource_id) > 128
        or not config.resource_id.startswith("volc.")
        or any(char.isspace() for char in config.resource_id)
        or _has_unsafe_control(config.resource_id)
    ):
        raise AsrConfigurationError("火山豆包 ASR Resource ID 格式无效。")
    if not config.endpoint or len(config.endpoint) > 2048 or _has_unsafe_control(config.endpoint):
        raise AsrConfigurationError("火山豆包 ASR endpoint 格式无效。")
    try:
        parsed = validate_provider_websocket_url(
            config.endpoint,
            resolve_dns=False,
        )
    except (TypeError, ValueError) as exc:
        raise AsrConfigurationError("火山豆包 ASR endpoint 必须是受信任的 WSS 地址。") from exc
    if parsed.scheme.lower() != "wss":
        raise AsrConfigurationError("火山豆包 ASR endpoint 必须使用 WSS。")
    if config.timeout_seconds < 1 or config.timeout_seconds > 300:
        raise AsrConfigurationError("ASR_TIMEOUT_SECONDS 必须在 1 到 300 秒之间。")
    if (
        config.connect_timeout_seconds <= 0
        or config.connect_timeout_seconds > config.timeout_seconds
        or config.final_timeout_seconds <= 0
        or config.final_timeout_seconds > config.timeout_seconds
    ):
        raise AsrConfigurationError("火山豆包 ASR 连接或最终结果超时配置无效。")
    if config.max_audio_bytes < 1024 or config.max_audio_bytes > 100 * 1024 * 1024:
        raise AsrConfigurationError("ASR_MAX_AUDIO_BYTES 必须在 1 KiB 到 100 MiB 之间。")


def recognition_payload(
    *,
    uid: str | None = None,
    sample_rate: int = PCM_SAMPLE_RATE,
) -> dict[str, Any]:
    return {
        "user": {"uid": uid or str(uuid.uuid4())},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": sample_rate,
            "bits": PCM_SAMPLE_WIDTH_BYTES * 8,
            "channel": PCM_CHANNELS,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_nonstream": True,
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
        },
    }


def _build_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        (
            0x11,
            ((message_type & 0x0F) << 4) | (flags & 0x0F),
            ((serialization & 0x0F) << 4) | (compression & 0x0F),
            0,
        )
    )


def _gzip_compress(payload: bytes) -> bytes:
    return gzip.compress(payload, mtime=0)


def _gzip_decompress_limited(payload: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as stream:
            decoded = stream.read(MAX_SERVER_PAYLOAD_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise AsrProviderResponseError("火山豆包 ASR 响应的 gzip 数据无效。") from exc
    if len(decoded) > MAX_SERVER_PAYLOAD_BYTES:
        raise AsrProviderResponseError("火山豆包 ASR 响应解压后超过 1 MiB 安全上限。")
    return decoded


def encode_full_client_request(payload: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AsrConfigurationError("火山豆包 ASR 初始化请求无法序列化。") from exc
    compressed = _gzip_compress(raw)
    return (
        _build_header(
            MSG_FULL_CLIENT_REQUEST,
            FLAG_NO_SEQUENCE,
            SERIALIZATION_JSON,
            COMPRESSION_GZIP,
        )
        + struct.pack(">I", len(compressed))
        + compressed
    )


def encode_audio_request(audio: bytes, *, final_packet: bool = False) -> bytes:
    compressed = _gzip_compress(bytes(audio))
    return (
        _build_header(
            MSG_AUDIO_ONLY_REQUEST,
            FLAG_FINAL_NO_SEQUENCE if final_packet else FLAG_NO_SEQUENCE,
            SERIALIZATION_NONE,
            COMPRESSION_GZIP,
        )
        + struct.pack(">I", len(compressed))
        + compressed
    )


def _read_u32(data: bytes, offset: int, field: str) -> tuple[int, int]:
    end = offset + 4
    if end > len(data):
        raise AsrProviderResponseError(f"火山豆包 ASR 响应缺少 {field}。")
    return struct.unpack(">I", data[offset:end])[0], end


def parse_server_frame(data: bytes) -> ServerFrame:
    raw = bytes(data)
    if len(raw) < 4:
        raise AsrProviderResponseError("火山豆包 ASR 响应帧过短。")
    if len(raw) > MAX_SERVER_FRAME_BYTES:
        raise AsrProviderResponseError("火山豆包 ASR 响应帧超过 2 MiB 安全上限。")

    version = raw[0] >> 4
    header_size = (raw[0] & 0x0F) * 4
    if version != 1:
        raise AsrProviderResponseError("火山豆包 ASR 响应协议版本无效。")
    if header_size < 4 or header_size > len(raw):
        raise AsrProviderResponseError("火山豆包 ASR 响应帧头无效。")

    message_type = raw[1] >> 4
    flags = raw[1] & 0x0F
    serialization = raw[2] >> 4
    compression = raw[2] & 0x0F
    if flags not in {
        FLAG_NO_SEQUENCE,
        FLAG_POSITIVE_SEQUENCE,
        FLAG_FINAL_NO_SEQUENCE,
        FLAG_NEGATIVE_SEQUENCE,
    }:
        raise AsrProviderResponseError("火山豆包 ASR 响应序列标志无效。")
    if serialization not in {SERIALIZATION_NONE, SERIALIZATION_JSON}:
        raise AsrProviderResponseError("火山豆包 ASR 响应序列化格式不受支持。")
    if compression not in {COMPRESSION_NONE, COMPRESSION_GZIP}:
        raise AsrProviderResponseError("火山豆包 ASR 响应压缩格式不受支持。")

    offset = header_size
    sequence = None
    if flags in {FLAG_POSITIVE_SEQUENCE, FLAG_NEGATIVE_SEQUENCE}:
        sequence_raw, offset = _read_u32(raw, offset, "sequence")
        sequence = struct.unpack(">i", struct.pack(">I", sequence_raw))[0]

    error_code = 0
    if message_type == MSG_SERVER_ERROR:
        error_code, offset = _read_u32(raw, offset, "error code")
    payload_size, offset = _read_u32(raw, offset, "payload size")
    if payload_size > MAX_SERVER_FRAME_BYTES:
        raise AsrProviderResponseError("火山豆包 ASR 响应负载长度超限。")
    end = offset + payload_size
    if end > len(raw):
        raise AsrProviderResponseError("火山豆包 ASR 响应内容不完整。")
    if end != len(raw):
        raise AsrProviderResponseError("火山豆包 ASR 响应包含未声明的尾部数据。")

    encoded_payload = raw[offset:end]
    payload = (
        encoded_payload
        if compression == COMPRESSION_NONE
        else _gzip_decompress_limited(encoded_payload)
    )
    if len(payload) > MAX_SERVER_PAYLOAD_BYTES:
        raise AsrProviderResponseError("火山豆包 ASR 响应超过 1 MiB 安全上限。")
    return ServerFrame(
        message_type=message_type,
        flags=flags,
        serialization=serialization,
        compression=compression,
        sequence=sequence,
        error_code=error_code,
        payload=payload,
    )


def _coerce_response_code(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise AsrProviderResponseError("火山豆包 ASR 响应状态码格式无效。")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise AsrProviderResponseError("火山豆包 ASR 响应状态码格式无效。") from exc
    raise AsrProviderResponseError("火山豆包 ASR 响应状态码格式无效。")


def _provider_code_error(code: int) -> AsrError:
    if code == 45_000_081:
        return AsrProviderUnavailableError(
            "火山豆包 ASR 等待音频包超时，请重试本轮语音。",
            status_code=code,
        )
    if code == 55_000_031:
        return AsrRateLimitError(
            "火山豆包 ASR 服务繁忙，请稍后重试。",
            status_code=code,
        )
    if 55_000_000 <= code < 56_000_000:
        return AsrProviderUnavailableError(
            f"火山豆包 ASR 服务暂时不可用（code={code}）。",
            status_code=code,
        )
    if 45_000_000 <= code < 46_000_000:
        return AsrInputError(
            f"火山豆包 ASR 拒绝了本轮音频或请求参数（code={code}）。",
            status_code=code,
        )

    if code in {401, 403}:
        return AsrAuthenticationError(
            "火山豆包 ASR 鉴权失败，请检查 API Key 与 Resource ID。",
            status_code=code,
        )
    if code in {408, 409, 425, 429}:
        return AsrRateLimitError(
            f"火山豆包 ASR 暂时繁忙或限流（code={code}），请稍后重试。",
            status_code=code,
        )
    if 500 <= code <= 599:
        return AsrProviderUnavailableError(
            f"火山豆包 ASR 服务暂时不可用（code={code}）。",
            status_code=code,
        )
    return AsrProviderResponseError(
        f"火山豆包 ASR 返回错误（code={code}）。",
        status_code=code,
    )


def decode_response_payload(frame: ServerFrame) -> dict[str, Any]:
    if not frame.payload:
        return {}
    if frame.serialization != SERIALIZATION_JSON:
        raise AsrProviderResponseError("火山豆包 ASR 响应必须使用 JSON 序列化。")
    try:
        value = json.loads(frame.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsrProviderResponseError("火山豆包 ASR 响应 JSON 无效。") from exc
    if not isinstance(value, dict):
        raise AsrProviderResponseError("火山豆包 ASR 响应 JSON 必须是对象。")
    code = _coerce_response_code(value.get("code", value.get("status_code")))
    if code not in SUCCESS_CODES:
        raise _provider_code_error(code)
    return value


def extract_transcript(payload: Mapping[str, Any]) -> str:
    result = payload.get("result")
    source: Mapping[str, Any] = result if isinstance(result, Mapping) else payload
    utterances = source.get("utterances")
    if isinstance(utterances, list):
        parts = [
            str(item.get("text"))
            for item in utterances
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        combined = "".join(parts).strip()
        if combined:
            return combined
    for candidate in (source.get("text"), payload.get("text")):
        if isinstance(candidate, str):
            return candidate.strip()
    return ""


def _safe_log_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_LOG_ID.fullmatch(text) else ""


def _response_log_id(websocket: Any) -> str:
    response = getattr(websocket, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        return _safe_log_id(headers.get("X-Tt-Logid") or headers.get("x-tt-logid"))
    except (AttributeError, KeyError, TypeError):
        return ""


def _request_headers(
    config: VolcengineStreamingAsrConfig,
    *,
    request_id: str,
    connect_id: str,
) -> dict[str, str]:
    return {
        "X-Api-Key": config.api_key,
        "X-Api-Resource-Id": config.resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
        "X-Api-Connect-Id": connect_id,
    }


def _handshake_status(exc: InvalidStatus) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code"))
    except (TypeError, ValueError):
        return None


def _http_status_error(status: int | None) -> AsrError:
    if status in {401, 403}:
        return AsrAuthenticationError(
            "火山豆包 ASR 鉴权失败，请检查 API Key 与 Resource ID。",
            status_code=status,
        )
    if status in {408, 409, 425, 429}:
        return AsrRateLimitError(
            f"火山豆包 ASR 暂时繁忙或限流（HTTP {status}），请稍后重试。",
            status_code=status,
        )
    if status is not None and status >= 500:
        return AsrProviderUnavailableError(
            f"火山豆包 ASR 服务暂时不可用（HTTP {status}）。",
            status_code=status,
        )
    return AsrProviderResponseError(
        (
            f"火山豆包 ASR WebSocket 握手失败（HTTP {status}）。"
            if status is not None
            else "火山豆包 ASR WebSocket 握手失败。"
        ),
        status_code=status,
    )


def _server_error(frame: ServerFrame) -> AsrError:
    return _provider_code_error(frame.error_code)


async def _connect_websocket(
    config: VolcengineStreamingAsrConfig,
    *,
    request_id: str,
    connect_id: str,
) -> tuple[Any, str]:
    parsed, pinned_socket = await asyncio.to_thread(
        connect_provider_websocket_socket,
        config.endpoint,
        timeout=config.connect_timeout_seconds,
    )
    connect_kwargs: dict[str, Any] = {"sock": pinned_socket}
    if parsed.scheme.lower() == "wss":
        connect_kwargs["server_hostname"] = parsed.hostname
    try:
        websocket = await websockets.connect(
            config.endpoint,
            additional_headers=_request_headers(
                config,
                request_id=request_id,
                connect_id=connect_id,
            ),
            compression=None,
            max_size=MAX_SERVER_FRAME_BYTES,
            max_queue=32,
            open_timeout=config.connect_timeout_seconds,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            user_agent_header="deskbot-server/0.1",
            logger=_wire_logger,
            **connect_kwargs,
        )
    except BaseException:
        pinned_socket.close()
        raise
    return websocket, _response_log_id(websocket)


async def _receive_final_transcript(websocket: Any) -> str:
    latest_text = ""
    for _ in range(MAX_SERVER_FRAMES):
        message = await websocket.recv()
        if not isinstance(message, (bytes, bytearray, memoryview)):
            raise AsrProviderResponseError("火山豆包 ASR 返回了非二进制 WebSocket 消息。")
        frame = parse_server_frame(bytes(message))
        if frame.message_type == MSG_SERVER_ERROR:
            raise _server_error(frame)
        if frame.message_type != MSG_FULL_SERVER_RESPONSE:
            raise AsrProviderResponseError(
                f"火山豆包 ASR 返回未知消息类型 0x{frame.message_type:x}。"
            )
        payload = decode_response_payload(frame)
        text = extract_transcript(payload)
        if text:
            latest_text = text
        if frame.is_final:
            return latest_text
    raise AsrProviderResponseError("火山豆包 ASR 响应帧数量超过安全上限。")


async def _close_quietly(websocket: Any) -> None:
    try:
        await asyncio.wait_for(websocket.close(), timeout=2.0)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def _websocket_looks_closed(websocket: Any) -> bool:
    """Best-effort staleness probe; the send-time fallback covers the rest."""

    return getattr(websocket, "close_code", None) is not None


@dataclass(slots=True)
class _PrewarmedConnection:
    """一条已完成 TLS+WSS 握手的备用连接及其握手期头部标识。

    Seed ASR 的 ``X-Api-Request-Id`` 在握手头里固定，因此一条连接只能承载
    一次识别 session；预热的价值是把 100-300 ms 的握手挪出首响/打断确认的
    关键路径，而不是复用同一连接跑多句。
    """

    websocket: Any
    request_id: str
    connect_id: str
    log_id: str
    created_mono: float


class AsrConnectionPrewarmPool:
    """Seed ASR 连接预热池：每个配置最多保留一条备用连接。

    ``acquire`` 在锁内弹出条目，同设备连续两句并发抢用时只有一句能拿到
    预热连接，另一句自动回退现建。预热失败永远静默，正常识别路径不受
    任何影响。
    """

    def __init__(self, *, ttl_seconds: float = PREWARM_TTL_SECONDS) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._entries: dict[tuple[str, str, str], _PrewarmedConnection] = {}
        self._tasks: dict[tuple[str, str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _key(config: VolcengineStreamingAsrConfig) -> tuple[str, str, str]:
        return (config.endpoint, config.api_key, config.resource_id)

    async def acquire(
        self,
        config: VolcengineStreamingAsrConfig,
    ) -> _PrewarmedConnection | None:
        """取走一条仍然可用的预热连接；过期或已断开的就地关闭。"""

        async with self._lock:
            entry = self._entries.pop(self._key(config), None)
        if entry is None:
            return None
        if (
            time.monotonic() - entry.created_mono > self._ttl_seconds
            or _websocket_looks_closed(entry.websocket)
        ):
            await _close_quietly(entry.websocket)
            return None
        return entry

    def schedule(self, config: VolcengineStreamingAsrConfig) -> None:
        """后台预建下一条连接；调用方无需等待，失败自动回退现建。"""

        if self._closed:
            return
        key = self._key(config)
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._prewarm(config),
            name="deskbot-asr-prewarm",
        )
        self._tasks[key] = task
        task.add_done_callback(
            lambda done, key=key: (
                self._tasks.pop(key, None)
                if self._tasks.get(key) is done
                else None
            )
        )

    async def _discard(
        self,
        key: tuple[str, str, str],
        entry: _PrewarmedConnection,
    ) -> bool:
        """若条目仍归本池持有则移除并关闭；返回是否确实由本池处置。"""

        async with self._lock:
            still_ours = self._entries.get(key) is entry
            if still_ours:
                self._entries.pop(key, None)
        if still_ours:
            await _close_quietly(entry.websocket)
        return still_ours

    async def _prewarm(self, config: VolcengineStreamingAsrConfig) -> None:
        key = self._key(config)
        refreshes = 0
        while not self._closed:
            request_id = str(uuid.uuid4())
            connect_id = str(uuid.uuid4())
            try:
                websocket, log_id = await _connect_websocket(
                    config,
                    request_id=request_id,
                    connect_id=connect_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 预热失败只意味着下一句退回现建；不打扰正常识别路径。
                logger.debug(
                    "[ASR] volcengine prewarm connect failed error=%s",
                    type(exc).__name__,
                )
                return
            entry = _PrewarmedConnection(
                websocket=websocket,
                request_id=request_id,
                connect_id=connect_id,
                log_id=log_id,
                created_mono=time.monotonic(),
            )
            stale: _PrewarmedConnection | None = None
            async with self._lock:
                if self._closed:
                    stale = entry
                else:
                    stale = self._entries.get(key)
                    self._entries[key] = entry
            if stale is entry:
                await _close_quietly(entry.websocket)
                return
            if stale is not None:
                await _close_quietly(stale.websocket)
            try:
                # 等待三种结局之一：被识别 session 取走 / TTL 到期 /
                # 服务端主动断开（此时自动补充一条新连接）。
                await asyncio.wait_for(
                    entry.websocket.wait_closed(),
                    timeout=self._ttl_seconds,
                )
            except asyncio.CancelledError:
                await self._discard(key, entry)
                raise
            except TimeoutError:
                pass
            except Exception:
                pass
            if not await self._discard(key, entry):
                # 已被识别 session 取走；该 session 结束时会重新调度预热。
                return
            refreshes += 1
            if refreshes >= PREWARM_MAX_IDLE_REFRESHES:
                return

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await _close_quietly(entry.websocket)


# 预热池持有 asyncio.Lock 与任务，绝不能跨事件循环复用：常驻服务只有一个
# 长期 loop，而 Flask 预览/测试用 ``asyncio.run()`` 每次新建 loop，因此按
# loop 建池。普通线程锁只保护这份进程级注册表本身。
_prewarm_pools_lock = threading.Lock()
_prewarm_pools: dict[
    asyncio.AbstractEventLoop,
    AsrConnectionPrewarmPool,
] = {}


def _prewarm_pool_for_current_loop() -> AsrConnectionPrewarmPool:
    current_loop = asyncio.get_running_loop()
    with _prewarm_pools_lock:
        for stale_loop in [
            loop for loop in _prewarm_pools if loop.is_closed()
        ]:
            _prewarm_pools.pop(stale_loop, None)
        pool = _prewarm_pools.get(current_loop)
        if pool is None:
            pool = AsrConnectionPrewarmPool()
            _prewarm_pools[current_loop] = pool
        return pool


class VolcengineStreamingAsrAdapter:
    """Stream one complete 16 kHz mono S16LE utterance to Seed ASR 2.0."""

    def __init__(self, settings: AsrSettings) -> None:
        self._settings = settings

    def is_valid_text(self, text: str) -> bool:
        text_filter = self._settings.text_filter
        return is_asr_text_acceptable(
            text,
            min_len=text_filter.min_text_len,
            min_chinese_ratio=text_filter.min_chinese_ratio,
        )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        if not pcm_bytes:
            return ""
        if sample_rate != PCM_SAMPLE_RATE:
            raise AsrInputError("火山豆包 ASR 当前仅接受 16 kHz 单声道 S16LE PCM。")
        if len(pcm_bytes) % PCM_SAMPLE_WIDTH_BYTES:
            raise AsrInputError("ASR PCM 长度必须按 16-bit 样本对齐。")

        config = resolve_volcengine_streaming_asr_config(self._settings)
        validate_volcengine_streaming_asr_config(config)
        if len(pcm_bytes) > config.max_audio_bytes:
            raise AsrInputError(
                f"本轮音频大小 {len(pcm_bytes)} B 超过 ASR_MAX_AUDIO_BYTES "
                f"限制 {config.max_audio_bytes} B。"
            )

        async with asr_infer_slot():
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    return await self._transcribe_stream(
                        bytes(pcm_bytes),
                        sample_rate,
                        config,
                    )
            except AsrError:
                raise
            except InvalidStatus as exc:
                raise _http_status_error(_handshake_status(exc)) from None
            except InvalidURI as exc:
                raise AsrConfigurationError("火山豆包 ASR WebSocket 地址无效。") from exc
            except TimeoutError as exc:
                raise AsrProviderUnavailableError(
                    "火山豆包 ASR 请求超时，请检查 PC 网络后重试。"
                ) from exc
            except ConnectionClosed as exc:
                raise AsrProviderUnavailableError(
                    "火山豆包 ASR 在返回最终文本前关闭了连接。"
                ) from exc
            except (OSError, WebSocketException) as exc:
                raise AsrProviderUnavailableError(
                    "无法连接火山豆包 ASR，请检查 PC 网络、DNS 和 TLS。"
                ) from exc

    async def _transcribe_stream(
        self,
        pcm_bytes: bytes,
        sample_rate: int,
        config: VolcengineStreamingAsrConfig,
    ) -> str:
        pool = _prewarm_pool_for_current_loop()
        prewarmed = await pool.acquire(config)
        used_prewarmed = prewarmed is not None
        if prewarmed is not None:
            # request_id/connect_id 已在预热握手头里固定，本句必须沿用。
            request_id = prewarmed.request_id
            connect_id = prewarmed.connect_id
            log_id = prewarmed.log_id
            websocket: Any | None = prewarmed.websocket
        else:
            request_id = str(uuid.uuid4())
            connect_id = str(uuid.uuid4())
            log_id = ""
            websocket = None
        receiver: asyncio.Task[str] | None = None
        started = time.monotonic()
        try:
            if websocket is None:
                websocket, log_id = await _connect_websocket(
                    config,
                    request_id=request_id,
                    connect_id=connect_id,
                )
            initial = encode_full_client_request(
                recognition_payload(sample_rate=sample_rate)
            )
            try:
                await websocket.send(initial)
            except (ConnectionClosed, OSError):
                if not used_prewarmed:
                    raise
                # 预热连接恰在取用与首包之间被服务端断开：静默回退现建。
                await _close_quietly(websocket)
                used_prewarmed = False
                request_id = str(uuid.uuid4())
                connect_id = str(uuid.uuid4())
                websocket, log_id = await _connect_websocket(
                    config,
                    request_id=request_id,
                    connect_id=connect_id,
                )
                await websocket.send(initial)
            logger.info(
                "[ASR] volcengine stream start request_id=%s pcm_bytes=%d "
                "sample_rate=%d resource_id=%s prewarmed=%d",
                request_id,
                len(pcm_bytes),
                sample_rate,
                config.resource_id,
                int(used_prewarmed),
            )
            receiver = asyncio.create_task(_receive_final_transcript(websocket))
            for offset in range(0, len(pcm_bytes), PCM_FRAME_BYTES):
                if receiver.done():
                    return await receiver
                chunk = pcm_bytes[offset : offset + PCM_FRAME_BYTES]
                await websocket.send(encode_audio_request(chunk))
                # Let the receive task process partial/error responses while a
                # buffered utterance is uploaded without imposing real-time delay.
                await asyncio.sleep(0)
            await websocket.send(encode_audio_request(b"", final_packet=True))
            text = await asyncio.wait_for(
                receiver,
                timeout=config.final_timeout_seconds,
            )
            logger.info(
                "[ASR] volcengine stream complete request_id=%s log_id=%s "
                "elapsed_ms=%d transcript_chars=%d",
                request_id,
                log_id or "-",
                int((time.monotonic() - started) * 1000),
                len(text),
            )
            return text
        finally:
            if receiver is not None and not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if websocket is not None:
                await _close_quietly(websocket)
            # 本句结束后立刻在后台预建下一条连接，把 TLS+WSS 握手挪出
            # 下一轮首响与打断确认的关键路径。
            pool.schedule(config)
