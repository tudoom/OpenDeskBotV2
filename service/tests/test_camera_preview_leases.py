from __future__ import annotations

import asyncio
import json

import pytest

from deskbot_server.application import camera_preview
from deskbot_server.application.camera_preview import (
    CameraPreviewLeaseManager,
    preview_target_fps,
)
from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb


class _Hub:
    def __init__(self) -> None:
        self.listeners: set = set()
        self.sent: list[tuple[str, dict]] = []
        self.fail_next = 0
        self.target_count = 0
        self.target_reached = asyncio.Event()

    def add_online_listener(self, listener) -> None:
        self.listeners.add(listener)

    def remove_online_listener(self, listener) -> None:
        self.listeners.discard(listener)

    async def send(self, device_id: str, payload: dict) -> int:
        self.sent.append((device_id, dict(payload)))
        if len(self.sent) >= self.target_count:
            self.target_reached.set()
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("simulated USB send failure")
        return 1

    async def notify_online(self, device_id: str) -> None:
        for listener in tuple(self.listeners):
            await listener(device_id)

    def wait_for_call_count(self, count: int) -> asyncio.Event:
        self.target_count = count
        self.target_reached.clear()
        if len(self.sent) >= count:
            self.target_reached.set()
        return self.target_reached


def _commands(hub: _Hub) -> list[tuple[str, int, bool]]:
    return [
        (
            device_id,
            int(payload["cam_fps"]),
            bool(payload.get("camera_once")),
        )
        for device_id, payload in hub.sent
    ]


def test_preview_leases_are_reference_counted_per_device() -> None:
    async def _run() -> list[tuple[str, int, bool]]:
        hub = _Hub()
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=60,
        )
        await leases.acquire("dev-a")
        await leases.acquire("dev-a")
        await leases.acquire("dev-b")
        assert _commands(hub) == [
            ("dev-a", 2, False),
            ("dev-b", 2, False),
        ]

        await leases.release("dev-a")
        assert _commands(hub)[-1] == ("dev-b", 2, False)
        await leases.release("dev-b")
        await leases.release("dev-a")
        commands = _commands(hub)
        assert leases._command_locks == {}
        assert leases._command_users == {}
        await leases.close()
        assert hub.listeners == set()
        return commands

    assert asyncio.run(_run()) == [
        ("dev-a", 2, False),
        ("dev-b", 2, False),
        ("dev-b", 0, False),
        ("dev-a", 0, False),
    ]


def test_target_fps_query_reflects_lease_refcount() -> None:
    async def _run() -> None:
        hub = _Hub()
        leases = CameraPreviewLeaseManager(hub, fps=2, refresh_seconds=60)
        assert preview_target_fps("dev-query") == 0
        await leases.acquire("dev-query")
        assert leases.target_fps("dev-query") == 2
        assert preview_target_fps("dev-query") == 2
        assert preview_target_fps("dev-other") == 0
        assert preview_target_fps("") == 0

        await leases.acquire("dev-query")
        await leases.release("dev-query")
        # 还有一个订阅者：期望值保持 2FPS。
        assert preview_target_fps("dev-query") == 2
        await leases.release("dev-query")
        assert preview_target_fps("dev-query") == 0

        await leases.close()
        assert leases.target_fps("dev-query") == 0
        assert camera_preview._active_manager is None
        assert preview_target_fps("dev-query") == 0

    asyncio.run(_run())


def test_active_preview_restarts_on_device_online() -> None:
    async def _run() -> list[tuple[str, int, bool]]:
        hub = _Hub()
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=60,
        )
        await leases.acquire("dev-reconnect")
        await hub.notify_online("dev-reconnect")
        await leases.release("dev-reconnect")
        before = list(_commands(hub))
        await hub.notify_online("dev-reconnect")
        assert _commands(hub) == before
        await leases.close()
        return before

    assert asyncio.run(_run()) == [
        ("dev-reconnect", 2, False),
        ("dev-reconnect", 2, False),
        ("dev-reconnect", 0, False),
    ]


def test_preview_send_failure_is_retried_and_does_not_poison_refcount() -> None:
    async def _run() -> list[tuple[str, int, bool]]:
        hub = _Hub()
        hub.fail_next = 1
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=60,
        )

        # The failed first command must not reject the browser subscription.
        await leases.acquire("dev-retry")
        await hub.notify_online("dev-retry")

        # A failed final stop must still remove the count.  A later lease is a
        # new first subscriber and therefore sends a fresh start command.
        hub.fail_next = 1
        await leases.release("dev-retry")
        await leases.acquire("dev-retry")
        await leases.release("dev-retry")
        commands = _commands(hub)
        await leases.close()
        return commands

    assert asyncio.run(_run()) == [
        ("dev-retry", 2, False),
        ("dev-retry", 2, False),
        ("dev-retry", 0, False),
        ("dev-retry", 0, False),
        ("dev-retry", 2, False),
        ("dev-retry", 0, False),
    ]


