from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import typing
from types import SimpleNamespace

import pytest


def _rtc_context(*, device_id: str, room_name: str | None = None):
    participant = SimpleNamespace(identity=f"deskbot-usb-{device_id}")
    room = SimpleNamespace(
        name=room_name or f"deskbot-{device_id}-1234abcd",
        remote_participants={"device": participant},
    )
    return SimpleNamespace(
        session=SimpleNamespace(room_io=SimpleNamespace(room=room)),
        function_call=SimpleNamespace(id="call-1"),
    )


def test_worker_tools_bind_to_exact_room_device():
    from deskbot_server.rtc_worker_tools import _device_id_from_context

    assert _device_id_from_context(_rtc_context(device_id="deskbot_a")) == "deskbot_a"
    with pytest.raises(RuntimeError, match="do not match"):
        _device_id_from_context(
            _rtc_context(
                device_id="deskbot_a",
                room_name="deskbot-deskbot_b-1234abcd",
            )
        )


def test_worker_exports_named_livekit_tools():
    from deskbot_server.rtc_worker_tools import (
        RTC_TOOL_SCHEMAS,
        build_livekit_deskbot_tools,
    )

    tools = build_livekit_deskbot_tools()
    assert {tool.id for tool in tools} == {
        str(schema["name"]) for schema in RTC_TOOL_SCHEMAS
    }
    assert {
        "capture_and_describe",
        "move_head",
        "play_expression",
    } <= {tool.id for tool in tools}
    for tool in tools:
        callback = getattr(tool, "_func", None) or getattr(tool, "func", None)
        assert callback is not None
        hints = typing.get_type_hints(inspect.unwrap(callback))
        assert hints["ctx"].__name__ == "RunContext"


def test_visual_servo_tool_schema_is_removed():
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    assert "look_at_person" not in {
        row["name"] for row in build_rtc_tool_schemas()
    }


def test_play_expression_schema_refreshes_real_catalog_and_aliases(monkeypatch):
    from deskbot_server.application import expression_runtime
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    monkeypatch.setattr(
        expression_runtime,
        "expression_tool_catalog",
        lambda: {
            "values": ["idle", "happy", "grin"],
            "aliases": {"grin": "happy"},
            "state_mappings": {"idle": "idle", "happy": "happy"},
            "expressions": [
                {"name": "idle", "title": "Idle", "aliases": []},
                {"name": "happy", "title": "Happy", "aliases": ["grin"]},
            ],
        },
    )

    schema = next(
        row for row in build_rtc_tool_schemas() if row["name"] == "play_expression"
    )
    assert schema["parameters"]["properties"]["name"]["enum"] == [
        "idle",
        "happy",
        "grin",
    ]
    assert "grin->happy" in schema["description"]
    assert "never invent" in schema["description"]


@pytest.mark.parametrize(
    ("server_scheme", "bridge_scheme"),
    [
        ("http", "http"),
        ("https", "https"),
        ("ws", "http"),
        ("wss", "https"),
    ],
)
def test_tool_bridge_maps_server_transport_scheme(
    server_scheme: str,
    bridge_scheme: str,
):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    manager = RtcAgentSdkManager(
        SimpleNamespace(server=SimpleNamespace(port=9000))
    )
    manager.configure_tool_bridge(scheme=server_scheme, port=9000)

    assert (
        manager.tool_bridge_url
        == f"{bridge_scheme}://127.0.0.1:9000/internal/rtc/tools"
    )


class _Connection:
    remote_address = ("127.0.0.1", 43123)


class _Request:
    path = "/internal/rtc/tools"
    method = "POST"

    def __init__(self, *, token: str, body: dict):
        self.headers = {"X-Deskbot-RTC-Bridge": token}
        self.body = json.dumps(body).encode("utf-8")


class _Broker:
    max_events = 100

    @staticmethod
    def snapshot_events(_device_id, _limit):
        return []


class _Registry:
    @staticmethod
    def snapshot():
        return []

    @staticmethod
    async def pb_ack_llm_context(_device_id):
        return '{"servo":{"x":91,"y":89}}'


class _Hub:
    @staticmethod
    async def first_ws(_device_id):
        return object()


class _Chat:
    settings = None


