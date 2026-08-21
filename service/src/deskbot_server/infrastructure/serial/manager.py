"""Discovery, reconnect, and connection generations for USB CDC sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from deskbot_server.infrastructure.serial.protocol import Frame
from deskbot_server.infrastructure.serial.session import (
    ClosedCallback,
    DeviceSession,
    FrameCallback,
    HelloInfo,
    ReadyCallback,
)

logger = logging.getLogger("deskbot-server")

_ESPRESSIF_USB_VID = 0x303A


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("[usb_cdc] invalid %s=%r; using %.2f", name, raw, default)
        return default


@dataclass(frozen=True, slots=True)
class SerialManagerConfig:
    enabled: bool = True
    baudrate: int = 115_200
    # pyserial read(size) waits for either ``size`` bytes or this timeout.
    # DBOT transport ACKs are small, so 200 ms here adds 200 ms to every
    # stop-and-wait PB fragment even though the device answered immediately.
    read_timeout: float = 0.02
    write_timeout: float = 2.0
    scan_interval: float = 1.0
    reconnect_delay: float = 1.0
    hello_timeout: float = 6.0
    reconnect_max_delay: float = 30.0
    explicit_ports: tuple[str, ...] = ()
    probe_all_ports: bool = False

    @classmethod
    def from_env(cls) -> "SerialManagerConfig":
        ports_raw = os.environ.get(
            "DESKBOT_SERIAL_PORTS",
            os.environ.get("DESKBOT_USB_SERIAL_PORTS", ""),
        )
        ports = tuple(
            dict.fromkeys(
                part.strip()
                for part in ports_raw.split(",")
                if part.strip()
            )
        )
        try:
            baudrate = int(
                os.environ.get("DESKBOT_USB_SERIAL_BAUD", "115200")
            )
        except ValueError:
            baudrate = 115_200
        return cls(
            enabled=_env_bool("DESKBOT_USB_SERIAL_ENABLED", True),
            baudrate=max(1, baudrate),
            read_timeout=_env_float(
                "DESKBOT_USB_SERIAL_READ_TIMEOUT",
                0.02,
                minimum=0.005,
            ),
            write_timeout=_env_float(
                "DESKBOT_USB_SERIAL_WRITE_TIMEOUT",
                2.0,
                minimum=0.1,
            ),
            scan_interval=_env_float(
                "DESKBOT_USB_SERIAL_SCAN_INTERVAL",
                1.0,
                minimum=0.1,
            ),
            reconnect_delay=_env_float(
                "DESKBOT_USB_SERIAL_RECONNECT_DELAY",
                1.0,
                minimum=0.1,
            ),
            reconnect_max_delay=_env_float(
                "DESKBOT_USB_SERIAL_RECONNECT_MAX_DELAY",
                30.0,
                minimum=1.0,
            ),

            hello_timeout=_env_float(
                "DESKBOT_USB_SERIAL_HELLO_TIMEOUT",
                6.0,
                minimum=1.0,
            ),
            explicit_ports=ports,
            probe_all_ports=_env_bool(
                "DESKBOT_USB_SERIAL_PROBE_ALL",
                False,
            ),
        )


@dataclass(frozen=True, slots=True)
class SerialPortCandidate:
    device: str
    vid: int | None = None
    pid: int | None = None
    description: str = ""
    hwid: str = ""


class SerialConnector:
    """Discover likely Deskbot ports and open them without touching asyncio."""

    def __init__(
        self,
        config: SerialManagerConfig,
        *,
        port_lister: Callable[[], Iterable[Any]] | None = None,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._port_lister = port_lister
        self._serial_factory = serial_factory

    @staticmethod
    def _default_port_lister() -> Iterable[Any]:
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise RuntimeError("pyserial is required for USB CDC") from exc
        return list_ports.comports()

    @staticmethod
    def _looks_like_deskbot(info: Any) -> bool:
        vid = getattr(info, "vid", None)
        if vid == _ESPRESSIF_USB_VID:
            return True
        text = " ".join(
            str(getattr(info, field, "") or "")
            for field in ("description", "manufacturer", "product", "hwid")
        ).lower()
        return "esp32" in text or (
            "espressif" in text and ("serial" in text or "jtag" in text)
        )

    def discover(self) -> list[SerialPortCandidate]:
        if self.config.explicit_ports:
            return [
                SerialPortCandidate(device=port)
                for port in self.config.explicit_ports
            ]
        lister = self._port_lister or self._default_port_lister
        candidates: list[SerialPortCandidate] = []
        seen: set[str] = set()
        for info in lister():
            device = str(getattr(info, "device", "") or "").strip()
            if not device or device in seen:
                continue
            if (
                not self.config.probe_all_ports
                and not self._looks_like_deskbot(info)
            ):
                continue
            seen.add(device)
            candidates.append(
                SerialPortCandidate(
                    device=device,
                    vid=getattr(info, "vid", None),
                    pid=getattr(info, "pid", None),
                    description=str(
                        getattr(info, "description", "") or ""
                    ),
                    hwid=str(getattr(info, "hwid", "") or ""),
                )
            )
        candidates.sort(key=lambda candidate: candidate.device)
        return candidates

    def open(self, candidate: SerialPortCandidate):
        factory = self._serial_factory
        if factory is None:
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError("pyserial is required for USB CDC") from exc
            # Construct closed so DTR/RTS are deasserted before Windows opens
            # the ESP32-S3 USB Serial/JTAG endpoint. Opening it with pyserial's
            # default asserted line state can reset the chip or leave it in the
            # ROM loader, turning reconnect into an endless hello-timeout loop.
            serial_port = serial.Serial(
                port=None,
                baudrate=self.config.baudrate,
                timeout=self.config.read_timeout,
                write_timeout=self.config.write_timeout,
            )
            serial_port.port = candidate.device
            serial_port.dtr = False
            serial_port.rts = False
            try:
                serial_port.open()
            except BaseException:
                serial_port.close()
                raise
            return serial_port
        return factory(
            port=candidate.device,
            baudrate=self.config.baudrate,
            timeout=self.config.read_timeout,
            write_timeout=self.config.write_timeout,
        )


async def _invoke(callback, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class SerialDeviceManager:
    """Own current sessions and reconnect them with monotonically newer IDs."""

    def __init__(
        self,
        config: SerialManagerConfig | None = None,
        *,
        connector: SerialConnector | None = None,
        on_ready: ReadyCallback | None = None,
        on_frame: FrameCallback | None = None,
        on_closed: ClosedCallback | None = None,
    ) -> None:
        self.config = config or SerialManagerConfig.from_env()
        self.connector = connector or SerialConnector(self.config)
        self._on_ready = on_ready
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._sessions_by_port: dict[str, DeviceSession] = {}
        self._sessions_by_device: dict[str, DeviceSession] = {}
        self._retry_after: dict[str, float] = {}
        self._open_failures_warned: set[str] = set()
        self._generation = 0
        self._probe_failures: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._scan_wakeup = asyncio.Event()

    @property
    def sessions(self) -> tuple[DeviceSession, ...]:
        return tuple(self._sessions_by_port.values())

    def session_for_device(self, device_id: str) -> DeviceSession | None:
        session = self._sessions_by_device.get(str(device_id or "").strip())
        return session if session is not None and session.is_ready else None

    def set_callbacks(
        self,
        *,
        on_ready: ReadyCallback | None,
        on_frame: FrameCallback | None,
        on_closed: ClosedCallback | None,
    ) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("cannot replace serial callbacks while running")
        self._on_ready = on_ready
        self._on_frame = on_frame
        self._on_closed = on_closed

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("[usb_cdc] serial manager disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(),
            name="deskbot-usb-serial-manager",
        )
        logger.info(
            "[usb_cdc] serial manager started ports=%s probe_all=%s",
            self.config.explicit_ports or "auto-espressif",
            self.config.probe_all_ports,
        )

    async def stop(self) -> None:
        self._stopping = True
        self._scan_wakeup.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        sessions = list(self._sessions_by_port.values())
        if sessions:
            await asyncio.gather(
                *(
                    session.close(reason="serial manager stopping")
                    for session in sessions
                ),
                return_exceptions=True,
            )
        self._sessions_by_port.clear()
        self._sessions_by_device.clear()

    def request_scan(self) -> None:
        self._scan_wakeup.set()

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[usb_cdc] serial discovery failed")
                self._scan_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._scan_wakeup.wait(),
                        timeout=self.config.scan_interval,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _scan_once(self) -> None:
        candidates = await asyncio.to_thread(self.connector.discover)
        candidate_by_port = {
            candidate.device: candidate for candidate in candidates
        }
        if not self.config.explicit_ports:
            vanished = [
                session
                for port, session in self._sessions_by_port.items()
                if port not in candidate_by_port
            ]
            if vanished:
                await asyncio.gather(
                    *(
                        session.close(reason="serial port disappeared")
                        for session in vanished
                    ),
                    return_exceptions=True,
                )

        loop = asyncio.get_running_loop()
        now = loop.time()
        for port, candidate in candidate_by_port.items():
            current = self._sessions_by_port.get(port)
            if current is not None and not current.is_closed:
                continue
            if now < self._retry_after.get(port, 0.0):
                continue
            await self._open(candidate)

    def _schedule_retry(self, port: str, *, failed_probe: bool) -> float:
        if failed_probe:
            failures = self._probe_failures.get(port, 0) + 1
            self._probe_failures[port] = failures
        else:
            failures = 0
            self._probe_failures.pop(port, None)
        exponent = min(max(failures - 1, 0), 8)
        delay = min(
            self.config.reconnect_max_delay,
            self.config.reconnect_delay * (2**exponent),
        )
        self._retry_after[port] = asyncio.get_running_loop().time() + delay
        return delay

    async def _open(self, candidate: SerialPortCandidate) -> None:
        port = candidate.device
        try:
            serial_port = await asyncio.to_thread(
                self.connector.open,
                candidate,
            )
        except Exception as exc:
            delay = self._schedule_retry(port, failed_probe=True)
            if port not in self._open_failures_warned:
                self._open_failures_warned.add(port)
                logger.warning(
                    "[usb_cdc] cannot open port=%s; it may be busy or "
                    "unavailable. Deskbot will retry without terminating "
                    "other processes. retry_in=%.1fs err=%s",
                    port,
                    delay,
                    exc,
                )
            else:
                logger.debug(
                    "[usb_cdc] open retry failed port=%s retry_in=%.1fs err=%s",
                    port,
                    delay,
                    exc,
                )
            return

        self._open_failures_warned.discard(port)
        self._generation += 1
        session = DeviceSession(
            port,
            serial_port,
            generation=self._generation,
            on_ready=self._session_ready,
            on_frame=self._session_frame,
            on_closed=self._session_closed,
            hello_timeout=self.config.hello_timeout,
        )
        self._sessions_by_port[port] = session
        try:
            await session.start()
        except Exception:
            self._sessions_by_port.pop(port, None)
            try:
                serial_port.close()
            except Exception:
                pass
            raise
        logger.info(
            "[usb_cdc] probing port=%s generation=%d",
            port,
            session.generation,
        )

    async def _session_ready(
        self,
        session: DeviceSession,
        hello: HelloInfo,
    ) -> None:
        if self._sessions_by_port.get(session.port) is not session:
            await session.close(reason="stale manager generation")
            return
        self._probe_failures.pop(session.port, None)
        self._retry_after.pop(session.port, None)
        old = self._sessions_by_device.get(hello.device_id)
        self._sessions_by_device[hello.device_id] = session
        if old is not None and old is not session:
            await old.close(reason="superseded USB device session")
        await _invoke(self._on_ready, session, hello)

    async def _session_frame(
        self,
        session: DeviceSession,
        frame: Frame,
    ) -> None:
        if self._sessions_by_port.get(session.port) is not session:
            return
        await _invoke(self._on_frame, session, frame)

    async def _session_closed(
        self,
        session: DeviceSession,
        error: BaseException | None,
    ) -> None:
        if self._sessions_by_port.get(session.port) is session:
            self._sessions_by_port.pop(session.port, None)
        device_id = session.device_id
        if (
            device_id
            and self._sessions_by_device.get(device_id) is session
        ):
            self._sessions_by_device.pop(device_id, None)
        if not self._stopping:
            failed_probe = session.hello_info is None
            delay = self._schedule_retry(
                session.port,
                failed_probe=failed_probe,
            )
            if failed_probe:
                logger.warning(
                    "[usb_cdc] port degraded port=%s consecutive_probe_failures=%d "
                    "retry_in=%.1fs error=%s",
                    session.port,
                    self._probe_failures.get(session.port, 0),
                    delay,
                    error,
                )
            self.request_scan()
        await _invoke(self._on_closed, session, error)
