from __future__ import annotations

import asyncio
import gzip
import json
import struct
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from deskbot_server.core.ports.asr import (
    AsrAuthenticationError,
    AsrConfigurationError,
    AsrInputError,
    AsrProviderResponseError,
    AsrProviderUnavailableError,
    AsrRateLimitError,
)
from deskbot_server.core.settings import AsrSettings
from deskbot_server.infrastructure.asr import volcengine_streaming as volc


@pytest.fixture()
def settings(monkeypatch) -> AsrSettings:
    for name in (
        "ASR_API_KEY",
        "ASR_ENDPOINT",
        "ASR_MAX_AUDIO_BYTES",
        "ASR_RESOURCE_ID",
        "ASR_TIMEOUT_SECONDS",
        "VOLCENGINE_ASR_API_KEY",
        "VOLCENGINE_ASR_ENDPOINT",
        "VOLCENGINE_ASR_RESOURCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(volc, "load_dotenv", lambda: False)
    return AsrSettings(
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        timeout_seconds=12,
        max_audio_bytes=16384,
    )


def _server_frame(
    payload: dict,
    *,
    final_packet: bool = False,
    message_type: int = volc.MSG_FULL_SERVER_RESPONSE,
    error_code: int = 0,
    flags: int | None = None,
    compression: int = volc.COMPRESSION_GZIP,
    serialization: int = volc.SERIALIZATION_JSON,
    trailing: bytes = b"",
) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if compression == volc.COMPRESSION_GZIP:
        encoded = gzip.compress(encoded, mtime=0)
    actual_flags = (
        flags
        if flags is not None
        else (volc.FLAG_FINAL_NO_SEQUENCE if final_packet else volc.FLAG_NO_SEQUENCE)
    )
    frame = bytearray(
        volc._build_header(
            message_type,
            actual_flags,
            serialization,
            compression,
        )
    )
    if actual_flags in {volc.FLAG_POSITIVE_SEQUENCE, volc.FLAG_NEGATIVE_SEQUENCE}:
        frame.extend(struct.pack(">i", -1 if final_packet else 1))
    if message_type == volc.MSG_SERVER_ERROR:
        frame.extend(struct.pack(">I", error_code))
    frame.extend(struct.pack(">I", len(encoded)))
    frame.extend(encoded)
    frame.extend(trailing)
    return bytes(frame)


def _decode_client_payload(frame: bytes) -> bytes:
    payload_size = struct.unpack(">I", frame[4:8])[0]
    assert len(frame) == 8 + payload_size
    return gzip.decompress(frame[8:])


def test_config_uses_seed_defaults_and_never_contains_a_baked_in_key(
    settings,
    monkeypatch,
):
    monkeypatch.setenv("ASR_API_KEY", "runtime-only-secret")

    config = volc.resolve_volcengine_streaming_asr_config(settings)

    assert config.endpoint == volc.DEFAULT_ENDPOINT
    assert config.resource_id == volc.DEFAULT_RESOURCE_ID
    assert config.api_key == "runtime-only-secret"
    assert "runtime-only-secret" not in volc.DEFAULT_ENDPOINT
    assert "runtime-only-secret" not in volc.DEFAULT_RESOURCE_ID


def test_missing_key_fails_before_dns_or_websocket(settings, monkeypatch):
    opened = False

    def fail_connect(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("network must not open without a key")

    monkeypatch.setattr(volc, "connect_provider_websocket_socket", fail_connect)

    with pytest.raises(AsrConfigurationError, match="ASR_API_KEY"):
        asyncio.run(
            volc.VolcengineStreamingAsrAdapter(settings).transcribe(
                b"\0\0",
                16_000,
            )
        )
    assert opened is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", "key\r\nX-Evil: yes"),
        ("resource_id", "volc.seedasr.sauc.duration\r\nX-Evil: yes"),
        ("resource_id", "not-volc-resource"),
        ("endpoint", "ws://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"),
        ("endpoint", "wss://localhost/api/v3/sauc/bigmodel_async"),
    ],
)
def test_config_rejects_header_injection_and_unsafe_endpoints(field, value):
    values = {
        "endpoint": volc.DEFAULT_ENDPOINT,
        "api_key": "test-key",
        "resource_id": volc.DEFAULT_RESOURCE_ID,
        "timeout_seconds": 12.0,
        "max_audio_bytes": 4096,
    }
    values[field] = value

    with pytest.raises(AsrConfigurationError):
        volc.validate_volcengine_streaming_asr_config(volc.VolcengineStreamingAsrConfig(**values))