def test_core_bridge_requires_token_and_executes_inside_core(monkeypatch):
    from deskbot_server.application import rtc_tool_service
    from deskbot_server.ws.http_api import _build_http_request_handler

    calls: list[dict] = []

    async def _execute(**kwargs):
        calls.append(kwargs)
        return {"tool": kwargs["tool"], "ok": True, "status": "done"}

    monkeypatch.setattr(rtc_tool_service, "execute_rtc_tool", _execute)
    handler = _build_http_request_handler(
        _Broker(),
        _Registry(),
        asr_chat_hub=_Hub(),
        chat=_Chat(),
        rtc_tool_token="bridge-secret",
    )
    body = {
        "device_id": "deskbot_a",
        "tool": "capture_camera",
        "arguments": {},
        "call_id": "call-1",
    }

    denied = asyncio.run(
        handler(_Connection(), _Request(token="wrong-secret", body=body))
    )
    assert denied.status_code == 401
    assert calls == []

    accepted = asyncio.run(
        handler(_Connection(), _Request(token="bridge-secret", body=body))
    )
    assert accepted.status_code == 200
    payload = json.loads(accepted.body.decode("utf-8"))
    assert payload["result"]["status"] == "done"
    assert calls[0]["device_id"] == "deskbot_a"
    assert calls[0]["asr_chat_hub"].__class__ is _Hub
    assert calls[0]["device_context"] == '{"servo":{"x":91,"y":89}}'


def test_core_bridge_rejects_non_loopback_before_auth():
    from deskbot_server.ws.http_api import _build_http_request_handler

    handler = _build_http_request_handler(
        _Broker(),
        _Registry(),
        asr_chat_hub=_Hub(),
        chat=_Chat(),
        rtc_tool_token="bridge-secret",
    )
    connection = SimpleNamespace(remote_address=("192.0.2.10", 40000))
    response = asyncio.run(
        handler(
            connection,
            _Request(
                token="bridge-secret",
                body={
                    "device_id": "deskbot_a",
                    "tool": "capture_camera",
                    "arguments": {},
                },
            ),
        )
    )
    assert response.status_code == 403


def _valid_jpeg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 40, 60)).save(out, format="JPEG")
    return out.getvalue()


def test_capture_and_describe_uses_only_same_session_transient_context(monkeypatch):
    from livekit.agents import llm

    import deskbot_server.rtc_worker_tools as worker_tools

    jpeg = _valid_jpeg()
    core_result = {
        "tool": "capture_and_describe",
        "ok": True,
        "width": 8,
        "height": 8,
        worker_tools._RTC_VISION_IMAGE_B64_KEY: base64.b64encode(jpeg).decode(
            "ascii"
        ),
    }

    async def _fake_core(_ctx, tool, raw_arguments):
        assert tool == "capture_and_describe"
        assert raw_arguments == {"question": "我手里是什么颜色？"}
        return core_result

    monkeypatch.setattr(worker_tools, "_call_core_tool", _fake_core)

    class _Handle:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def finish(self):
            for callback in list(self.callbacks):
                callback(self)

    class _Session:
        def __init__(self):
            self.history = llm.ChatContext.empty()
            self.history.add_message(role="user", content="原始语音问题")
            self.room_io = _rtc_context(device_id="deskbot_a").session.room_io
            self.calls = []
            self.handle = _Handle()

        def generate_reply(self, **kwargs):
            self.calls.append(kwargs)
            return self.handle

    session = _Session()
    ctx = SimpleNamespace(
        session=session,
        function_call=SimpleNamespace(call_id="call-real", id="call-wrong"),
    )
    tool = next(
        row
        for row in worker_tools.build_livekit_deskbot_tools()
        if row.id == "capture_and_describe"
    )
    callback = getattr(tool, "_func", None) or getattr(tool, "func", None)

    result = asyncio.run(callback({"question": "我手里是什么颜色？"}, ctx))

    assert result is None
    assert worker_tools._RTC_VISION_IMAGE_B64_KEY not in core_result
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["tool_choice"] == "none"
    transient = call["chat_ctx"]
    assert transient is not session.history
    assert len(session.history.items) == 1
    image_items = [
        content
        for message in transient.messages()
        for content in message.content
        if isinstance(content, llm.ImageContent)
    ]
    assert len(image_items) == 1
    assert image_items[0].image.startswith("data:image/jpeg;base64,")
    session.handle.finish()
    assert transient.items == []
    assert len(session.history.items) == 1


def test_transient_rtc_image_rejects_oversized_base64():
    from deskbot_server.llm.vision_input import MAX_VISION_IMAGE_BYTES
    from deskbot_server.rtc_worker_tools import (
        _RTC_VISION_IMAGE_B64_KEY,
        _consume_rtc_vision_jpeg,
    )

    max_encoded = ((MAX_VISION_IMAGE_BYTES + 2) // 3) * 4
    result = {_RTC_VISION_IMAGE_B64_KEY: "A" * (max_encoded + 1)}
    with pytest.raises(RuntimeError, match="oversized"):
        _consume_rtc_vision_jpeg(result)
    assert _RTC_VISION_IMAGE_B64_KEY not in result


def test_worker_bridge_prefers_livekit_call_id(monkeypatch):
    import httpx

    import deskbot_server.rtc_worker_tools as worker_tools

    posted: list[dict] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"tool": "capture_camera", "ok": True}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            posted.append(kwargs["json"])
            return _Response()

    monkeypatch.setenv(worker_tools._BRIDGE_URL_ENV, "http://127.0.0.1:9000/tools")
    monkeypatch.setenv(worker_tools._BRIDGE_TOKEN_ENV, "token")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())
    ctx = _rtc_context(device_id="deskbot_a")
    ctx.function_call = SimpleNamespace(call_id="call-right", id="call-wrong")

    asyncio.run(worker_tools._call_core_tool(ctx, "capture_camera", {}))

    assert posted[0]["call_id"] == "call-right"


