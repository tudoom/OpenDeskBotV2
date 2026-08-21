from __future__ import annotations

import asyncio
import json

import pytest


def test_task_request_includes_official_event_and_pcm_is_bounded(monkeypatch):
    from deskbot_server.tts import doubao
    from deskbot_server.tts.protocols import EventType, Message, MsgType

    captured: dict[str, object] = {}
    messages = [
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=b"1234",
        ),
    ]

    async def noop(*_args, **_kwargs):
        return None

    async def capture_task(_websocket, payload: bytes, _session_id: str):
        captured.update(json.loads(payload.decode("utf-8")))

    async def receive(_websocket):
        return messages.pop(0)

    monkeypatch.setattr(doubao, "start_session", noop)
    monkeypatch.setattr(doubao, "task_request", capture_task)
    monkeypatch.setattr(doubao, "finish_session", noop)
    monkeypatch.setattr(doubao, "receive_message", receive)
    monkeypatch.setattr(doubao, "MAX_PCM_BYTES", 3)

    connection = doubao.DoubaoTtsConnection(
        doubao.DoubaoTtsConfig(api_key="local-test-key")
    )
    connection._ws = object()

    with pytest.raises(RuntimeError, match="安全上限"):
        asyncio.run(connection._synthesize_once("你好"))

    assert captured["event"] == int(EventType.TaskRequest)
    assert captured["req_params"] == {"text": "你好"}


def test_cancelled_synthesis_resets_connection(monkeypatch):
    from deskbot_server.tts import doubao

    connection = doubao.DoubaoTtsConnection(
        doubao.DoubaoTtsConfig(api_key="local-test-key")
    )
    connection._ws = object()
    connection._ready = True
    reset_done = asyncio.Event()

    async def ensure_ready():
        return None

    async def never_finishes(_text: str):
        await asyncio.Future()

    async def reset():
        connection._ws = None
        connection._ready = False
        reset_done.set()

    monkeypatch.setattr(connection, "_ensure_ready", ensure_ready)
    monkeypatch.setattr(connection, "_synthesize_once", never_finishes)
    monkeypatch.setattr(connection, "_reset", reset)

    async def scenario():
        task = asyncio.create_task(connection.synthesize("你好"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reset_done.is_set()
        assert connection._ws is None
        assert connection._ready is False

    asyncio.run(scenario())


def test_connection_pool_is_scoped_to_each_asyncio_run(monkeypatch):
    from deskbot_server.tts import doubao

    created: list[object] = []

    class FakeConnection:
        def __init__(self, _cfg):
            self.owner_loop = asyncio.get_running_loop()
            created.append(self)

        async def synthesize(self, text: str):
            assert asyncio.get_running_loop() is self.owner_loop
            return doubao.DoubaoTtsResult(
                pcm=text.encode("utf-8"),
                sample_rate=24_000,
            )

    monkeypatch.setattr(doubao, "DoubaoTtsConnection", FakeConnection)
    monkeypatch.setattr(doubao, "_pools", {})
    cfg = doubao.DoubaoTtsConfig(api_key="local-test-key")

    async def synthesize_once(text: str) -> bytes:
        result = await doubao.synthesize_doubao_tts(text, cfg)
        return result.pcm

    assert asyncio.run(synthesize_once("first")) == b"first"
    assert asyncio.run(synthesize_once("second")) == b"second"
    assert len(created) == 2

    async def synthesize_twice_on_one_loop() -> tuple[bytes, bytes]:
        first = await synthesize_once("same-loop-1")
        second = await synthesize_once("same-loop-2")
        return first, second

    assert asyncio.run(synthesize_twice_on_one_loop()) == (
        b"same-loop-1",
        b"same-loop-2",
    )
    assert len(created) == 3


def _make_retry_connection(monkeypatch, script: list[object]):
    """构造一个跳过真实网络、按脚本回放服务端消息的重试测试连接。"""

    from deskbot_server.tts import doubao

    connection = doubao.DoubaoTtsConnection(
        doubao.DoubaoTtsConfig(api_key="local-test-key")
    )

    async def ensure_ready():
        connection._ws = object()
        connection._ready = True

    async def reset():
        connection._ws = None
        connection._ready = False

    async def noop(*_args, **_kwargs):
        return None

    async def receive(_websocket):
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(connection, "_ensure_ready", ensure_ready)
    monkeypatch.setattr(connection, "_reset", reset)
    monkeypatch.setattr(doubao, "start_session", noop)
    monkeypatch.setattr(doubao, "task_request", noop)
    monkeypatch.setattr(doubao, "finish_session", noop)
    monkeypatch.setattr(doubao, "receive_message", receive)
    return connection


def test_streaming_retry_never_replays_already_emitted_pcm(monkeypatch):
    from deskbot_server.tts.protocols import EventType, Message, MsgType

    full_pcm = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    script: list[object] = [
        # 第一次会话：向下游流出前 4 字节后连接中断。
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=full_pcm[:4],
        ),
        RuntimeError("mock connection drop"),
        # 重试会话：供应商从头重发整句，且分块边界与首次不同。
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=full_pcm[:2],
        ),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=full_pcm[2:6],
        ),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=full_pcm[6:],
        ),
        Message(type=MsgType.FullServerResponse, event=EventType.SessionFinished),
    ]
    connection = _make_retry_connection(monkeypatch, script)

    chunks: list[bytes] = []
    result = asyncio.run(connection.synthesize("你好", on_pcm=chunks.append))

    # 重发的前缀被吞掉，下游只收到未播出的尾部：无任何字节重叠。
    assert chunks == [full_pcm[:4], full_pcm[4:6], full_pcm[6:]]
    assert b"".join(chunks) == full_pcm
    assert result.pcm == full_pcm
    assert script == []


def test_streaming_retry_shorter_resend_prefers_missing_tail_over_replay(
    monkeypatch,
):
    from deskbot_server.tts.protocols import EventType, Message, MsgType

    first_pcm = b"\x11\x12\x13\x14\x15\x16"
    retry_pcm = b"\x11\x12\x13\x14"
    script: list[object] = [
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=first_pcm,
        ),
        RuntimeError("mock connection drop"),
        # 重试重发的音频比已播偏移还短：宁可缺尾音也不能重播。
        Message(type=MsgType.FullServerResponse, event=EventType.SessionStarted),
        Message(
            type=MsgType.AudioOnlyServer,
            event=EventType.TTSResponse,
            payload=retry_pcm,
        ),
        Message(type=MsgType.FullServerResponse, event=EventType.SessionFinished),
    ]
    connection = _make_retry_connection(monkeypatch, script)

    chunks: list[bytes] = []
    result = asyncio.run(connection.synthesize("你好", on_pcm=chunks.append))

    assert chunks == [first_pcm]
    assert result.pcm == retry_pcm
    assert script == []


def test_provider_error_detail_redacts_known_credentials(monkeypatch):
    from deskbot_server.tts import doubao

    monkeypatch.setattr(doubao, "MAX_ERROR_DETAIL_CHARS", 64)
    secret = "local-test-secret"
    access_token = "local-test-access-token"
    result = doubao._redact_provider_detail(
        f"failure key={secret} token={access_token}\n" + ("x" * 200),
        secret,
        access_token,
    )

    assert secret not in result
    assert access_token not in result
    assert result.count("<redacted>") == 2
    assert "\n" not in result
    assert len(result) == 65
    assert result.endswith("…")
