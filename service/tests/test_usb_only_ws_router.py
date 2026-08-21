from __future__ import annotations

import asyncio
import json

import pytest


class _WebSocket:
    def __init__(self, path: str) -> None:
        self.path = path
        self.remote_address = ("127.0.0.1", 40123)
        self.sent: list[str | bytes] = []
        self.closed: tuple[int, str] | None = None

    async def send(self, message):
        self.sent.append(message)

    async def close(self, *, code: int, reason: str):
        self.closed = (code, reason)


class _IterableWebSocket(_WebSocket):
    def __init__(self, path: str, incoming: list[str | bytes] | None = None) -> None:
        super().__init__(path)
        self._incoming = list(incoming or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class _SubscriberBroker:
    max_events = 100

    def __init__(self) -> None:
        self.added: list[tuple[object, str | None]] = []
        self.removed: list[object] = []

    async def add_subscriber(self, websocket, device_filter=None) -> None:
        self.added.append((websocket, device_filter))

    async def remove_subscriber(self, websocket) -> None:
        self.removed.append(websocket)


@pytest.mark.parametrize(
    "path",
    [
        "/asr_chat?device_id=deskbot_001122334455",
        "/camera?device_id=deskbot_001122334455",
        "/camera_uplink?device_id=deskbot_001122334455",
        "/device_pipeline?role=producer&device_id=deskbot_001122334455",
    ],
)
def test_physical_device_websocket_transports_are_rejected(path):
    from deskbot_server.ws.router import handle_client

    websocket = _WebSocket(path)
    asyncio.run(
        handle_client(
            websocket,
            pipeline=object(),
            audio_cfg=object(),
            device_pipeline_broker=object(),
            registry=object(),
            asr_chat_hub=object(),
            camera_image_broker=object(),
            camera_face_runtime=object(),
        )
    )

    assert websocket.closed == (
        1008,
        "physical devices must use USB CDC",
    )
    assert len(websocket.sent) == 1
    payload = json.loads(str(websocket.sent[0]))
    assert payload["error"] == "usb_cdc_required"


def test_device_pipeline_handler_defensively_rejects_direct_producer_call():
    from deskbot_server.ws.device_pipeline import handle_device_pipeline

    websocket = _IterableWebSocket(
        "/device_pipeline?role=producer&device_id=deskbot_001122334455"
    )
    asyncio.run(handle_device_pipeline(websocket, object()))

    assert websocket.closed == (
        1008,
        "physical devices must use USB CDC",
    )
    payload = json.loads(str(websocket.sent[0]))
    assert payload["error"] == "usb_cdc_required"


def test_device_pipeline_handler_only_serves_browser_subscribers():
    from deskbot_server.ws.device_pipeline import handle_device_pipeline

    websocket = _IterableWebSocket(
        "/device_pipeline?role=subscriber&device_id=deskbot_001122334455",
        incoming=[json.dumps({"type": "ping"})],
    )
    broker = _SubscriberBroker()
    asyncio.run(handle_device_pipeline(websocket, broker))

    assert websocket.closed is None
    assert broker.added == [(websocket, "deskbot_001122334455")]
    assert broker.removed == [websocket]
    assert [json.loads(str(message))["type"] for message in websocket.sent] == [
        "ready",
        "pong",
    ]
