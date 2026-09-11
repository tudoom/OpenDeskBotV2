"""首页摄像头改成实时流（/camera_view，1 fps）：订阅带 fps、设备节拍取所有订阅者的最大值；
慢订阅者只收最新一帧；订阅一接上先回放上一帧；首页页面契约（不再轮询抓拍、只在可见时接入）。"""

from __future__ import annotations

import asyncio
import io
import json

from PIL import Image

from tests.test_camera_preview_leases import _commands, _Hub


def _jpeg(color):
    from deskbot_server.llm.vision_input import validate_jpeg

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(buf, format="JPEG")
    return validate_jpeg(buf.getvalue())


def test_parse_preview_fps_clamps_and_defaults():
    from deskbot_server.application.camera_preview import CAMERA_PREVIEW_MAX_FPS, parse_preview_fps

    assert parse_preview_fps("1") == 1 and parse_preview_fps(3) == 3
    assert parse_preview_fps("0") == 1 and parse_preview_fps("99") == CAMERA_PREVIEW_MAX_FPS
    assert parse_preview_fps(None) == 2 and parse_preview_fps("x") == 2 and parse_preview_fps("", default=1) == 1


def test_device_cadence_follows_fastest_subscriber():
    from deskbot_server.application.camera_preview import CameraPreviewLeaseManager

    async def _run():
        hub = _Hub()
        leases = CameraPreviewLeaseManager(hub, fps=2, refresh_seconds=60)
        await leases.acquire("dev", 1)  # 首页 1 fps
        assert leases.target_fps("dev") == 1 and _commands(hub)[-1][1] == 1
        await leases.acquire("dev", 2)  # 调试页 2 fps → 设备升到 2
        assert leases.target_fps("dev") == 2 and _commands(hub)[-1][1] == 2
        await leases.acquire("dev", 1)  # 又一个首页订阅者：不变，不发命令
        n = len(hub.sent)
        assert leases.target_fps("dev") == 2 and len(hub.sent) == n
        await leases.release("dev", 2)  # 调试页走了 → 回到 1
        assert leases.target_fps("dev") == 1 and _commands(hub)[-1][1] == 1
        await leases.release("dev", 1)
        assert leases.target_fps("dev") == 1 and len(hub.sent) == n + 1
        await leases.release("dev", 1)  # 最后一个走 → 0
        assert leases.target_fps("dev") == 0 and _commands(hub)[-1][1] == 0
        await leases.close()

    asyncio.run(_run())


def test_slow_subscriber_only_receives_latest_frame():
    from deskbot_server.application.camera_broker import CameraImageBroker

    async def _run():
        gate = asyncio.Event()
        sent: list = []

        async def _send(ws, payload):
            sent.append(payload)
            if isinstance(payload, (bytes, bytearray)) and len([p for p in sent if isinstance(p, (bytes, bytearray))]) == 1:
                await gate.wait()  # 第一帧的二进制卡住：模拟慢网页

        broker = CameraImageBroker(_send)
        ws = object()
        await broker.add_subscriber(ws, "dev")
        red, green, blue = _jpeg((255, 0, 0)), _jpeg((0, 255, 0)), _jpeg((0, 0, 255))
        for frame in (red, green, blue):
            await broker.publish_validated("dev", frame)
            await asyncio.sleep(0)
        gate.set()
        await asyncio.sleep(0.05)
        frames = [bytes(p) for p in sent if isinstance(p, (bytes, bytearray))]
        assert frames == [red.data, blue.data]  # 中间那帧被丢掉，最新的一帧补发
        metas = [json.loads(p) for p in sent if isinstance(p, str)]
        assert [m["type"] for m in metas] == ["camera_frame", "camera_frame"] and metas[1]["size"] == len(blue.data)
        await broker.remove_subscriber(ws)

    asyncio.run(_run())


def test_new_subscriber_gets_cached_last_frame_when_nothing_fresh(monkeypatch):
    from deskbot_server.application import camera_broker

    async def _run():
        sent: list = []

        async def _send(ws, payload):
            sent.append((ws, payload))

        broker = camera_broker.CameraImageBroker(_send)
        frame = _jpeg((9, 9, 9))
        await broker.publish_validated("dev", frame)  # 没人订阅，但上一帧记下了
        monkeypatch.setattr(camera_broker, "_snapshot_ttl_seconds", lambda: 0.0)  # 新鲜帧窗口已过
        ws = object()
        await broker.add_subscriber(ws, "dev")
        await asyncio.sleep(0.02)
        assert [w for w, _p in sent] == [ws, ws]
        meta = json.loads(sent[0][1])
        assert meta["type"] == "camera_frame" and meta["cached"] is True and meta["age_s"] >= 0
        assert bytes(sent[1][1]) == frame.data
        # 别的设备的订阅者拿不到
        other = object()
        await broker.add_subscriber(other, "dev-2")
        await asyncio.sleep(0.02)
        assert all(w is not other for w, _p in sent)

    asyncio.run(_run())


def test_camera_view_handler_leases_at_requested_fps():
    from deskbot_server.application.camera_broker import CameraImageBroker
    from deskbot_server.ws.camera import handle_camera_view

    class _Leases:
        def __init__(self):
            self.calls: list = []

        async def acquire(self, dev, fps=None):
            self.calls.append(("acquire", dev, fps))
            return True

        async def release(self, dev, fps=None):
            self.calls.append(("release", dev, fps))

    class _Ws:
        path = "/camera_view?device_id=dev-1&fps=1"
        remote_address = ("127.0.0.1", 40124)

        def __init__(self):
            self.sent: list = []

        async def send(self, message):
            self.sent.append(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _send(ws, payload):
        await ws.send(payload)

    async def _run():
        leases = _Leases()
        ws = _Ws()
        await handle_camera_view(ws, CameraImageBroker(_send), leases)
        assert leases.calls == [("acquire", "dev-1", 1), ("release", "dev-1", 1)]
        ready = json.loads(ws.sent[0])
        assert ready["type"] == "ready" and ready["fps"] == 1

    asyncio.run(_run())