def test_client_frames_match_seed_asr_binary_protocol():
    request = volc.encode_full_client_request(volc.recognition_payload(uid="unit-test-user"))
    assert request[:4] == bytes((0x11, 0x10, 0x11, 0x00))
    payload = json.loads(_decode_client_payload(request))
    assert payload["user"]["uid"] == "unit-test-user"
    assert payload["audio"] == {
        "format": "pcm",
        "codec": "raw",
        "rate": 16_000,
        "bits": 16,
        "channel": 1,
    }
    assert payload["request"]["model_name"] == "bigmodel"
    assert payload["request"]["enable_nonstream"] is True
    assert "end_window_size" not in payload["request"]

    regular = volc.encode_audio_request(b"\x01\x00" * 10)
    final = volc.encode_audio_request(b"", final_packet=True)
    assert regular[:4] == bytes((0x11, 0x20, 0x01, 0x00))
    assert final[:4] == bytes((0x11, 0x22, 0x01, 0x00))
    assert _decode_client_payload(regular) == b"\x01\x00" * 10
    assert _decode_client_payload(final) == b""


def test_parser_decodes_gzip_final_response_and_utterances():
    raw = _server_frame(
        {
            "code": 20_000_000,
            "result": {
                "utterances": [
                    {"text": "你好，", "definite": True},
                    {"text": "继续任务。", "definite": False},
                ]
            },
        },
        final_packet=True,
        flags=volc.FLAG_NEGATIVE_SEQUENCE,
    )

    frame = volc.parse_server_frame(raw)
    payload = volc.decode_response_payload(frame)

    assert frame.sequence == -1
    assert frame.is_final is True
    assert volc.extract_transcript(payload) == "你好，继续任务。"


@pytest.mark.parametrize(
    "raw",
    [
        b"\x11",
        b"\x21\x90\x10\x00",
        bytes((0x11, 0x90, 0x10, 0x00)) + struct.pack(">I", 10) + b"{}",
        _server_frame({"code": 0}, trailing=b"undeclared"),
        bytes((0x11, 0x90, 0x12, 0x00)) + struct.pack(">I", 0),
    ],
)
def test_parser_rejects_malformed_or_ambiguous_frames(raw):
    with pytest.raises(AsrProviderResponseError):
        volc.parse_server_frame(raw)


def test_parser_limits_gzip_decompression():
    compressed = gzip.compress(
        b"x" * (volc.MAX_SERVER_PAYLOAD_BYTES + 1),
        mtime=0,
    )
    raw = (
        volc._build_header(
            volc.MSG_FULL_SERVER_RESPONSE,
            volc.FLAG_FINAL_NO_SEQUENCE,
            volc.SERIALIZATION_JSON,
            volc.COMPRESSION_GZIP,
        )
        + struct.pack(">I", len(compressed))
        + compressed
    )

    with pytest.raises(AsrProviderResponseError, match="1 MiB"):
        volc.parse_server_frame(raw)


@pytest.mark.parametrize(
    ("code", "error_type", "retryable"),
    [
        (45_000_001, AsrInputError, False),
        (45_000_002, AsrInputError, False),
        (45_000_081, AsrProviderUnavailableError, True),
        (45_000_151, AsrInputError, False),
        (55_000_031, AsrRateLimitError, True),
        (55_000_099, AsrProviderUnavailableError, True),
    ],
)
def test_provider_binary_error_codes_are_actionable(
    code,
    error_type,
    retryable,
):
    raw = _server_frame(
        {"message": "provider body must stay private"},
        message_type=volc.MSG_SERVER_ERROR,
        error_code=code,
        final_packet=True,
    )

    error = volc._server_error(volc.parse_server_frame(raw))

    assert isinstance(error, error_type)
    assert error.retryable is retryable
    assert error.status_code == code


