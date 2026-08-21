"""豆包语音 2.0 TTS（火山引擎 V3 双向 WebSocket）客户端。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from deskbot_server.env import load_dotenv
from deskbot_server.safe_fetch import connect_provider_websocket_socket
from deskbot_server.tts.protocols import (
    EventType,
    MsgType,
    finish_connection,
    finish_session,
    new_session_id,
    receive_message,
    start_connection,
    start_session,
    task_request,
    wait_for_event,
)

logger = logging.getLogger("deskbot-server")
_wire_logger = logging.getLogger("deskbot-server.tts.doubao.websocket-wire")
_wire_logger.setLevel(logging.CRITICAL + 1)
_wire_logger.propagate = False
_wire_logger.disabled = True
if not _wire_logger.handlers:
    _wire_logger.addHandler(logging.NullHandler())

PROVIDER_ID = "doubao"
PROVIDER_DISPLAY_NAME = "火山引擎豆包流式 TTS 2.0"
PROTOCOL_ID = "volcengine_v3_bidirectional_websocket"
PROVIDER_VERSION = "2.0"
DEFAULT_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEFAULT_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_MODEL = "seed-tts-2.0-standard"
DEFAULT_SPEAKER = "zh_female_vv_uranus_bigtts"
# 16 kHz 与设备 Opus 链路/Seed ASR 一致：TTS→LiveKit→Core→设备全程免重采样。
# 通过 DOUBAO_TTS_SAMPLE_RATE / TTS_SAMPLE_RATE 环境变量可覆盖。
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_AUDIO_FORMAT = "pcm"
CONNECTION_EVENT_TIMEOUT_SECONDS = 10.0
SESSION_TIMEOUT_SECONDS = 90.0
CLOSE_TIMEOUT_SECONDS = 2.0
MAX_SERVER_FRAME_BYTES = 2 * 1024 * 1024
MAX_PCM_BYTES = 32 * 1024 * 1024
MAX_SESSION_EVENTS = 4096
MAX_ERROR_DETAIL_CHARS = 2048
DEFAULT_VOICE_CLONE_RESOURCE_ID = "seed-icl-2.0"
DEFAULT_VOICE_CLONE_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
DEFAULT_VOICE_STATUS_URL = "https://openspeech.bytedance.com/api/v3/tts/get_voice"
# The first name is canonical. The remaining names are read-only migration
# aliases; configuration writes always use DOUBAO_TTS_API_KEY.
TTS_API_KEY_ENV_NAMES = (
    "DOUBAO_TTS_API_KEY",
    "VOLCENGINE_TTS_API_KEY",
    "SEED_TTS_API_KEY",
    "BYTEPLUS_SEED_SPEECH_API_KEY",
)


@dataclass(frozen=True)
class DoubaoTtsConfig:
    api_key: str
    speaker: str = DEFAULT_SPEAKER
    resource_id: str = DEFAULT_RESOURCE_ID
    model: str = DEFAULT_MODEL
    ws_url: str = DEFAULT_WS_URL
    sample_rate: int = DEFAULT_SAMPLE_RATE
    audio_format: str = DEFAULT_AUDIO_FORMAT
    enable_timestamp: bool = True
    app_id: str = ""
    access_token: str = ""
    voice_clone_resource_id: str = DEFAULT_VOICE_CLONE_RESOURCE_ID
    voice_clone_url: str = DEFAULT_VOICE_CLONE_URL
    voice_status_url: str = DEFAULT_VOICE_STATUS_URL

    def ws_headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": uuid4().hex,
        }

    def masked(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER_ID,
            "provider_name": PROVIDER_DISPLAY_NAME,
            "protocol": PROTOCOL_ID,
            "version": PROVIDER_VERSION,
            "streaming": True,
            "api_key": "",
            "api_key_set": bool(self.api_key),
            "speaker": self.speaker,
            "resource_id": self.resource_id,
            "model": self.model,
            "ws_url": self.ws_url,
            "sample_rate": self.sample_rate,
            "audio_format": self.audio_format,
            "enable_timestamp": self.enable_timestamp,
            "app_id": "",
            "app_id_set": bool(self.app_id),
            "access_token": "",
            "access_token_set": bool(self.access_token),
            "voice_clone_resource_id": self.voice_clone_resource_id,
            "voice_clone_url": self.voice_clone_url,
            "voice_status_url": self.voice_status_url,
        }




def _is_masked_secret(value: str) -> bool:
    """判断是否为 masked() 产生的占位串，避免误当作真实密钥。"""
    raw = (value or "").strip()
    if not raw or "*" not in raw:
        return False
    if len(raw) <= 6:
        return all(c == "*" for c in raw)
    return raw[3:-3] == "*" * (len(raw) - 6)


def resolve_optional_secret(incoming, fallback: str) -> str:
    """表单留空或脱敏占位时不覆盖，使用 fallback（.env / 进程环境）。"""
    raw = str(incoming or "").strip()
    if not raw or _is_masked_secret(raw):
        return (fallback or "").strip()
    return raw


def _resolve_tts_api_key() -> str:
    for name in TTS_API_KEY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def load_doubao_tts_config() -> DoubaoTtsConfig:
    load_dotenv()
    speaker = (os.environ.get("DOUBAO_TTS_SPEAKER") or "").strip()
    resource_id = (os.environ.get("DOUBAO_TTS_RESOURCE_ID") or DEFAULT_RESOURCE_ID).strip()
    model = (os.environ.get("DOUBAO_TTS_MODEL") or DEFAULT_MODEL).strip()
    from deskbot_server.device_preferences import load_preferences
    from deskbot_server.tts.speakers import find_doubao_tts_speaker_preset, suggest_resource_id

    local_tts = load_preferences().get("tts") or {}
    preferred_speaker = str(local_tts.get("speaker") or "").strip()
    preferred_resource = str(local_tts.get("resource_id") or "").strip()
    if preferred_speaker:
        speaker = preferred_speaker
    if preferred_resource:
        resource_id = preferred_resource

    speaker = speaker or DEFAULT_SPEAKER
    expected_rid = suggest_resource_id(speaker)
    preset = find_doubao_tts_speaker_preset(speaker)
    if preset and preset.resource_id:
        expected_rid = preset.resource_id.strip()
    if expected_rid and resource_id != expected_rid:
        logger.info(
            "DOUBAO_TTS_RESOURCE_ID=%s 与 speaker=%s 不匹配，自动改用 %s",
            resource_id,
            speaker,
            expected_rid,
        )
        resource_id = expected_rid
    if resource_id == "seed-tts-1.0" and model.startswith("seed-tts-2"):
        logger.info(
            "speaker=%s 使用 seed-tts-1.0，忽略 2.0 model=%s",
            speaker,
            model,
        )
        model = ""
    return DoubaoTtsConfig(
        api_key=_resolve_tts_api_key(),
        speaker=speaker,
        resource_id=resource_id,
        model=model,
        ws_url=(os.environ.get("DOUBAO_TTS_WS_URL") or DEFAULT_WS_URL).strip(),
        sample_rate=int(
            os.environ.get("DOUBAO_TTS_SAMPLE_RATE")
            or os.environ.get("TTS_SAMPLE_RATE")
            or DEFAULT_SAMPLE_RATE
        ),
        audio_format=(
            os.environ.get("DOUBAO_TTS_FORMAT") or DEFAULT_AUDIO_FORMAT
        ).strip(),
        enable_timestamp=(os.environ.get("DOUBAO_TTS_ENABLE_TIMESTAMP") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
        app_id=(os.environ.get("DOUBAO_TTS_APP_ID") or "").strip(),
        access_token=(os.environ.get("DOUBAO_TTS_ACCESS_TOKEN") or "").strip(),
        voice_clone_resource_id=(
            os.environ.get("DOUBAO_TTS_VOICE_CLONE_RESOURCE_ID") or DEFAULT_VOICE_CLONE_RESOURCE_ID
        ).strip(),
        voice_clone_url=(os.environ.get("DOUBAO_TTS_VOICE_CLONE_URL") or DEFAULT_VOICE_CLONE_URL).strip(),
        voice_status_url=(os.environ.get("DOUBAO_TTS_VOICE_STATUS_URL") or DEFAULT_VOICE_STATUS_URL).strip(),
    )


def _build_start_session_payload(cfg: DoubaoTtsConfig) -> bytes:
    audio_params: dict[str, Any] = {
        "format": cfg.audio_format,
        "sample_rate": cfg.sample_rate,
    }
    if cfg.enable_timestamp:
        audio_params["enable_timestamp"] = True
    req_params: dict[str, Any] = {
        "speaker": cfg.speaker,
        "audio_params": audio_params,
    }
    if cfg.model:
        req_params["model"] = cfg.model
    if cfg.enable_timestamp:
        req_params["enable_timestamp"] = True
        req_params["enable_subtitle"] = True
        req_params["additions"] = json.dumps(
            {"enable_timestamp": True, "enable_subtitle": True},
            ensure_ascii=False,
        )
    body = {
        "user": {"uid": uuid4().hex},
        "event": EventType.StartSession,
        "namespace": "BidirectionalTTS",
        "req_params": req_params,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _redact_provider_detail(value: object, *secrets: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        raw = (secret or "").strip()
        if raw:
            text = text.replace(raw, "<redacted>")
    if len(text) > MAX_ERROR_DETAIL_CHARS:
        text = f"{text[:MAX_ERROR_DETAIL_CHARS]}…"
    return text


def _describe_ws_connect_error(exc: Exception, *secrets: str) -> str:
    if isinstance(exc, InvalidStatus):
        body = _redact_provider_detail(
            (exc.response.body or b"").decode("utf-8", errors="replace").strip(),
            *secrets,
        )
        status = exc.response.status_code
        hint = ""
        if status == 401:
            hint = (
                "（鉴权失败：请核对火山引擎豆包流式语音 API Key，"
                "使用 DOUBAO_TTS_API_KEY，不要使用 ARK_API_KEY）"
            )
        elif status == 403 and "not granted" in body:
            hint = "（资源未授权：请在控制台开通对应 Resource ID，例如 seed-tts-2.0）"
        elif status == 400 and "no token" in body:
            hint = "（未提供 API Key：请配置 DOUBAO_TTS_API_KEY）"
        detail = f"HTTP {status}"
        if body:
            detail += f": {body}"
        return f"WebSocket 连接被拒 {detail}{hint}"
    return f"{type(exc).__name__}: {_redact_provider_detail(exc, *secrets)}"


def _parse_session_meta(payload: bytes) -> tuple[int, str]:
    raw = payload.decode("utf-8", errors="replace") if payload else ""
    try:
        meta = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return 0, raw or "unknown"
    return int(meta.get("status_code", 0) or 0), str(meta.get("message", raw or "unknown"))


@dataclass
class DoubaoTtsResult:
    pcm: bytes
    sample_rate: int
    elapsed_ms: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    sentence_end: dict[str, Any] | None = None
    subtitles: list[Any] = field(default_factory=list)


def _config_pool_key(cfg: DoubaoTtsConfig) -> tuple[Any, ...]:
    return (
        cfg.ws_url,
        cfg.api_key,
        cfg.speaker,
        cfg.resource_id,
        cfg.model,
        cfg.sample_rate,
        cfg.audio_format,
        cfg.enable_timestamp,
    )


class DoubaoTtsConnection:
    """复用同一 WebSocket 连接，多次 StartSession 合成，避免每句重连。"""

    def __init__(self, cfg: DoubaoTtsConfig) -> None:
        self._cfg = cfg
        self._ws: Any | None = None
        self._lock = asyncio.Lock()
        self._ready = False

    async def synthesize(
        self,
        text: str,
        *,
        on_pcm: Callable[[bytes], None] | None = None,
    ) -> DoubaoTtsResult:
        clean = (text or "").strip()
        if not clean:
            raise ValueError("空文本")
        if not self._cfg.api_key:
            raise ValueError("豆包 TTS 未配置 DOUBAO_TTS_API_KEY")
        if not self._cfg.speaker:
            raise ValueError("未设置 speaker（音色）")

        async with self._lock:
            t0 = time.monotonic()
            # The provider occasionally finishes a syntactically successful
            # session without returning AudioOnlyServer frames.  A fresh
            # connection usually succeeds on the next request; allow one
            # additional clean reconnect before failing the user turn.
            max_attempts = 3
            # 流式路径下重试会让供应商把整句音频从头重发一遍。跟踪本句已经
            # 向下游 emit 的字节数，重试时先吞掉重发前缀，只补发未播出的
            # 尾部——宁可接缝处缺一点尾音，也绝不能让用户听到句子重复。
            emitted_bytes = 0
            for attempt in range(max_attempts):
                skip_remaining = emitted_bytes

                def _emit_new_audio(chunk: bytes) -> None:
                    nonlocal emitted_bytes, skip_remaining
                    assert on_pcm is not None
                    if skip_remaining > 0:
                        if len(chunk) <= skip_remaining:
                            skip_remaining -= len(chunk)
                            return
                        chunk = chunk[skip_remaining:]
                        skip_remaining = 0
                    on_pcm(chunk)
                    emitted_bytes += len(chunk)

                try:
                    await self._ensure_ready()
                    async with asyncio.timeout(SESSION_TIMEOUT_SECONDS):
                        if on_pcm is None:
                            # Preserve the original call contract for existing
                            # subclasses and test doubles. RTC streaming opts
                            # into the callback explicitly.
                            result = await self._synthesize_once(clean)
                        else:
                            result = await self._synthesize_once(
                                clean,
                                on_pcm=_emit_new_audio,
                            )
                        pcm, events, sentence_end, subtitles = result
                    if on_pcm is not None and skip_remaining > 0:
                        # 重试重发的音频比已播偏移还短，无法对齐补尾；按
                        # "缺尾音"处理并留证，绝不回退成重播。
                        logger.warning(
                            "豆包 TTS 重试音频短于已播偏移，本句尾部缺失 "
                            "emitted_bytes=%d missing_bytes=%d",
                            emitted_bytes,
                            skip_remaining,
                        )
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    logger.info(
                        "豆包流式 TTS 2.0 合成完成 speaker=%r pcm_bytes=%d "
                        "elapsed_ms=%d segs_hint words=%d phonemes=%d",
                        self._cfg.speaker,
                        len(pcm),
                        elapsed_ms,
                        len((sentence_end or {}).get("words") or []),
                        len((sentence_end or {}).get("phonemes") or []),
                    )
                    return DoubaoTtsResult(
                        pcm=bytes(pcm),
                        sample_rate=self._cfg.sample_rate,
                        elapsed_ms=elapsed_ms,
                        events=events,
                        sentence_end=sentence_end,
                        subtitles=subtitles,
                    )
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(self._reset())
                    finally:
                        raise
                except TimeoutError as exc:
                    await self._reset()
                    raise RuntimeError(
                        f"豆包 TTS 请求超过 {SESSION_TIMEOUT_SECONDS:g} 秒，已关闭连接"
                    ) from exc
                except (
                    ConnectionClosed,
                    InvalidStatus,
                    RuntimeError,
                    OSError,
                    websockets.WebSocketException,
                ) as exc:
                    logger.warning(
                        "豆包 TTS 会话失败 attempt=%d emitted_bytes=%d "
                        "err=%s，将重连并跳过已播前缀",
                        attempt + 1,
                        emitted_bytes,
                        _redact_provider_detail(
                            exc,
                            self._cfg.api_key,
                            self._cfg.access_token,
                        ),
                    )
                    await self._reset()
                    if attempt + 1 >= max_attempts:
                        if isinstance(exc, InvalidStatus):
                            raise
                        raise RuntimeError(
                            "豆包 TTS 请求失败："
                            + _redact_provider_detail(
                                exc,
                                self._cfg.api_key,
                                self._cfg.access_token,
                            )
                        ) from None
                    await asyncio.sleep(0.12 * (attempt + 1))
            raise RuntimeError("豆包 TTS 合成失败")

    async def close(self) -> None:
        async with self._lock:
            await self._reset()

    async def _ensure_ready(self) -> None:
        if self._ws is not None and self._ready:
            return
        await self._reset()
        cfg = self._cfg
        parsed, pinned_sock = await asyncio.to_thread(
            connect_provider_websocket_socket,
            cfg.ws_url,
            timeout=30,
        )
        connect_kwargs: dict[str, Any] = {"sock": pinned_sock}
        if parsed.scheme.lower() == "wss":
            # websockets 14.2 also derives this from the original URI. Keep it
            # explicit so a future transport refactor cannot drop SNI or
            # certificate hostname verification when a connected socket is
            # supplied.
            connect_kwargs["server_hostname"] = parsed.hostname
        try:
            ws = await websockets.connect(
                cfg.ws_url,
                additional_headers=cfg.ws_headers(),
                compression=None,
                max_size=MAX_SERVER_FRAME_BYTES,
                max_queue=32,
                open_timeout=30,
                close_timeout=CLOSE_TIMEOUT_SECONDS,
                ping_interval=20,
                ping_timeout=20,
                logger=_wire_logger,
                **connect_kwargs,
            )
        except BaseException:
            pinned_sock.close()
            raise
        try:
            async with asyncio.timeout(CONNECTION_EVENT_TIMEOUT_SECONDS):
                await start_connection(ws)
                await wait_for_event(ws, MsgType.FullServerResponse, EventType.ConnectionStarted)
        except BaseException:
            try:
                await asyncio.wait_for(asyncio.shield(ws.close()), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
            raise
        self._ws = ws
        self._ready = True
        logger.info(
            "豆包流式 TTS 2.0 WebSocket 长连接已建立 url=%s speaker=%r",
            cfg.ws_url,
            cfg.speaker,
        )

    async def _reset(self) -> None:
        self._ready = False
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                await finish_connection(ws)
                await receive_message(ws)
        except Exception:
            pass
        try:
            await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def _synthesize_once(
        self,
        clean: str,
        *,
        on_pcm: Callable[[bytes], None] | None = None,
    ) -> tuple[bytearray, list[dict[str, Any]], dict[str, Any] | None, list[Any]]:
        ws = self._ws
        if ws is None:
            raise RuntimeError("WebSocket 未连接")
        cfg = self._cfg
        session_id = new_session_id()
        pcm = bytearray()
        events: list[dict[str, Any]] = []
        sentence_end: dict[str, Any] | None = None
        subtitles: list[Any] = []

        await start_session(ws, _build_start_session_payload(cfg), session_id)
        session_msg = await receive_message(ws)
        _append_event(events, session_msg)
        if session_msg.event == EventType.SessionFailed:
            code, message = _parse_session_meta(session_msg.payload)
            raise RuntimeError(
                f"SessionFailed ({code}): "
                f"{_redact_provider_detail(message, cfg.api_key, cfg.access_token)}"
            )
        if session_msg.event != EventType.SessionStarted:
            raise RuntimeError(f"期望 SessionStarted，收到 {session_msg.event}")

        task_body = json.dumps(
            {
                "event": EventType.TaskRequest,
                "req_params": {"text": clean},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await task_request(ws, task_body, session_id)
        await finish_session(ws, session_id)

        while True:
            msg = await receive_message(ws)
            _append_event(events, msg)
            if msg.type == MsgType.Error:
                err_text = _redact_provider_detail(
                    msg.payload.decode("utf-8", errors="replace"),
                    cfg.api_key,
                    cfg.access_token,
                )
                raise RuntimeError(f"协议错误 ({msg.error_code}): {err_text}")
            if msg.type == MsgType.AudioOnlyServer and msg.event == EventType.TTSResponse:
                if msg.payload:
                    if len(pcm) + len(msg.payload) > MAX_PCM_BYTES:
                        raise RuntimeError(
                            f"豆包 TTS 音频超过安全上限 {MAX_PCM_BYTES} 字节"
                        )
                    pcm.extend(msg.payload)
                    if on_pcm is not None:
                        # The callback is deliberately synchronous: LiveKit's
                        # AudioEmitter only enqueues bytes, so forwarding each
                        # provider packet here preserves first-audio latency
                        # without blocking the WebSocket receive loop.
                        on_pcm(bytes(msg.payload))
                continue
            if msg.event == EventType.TTSSubtitle and msg.payload:
                try:
                    subtitles.append(json.loads(msg.payload.decode("utf-8")))
                except json.JSONDecodeError:
                    pass
                continue
            if msg.event == EventType.TTSSentenceEnd and msg.payload:
                try:
                    sentence_end = json.loads(msg.payload.decode("utf-8"))
                except json.JSONDecodeError:
                    pass
                continue
            if msg.event == EventType.SessionFinished:
                break
            if msg.event == EventType.SessionFailed:
                code, message = _parse_session_meta(msg.payload)
                raise RuntimeError(
                    f"SessionFailed ({code}): "
                    f"{_redact_provider_detail(message, cfg.api_key, cfg.access_token)}"
                )

        if not pcm:
            raise RuntimeError("豆包 TTS 会话已结束，但未返回音频")
        return pcm, events, sentence_end, subtitles


_pool_lock = threading.Lock()
_pools: dict[
    tuple[Any, ...],
    tuple[asyncio.AbstractEventLoop, DoubaoTtsConnection],
] = {}


async def get_doubao_tts_connection(cfg: DoubaoTtsConfig) -> DoubaoTtsConnection:
    """Return a connection owned by the current event loop.

    The realtime service keeps one long-lived loop, while Flask preview/test
    handlers use a fresh ``asyncio.run()`` loop per request. A WebSocket and
    its asyncio lock must never be reused by a different loop. The ordinary
    thread lock only protects this process-wide registry; no blocking I/O or
    await happens while it is held.
    """

    key = _config_pool_key(cfg)
    current_loop = asyncio.get_running_loop()
    with _pool_lock:
        for stale_key, (owner_loop, _stale_conn) in list(_pools.items()):
            if owner_loop.is_closed():
                _pools.pop(stale_key, None)

        pooled = _pools.get(key)
        if pooled is not None:
            owner_loop, conn = pooled
            if owner_loop is current_loop:
                return conn

        conn = DoubaoTtsConnection(cfg)
        _pools[key] = (current_loop, conn)
        return conn


async def prewarm_doubao_tts(cfg: DoubaoTtsConfig) -> None:
    """Open the pooled provider connection before the first spoken reply."""

    conn = await get_doubao_tts_connection(cfg)
    async with conn._lock:
        await conn._ensure_ready()


async def synthesize_doubao_tts(
    text: str,
    cfg: DoubaoTtsConfig,
    *,
    on_pcm: Callable[[bytes], None] | None = None,
) -> DoubaoTtsResult:
    """合成整段文本；复用进程内 WebSocket 长连接。"""
    conn = await get_doubao_tts_connection(cfg)
    try:
        if on_pcm is None:
            return await conn.synthesize(text)
        return await conn.synthesize(text, on_pcm=on_pcm)
    except InvalidStatus as exc:
        err = _describe_ws_connect_error(exc, cfg.api_key, cfg.access_token)
        logger.error(
            "豆包 TTS WebSocket 连接失败 url=%s resource_id=%s err=%s",
            cfg.ws_url,
            cfg.resource_id,
            err,
        )
        raise RuntimeError(err) from None


def _append_event(events: list[dict[str, Any]], msg: Any) -> None:
    if len(events) >= MAX_SESSION_EVENTS:
        raise RuntimeError(f"豆包 TTS 响应事件超过安全上限 {MAX_SESSION_EVENTS}")
    events.append(_event_row(msg))


def _event_row(msg) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": str(msg.type),
        "event": str(msg.event),
        "session_id": msg.session_id,
        "payload_bytes": len(msg.payload or b""),
    }
    if msg.type == MsgType.Error:
        row["error_code"] = msg.error_code
        row["payload"] = (msg.payload or b"").decode("utf-8", errors="replace")
    elif msg.type != MsgType.AudioOnlyServer and msg.payload:
        try:
            row["payload"] = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            row["payload"] = (msg.payload or b"").decode("utf-8", errors="replace")
    return row
