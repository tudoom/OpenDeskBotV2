from __future__ import annotations

import asyncio
import json

from deskbot_server.infrastructure.serial import integration
from deskbot_server.infrastructure.serial.integration import SerialServiceBridge
from deskbot_server.infrastructure.serial.session import HelloInfo


class FakeManager:
    def __init__(self) -> None:
        self.on_ready = None
        self.on_frame = None
        self.on_closed = None
        self.started = False
        self.stopped = False

    def set_callbacks(self, *, on_ready, on_frame, on_closed) -> None:
        self.on_ready = on_ready
        self.on_frame = on_frame
        self.on_closed = on_closed

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeSession:
    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.device_id = "deskbot_123456abcdef"
        self.port = "COM4"
        self.is_closed = False
        self.sent: list[str | bytes] = []
        self.closed = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, *, reason: str = "") -> None:
        del reason
        self.is_closed = True
        self.closed.set()

    @property
    def is_ready(self) -> bool:
        return not self.is_closed


def _hello() -> HelloInfo:
    return HelloInfo(
        device_id="deskbot_123456abcdef",
        product="Deskbot",
        firmware="usb-test",
        session_epoch=42,
        heartbeat_ms=2_000,
        timeout_ms=6_500,
        max_payload=1024 * 1024,
        capabilities=(
            "control_json",
            "pb_wire",
            "audio_up_opus",
            "audio_down_opus",
            "camera_jpeg",
        ),
    )


def test_bridge_routes_ready_session_as_usb_and_cleans_handler(monkeypatch):
    async def _run() -> None:
        calls = []
        registered = []
        handler_started = asyncio.Event()

        monkeypatch.setattr(
            integration,
            "ensure_local_device",
            lambda device_id: registered.append(device_id),
        )

        async def fake_handle_asr_chat(
            session,
            pipeline,
            audio_cfg,
            device_id,
            registry,
            dp_broker,
            hub,
            camera_image_broker,
            camera_face_runtime,
            **kwargs,
        ) -> None:
            calls.append(
                {
                    "session": session,
                    "pipeline": pipeline,
                    "audio_cfg": audio_cfg,
                    "device_id": device_id,
                    "registry": registry,
                    "dp_broker": dp_broker,
                    "hub": hub,
                    "camera_image_broker": camera_image_broker,
                    "camera_face_runtime": camera_face_runtime,
                    **kwargs,
                }
            )
            await session.send(
                json.dumps(
                    {
                        "type": "ready",
                        "device_id": device_id,
                        "transport": kwargs["registry_transport"],
                    }
                )
            )
            handler_started.set()
            await session.closed.wait()

        monkeypatch.setattr(
            integration,
            "handle_asr_chat",
            fake_handle_asr_chat,
        )
        manager = FakeManager()
        registry = object()
        hub = object()
        pipeline = object()
        audio_cfg = object()
        dp_broker = object()
        bridge = SerialServiceBridge(
            registry,  # type: ignore[arg-type]
            hub,  # type: ignore[arg-type]
            pipeline=pipeline,  # type: ignore[arg-type]
            audio_cfg=audio_cfg,  # type: ignore[arg-type]
            dp_broker=dp_broker,  # type: ignore[arg-type]
            manager=manager,  # type: ignore[arg-type]
        )

        await bridge.start()
        assert manager.started is True
        first = FakeSession(generation=7)
        await manager.on_ready(first, _hello())
        await asyncio.wait_for(handler_started.wait(), timeout=1.0)

        assert registered == ["deskbot_123456abcdef"]
        assert len(calls) == 1
        call = calls[0]
        assert call["session"] is first
        assert call["device_id"] == "deskbot_123456abcdef"
        assert call["registry_channel"] == "usb_cdc"
        assert call["registry_transport"] == "usb_cdc"
        assert call["session_generation"] == 7
        sent_messages = [json.loads(message) for message in first.sent]
        assert {
            "type": "ready",
            "device_id": "deskbot_123456abcdef",
            "transport": "usb_cdc",
        } in sent_messages
        mic_resets = [
            message for message in sent_messages if message.get("mic") == "open"
        ]
        assert len(mic_resets) == 1
        assert mic_resets[0]["type"] == "pb_single"

        await first.close(reason="unplugged")
        await manager.on_closed(first, None)
        await asyncio.sleep(0)
        assert first not in bridge._handlers

        await bridge.stop()
        assert manager.stopped is True
        assert bridge._handlers == {}

    asyncio.run(_run())