class _PinnedSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeWebSocket:
    def __init__(self, responses: list[bytes], *, log_id: str = "log-safe-123") -> None:
        self.sent: list[bytes] = []
        self.responses = list(responses)
        self.final_sent = asyncio.Event()
        self.closed = False
        self.response = SimpleNamespace(headers={"X-Tt-Logid": log_id})

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))
        if (
            len(payload) >= 2
            and payload[1] >> 4 == volc.MSG_AUDIO_ONLY_REQUEST
            and payload[1] & 0x0F == volc.FLAG_FINAL_NO_SEQUENCE
        ):
            self.final_sent.set()

    async def recv(self) -> bytes:
        await self.final_sent.wait()
        if not self.responses:
            await asyncio.Future()
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_adapter_streams_200ms_frames_and_returns_final_without_logging_secrets(
    settings,
    monkeypatch,
    caplog,
):
    secret = "volc-secret-never-log"
    transcript = "你好，继续任务。"
    monkeypatch.setenv("ASR_API_KEY", secret)
    partial = _server_frame(
        {"code": 20_000_000, "result": {"text": "你好"}},
    )
    final = _server_frame(
        {"code": 20_000_000, "result": {"text": transcript}},
        final_packet=True,
    )
    websocket = _FakeWebSocket([partial, final])
    pinned = _PinnedSocket()
    captured: dict = {}

    def fake_socket(endpoint, *, timeout):
        captured["socket_endpoint"] = endpoint
        captured["socket_timeout"] = timeout
        return urlsplit(endpoint), pinned

    async def fake_connect(endpoint, **kwargs):
        captured["endpoint"] = endpoint
        captured["kwargs"] = kwargs
        return websocket

    monkeypatch.setattr(volc, "connect_provider_websocket_socket", fake_socket)
    monkeypatch.setattr(volc.websockets, "connect", fake_connect)
    pcm = b"\x01\x00" * 3500  # 7000 bytes: one 6400-byte frame plus a short tail.

    with caplog.at_level("INFO"):
        text = asyncio.run(
            volc.VolcengineStreamingAsrAdapter(settings).transcribe(
                pcm,
                16_000,
            )
        )

    assert text == transcript
    assert websocket.closed is True
    assert len(websocket.sent) == 4
    assert websocket.sent[0][1] >> 4 == volc.MSG_FULL_CLIENT_REQUEST
    assert _decode_client_payload(websocket.sent[1]) == pcm[:6400]
    assert _decode_client_payload(websocket.sent[2]) == pcm[6400:]
    assert _decode_client_payload(websocket.sent[3]) == b""
    assert websocket.sent[3][1] & 0x0F == volc.FLAG_FINAL_NO_SEQUENCE

    headers = captured["kwargs"]["additional_headers"]
    assert headers["X-Api-Key"] == secret
    assert headers["X-Api-Resource-Id"] == volc.DEFAULT_RESOURCE_ID
    assert headers["X-Api-Sequence"] == "-1"
    uuid.UUID(headers["X-Api-Request-Id"])
    uuid.UUID(headers["X-Api-Connect-Id"])
    assert headers["X-Api-Request-Id"] != headers["X-Api-Connect-Id"]
    assert captured["kwargs"]["server_hostname"] == "openspeech.bytedance.com"
    assert captured["kwargs"]["compression"] is None
    assert captured["kwargs"]["max_size"] == volc.MAX_SERVER_FRAME_BYTES
    wire_logger = captured["kwargs"]["logger"]
    assert wire_logger is volc._wire_logger
    assert wire_logger.disabled is True
    assert wire_logger.propagate is False
    wire_logger.critical("wire headers: %s", headers)
    assert secret not in caplog.text
    assert transcript not in caplog.text


def test_handshake_auth_error_is_classified_without_echoing_key(
    settings,
    monkeypatch,
    caplog,
):
    secret = "auth-secret-never-echo"
    monkeypatch.setenv("ASR_API_KEY", secret)
    pinned = _PinnedSocket()

    monkeypatch.setattr(
        volc,
        "connect_provider_websocket_socket",
        lambda endpoint, *, timeout: (urlsplit(endpoint), pinned),
    )

    async def reject_connect(*_args, **_kwargs):
        raise InvalidStatus(Response(401, "Unauthorized", Headers(), b"provider-secret-body"))

    monkeypatch.setattr(volc.websockets, "connect", reject_connect)

    with caplog.at_level("INFO"):
        with pytest.raises(AsrAuthenticationError) as caught:
            asyncio.run(
                volc.VolcengineStreamingAsrAdapter(settings).transcribe(
                    b"\0\0",
                    16_000,
                )
            )
    assert caught.value.code == "asr_auth_failed"
    assert caught.value.status_code == 401
    assert pinned.closed is True
    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert "provider-secret-body" not in str(caught.value)


