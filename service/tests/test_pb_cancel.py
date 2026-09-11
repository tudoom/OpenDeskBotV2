from __future__ import annotations

import asyncio
import json


def test_pb_cancel_aborts_inflight_worker_and_bypasses_old_queue():
    from deskbot_server.ws.ws_send import (
        _send_pb_wire_to_asr_device,
        cancel_pb_device_downlink,
    )

    class SlowWebSocket:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.sent: list[str] = []
            self._asr_chat_pb_serial_queue = True

        async def send(self, message) -> None:
            payload = json.loads(message) if isinstance(message, str) else {}
            if payload.get("type") != "pb_cancel":
                self.started.set()
                await asyncio.Event().wait()
            self.sent.append(message)

    async def _run() -> None:
        ws = SlowWebSocket()
        old_send = asyncio.create_task(
            _send_pb_wire_to_asr_device(
                ws,
                json.dumps({"type": "pb_start", "req": "old"}),
            )
        )
        await asyncio.wait_for(ws.started.wait(), timeout=1.0)

        cancelled = await cancel_pb_device_downlink(
            ws,
            json.dumps({"type": "pb_cancel"}),
        )
        assert cancelled is True
        assert await asyncio.wait_for(old_send, timeout=1.0) is False
        assert [json.loads(item)["type"] for item in ws.sent] == ["pb_cancel"]

    asyncio.run(_run())


def test_pb_worker_stop_and_cancel_never_await_or_cancel_current_task():
    from deskbot_server.ws import ws_send

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._asr_chat_pb_serial_queue = True

        async def send(self, message: str) -> None:
            self.sent.append(message)

    async def _exercise_stop() -> None:
        ws = WebSocket()
        q: asyncio.Queue = asyncio.Queue()
        abandoned = ws_send._PbDeviceJob(wire="abandoned")
        q.put_nowait(abandoned)
        current = asyncio.current_task()
        setattr(ws, ws_send._PB_DEVICE_QUEUE_ATTR, q)
        setattr(ws, ws_send._PB_DEVICE_WORKER_ATTR, current)

        await ws_send._stop_pb_device_downlink_worker(ws)

        assert abandoned.done.is_set()
        assert abandoned.ok_json is False
        assert not hasattr(ws, ws_send._PB_DEVICE_QUEUE_ATTR)
        assert not hasattr(ws, ws_send._PB_DEVICE_WORKER_ATTR)
        assert asyncio.current_task() is current

    async def _exercise_cancel() -> None:
        ws = WebSocket()
        q: asyncio.Queue = asyncio.Queue()
        abandoned = ws_send._PbDeviceJob(wire="abandoned")
        q.put_nowait(abandoned)
        current = asyncio.current_task()
        setattr(ws, ws_send._PB_DEVICE_QUEUE_ATTR, q)
        setattr(ws, ws_send._PB_DEVICE_WORKER_ATTR, current)

        cancel_wire = json.dumps({"type": "pb_cancel"})
        cancelled = await ws_send.cancel_pb_device_downlink(ws, cancel_wire)

        assert cancelled is True
        assert abandoned.done.is_set()
        assert abandoned.ok_json is False
        assert ws.sent == [cancel_wire]
        assert not hasattr(ws, ws_send._PB_DEVICE_QUEUE_ATTR)
        assert not hasattr(ws, ws_send._PB_DEVICE_WORKER_ATTR)
        assert asyncio.current_task() is current

    async def _run() -> None:
        await _exercise_stop()
        await _exercise_cancel()

    asyncio.run(_run())


def test_audio_cancel_waits_for_active_turn_and_resets_device_state():
    from deskbot_server.ws.asr_chat import _cancel_device_interaction

    class FakeSession:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel_rom_uplink(self) -> None:
            self.cancelled = True

    class FakeHub:
        def __init__(self) -> None:
            self.cancelled_devices: list[str] = []

        async def cancel_playback(self, device_id: str) -> None:
            self.cancelled_devices.append(device_id)

    class FakeRegistry:
        def __init__(self) -> None:
            self.states: list[tuple[str, str]] = []

        async def set_interaction_state(self, device_id: str, state: str) -> None:
            self.states.append((device_id, state))

    async def _run() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def _active_turn() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        turn = asyncio.create_task(_active_turn())
        turn_tasks = [turn]
        await asyncio.wait_for(started.wait(), timeout=1.0)
        session = FakeSession()
        hub = FakeHub()
        registry = FakeRegistry()

        await _cancel_device_interaction(
            session=session,  # type: ignore[arg-type]
            turn_tasks=turn_tasks,
            device_id="deskbot-cancel",
            asr_chat_hub=hub,  # type: ignore[arg-type]
            registry=registry,  # type: ignore[arg-type]
        )

        assert session.cancelled is True
        assert stopped.is_set()
        assert turn.cancelled()
        assert turn_tasks == []
        assert hub.cancelled_devices == ["deskbot-cancel"]
        assert registry.states == [("deskbot-cancel", "IDLE")]

    asyncio.run(_run())