def test_slow_rtc_bind_does_not_block_usb_ready(monkeypatch):
    async def _run() -> None:
        from deskbot_server import rtc_runtime

        handler_started = asyncio.Event()
        bind_started = asyncio.Event()
        bind_cancelled = asyncio.Event()

        async def fake_handle_asr_chat(session, *_args, **_kwargs) -> None:
            handler_started.set()
            await session.closed.wait()

        async def slow_bind(*_args, **_kwargs) -> None:
            bind_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                bind_cancelled.set()
                raise

        monkeypatch.setattr(integration, "handle_asr_chat", fake_handle_asr_chat)
        monkeypatch.setattr(rtc_runtime, "bind_usb_device", slow_bind)

        manager = FakeManager()
        bridge = SerialServiceBridge(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            pipeline=object(),  # type: ignore[arg-type]
            audio_cfg=object(),  # type: ignore[arg-type]
            dp_broker=object(),  # type: ignore[arg-type]
            manager=manager,  # type: ignore[arg-type]
        )
        session = FakeSession(generation=8)

        await asyncio.wait_for(manager.on_ready(session, _hello()), timeout=0.2)
        await asyncio.wait_for(handler_started.wait(), timeout=0.2)
        await asyncio.wait_for(bind_started.wait(), timeout=0.2)
        assert any(
            json.loads(message).get("mic") == "open"
            for message in session.sent
            if isinstance(message, str)
        )
        assert session in bridge._handlers
        assert session in bridge._rtc_bind_tasks

        await session.close(reason="unplugged")
        await manager.on_closed(session, None)
        await asyncio.wait_for(bind_cancelled.wait(), timeout=0.2)
        assert session not in bridge._rtc_bind_tasks
        await bridge.stop()

    asyncio.run(_run())


def test_ack_waiting_mic_reset_does_not_block_usb_ready(monkeypatch):
    async def _run() -> None:
        handler_started = asyncio.Event()
        mic_reset_started = asyncio.Event()
        mic_reset_ack = asyncio.Event()
        rtc_bound = asyncio.Event()

        class AckWaitingSession(FakeSession):
            async def send(self, message: str | bytes) -> None:
                self.sent.append(message)
                if (
                    isinstance(message, str)
                    and json.loads(message).get("mic") == "open"
                ):
                    mic_reset_started.set()
                    await mic_reset_ack.wait()

        async def fake_handle_asr_chat(session, *_args, **_kwargs) -> None:
            handler_started.set()
            await session.closed.wait()

        async def fake_bind(*_args, **_kwargs) -> None:
            rtc_bound.set()

        monkeypatch.setattr(integration, "handle_asr_chat", fake_handle_asr_chat)
        monkeypatch.setattr(integration, "ensure_local_device", lambda _id: None)
        monkeypatch.setattr(
            SerialServiceBridge,
            "_bind_rtc",
            staticmethod(fake_bind),
        )

        manager = FakeManager()
        bridge = SerialServiceBridge(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            pipeline=object(),  # type: ignore[arg-type]
            audio_cfg=object(),  # type: ignore[arg-type]
            dp_broker=object(),  # type: ignore[arg-type]
            manager=manager,  # type: ignore[arg-type]
        )
        session = AckWaitingSession(generation=9)

        # Returning from on_ready represents releasing DeviceSession's RX loop;
        # only then can that loop consume the device's frame ACK.
        await asyncio.wait_for(manager.on_ready(session, _hello()), timeout=0.2)
        await asyncio.wait_for(handler_started.wait(), timeout=0.2)
        await asyncio.wait_for(mic_reset_started.wait(), timeout=0.2)
        assert not rtc_bound.is_set()

        mic_reset_ack.set()
        await asyncio.wait_for(rtc_bound.wait(), timeout=0.2)

        await session.close(reason="done")
        await manager.on_closed(session, None)
        await bridge.stop()

    asyncio.run(_run())


def test_close_callback_does_not_wait_through_pb_worker_handler_cycle(monkeypatch):
    async def _run() -> None:
        from deskbot_server import rtc_runtime

        async def fake_unbind(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(rtc_runtime, "unbind_usb_device", fake_unbind)

        manager = FakeManager()
        bridge = SerialServiceBridge(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            pipeline=object(),  # type: ignore[arg-type]
            audio_cfg=object(),  # type: ignore[arg-type]
            dp_broker=object(),  # type: ignore[arg-type]
            manager=manager,  # type: ignore[arg-type]
        )
        session = FakeSession(generation=10)

        close_task_holder: list[asyncio.Task] = []
        close_started = asyncio.Event()

        async def pb_worker() -> None:
            await close_started.wait()
            await close_task_holder[0]

        worker_task = asyncio.create_task(pb_worker())

        async def asr_handler() -> None:
            await worker_task

        handler_task = asyncio.create_task(asr_handler())
        bridge._handlers[session] = handler_task
        handler_task.add_done_callback(
            lambda completed: bridge._handler_done(session, completed)
        )

        async def close_session() -> None:
            close_started.set()
            await bridge._on_closed(session, TimeoutError("frame ACK timeout"))

        close_task = asyncio.create_task(close_session())
        close_task_holder.append(close_task)

        # Synchronously awaiting the handler here creates the production task
        # cycle and makes Task.cancel recurse.  The callback must instead
        # release the close task before the handler/worker are reaped.
        await asyncio.wait_for(close_task, timeout=0.2)
        await asyncio.wait_for(worker_task, timeout=0.2)
        await asyncio.wait_for(handler_task, timeout=0.2)
        for _ in range(20):
            if not bridge._handler_reapers:
                break
            await asyncio.sleep(0)
        assert bridge._handler_reapers == set()

        await bridge.stop()

    asyncio.run(_run())
