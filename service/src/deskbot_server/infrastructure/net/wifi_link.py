"""WiFi TCP transport: the robot dials in and speaks DBOT frames.

The device is always the TCP client (it learned this PC's address during USB
provisioning), so no discovery protocol runs on the PC.  Each accepted socket
is wrapped in :class:`SocketSerial` — a blocking ``SerialLike`` — and adopted
by the existing :class:`SerialDeviceManager`, which runs the same hello /
heartbeat / PB machinery as a COM port.  Sessions over this transport must
present the per-device ``link_token`` minted at provisioning time; USB keeps
its physical-connection trust and needs none.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
from typing import Any, Callable

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.device_data import local_data_dir

logger = logging.getLogger("deskbot-server")

DEFAULT_LINK_PORT = 9020
TOKENS_FILENAME = "wifi_link_tokens.json"
_ACCEPT_BACKLOG = 4
# recv timeout doubles as the reader thread's stop-check interval.
_SOCKET_READ_TIMEOUT = 0.05


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def wifi_link_enabled() -> bool:
    return _env_bool("DESKBOT_WIFI_LINK_ENABLED", True)


def wifi_link_port() -> int:
    try:
        port = int(os.environ.get("DESKBOT_WIFI_LINK_PORT") or DEFAULT_LINK_PORT)
    except ValueError:
        return DEFAULT_LINK_PORT
    return port if 1 <= port <= 65535 else DEFAULT_LINK_PORT


def _tokens_path():
    return local_data_dir() / TOKENS_FILENAME


def load_link_tokens() -> dict[str, str]:
    try:
        raw = json.loads(_tokens_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def ensure_link_token(device_id: str) -> str:
    """Return the device's token, minting and persisting one if absent."""
    key = str(device_id or "").strip()
    if not key:
        raise ValueError("device_id required")
    path = _tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path)):
        tokens = load_link_tokens()
        token = tokens.get(key)
        if not token:
            token = secrets.token_hex(16)
            tokens[key] = token
            atomic_write_text(
                path,
                json.dumps(tokens, ensure_ascii=False, indent=2) + "\n",
            )
    return token


def validate_link_token(device_id: str, supplied: str) -> bool:
    expected = load_link_tokens().get(str(device_id or "").strip())
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, str(supplied))


def _wlan_adapter_ip() -> str:
    """IPv4 of the local wireless adapter, or "" when absent (Windows)."""
    import subprocess

    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction "
                "SilentlyContinue | Where-Object { $_.InterfaceAlias -match "
                "'WLAN|Wi-?Fi|无线' -and $_.IPAddress -notlike '169.254*' } "
                "| Select-Object -First 1 -ExpandProperty IPAddress)",
            ],
            capture_output=True,
            timeout=10.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    candidate = completed.stdout.decode("utf-8", errors="replace").strip()
    return candidate if candidate.count(".") == 3 else ""


def pc_lan_ip() -> str:
    """The local address a LAN peer should dial — no packets are sent.

    Multi-NIC hosts (or the Windows mobile-hotspot case, where the robot sits
    on 192.168.137.x) can override with DESKBOT_WIFI_LINK_ADVERTISE_HOST.
    """
    override = (os.environ.get("DESKBOT_WIFI_LINK_ADVERTISE_HOST") or "").strip()
    if override:
        return override
    # 机器人挂在 WiFi 上，PC 的 WLAN 口几乎总与它同网段；默认路由探测在
    # 开着 VPN 的机器上会选到 VPN 接口，所以 WLAN 网卡优先。
    wlan = _wlan_adapter_ip()
    if wlan:
        return wlan
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


class SocketSerial:
    """Adapt one connected TCP socket to the blocking ``SerialLike`` shape.

    pyserial's ``read`` returns ``b""`` on timeout and the stream stays open;
    a socket's ``recv`` returns ``b""`` only on EOF.  The adapter maps timeout
    to ``b""`` and EOF to ``OSError`` so the session reader thread fails the
    session instead of spinning on a dead peer.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._sock.settimeout(_SOCKET_READ_TIMEOUT)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.timeout: float | None = _SOCKET_READ_TIMEOUT
        self.write_timeout: float | None = None
        self._closed = False

    def read(self, size: int = 1) -> bytes:
        try:
            data = self._sock.recv(max(1, int(size)))
        except socket.timeout:
            return b""
        except OSError:
            if self._closed:
                return b""
            raise
        if data == b"":
            raise OSError("wifi link peer closed the connection")
        return data

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        """TCP 是有序字节流，重连即新流：没有"上一次会话残留"要丢。"""
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class WifiLinkListener:
    """Accept robot TCP connections and hand them to the serial manager.

    The accept loop runs on a daemon thread because ``SerialLike`` is a
    blocking interface consumed from the session's own reader thread; the
    asyncio side only ever sees the adopted :class:`DeviceSession`.
    """

    def __init__(
        self,
        attach: Callable[..., Any],
        loop,
        *,
        host: str = "0.0.0.0",
        port: int | None = None,
    ) -> None:
        self._attach = attach
        self._loop = loop
        self._host = host
        self._port = port if port is not None else wifi_link_port()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> bool:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self._host, self._port))
            server.listen(_ACCEPT_BACKLOG)
        except OSError as exc:
            server.close()
            logger.warning(
                "[wifi_link] cannot listen on %s:%d err=%s; WiFi transport off",
                self._host,
                self._port,
                exc,
            )
            return False
        server.settimeout(0.5)
        self._server = server
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="deskbot-wifi-link-accept",
            daemon=True,
        )
        self._thread.start()
        logger.info("[wifi_link] listening on %s:%d", self._host, self._port)
        return True

    def stop(self) -> None:
        self._stopping.set()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        import asyncio

        while not self._stopping.is_set():
            server = self._server
            if server is None:
                return
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                continue
            label = f"tcp://{addr[0]}:{addr[1]}"
            adapter = SocketSerial(sock)
            future = asyncio.run_coroutine_threadsafe(
                self._attach(
                    label,
                    adapter,
                    transport="wifi_tcp",
                    link_token_validator=validate_link_token,
                ),
                self._loop,
            )

            def _log_attach(done, bound_label=label, bound_adapter=adapter):
                try:
                    error = done.exception()
                except BaseException as exc:  # cancelled during shutdown
                    error = exc
                if error is not None:
                    logger.warning(
                        "[wifi_link] attach failed peer=%s err=%s",
                        bound_label,
                        error,
                    )
                    bound_adapter.close()

            future.add_done_callback(_log_attach)
