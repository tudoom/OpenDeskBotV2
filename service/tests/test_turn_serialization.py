from __future__ import annotations

import asyncio


def test_client_request_id_is_bounded_ascii_and_protocol_safe():
    from deskbot_server.ws.asr_chat import _normalize_client_request_id

    assert _normalize_client_request_id(" client.req_1:retry-2 ") == (
        "client.req_1:retry-2"
    )
    assert _normalize_client_request_id("") is None
    assert _normalize_client_request_id("x" * 65) is None
    assert _normalize_client_request_id("request id") is None
    assert _normalize_client_request_id("请求-1") is None
    assert _normalize_client_request_id("request/1") is None


def test_latest_pending_turn_replacement_never_cancels_or_overlaps_active_turn():
    from deskbot_server.ws.asr_chat import _enqueue_turn

    async def _run() -> None:
        holder: list[asyncio.Task] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        executed: list[str] = []
        active = 0
        max_active = 0

        def _job(name: str, *, block: bool = False):
            async def _run_job() -> None:
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                executed.append(name)
                try:
                    if block:
                        first_started.set()
                        await release_first.wait()
                finally:
                    active -= 1

            return _run_job

        first = _enqueue_turn(
            holder,
            _job("first", block=True),
            device_id="deskbot-actor",
            source="text",
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        second = _enqueue_turn(
            holder,
            _job("second"),
            device_id="deskbot-actor",
            source="text",
        )
        await asyncio.sleep(0)
        third = _enqueue_turn(
            holder,
            _job("third"),
            device_id="deskbot-actor",
            source="text",
        )
        await asyncio.sleep(0)

        assert not first.done()
        assert second.cancelled()
        assert executed == ["first"]

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, third), timeout=1.0)
        assert executed == ["first", "third"]
        assert max_active == 1

    asyncio.run(_run())
