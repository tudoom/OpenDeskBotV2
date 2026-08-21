from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from deskbot_server.infrastructure.serial.manager import (
    SerialConnector,
    SerialDeviceManager,
    SerialManagerConfig,
    SerialPortCandidate,
)
from tests.test_usb_serial_session import FakeSerial, _hello_ack_frame


def test_default_read_timeout_keeps_small_transport_acks_low_latency():
    assert SerialManagerConfig().read_timeout == 0.02


def test_connector_filters_unrelated_ports_without_opening_them():
    infos = [
        SimpleNamespace(
            device="COM4",
            vid=0x303A,
            pid=0x1001,
            description="USB JTAG/serial debug unit",
            manufacturer="Espressif",
            product="ESP32-S3",
            hwid="USB VID:PID=303A:1001",
        ),
        SimpleNamespace(
            device="COM9",
            vid=0x1234,
            pid=0x0001,
            description="Unrelated modem",
            manufacturer="Other",
            product="Modem",
            hwid="USB VID:PID=1234:0001",
        ),
    ]
    connector = SerialConnector(
        SerialManagerConfig(),
        port_lister=lambda: infos,
    )

    candidates = connector.discover()

    assert [candidate.device for candidate in candidates] == ["COM4"]


def test_explicit_ports_do_not_require_enumeration():
    connector = SerialConnector(
        SerialManagerConfig(explicit_ports=("COM4", "COM8")),
        port_lister=lambda: (_ for _ in ()).throw(
            AssertionError("must not enumerate")
        ),
    )
    assert [
        candidate.device for candidate in connector.discover()
    ] == ["COM4", "COM8"]


def test_manager_reconnect_cleans_indexes_and_increments_generation():
    class QueueConnector:
        def __init__(self) -> None:
            self.serials = [FakeSerial(), FakeSerial()]

        def discover(self):
            return [SerialPortCandidate(device="COM4")]

        def open(self, _candidate):
            return self.serials.pop(0)

    async def _run() -> None:
        connector = QueueConnector()
        manager = SerialDeviceManager(
            SerialManagerConfig(
                explicit_ports=("COM4",),
                reconnect_delay=0.0,
                hello_timeout=1.0,
            ),
            connector=connector,  # type: ignore[arg-type]
        )

        await manager._scan_once()
        first = manager.sessions[0]
        first_serial = first._serial
        first_serial.inject(
            _hello_ack_frame(
                ack_client_nonce=first.client_nonce,
                epoch=41,
            )
        )
        await first.wait_ready(timeout=1.0)
        assert manager.session_for_device("deskbot_123456abcdef") is first

        await first.close(reason="test reconnect")
        assert manager.sessions == ()
        assert manager.session_for_device("deskbot_123456abcdef") is None

        await manager._scan_once()
        second = manager.sessions[0]
        assert second.generation == first.generation + 1
        second_serial = second._serial
        second_serial.inject(
            _hello_ack_frame(
                ack_client_nonce=second.client_nonce,
                epoch=42,
            )
        )
        await second.wait_ready(timeout=1.0)
        assert manager.session_for_device("deskbot_123456abcdef") is second
        await manager.stop()

    asyncio.run(_run())


def test_manager_warns_once_when_explicit_port_is_busy(caplog):
    class BusyConnector:
        def open(self, _candidate):
            raise PermissionError("access denied")

    async def _run() -> None:
        manager = SerialDeviceManager(
            SerialManagerConfig(reconnect_delay=0.0),
            connector=BusyConnector(),  # type: ignore[arg-type]
        )
        candidate = SerialPortCandidate(device="COM4")
        await manager._open(candidate)
        await manager._open(candidate)

    caplog.set_level(logging.DEBUG, logger="deskbot-server")
    asyncio.run(_run())
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "port=COM4" in record.message
    ]
    assert len(warnings) == 1
    assert "without terminating other processes" in warnings[0].message
    assert any(
        record.levelno == logging.DEBUG
        and "open retry failed port=COM4" in record.message
        for record in caplog.records
    )