def test_rtc_capture_and_describe_is_fresh_one_shot_without_tool_round(
    monkeypatch,
):
    import deskbot_server.application.rtc_tool_service as service
    from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY

    capture_calls: list[dict] = []

    async def _capture(device_id, **kwargs):
        capture_calls.append({"device_id": device_id, **kwargs})
        return {
            "ok": True,
            "width": 8,
            "height": 8,
            TRANSIENT_VISION_IMAGE_KEY: {
                "mime_type": "image/jpeg",
                "bytes": _valid_jpeg(),
            },
        }

    async def _unexpected_round(*_args, **_kwargs):
        raise AssertionError("capture_and_describe must bypass generic tool round")

    monkeypatch.setattr(service, "capture_camera_for_device_async", _capture)
    monkeypatch.setattr(service, "execute_tools_round", _unexpected_round)

    hub = object()
    result = asyncio.run(
        service.execute_rtc_tool(
            device_id="deskbot_a",
            tool="capture_and_describe",
            arguments={"question": "看到了什么？"},
            call_id="call-1",
            asr_chat_hub=hub,
        )
    )

    assert result["ok"] is True
    assert service._RTC_VISION_IMAGE_B64_KEY in result
    assert TRANSIENT_VISION_IMAGE_KEY not in result
    assert len(capture_calls) == 1
    assert capture_calls[0]["device_id"] == "deskbot_a"
    assert capture_calls[0]["hub"] is hub
    assert capture_calls[0]["display"] is False
    assert capture_calls[0]["require_fresh"] is True


def test_rtc_core_tool_allowlist_excludes_visual_head_tracking():
    from deskbot_server.application.rtc_tool_service import rtc_tool_names

    assert "look_at_person" not in rtc_tool_names()
    assert "move_head" in rtc_tool_names()


@pytest.mark.parametrize(
    ("delivery_status", "played", "expected_status"),
    [
        ("played", True, "moved"),
        ("failed", False, "failed"),
        ("cancelled", False, "cancelled"),
        ("disconnected", False, "disconnected"),
    ],
)
def test_rtc_move_head_requires_terminal_played_ack(
    monkeypatch,
    delivery_status,
    played,
    expected_status,
):
    import deskbot_server.application.rtc_tool_service as service
    from deskbot_server.application.interaction_feedback import ServoMoveDelivery

    send_kwargs = []

    async def _send(*_args, **kwargs):
        send_kwargs.append(kwargs)
        return ServoMoveDelivery(
            request_id="rtc-move-req",
            delivered=1,
            accepted=True,
            played=played,
            status=delivery_status,
        )

    monkeypatch.setattr(service, "send_servo_moves_and_wait", _send)
    result = asyncio.run(
        service.execute_rtc_tool(
            device_id="deskbot_a",
            tool="move_head",
            arguments={"move": "center", "duration_ms": 400},
            call_id="call-move",
            asr_chat_hub=object(),
        )
    )

    assert result["ok"] is played
    assert result["status"] == expected_status
    assert result["played"] is played
    assert result["request_id"] == "rtc-move-req"
    assert len(send_kwargs[0]["request_id"]) == 16
    assert send_kwargs[0]["request_id"].startswith("rtcm")


def test_rtc_move_head_maps_protocol_error_to_invalid_move(monkeypatch):
    import deskbot_server.application.rtc_tool_service as service
    from deskbot_server.servo_protocol import ServoProtocolError

    async def _send(*_args, **_kwargs):
        raise ServoProtocolError("race with catalog update")

    monkeypatch.setattr(service, "send_servo_moves_and_wait", _send)
    result = asyncio.run(
        service.execute_rtc_tool(
            device_id="deskbot_a",
            tool="move_head",
            arguments={"move": "center"},
            call_id="stable-call",
            asr_chat_hub=object(),
        )
    )
    assert result["ok"] is False
    assert result["status"] == "invalid_move"


def test_rtc_prompt_requires_vision_tool_only_for_visual_facts(monkeypatch):
    from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager

    monkeypatch.delenv("DESKBOT_RTC_SYSTEM_PROMPT", raising=False)
    prompt = RtcAgentSdkManager(SimpleNamespace())._rtc_system_prompt()

    assert "capture_and_describe" in prompt
    assert "当前环境" in prompt
    assert "普通对话不要拍照" in prompt
    assert "当前一轮" in prompt