def test_binary_server_error_does_not_surface_provider_body_or_key(
    settings,
    monkeypatch,
    caplog,
):
    secret = "server-error-secret"
    monkeypatch.setenv("ASR_API_KEY", secret)
    error = _server_frame(
        {"message": f"provider echoed {secret}"},
        message_type=volc.MSG_SERVER_ERROR,
        error_code=45_000_000,
        final_packet=True,
    )
    websocket = _FakeWebSocket([error], log_id="bad\r\nlog")

    monkeypatch.setattr(
        volc,
        "connect_provider_websocket_socket",
        lambda endpoint, *, timeout: (urlsplit(endpoint), _PinnedSocket()),
    )
    monkeypatch.setattr(
        volc.websockets,
        "connect",
        lambda *_args, **_kwargs: _async_value(websocket),
    )

    with caplog.at_level("INFO"):
        with pytest.raises(AsrInputError) as caught:
            asyncio.run(
                volc.VolcengineStreamingAsrAdapter(settings).transcribe(
                    b"\0\0",
                    16_000,
                )
            )
    assert caught.value.status_code == 45_000_000
    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert "bad\r\nlog" not in caplog.text


async def _async_value(value):
    return value


def test_adapter_rejects_wrong_pcm_contract_and_size(settings, monkeypatch):
    monkeypatch.setenv("ASR_API_KEY", "test-key")
    adapter = volc.VolcengineStreamingAsrAdapter(settings)

    with pytest.raises(AsrInputError, match="16 kHz"):
        asyncio.run(adapter.transcribe(b"\0\0", 48_000))
    with pytest.raises(AsrInputError, match="16-bit"):
        asyncio.run(adapter.transcribe(b"\0", 16_000))

    monkeypatch.setenv("ASR_MAX_AUDIO_BYTES", "1024")
    with pytest.raises(AsrInputError, match="ASR_MAX_AUDIO_BYTES"):
        asyncio.run(adapter.transcribe(b"\0\0" * 513, 16_000))


class _PrewarmFakeWebSocket:
    """带 ``wait_closed``/``close_code`` 的最小 WSS 桩，供预热池单测使用。"""

    def __init__(self) -> None:
        self.closed = False
        self.close_code: int | None = None
        self._closed_event = asyncio.Event()

    async def close(self) -> None:
        self.closed = True
        self.close_code = 1000
        self._closed_event.set()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()


def test_prewarm_pool_hands_out_connection_once_then_falls_back(
    settings,
    monkeypatch,
):
    monkeypatch.setenv("ASR_API_KEY", "test-key")
    config = volc.resolve_volcengine_streaming_asr_config(settings)
    created: list[tuple[_PrewarmFakeWebSocket, str]] = []

    async def fake_connect(_config, *, request_id, connect_id):
        websocket = _PrewarmFakeWebSocket()
        created.append((websocket, request_id))
        return websocket, "log-prewarm"

    monkeypatch.setattr(volc, "_connect_websocket", fake_connect)

    async def _run() -> None:
        pool = volc.AsrConnectionPrewarmPool()
        pool.schedule(config)
        for _ in range(200):
            if pool._entries:
                break
            await asyncio.sleep(0)

        entry = await pool.acquire(config)
        assert entry is not None
        assert entry.websocket is created[0][0]
        # 识别 session 必须沿用预热握手头里固定的 request_id。
        assert entry.request_id == created[0][1]
        assert entry.log_id == "log-prewarm"
        # 同设备连续两句抢同一条预热连接：第二个取用者拿不到，只能回退现建。
        assert await pool.acquire(config) is None

        await pool.close()
        # 已被取走的连接归识别 session 所有，池关闭不得替它关连接。
        assert created[0][0].closed is False
        await entry.websocket.close()

    asyncio.run(_run())


