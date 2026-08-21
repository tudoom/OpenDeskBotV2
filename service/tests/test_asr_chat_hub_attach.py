"""AsrChatHub attach：同 device 仅保留最新 /asr_chat 连接。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from deskbot_server.ws.asr_chat_hub import AsrChatHub


def _mock_ws() -> MagicMock:
    """构造 attach 用的假连接。

    必须显式把 PB worker/queue 属性钉成 None：裸 MagicMock 的任意属性访问都
    会自动生成子 Mock，使 ws_send 里 ``getattr(ws, ..., None) is None`` 的
    闸门永远不生效，随后 ``_abort_queued_pb_device_jobs`` 对 Mock 队列的
    排空循环变成死循环并把整机内存吃满（8/19 三次宿主卡死的根因）。
    """

    ws = MagicMock()
    ws.close = AsyncMock()
    ws._bot_pb_device_downlink_worker = None
    ws._bot_pb_device_downlink_queue = None
    return ws


def test_attach_closes_previous_connection_for_same_device():
    async def _run() -> None:
        hub = AsrChatHub(device_pb_only=True)
        old_ws = _mock_ws()
        new_ws = _mock_ws()

        await hub.attach("dev1", old_ws)
        await hub.attach("dev1", new_ws)

        async with hub._lock:
            conns = hub._by_device.get("dev1", set())
        assert conns == {new_ws}
        old_ws.close.assert_awaited_once()
        assert old_ws.close.await_args.kwargs.get("code") == 1000

    asyncio.run(_run())


def test_attach_keeps_only_one_ws_in_hub():
    async def _run() -> None:
        hub = AsrChatHub(device_pb_only=True)
        ws_a = _mock_ws()
        ws_b = _mock_ws()
        ws_c = _mock_ws()

        await hub.attach("dev1", ws_a)
        await hub.attach("dev1", ws_b)
        await hub.attach("dev1", ws_c)

        async with hub._lock:
            conns = hub._by_device.get("dev1", set())
        assert conns == {ws_c}
        assert ws_a.close.await_count == 1
        assert ws_b.close.await_count == 1
        ws_c.close.assert_not_awaited()

    asyncio.run(_run())
