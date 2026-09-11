"""network_connectivity_test 工具与 pb_ack 路径的单元测试。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from deskbot_server.ws.pb_ack_waiter import PbAckGate


def test_pb_ack_gate_wait_idx():
    async def _run():
        gate = PbAckGate()
        device_id = "test_dev"
        req = "req001"
        await gate.begin_req(device_id, req)

        async def delayed_ack():
            await asyncio.sleep(0.05)
            await gate.notify(device_id, {"req": req, "idx": 0})

        task = asyncio.create_task(delayed_ack())
        ok = await gate.wait_idx(device_id, req, 0, timeout=2.0)
        await task
        assert ok is True

    asyncio.run(_run())


def test_pb_ack_gate_out_of_order_still_advances():
    async def _run():
        gate = PbAckGate()
        device_id = "test_dev2"
        req = "req002"
        await gate.begin_req(device_id, req)
        await gate.notify(device_id, {"req": req, "idx": 2})
        ok = await gate.wait_idx(device_id, req, 1, timeout=0.5)
        assert ok is True

    asyncio.run(_run())


def test_pb_ack_gate_ignores_late_ack_after_end():
    async def _run() -> None:
        gate = PbAckGate()
        await gate.begin_req("late_dev", "late_req")
        await gate.end_req("late_dev", "late_req")
        await gate.notify(
            "late_dev",
            {"req": "late_req", "idx": 99, "phase": "played"},
        )
        assert await gate.state_count() == 0
        assert not await gate.wait_played(
            "late_dev",
            "late_req",
            99,
            timeout=0.01,
        )

    asyncio.run(_run())


def test_pb_ack_gate_legacy_ack_is_accepted_only():
    async def _run():
        gate = PbAckGate()
        await gate.begin_req("legacy_dev", "legacy_req")
        await gate.notify("legacy_dev", {"req": "legacy_req", "idx": 3})
        assert await gate.wait_accepted(
            "legacy_dev", "legacy_req", 3, timeout=0.1
        )
        assert not await gate.wait_played(
            "legacy_dev", "legacy_req", 3, timeout=0.05
        )

    asyncio.run(_run())


def test_pb_ack_gate_played_implies_accepted():
    async def _run():
        gate = PbAckGate()
        await gate.begin_req("played_dev", "played_req")
        await gate.notify(
            "played_dev",
            {"req": "played_req", "idx": 4, "phase": "played"},
        )
        assert await gate.wait_accepted(
            "played_dev", "played_req", 4, timeout=0.1
        )
        assert await gate.wait_played(
            "played_dev", "played_req", 4, timeout=0.1
        )

    asyncio.run(_run())


@pytest.mark.parametrize("terminal_phase", ["failed", "cancelled"])
def test_pb_ack_gate_terminal_failure_wakes_waiters(terminal_phase):
    async def _run():
        gate = PbAckGate()
        await gate.begin_req("terminal_dev", "terminal_req")
        waiter = asyncio.create_task(
            gate.wait_played_result(
                "terminal_dev",
                "terminal_req",
                0,
                timeout=5.0,
            )
        )
        await asyncio.sleep(0)
        await gate.notify(
            "terminal_dev",
            {"req": "terminal_req", "idx": 0, "phase": terminal_phase},
        )
        result = await asyncio.wait_for(waiter, timeout=0.2)
        assert result.ok is False
        assert result.status == terminal_phase
        assert not await gate.wait_accepted(
            "terminal_dev", "terminal_req", 0, timeout=0.01
        )
        assert not await gate.wait_played(
            "terminal_dev", "terminal_req", 0, timeout=0.01
        )
        await gate.end_req("terminal_dev", "terminal_req")

    asyncio.run(_run())


def test_pb_ack_gate_disconnect_wakes_waiters_and_releases_state():
    async def _run():
        gate = PbAckGate()
        await gate.begin_req("disconnect_dev", "disconnect_req")
        waiter = asyncio.create_task(
            gate.wait_accepted_result(
                "disconnect_dev",
                "disconnect_req",
                0,
                timeout=5.0,
            )
        )
        await asyncio.sleep(0)
        await gate.cancel_device("disconnect_dev")
        result = await asyncio.wait_for(waiter, timeout=0.2)
        assert result.ok is False
        assert result.status == "disconnected"
        assert await gate.state_count() == 0

    asyncio.run(_run())


def test_pb_ack_gate_duplicate_begin_does_not_replace_active_state():
    async def _run():
        gate = PbAckGate()
        await gate.begin_req("duplicate_dev", "duplicate_req")
        with pytest.raises(RuntimeError, match="already active"):
            await gate.begin_req("duplicate_dev", "duplicate_req")
        await gate.notify(
            "duplicate_dev",
            {"req": "duplicate_req", "idx": 0, "phase": "played"},
        )
        assert await gate.wait_played(
            "duplicate_dev", "duplicate_req", 0, timeout=0.1
        )
        await gate.end_req("duplicate_dev", "duplicate_req")

    asyncio.run(_run())


def test_pb_ack_normalizer_defaults_legacy_phase_to_accepted():
    from deskbot_server.util import _normalize_incoming_pb_ack

    legacy = _normalize_incoming_pb_ack(
        {"type": "pb_ack", "req": "r1", "idx": 1}
    )
    assert legacy is not None
    assert legacy["phase"] == "accepted"
    played = _normalize_incoming_pb_ack(
        {"type": "pb_ack", "phase": "played", "req": "r1", "idx": 1}
    )
    assert played is not None
    assert played["phase"] == "played"
    for phase in ("failed", "cancelled"):
        terminal = _normalize_incoming_pb_ack(
            {
                "type": "pb_ack",
                "phase": phase,
                "req": "r1",
                "idx": 1,
                "error": " motor stopped ",
            }
        )
        assert terminal is not None
        assert terminal["phase"] == phase
        assert terminal["error"] == "motor stopped"


def test_pb_ack_normalizer_accepts_display_crc_only_on_played():
    from deskbot_server.util import _normalize_incoming_pb_ack

    played = _normalize_incoming_pb_ack(
        {
            "type": "pb_ack",
            "phase": "played",
            "req": "display-1",
            "idx": 2,
            "display_crc32": "A1B2C3D4",
        }
    )
    accepted = _normalize_incoming_pb_ack(
        {
            "type": "pb_ack",
            "phase": "accepted",
            "req": "display-1",
            "idx": 2,
            "display_crc32": "ffffffff",
        }
    )
    malformed = _normalize_incoming_pb_ack(
        {
            "type": "pb_ack",
            "phase": "played",
            "req": "display-2",
            "idx": 2,
            "display_crc32": "not-a-crc",
        }
    )

    assert played["display_crc32"] == "a1b2c3d4"
    assert "display_crc32" not in accepted
    assert "display_crc32" not in malformed


def test_pb_sender_waits_for_terminal_played_ack():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        sent: list[dict] = []

        class _Downlink:
            async def send_pb_wire(self, wire, binaries=None):
                sent.append(json.loads(wire))
                return True

        req = "two_phase_req"

        async def _acks():
            await asyncio.sleep(0.02)
            await pb_ack_gate.notify(
                "two_phase_dev",
                {"req": req, "idx": 0, "phase": "accepted"},
            )
            await asyncio.sleep(0.02)
            await pb_ack_gate.notify(
                "two_phase_dev",
                {"req": req, "idx": 0, "phase": "played"},
            )

        notifier = asyncio.create_task(_acks())
        delivery = await _send_pb_pairs(
            _Downlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 4},
                    },
                    [b"\x00\x00\x00\x00"],
                )
            ],
            pb_req=req,
            device_id="two_phase_dev",
            n_pb=1,
            durable_replay=True,
        )
        await notifier
        assert delivery == "played"
        assert sent[0]["durable"] is True
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_usb_media_sender_mutes_before_declaration_and_restores_after_playback():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "half_duplex_dev"
        media_req = "half_duplex_media"
        sent: list[dict] = []

        class _UsbDownlink:
            half_duplex_media_mic = True

            async def send_pb_wire(self, wire, binaries=None):
                message = json.loads(wire)
                sent.append(message)
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

        delivery = await _send_pb_pairs(
            _UsbDownlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": media_req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 4},
                    },
                    [b"\x00\x00\x00\x00"],
                )
            ],
            pb_req=media_req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "played"
        assert [message.get("mic") for message in sent] == [
            "mute",
            None,
            "open",
        ]
        assert sent[1]["req"] == media_req
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_usb_media_sender_restores_mic_when_media_delivery_fails():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "half_duplex_fail_dev"
        media_req = "half_duplex_fail_media"
        sent: list[dict] = []

        class _UsbDownlink:
            half_duplex_media_mic = True

            async def send_pb_wire(self, wire, binaries=None):
                message = json.loads(wire)
                sent.append(message)
                if message.get("mic") is None:
                    return False
                await pb_ack_gate.notify(
                    device_id,
                    {
                        "req": str(message["req"]),
                        "idx": 0,
                        "phase": "accepted",
                    },
                )
                return True

        delivery = await _send_pb_pairs(
            _UsbDownlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": media_req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 4},
                    },
                    [b"\x00\x00\x00\x00"],
                )
            ],
            pb_req=media_req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "failed"
        assert [message.get("mic") for message in sent] == [
            "mute",
            None,
            "open",
        ]
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_usb_media_sender_restores_mic_when_mute_ack_is_lost(monkeypatch):
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws import pb_ack_waiter
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "half_duplex_mute_timeout_dev"
        media_req = "half_duplex_mute_timeout_media"
        sent: list[dict] = []
        monkeypatch.setattr(
            pb_ack_waiter, "pb_wait_ack_timeout_sec", lambda: 0.01
        )

        class _UsbDownlink:
            half_duplex_media_mic = True

            async def send_pb_wire(self, wire, binaries=None):
                message = json.loads(wire)
                sent.append(message)
                if message.get("mic") == "open":
                    await pb_ack_gate.notify(
                        device_id,
                        {
                            "req": str(message["req"]),
                            "idx": 0,
                            "phase": "accepted",
                        },
                    )
                return True

        delivery = await _send_pb_pairs(
            _UsbDownlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": media_req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 4},
                    },
                    [b"\x00\x00\x00\x00"],
                )
            ],
            pb_req=media_req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "failed"
        assert [message.get("mic") for message in sent] == ["mute", "open"]
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_pb_sender_waits_for_terminal_ack_without_audio():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        sent: list[dict] = []

        class _Downlink:
            async def send_pb_wire(self, wire, binaries=None):
                sent.append(json.loads(wire))
                assert not binaries
                return True

        req = "visual_motor_only_req"

        async def _ack():
            await asyncio.sleep(0.02)
            await pb_ack_gate.notify(
                "visual_motor_dev",
                {"req": req, "idx": 0, "phase": "played"},
            )

        notifier = asyncio.create_task(_ack())
        delivery = await _send_pb_pairs(
            _Downlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": req,
                        "idx": 0,
                        "chunk_ms": 50,
                        "anim": [{"ms": 50, "elements": {"eye": "blink"}}],
                        "servo": [
                            {"xm": 0, "ym": 0, "x": 90, "y": 90, "ms": 50}
                        ],
                    },
                    [],
                )
            ],
            pb_req=req,
            device_id="visual_motor_dev",
            n_pb=1,
        )
        await notifier
        assert delivery == "played"
        assert "audio" not in sent[0]
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_pb_sender_fail_safe_cancels_when_played_ack_never_arrives():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "servo_timeout_dev"
        req = "servo_timeout_req"

        class _Downlink:
            def __init__(self) -> None:
                self.cancel_calls = 0

            async def send_pb_wire(self, wire, binaries=None):
                assert not binaries
                message = json.loads(wire)
                await pb_ack_gate.notify(
                    device_id,
                    {"req": message["req"], "idx": 0, "phase": "accepted"},
                )
                return True

            async def cancel_pb_playback(self, request_id):
                assert request_id == req
                self.cancel_calls += 1
                return True

        downlink = _Downlink()
        delivery = await _send_pb_pairs(
            downlink,
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": req,
                        "idx": 0,
                        "chunk_ms": 600,
                        "servo": [
                            {"xm": 0, "ym": 0, "x": 84, "y": 90, "ms": 600}
                        ],
                    },
                    [],
                )
            ],
            pb_req=req,
            device_id=device_id,
            n_pb=1,
            played_timeout_sec=0.02,
        )

        assert delivery == "failed"
        assert downlink.cancel_calls == 1
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_pb_sender_fail_safe_cancels_when_binary_accepted_ack_is_missing(
    monkeypatch,
):
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws import pb_ack_waiter
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        monkeypatch.setattr(
            pb_ack_waiter,
            "pb_wait_ack_timeout_sec",
            lambda: 0.01,
        )
        device_id = "accepted_timeout_dev"
        req = "accepted_timeout_req"

        class _Downlink:
            def __init__(self) -> None:
                self.cancel_calls = 0

            async def send_pb_wire(self, _wire, binaries=None):
                assert binaries == [b"pcm"]
                return True

            async def cancel_pb_playback(self, request_id):
                assert request_id == req
                self.cancel_calls += 1
                return True

        downlink = _Downlink()
        delivery = await _send_pb_pairs(
            downlink,
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": req,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 3},
                    },
                    [b"pcm"],
                )
            ],
            pb_req=req,
            device_id=device_id,
            n_pb=1,
        )

        assert delivery == "failed"
        assert downlink.cancel_calls == 1
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_pb_sender_cancellation_still_sends_fail_safe_cancel_and_cleans_gate():
    async def _run():
        from deskbot_server.application.chat_flow import _send_pb_pairs
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "servo_task_cancel_dev"
        req = "servo_task_cancel_req"
        sent = asyncio.Event()

        class _Downlink:
            def __init__(self) -> None:
                self.cancel_calls = 0

            async def send_pb_wire(self, wire, binaries=None):
                assert not binaries
                message = json.loads(wire)
                await pb_ack_gate.notify(
                    device_id,
                    {"req": message["req"], "idx": 0, "phase": "accepted"},
                )
                sent.set()
                return True

            async def cancel_pb_playback(self, request_id):
                assert request_id == req
                self.cancel_calls += 1
                return True

        downlink = _Downlink()
        sender = asyncio.create_task(
            _send_pb_pairs(
                downlink,
                pairs=[
                    (
                        {
                            "type": "pb_single",
                            "req": req,
                            "idx": 0,
                            "chunk_ms": 600,
                            "servo": [
                                {
                                    "xm": 0,
                                    "ym": 0,
                                    "x": 84,
                                    "y": 90,
                                    "ms": 600,
                                }
                            ],
                        },
                        [],
                    )
                ],
                pb_req=req,
                device_id=device_id,
                n_pb=1,
                played_timeout_sec=30.0,
            )
        )
        await asyncio.wait_for(sent.wait(), timeout=1.0)
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender

        assert downlink.cancel_calls == 1
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_usb_servo_probe_waits_for_completed_control_operation(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from tools import network_connectivity_test as network_test

    calls: list[str] = []
    submitted_operation_id = ""

    def fake_http_json(url, _timeout, **_kwargs):
        nonlocal submitted_operation_id
        calls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/api/device_servo"):
            submitted_operation_id = query["operation_id"][0]
            return 202, {
                "ok": True,
                "operation_id": submitted_operation_id,
                "terminal": False,
                "status": "accepted",
            }
        assert parsed.path.endswith("/api/control_operation")
        assert query["operation_id"] == [submitted_operation_id]
        completed = len(calls) >= 3
        return 200, {
            "ok": True,
            "terminal": completed,
            "operation": {
                "terminal": completed,
                "status": "completed" if completed else "running",
            },
        }

    monkeypatch.setattr(network_test, "_http_json", fake_http_json)

    async def _run():
        _started, result = await network_test._send_pb_servo(
            "http://127.0.0.1:9000",
            "deskbot_probe",
            1.0,
            -1.0,
            api_key="test-key",
        )
        assert result["status"] == "completed"
        assert result["terminal"] is True

    asyncio.run(_run())
    assert len(calls) == 3
    assert submitted_operation_id.startswith("usb-servo:")


def test_legacy_llm_scene_argument_never_appends_display_pb(monkeypatch):
    import deskbot_server.application.chat_flow as chat_flow
    from deskbot_server.core.types import ChatTurnResult

    calls: list[dict] = []

    async def _send_pairs(_downlink, **kwargs):
        calls.append(kwargs)
        return "played" if len(calls) == 1 else "failed"

    monkeypatch.setattr(chat_flow, "_send_pb_pairs", _send_pairs)
    monkeypatch.setattr(
        chat_flow,
        "build_pb_wire_pairs",
        lambda *_args, **_kwargs: (
            [
                (
                    {
                        "type": "pb_single",
                        "req": "main-req",
                        "idx": 0,
                        "chunk_ms": 20,
                    },
                    [],
                )
            ],
            "main-req",
            1,
            24000,
        ),
    )
    class _Chat:
        tts_cfg = {"sample_rate": 24000}

    class _Downlink:
        @asynccontextmanager
        async def pb_serial_chain(self):
            yield

    async def _run():
        result = ChatTurnResult()
        await chat_flow._run_pb_playback(
            _Downlink(),
            _Chat(),
            reply_text="",
            parsed={
                "moves": [],
                "anims": [],
                "images": [],
            },
            llm_scenes=["happy"],
            request_id="turn-with-scene",
            device_id="scene-device",
            result=result,
            t_asr_start=None,
            motion_only=True,
        )
        assert len(calls) == 1
        assert result.status != "error"
        assert result.playback_status == "played"

    asyncio.run(_run())


def test_usb_service_test_report_summary():
    from tools.network_connectivity_test import TestReport

    r = TestReport(device_id="d1", base_url="http://127.0.0.1:9000")
    r.ok("health")
    r.control_latencies_ms = [120.0, 180.0, 150.0]
    text = r.summary()
    assert "PASS: health" in text
    assert "p50=150" in text
    assert "全部通过" in text


def test_usb_service_probe_requires_service_key_before_network_access():
    from tools.network_connectivity_test import run_tests

    args = SimpleNamespace(
        base_url="http://127.0.0.1:9000",
        allow_insecure_transport=False,
        device_id="deskbot_1",
        api_key="",
    )
    with pytest.raises(ValueError, match="service API Key is required"):
        asyncio.run(run_tests(args))


def test_usb_service_probe_accepts_live_usb_device(monkeypatch):
    from tools import network_connectivity_test as usb_test

    def _http_json(url, _timeout, **_kwargs):
        if url.endswith("/health"):
            return 200, {"ok": True}
        assert url.endswith("/api/devices")
        return 200, {
            "devices": [
                {
                    "device_id": "deskbot_1",
                    "online": True,
                    "transport": "usb_cdc",
                    "channels": {"usb_cdc": 1},
                    "session_generation": 4,
                    "interaction_state": "IDLE",
                }
            ]
        }

    monkeypatch.setattr(usb_test, "_http_json", _http_json)
    args = SimpleNamespace(
        base_url="http://127.0.0.1:9000",
        allow_insecure_transport=False,
        device_id="deskbot_1",
        api_key="service-key",
        timeout=1.0,
        control_rounds=0,
    )
    report = asyncio.run(usb_test.run_tests(args))
    assert report.failures == []
    assert any("live USB CDC session" in check for check in report.checks)


def test_usb_service_probe_rejects_remote_plaintext_origin():
    from tools.network_connectivity_test import validate_http_base

    with pytest.raises(ValueError, match="plaintext http://"):
        validate_http_base("http://deskbot.example:9000")
    assert (
        validate_http_base("https://deskbot.example:9000/")
        == "https://deskbot.example:9000"
    )


def test_usb_service_probe_identifies_only_live_usb_sessions():
    from tools.network_connectivity_test import _is_live_usb_session

    assert _is_live_usb_session(
        {
            "online": True,
            "transport": "usb_cdc",
            "channels": {"usb_cdc": 1},
        }
    )
    assert not _is_live_usb_session(
        {
            "online": True,
            "transport": "websocket",
            "channels": {"asr_chat": 1},
        }
    )
    assert not _is_live_usb_session(
        {
            "online": False,
            "transport": "usb_cdc",
            "channels": {"usb_cdc": 1},
        }
    )


def test_device_websocket_client_tools_are_removed():
    tools_dir = Path(__file__).resolve().parents[1] / "tools"
    for filename in (
        "camera_test_client.py",
        "live_mic_client.py",
        "test_client.py",
        "ws_auth.py",
    ):
        assert not (tools_dir / filename).exists()

    diagnostic_source = (tools_dir / "network_connectivity_test.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import websockets",
        "/asr_chat",
        "/camera_uplink",
        "device_token",
    ):
        assert forbidden not in diagnostic_source