def test_prewarm_pool_expires_stale_entry_and_rebuilds_in_background(
    settings,
    monkeypatch,
):
    monkeypatch.setenv("ASR_API_KEY", "test-key")
    config = volc.resolve_volcengine_streaming_asr_config(settings)
    created: list[_PrewarmFakeWebSocket] = []

    async def fake_connect(_config, *, request_id, connect_id):
        websocket = _PrewarmFakeWebSocket()
        created.append(websocket)
        return websocket, ""

    monkeypatch.setattr(volc, "_connect_websocket", fake_connect)

    async def _run() -> None:
        # acquire 时发现条目超龄：就地关闭并回退现建。
        stale_pool = volc.AsrConnectionPrewarmPool(ttl_seconds=60.0)
        stale_socket = _PrewarmFakeWebSocket()
        stale_pool._entries[stale_pool._key(config)] = volc._PrewarmedConnection(
            websocket=stale_socket,
            request_id="stale-request",
            connect_id="stale-connect",
            log_id="",
            created_mono=time.monotonic() - 120.0,
        )
        assert await stale_pool.acquire(config) is None
        assert stale_socket.closed is True
        await stale_pool.close()

        # 后台看护：TTL 到期的连接被关闭，并自动重建一条新连接备用。
        pool = volc.AsrConnectionPrewarmPool(ttl_seconds=0.05)
        pool.schedule(config)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(created) < 2:
            await asyncio.sleep(0.01)
        assert len(created) >= 2
        assert created[0].closed is True
        entry = await pool.acquire(config)
        if entry is not None:
            assert any(entry.websocket is socket for socket in created[1:])
        await pool.close()

    asyncio.run(_run())


def test_prewarm_connect_failure_is_silent_and_acquire_falls_back(
    settings,
    monkeypatch,
):
    monkeypatch.setenv("ASR_API_KEY", "test-key")
    config = volc.resolve_volcengine_streaming_asr_config(settings)
    attempts: list[str] = []

    async def failing_connect(_config, *, request_id, connect_id):
        attempts.append(request_id)
        raise OSError("dns down")

    monkeypatch.setattr(volc, "_connect_websocket", failing_connect)

    async def _run() -> None:
        pool = volc.AsrConnectionPrewarmPool()
        pool.schedule(config)
        for _ in range(200):
            if attempts and not pool._tasks:
                break
            await asyncio.sleep(0)

        # 预热失败静默吞掉；acquire 返回 None 让识别路径回退现建。
        assert attempts
        assert pool._entries == {}
        assert await pool.acquire(config) is None
        await pool.close()

    asyncio.run(_run())


def test_transcribe_uses_prewarmed_connection_without_new_handshake(
    settings,
    monkeypatch,
):
    monkeypatch.setenv("ASR_API_KEY", "test-key")
    transcript = "预热连接直接可用。"
    partial = _server_frame(
        {"code": 20_000_000, "result": {"text": "预热"}},
    )
    final = _server_frame(
        {"code": 20_000_000, "result": {"text": transcript}},
        final_packet=True,
    )
    handshakes: list[str] = []

    async def fail_connect(_config, *, request_id, connect_id):
        handshakes.append(request_id)
        raise AssertionError("预热命中时关键路径不允许再握手")

    monkeypatch.setattr(volc, "_connect_websocket", fail_connect)

    async def _run() -> None:
        pool = volc._prewarm_pool_for_current_loop()
        config = volc.resolve_volcengine_streaming_asr_config(settings)
        websocket = _FakeWebSocket([partial, final])
        pool._entries[pool._key(config)] = volc._PrewarmedConnection(
            websocket=websocket,
            request_id="prewarmed-request",
            connect_id="prewarmed-connect",
            log_id="log-prewarmed",
            created_mono=time.monotonic(),
        )

        text = await volc.VolcengineStreamingAsrAdapter(settings).transcribe(
            b"\x01\x00" * 320,
            16_000,
        )

        assert text == transcript
        assert websocket.closed is True
        # 本句全部帧都走了预热连接，关键路径零新建握手。
        assert handshakes == []
        await pool.close()

    asyncio.run(_run())
