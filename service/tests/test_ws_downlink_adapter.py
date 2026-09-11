from __future__ import annotations

import asyncio
import json

from deskbot_server.infrastructure.serial.session import DeviceSession
from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter


class _UnusedSerial:
    timeout = 0.1
    write_timeout = 0.1

    def read(self, size: int = 1) -> bytes:
        del size
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        return None


class _PlainWebSocket:
    async def send(self, message) -> None:
        del message


def _adapter(websocket) -> WsDownlinkAdapter:
    return WsDownlinkAdapter(
        websocket,
        settings=object(),  # type: ignore[arg-type]
        device_id="deskbot_123456abcdef",
        dp_broker=None,
    )


def test_ws_downlink_adapter_enables_half_duplex_only_for_usb_session():
    async def _run() -> None:
        usb_session = DeviceSession(
            "COM_TEST",
            _UnusedSerial(),
            generation=1,
        )

        assert _adapter(usb_session).half_duplex_media_mic is True
        assert _adapter(_PlainWebSocket()).half_duplex_media_mic is False

    asyncio.run(_run())


def test_ws_downlink_adapter_cancel_uses_out_of_band_sender(monkeypatch):
    async def _run() -> None:
        from deskbot_server.infrastructure.ws import downlink_adapter as adapter_module

        websocket = _PlainWebSocket()
        calls: list[tuple[object, dict]] = []

        async def _cancel(target, wire):
            calls.append((target, json.loads(wire)))
            return True

        monkeypatch.setattr(adapter_module, "cancel_pb_device_downlink", _cancel)
        assert await _adapter(websocket).cancel_pb_playback("cancel-ws") is True
        assert calls == [
            (
                websocket,
                {
                    "type": "pb_cancel",
                    "req": "cancel-ws",
                    "t_mono": calls[0][1]["t_mono"],
                },
            )
        ]

    asyncio.run(_run())


def test_plain_ws_media_does_not_emit_usb_mic_barrier(monkeypatch):
    async def _run() -> None:
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.infrastructure.ws import downlink_adapter as adapter_module
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "plain_ws_dev"
        media_req = "plain_ws_media"
        sent: list[dict] = []
        websocket = _PlainWebSocket()

        async def _send(target, wire, binaries=None, pcm=None):
            del pcm
            assert target is websocket
            message = json.loads(wire)
            sent.append(message)
            assert binaries == [b"media"]
            await pb_ack_gate.notify(
                device_id,
                {
                    "req": str(message["req"]),
                    "idx": 0,
                    "phase": "played",
                },
            )
            return True

        monkeypatch.setattr(
            adapter_module,
            "_send_pb_wire_to_asr_device",
            _send,
        )
        delivery = await _send_pb_pairs(
            _adapter(websocket),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": media_req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 5},
                    },
                    [b"media"],
                )
            ],
            pb_req=media_req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "played"
        assert [message.get("mic") for message in sent] == [None]
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_usb_device_tts_media_orders_mute_media_open(monkeypatch):
    async def _run() -> None:
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.infrastructure.ws import downlink_adapter as adapter_module
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "usb_device_tts_dev"
        media_req = "usb_device_tts_media"
        sent: list[tuple[dict, list[bytes]]] = []
        usb_session = DeviceSession(
            "COM_TEST",
            _UnusedSerial(),
            generation=1,
        )

        async def _send(target, wire, binaries=None, pcm=None):
            del pcm
            assert target is usb_session
            message = json.loads(wire)
            payloads = list(binaries or [])
            sent.append((message, payloads))
            req = str(message["req"])
            await pb_ack_gate.notify(
                device_id,
                {"req": req, "idx": 0, "phase": "accepted"},
            )
            if message.get("mic") is None:
                await pb_ack_gate.notify(
                    device_id,
                    {"req": req, "idx": 0, "phase": "played"},
                )
            return True

        monkeypatch.setattr(
            adapter_module,
            "_send_pb_wire_to_asr_device",
            _send,
        )
        delivery = await _send_pb_pairs(
            _adapter(usb_session),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": media_req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 5},
                    },
                    [b"media"],
                )
            ],
            pb_req=media_req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "played"
        assert [message.get("mic") for message, _payloads in sent] == [
            "mute",
            None,
            "open",
        ]
        assert [payloads for _message, payloads in sent] == [
            [],
            [b"media"],
            [],
        ]
        assert sent[1][0]["req"] == media_req
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())
