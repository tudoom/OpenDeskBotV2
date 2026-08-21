from __future__ import annotations

import asyncio


def test_registry_hides_offline_device_after_ttl(monkeypatch):
    from deskbot_server.ws import registry as registry_module

    clock = [1_000.0]
    monkeypatch.setattr(registry_module.time, "time", lambda: clock[0])

    async def _run() -> None:
        registry = registry_module.DeviceRegistry(offline_ttl_seconds=60)
        ws = object()
        await registry.connect("deskbot_a", "asr_chat", ws)
        assert registry.snapshot()[0]["online"] is True
        assert registry.snapshot()[0]["interaction_state"] == "AUTHENTICATING"
        assert await registry.set_interaction_state("deskbot_a", "listening")
        assert registry.snapshot()[0]["interaction_state"] == "LISTENING"
        assert not await registry.set_interaction_state("deskbot_a", "invalid")
        await registry.disconnect(ws)
        assert registry.snapshot()[0]["online"] is False
        assert registry.snapshot()[0]["interaction_state"] == "OFFLINE"
        clock[0] += 61
        assert registry.snapshot() == []

    asyncio.run(_run())


def test_old_asr_disconnect_does_not_overwrite_new_connection_state(monkeypatch):
    from deskbot_server.ws.registry import DeviceRegistry

    class Socket:
        pass

    async def _run() -> None:
        registry = DeviceRegistry(offline_ttl_seconds=60)
        old = Socket()
        new = Socket()
        await registry.connect("deskbot_replace", "asr_chat", old)
        await registry.connect("deskbot_replace", "asr_chat", new)
        assert await registry.set_interaction_state("deskbot_replace", "IDLE")

        await registry.disconnect(old)

        snapshot = registry.snapshot()[0]
        assert snapshot["online"] is True
        assert snapshot["channels"]["asr_chat"] == 1
        assert snapshot["interaction_state"] == "IDLE"
        assert snapshot.get("interaction_error") is None

    asyncio.run(_run())


def test_device_quiesce_closes_and_waits_for_debug_observers():
    from deskbot_server.ws.registry import DeviceRegistry

    class Observer:
        def __init__(self) -> None:
            self.closed: list[tuple[int, str]] = []

        async def close(self, *, code, reason):
            self.closed.append((code, reason))

    async def _run() -> None:
        registry = DeviceRegistry()
        observer = Observer()
        assert await registry.add_observer("deskbot_observed", observer)
        assert not await registry.wait_device_offline(
            "deskbot_observed",
            timeout=0,
        )

        closed = await registry.close_device(
            "deskbot_observed",
            reason="route generation replaced",
        )
        assert closed == 1
        assert observer.closed == [(4003, "route generation replaced")]

        # The route's finally block removes the observer after close.
        await registry.remove_observer(observer)
        assert await registry.wait_device_offline(
            "deskbot_observed",
            timeout=0,
        )
        await registry.forget_device("deskbot_observed")

    asyncio.run(_run())


def test_debug_observer_watchdog_closes_revoked_connection(monkeypatch):
    from deskbot_server.ws import api_key_gate

    class Observer:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.closed: list[tuple[int, str]] = []

        async def send(self, message):
            self.sent.append(message)

        async def close(self, *, code, reason):
            self.closed.append((code, reason))

    async def _run() -> None:
        observer = Observer()
        auth = api_key_gate.DebugSubscriberAuth(
            mode="local_token",
            device_id="deskbot_revoked",
            raw_credential="signed-token",
        )
        started = asyncio.Event()

        async def handler():
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            api_key_gate,
            "debug_subscriber_auth_is_current",
            lambda _auth: False,
        )
        task = asyncio.create_task(
            api_key_gate.run_with_debug_subscriber_auth_watchdog(
                observer,
                auth,
                handler(),
                poll_interval=0.01,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(task, timeout=1)

        assert observer.closed == [(1008, "authorization_revoked")]
        assert any("credential_revoked" in message for message in observer.sent)

    asyncio.run(_run())
