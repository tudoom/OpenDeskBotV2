"""Discovery, reconnect, and connection generations for USB CDC sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from deskbot_server.infrastructure.serial.protocol import Frame
from deskbot_server.infrastructure.serial.session import (
    ClosedCallback,
    DeviceSession,
    FrameCallback,
    HelloInfo,
    ReadyCallback,
    SerialLike,
)
from deskbot_server.telemetry import emit

logger = logging.getLogger("deskbot-server")

_ESPRESSIF_USB_VID = 0x303A


def pyserial_open_kwargs(platform: str | None = None) -> dict[str, Any]:
    """按平台决定 pyserial 打开串口时的附加参数。

    macOS 内核在 open() 时会把 DTR/RTS 都拉高，pyserial 随后把 DTR 拉低（RTS 仍高）——ESP32-S3 USB-JTAG
    把 DTR=0 且 RTS=1 当作复位序列，于是 Core 每次重启都把机器人重启一遍（hello 里 reset_reason=usb），
    机器人开机竞速里 WiFi 先就绪就绑到 WiFi。dsrdtr=True 让 pyserial 不碰 DTR，实测不再复位（2026-09-11）。
    Windows 沿用原来的"先拉低再打开"。"""
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return {"dsrdtr": True}
    return {}


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
            open_kwargs = pyserial_open_kwargs()
            serial_port = serial.Serial(
                port=None,
                baudrate=self.config.baudrate,
                timeout=self.config.read_timeout,
                write_timeout=self.config.write_timeout,
                **open_kwargs,
            )
            serial_port.port = candidate.device
            if not open_kwargs.get("dsrdtr"):
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
        self._serial_ports: dict[str, SerialLike] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._scan_wakeup = asyncio.Event()
        # 固件烧录等独占操作期间挂起的 COM 口：扫描器不碰、缓存句柄已关。
        self._maintenance_hold: set[str] = set()

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
        for serial_port in self._serial_ports.values():
            try:
                serial_port.close()
            except Exception:
                logger.debug("stop: swallowed exception", exc_info=True)
        self._serial_ports.clear()

    def request_scan(self) -> None:
        self._scan_wakeup.set()

    async def begin_port_maintenance(self, port: str) -> None:
        """Release ``port`` entirely so an external tool (esptool) can own it.

        Closes the live session and the cached pyserial handle, and keeps the
        scan loop away until :meth:`end_port_maintenance`.
        """
        name = str(port or "").strip()
        if not name:
            raise ValueError("port is required")
        self._maintenance_hold.add(name)
        session = self._sessions_by_port.get(name)
        if session is not None and not session.is_closed:
            await session.close(reason="firmware update")
        # 会话关闭路径可能把句柄留在会话里或塞回缓存，两处都要真正 close，
        # 否则 esptool 打不开口。
        cached = self._serial_ports.pop(name, None)
        if cached is not None:
            try:
                cached.close()
            except Exception:
                logger.debug("begin_port_maintenance: swallowed exception", exc_info=True)
        if session is not None:
            try:
                session._serial.close()
            except Exception:
                logger.debug("begin_port_maintenance: swallowed exception", exc_info=True)

    def end_port_maintenance(self, port: str) -> None:
        name = str(port or "").strip()
        self._maintenance_hold.discard(name)
        self._retry_after.pop(name, None)
        self._probe_failures.pop(name, None)
        self.request_scan()

    async def attach_stream_session(
        self,
        label: str,
        stream: SerialLike,
        *,
        transport: str = "wifi_tcp",
        link_token_validator: Callable[[str, str], bool] | None = None,
    ) -> DeviceSession:
        """Adopt an externally accepted byte stream (e.g. a WiFi TCP socket).

        The stream runs the same DBOT hello/heartbeat/PB machinery as a COM
        port.  Reconnecting after failure is the remote peer's job, so these
        sessions never enter the scan/retry planner.
        """
        name = str(label or "").strip() or f"stream-{self._generation + 1}"
        old = self._sessions_by_port.get(name)
        if old is not None and not old.is_closed:
            await old.close(reason="stream label reused")
        self._generation += 1
        session = DeviceSession(
            name,
            stream,
            generation=self._generation,
            on_ready=self._session_ready,
            on_frame=self._session_frame,
            on_closed=self._session_closed,
            hello_timeout=self.config.hello_timeout,
            transport=transport,
            link_token_validator=link_token_validator,
            # 半双工空口下高占空比上行会挤压设备的接收窗口，AP 下行帧需要
            # 多轮重传；USB 的 2s 确认超时在这里过于苛刻。
            # 2026-09-14：设备 lwIP 接收窗口只有 5.7 KB，一旦塞满，PC 侧 TCP
            # 零窗口探测按秒退避（实测帧延迟 5～16 s）；6 s 就杀会话会让重连后
            # 重发的表情帧再次塞满窗口，循环误杀。固件 0.0.59 起整段收包根治
            # 慢泵，这里放宽到 12 s 兜空口丢包；死链路仍由 6.5 s 心跳兜底。
            frame_ack_timeout=12.0,
        )
        self._sessions_by_port[name] = session
        try:
            await session.start()
        except Exception:
            self._sessions_by_port.pop(name, None)
            try:
                stream.close()
            except Exception:
                logger.debug("attach_stream_session: swallowed exception", exc_info=True)
            raise
        logger.info(
            "[wifi_link] probing peer=%s generation=%d",
            name,
            session.generation,
        )
        return session

    async def _run(self) -> None:
        try:
            # stop() 先 set 唤醒事件再 cancel 本任务；在 Python 3.11 的
            # asyncio.wait_for 中，内层 future 完成与外层取消落在同一个
            # 事件循环节拍时取消会被吞掉（bpo-42130 一族）。循环条件必须
            # 检查 _stopping，否则被吞的取消让扫描循环永远运行，stop()
            # 的 gather 永远不返回，整条 shutdown 链卡死。
            while not self._stopping:
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
                # WiFi TCP 会话不是 COM 口，永远不会出现在串口枚举里。
                if session.transport == "usb_cdc"
                and port not in candidate_by_port
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
            if port in self._maintenance_hold:
                continue
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
        # Reuse a cached serial port from a previous session to avoid
        # resetting the ESP32-S3 firmware via macOS USB CDC close/reopen.
        serial_port = self._serial_ports.pop(port, None)
        if serial_port is not None:
            # 缓存句柄可能已随设备拔插失效：失效则关掉走全新 open，
            # 避免拿着死句柄反复起会话。
            stale = False
            try:
                if not getattr(serial_port, "is_open", True):
                    stale = True
                else:
                    serial_port.reset_input_buffer()
            except Exception:
                stale = True
            if stale:
                try:
                    serial_port.close()
                except Exception:
                    logger.debug("_open: swallowed exception", exc_info=True)
                serial_port = None
        if serial_port is None:
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
            # 2026-09-07：拍照回退流起步时设备处理了帧（PB ack 都回了），传输层
            # 确认却晚于 2s——相机帧占着 TX 互斥量或 TinyUSB 短暂停摆。固件
            # 0.0.39 起会在 3s 内补发确认；这里给足窗口，别把活着的会话杀掉。
            frame_ack_timeout=6.0,
        )
        # Never let the session close the underlying serial port on
        # hello-timeout failure.  On macOS, closing a USB CDC port
        # resets the ESP32-S3 firmware, which restarts the stale-audio
        # flood and makes the next hello handshake fail too.  The
        # manager owns the port lifetime and will close it on stop.
        session._reuse_port = True
        self._sessions_by_port[port] = session
        try:
            await session.start()
        except Exception:
            self._sessions_by_port.pop(port, None)
            try:
                serial_port.close()
            except Exception:
                logger.debug("_open: swallowed exception", exc_info=True)
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
        if (
            old is not None
            and old is not session
            and not old.is_closed
            and old.transport == "usb_cdc"
            and session.transport != "usb_cdc"
        ):
            # USB 在线时 WiFi 只当后备：拒绝新会话，设备端按退避重试，
            # 拔掉 USB 后的下一次重连即被接受。
            self._sessions_by_port.pop(session.port, None)
            await session.close(reason="usb link has priority")
            return
        self._sessions_by_device[hello.device_id] = session
        if old is not None and old is not session:
            await old.close(reason="superseded device session")
        # 新会话 = 设备重启/重连/刷了新固件：相机健康度从头算，别把上一次
        # 开机的初始化失败带进来。
        try:
            from deskbot_server.device_camera_health import reset_device

            reset_device(hello.device_id)
        except Exception:  # noqa: BLE001
            logger.debug("_session_ready: swallowed exception", exc_info=True)
        try:
            emit(
                "session.ready",
                device_id=hello.device_id,
                transport=session.transport,
                port=session.port,
                generation=session.generation,
            )
        except Exception:  # noqa: BLE001
            logger.debug("_session_ready: telemetry failed", exc_info=True)
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
        try:
            emit(
                "session.closed",
                device_id=session.device_id or "",
                transport=session.transport,
                port=session.port,
                generation=session.generation,
                error=(f"{type(error).__name__}: {error}" if error else ""),
            )
        except Exception:  # noqa: BLE001
            logger.debug("_session_closed: telemetry failed", exc_info=True)
        if self._sessions_by_port.get(session.port) is session:
            self._sessions_by_port.pop(session.port, None)
        device_id = session.device_id
        if (
            device_id
            and self._sessions_by_device.get(device_id) is session
        ):
            self._sessions_by_device.pop(device_id, None)
        if session.transport != "usb_cdc":
            # 设备端负责 TCP 重连；PC 不为网络会话排重试计划。
            await _invoke(self._on_closed, session, error)
            return
        if session.port in self._maintenance_hold:
            # 烧录挂起中：句柄由维护方关闭，不回缓存、不排重试。
            await _invoke(self._on_closed, session, error)
            return
        if not self._stopping:
            failed_probe = session.hello_info is None
            # Keep the serial port open across hello retries so the
            # ESP32-S3 firmware is not reset by a macOS USB CDC
            # close/reopen cycle.  _reuse_port is already set on the
            # session for re-opens; also cache the port for the NEXT
            # retry when this was a fresh open.
            #
            # 必须无条件回收（不只 failed_probe）：hello 成功后死亡的会话
            # （如帧 ACK 超时且串口未消失）若不回收句柄，_reuse_port=True
            # 又使 close() 不关口，句柄泄漏在死会话里，本进程后续 open
            # 永远 busy——设备再也连不上。坏句柄由 _open 的复用检查兜底。
            if session.port not in self._serial_ports:
                try:
                    self._serial_ports[session.port] = session._serial
                except Exception:
                    logger.debug("_session_closed: swallowed exception", exc_info=True)
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
        # _stopping 分支不得再调 session.close()：本回调只会从 close() 的
        # 持锁段内被调用（session.py _closed_callback_called 保证仅此一处），
        # 重入 close() 等待同一把非重入 _close_lock 必然死锁，进而卡死
        # manager.stop() 的会话收尾 gather 与整条 shutdown 链。停机时的
        # 串口句柄由 close(close_port=True) 与 stop() 末尾的缓存清理负责。
        await _invoke(self._on_closed, session, error)