def test_active_preview_periodically_refreshes_for_replacement_race() -> None:
    async def _run() -> list[tuple[str, int, bool]]:
        hub = _Hub()
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=0.01,
        )
        await leases.acquire("dev-refresh")
        refresh_ready = hub.wait_for_call_count(2)
        await asyncio.wait_for(refresh_ready.wait(), timeout=1)
        await leases.release("dev-refresh")
        commands = _commands(hub)
        await leases.close()
        return commands

    commands = asyncio.run(_run())
    assert commands[0] == ("dev-refresh", 2, False)
    assert commands[1] == ("dev-refresh", 2, False)
    assert commands[-1] == ("dev-refresh", 0, False)


def test_last_release_cannot_be_overtaken_by_inflight_start() -> None:
    class _BlockingStartHub(_Hub):
        def __init__(self) -> None:
            super().__init__()
            self.start_entered = asyncio.Event()
            self.allow_start = asyncio.Event()
            self.blocked = False

        async def send(self, device_id: str, payload: dict) -> int:
            self.sent.append((device_id, dict(payload)))
            if int(payload["cam_fps"]) > 0 and not self.blocked:
                self.blocked = True
                self.start_entered.set()
                await self.allow_start.wait()
            return 1

    async def _run() -> list[tuple[str, int, bool]]:
        hub = _BlockingStartHub()
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=60,
        )
        acquire_task = asyncio.create_task(leases.acquire("dev-race"))
        await asyncio.wait_for(hub.start_entered.wait(), timeout=1)
        release_task = asyncio.create_task(leases.release("dev-race"))
        # Let release publish the zero-subscriber state while the start send is
        # still blocked.  The final physical command must nevertheless be 0.
        await asyncio.sleep(0)
        hub.allow_start.set()
        await asyncio.gather(acquire_task, release_task)
        commands = _commands(hub)
        await leases.close()
        return commands

    commands = asyncio.run(_run())
    assert commands[0] == ("dev-race", 2, False)
    assert commands[-1] == ("dev-race", 0, False)


def test_new_acquire_cannot_be_overtaken_by_inflight_stop() -> None:
    class _BlockingStopHub(_Hub):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = asyncio.Event()
            self.allow_stop = asyncio.Event()
            self.block_stop = False

        async def send(self, device_id: str, payload: dict) -> int:
            self.sent.append((device_id, dict(payload)))
            if int(payload["cam_fps"]) == 0 and self.block_stop:
                self.block_stop = False
                self.stop_entered.set()
                await self.allow_stop.wait()
            return 1

    async def _run() -> list[tuple[str, int, bool]]:
        hub = _BlockingStopHub()
        leases = CameraPreviewLeaseManager(
            hub,
            fps=2,
            refresh_seconds=60,
        )
        await leases.acquire("dev-race")
        hub.block_stop = True
        release_task = asyncio.create_task(leases.release("dev-race"))
        await asyncio.wait_for(hub.stop_entered.wait(), timeout=1)
        acquire_task = asyncio.create_task(leases.acquire("dev-race"))
        await asyncio.sleep(0)
        hub.allow_stop.set()
        await asyncio.gather(release_task, acquire_task)
        commands = _commands(hub)
        await leases.release("dev-race")
        await leases.close()
        return commands

    commands = asyncio.run(_run())
    assert commands[0] == ("dev-race", 2, False)
    assert commands[-1] == ("dev-race", 2, False)


def test_camera_view_handler_balances_preview_lease() -> None:
    class _WebSocket:
        path = "/camera_view?device_id=dev-view"
        remote_address = ("127.0.0.1", 40123)

        def __init__(self) -> None:
            self.incoming = [json.dumps({"type": "ping"})]
            self.sent: list[str | bytes] = []

        async def send(self, message) -> None:
            self.sent.append(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

    class _Broker:
        def __init__(self) -> None:
            self.events: list[tuple[str, object, str | None]] = []

        async def add_subscriber(self, websocket, device_id) -> None:
            self.events.append(("add", websocket, device_id))

        async def remove_subscriber(self, websocket) -> None:
            self.events.append(("remove", websocket, None))

    class _Leases:
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []

        async def acquire(self, device_id: str) -> None:
            self.events.append(("acquire", device_id))

        async def release(self, device_id: str) -> None:
            self.events.append(("release", device_id))

    async def _run():
        from deskbot_server.ws.camera import handle_camera_view

        websocket = _WebSocket()
        broker = _Broker()
        leases = _Leases()
        await handle_camera_view(websocket, broker, leases)
        return websocket, broker, leases

    websocket, broker, leases = asyncio.run(_run())
    assert [json.loads(str(item))["type"] for item in websocket.sent] == [
        "ready",
        "pong",
    ]
    assert [event[0] for event in broker.events] == ["add", "remove"]
    assert leases.events == [
        ("acquire", "dev-view"),
        ("release", "dev-view"),
    ]


def test_camera_control_supports_explicit_stop_without_changing_one_shot() -> None:
    stop = build_cam_fps_signal_pb(cam_fps=0, req="stop-camera")
    assert stop["cam_fps"] == 0
    assert "camera_once" not in stop

    one_shot = build_cam_fps_signal_pb(
        cam_fps=5,
        req="one-camera-frame",
        one_shot=True,
    )
    assert one_shot["cam_fps"] == 5
    assert one_shot["camera_once"] is True

    with pytest.raises(ValueError):
        build_cam_fps_signal_pb(cam_fps=-1)
    with pytest.raises(ValueError):
        build_cam_fps_signal_pb(cam_fps=0, one_shot=True)
