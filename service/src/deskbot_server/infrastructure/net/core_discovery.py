"""Let the robot find this Core by name instead of by remembered IP.

Two independent paths, both answered from here:

* **Bonjour / mDNS** — the Core is registered as ``_deskbot-core._tcp`` with
  ``core_id`` in its TXT record.  On macOS the system ``dns-sd`` responder is
  used (no Python zeroconf in the bundled runtime); elsewhere ``zeroconf`` is
  used when importable, otherwise this path is simply off.
* **UDP broadcast** — the robot broadcasts ``deskbot_discover`` to port
  :data:`DISCOVERY_UDP_PORT`; the responder answers ``deskbot_core`` from the
  socket the request arrived on, so the reply's source address is the Core's
  address on the very interface the robot can reach.

The firmware (>= 0.0.49) tries mDNS, then UDP, then the stored host, and only
accepts a Core whose ``core_id`` matches the one it was paired with.  The
service name and the message shapes below are mirrored verbatim in
``hardware/firmware/wifi_link.cpp``; a contract test greps both sides.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
from typing import Any

from deskbot_server.infrastructure.net.core_identity import core_id as _core_id
from deskbot_server.infrastructure.net.core_identity import core_name as _core_name
from deskbot_server.infrastructure.net.wifi_link import pc_lan_ip, wifi_link_port

logger = logging.getLogger("deskbot-server")

SERVICE_TYPE = "_deskbot-core._tcp"
DISCOVERY_UDP_PORT = 9021
DISCOVER_REQUEST_TYPE = "deskbot_discover"
DISCOVER_REPLY_TYPE = "deskbot_core"
PROTOCOL_VERSION = 1
_MAX_DATAGRAM = 512
_SOCKET_POLL_S = 0.5


def discovery_udp_port() -> int:
    try:
        port = int(os.environ.get("DESKBOT_DISCOVERY_UDP_PORT") or DISCOVERY_UDP_PORT)
    except ValueError:
        return DISCOVERY_UDP_PORT
    return port if 1 <= port <= 65535 else DISCOVERY_UDP_PORT


# ── pure protocol helpers (unit-tested without sockets) ──────────────────────
def parse_discover_request(data: bytes) -> dict[str, Any] | None:
    """Return the request fields, or None when the datagram is not ours."""
    if not data or len(data) > _MAX_DATAGRAM:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("type") != DISCOVER_REQUEST_TYPE:
        return None
    return {
        "device_id": str(raw.get("device_id") or "").strip(),
        "core_id": str(raw.get("core_id") or "").strip(),
        "v": int(raw.get("v") or 0) if str(raw.get("v") or "0").isdigit() else 0,
    }


def build_discover_reply(request: dict[str, Any], *, core_id: str, port: int, name: str) -> bytes | None:
    """The answer for one request, or None when this Core must stay silent.

    A robot that already belongs to another Core names it in ``core_id``; we
    do not answer such requests, so two Cores on one LAN never steal each
    other's robots.  A robot with no binding yet (legacy provisioning) gets an
    answer from every Core; the firmware then keeps its stored host unless it
    has exactly one candidate.
    """
    wanted = str(request.get("core_id") or "")
    if wanted and wanted != core_id:
        return None
    reply = {
        "type": DISCOVER_REPLY_TYPE,
        "core_id": core_id,
        "port": int(port),
        "name": name,
        "v": PROTOCOL_VERSION,
    }
    return json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── UDP responder ────────────────────────────────────────────────────────────
class DiscoveryResponder:
    """Answer ``deskbot_discover`` broadcasts on a daemon thread."""

    def __init__(self, *, core_id: str, link_port: int, name: str, udp_port: int | None = None) -> None:
        self._core_id = core_id
        self._link_port = int(link_port)
        self._name = name
        self._udp_port = int(udp_port) if udp_port is not None else discovery_udp_port()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.answered = 0
        self.ignored = 0

    @property
    def port(self) -> int:
        return self._udp_port

    @property
    def running(self) -> bool:
        return self._sock is not None

    def start(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", self._udp_port))
        except OSError as exc:
            sock.close()
            logger.warning("[core_discovery] udp responder cannot bind :%d err=%s", self._udp_port, exc)
            return False
        sock.settimeout(_SOCKET_POLL_S)
        self._sock = sock
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="deskbot-core-discovery", daemon=True)
        self._thread.start()
        logger.info("[core_discovery] udp responder on :%d core_id=%s link_port=%d", self._udp_port, self._core_id, self._link_port)
        return True

    def stop(self) -> None:
        self._stopping.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stopping.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(_MAX_DATAGRAM + 1)
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                continue
            request = parse_discover_request(data)
            if request is None:
                self.ignored += 1
                continue
            reply = build_discover_reply(request, core_id=self._core_id, port=self._link_port, name=self._name)
            if reply is None:
                self.ignored += 1
                logger.info("[core_discovery] ignored device=%s wants core=%s (mine=%s)", request["device_id"], request["core_id"], self._core_id)
                continue
            try:
                sock.sendto(reply, addr)
                self.answered += 1
                logger.info("[core_discovery] answered device=%s from=%s:%d", request["device_id"], addr[0], addr[1])
            except OSError as exc:
                logger.warning("[core_discovery] reply to %s failed err=%s", addr, exc)


# ── Bonjour / mDNS registration ──────────────────────────────────────────────
class BonjourAdvertiser:
    """Register ``_deskbot-core._tcp`` with ``core_id`` in TXT.

    macOS: ``/usr/bin/dns-sd -R`` kept running as a child process (the system
    mDNSResponder answers queries).  Other platforms: ``zeroconf`` when
    available.  Absence is not an error — the UDP path still works.
    """

    DNS_SD = "/usr/bin/dns-sd"

    def __init__(self, *, core_id: str, link_port: int, name: str) -> None:
        self._core_id = core_id
        self._link_port = int(link_port)
        self._name = name
        self._proc: subprocess.Popen | None = None
        self._zeroconf: Any = None
        self._zc_info: Any = None
        self.backend = ""

    @property
    def instance_name(self) -> str:
        return f"Deskbot Core {self._name}"[:63]

    def txt_record(self) -> dict[str, str]:
        return {"core_id": self._core_id, "v": str(PROTOCOL_VERSION), "name": self._name}

    def dns_sd_command(self) -> list[str]:
        txt = [f"{k}={v}" for k, v in self.txt_record().items()]
        return [self.DNS_SD, "-R", self.instance_name, SERVICE_TYPE, "local", str(self._link_port), *txt]

    @property
    def running(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        return self._zeroconf is not None

    def start(self) -> bool:
        if sys.platform == "darwin" and os.path.exists(self.DNS_SD):
            try:
                self._proc = subprocess.Popen(
                    self.dns_sd_command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning("[core_discovery] dns-sd register failed err=%s", exc)
                self._proc = None
                return False
            self.backend = "dns-sd"
            logger.info("[core_discovery] bonjour registered %s as %r via dns-sd", SERVICE_TYPE, self.instance_name)
            return True
        try:
            from zeroconf import ServiceInfo, Zeroconf  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - optional dependency
            logger.info("[core_discovery] no mDNS backend on this platform; UDP discovery only")
            return False
        try:
            host = pc_lan_ip()
            addresses = [socket.inet_aton(host)] if host else []
            info = ServiceInfo(
                f"{SERVICE_TYPE}.local.",
                f"{self.instance_name}.{SERVICE_TYPE}.local.",
                addresses=addresses,
                port=self._link_port,
                properties=self.txt_record(),
            )
            zc = Zeroconf()
            zc.register_service(info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[core_discovery] zeroconf register failed err=%s", exc)
            return False
        self._zeroconf, self._zc_info = zc, info
        self.backend = "zeroconf"
        logger.info("[core_discovery] bonjour registered %s as %r via zeroconf", SERVICE_TYPE, self.instance_name)
        return True

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        zc, info = self._zeroconf, self._zc_info
        self._zeroconf = self._zc_info = None
        if zc is not None:
            try:
                if info is not None:
                    zc.unregister_service(info)
                zc.close()
            except Exception:  # noqa: BLE001
                pass


# ── one façade wired from main.py ────────────────────────────────────────────
class CoreDiscoveryService:
    def __init__(self, *, core_id: str, link_port: int, name: str, udp_port: int | None = None) -> None:
        self.core_id = core_id
        self.name = name
        self.link_port = int(link_port)
        self.responder = DiscoveryResponder(core_id=core_id, link_port=link_port, name=name, udp_port=udp_port)
        self.advertiser = BonjourAdvertiser(core_id=core_id, link_port=link_port, name=name)

    def start(self) -> None:
        self.responder.start()
        self.advertiser.start()

    def stop(self) -> None:
        self.advertiser.stop()
        self.responder.stop()

    def snapshot(self) -> dict[str, Any]:
        return {
            "core_id": self.core_id,
            "name": self.name,
            "port": self.link_port,
            "service": SERVICE_TYPE,
            "bonjour": self.advertiser.running,
            "bonjour_backend": self.advertiser.backend,
            "udp_port": self.responder.port,
            "udp": self.responder.running,
            "answered": self.responder.answered,
        }


_service: CoreDiscoveryService | None = None


def install_core_discovery(service: CoreDiscoveryService | None) -> None:
    global _service
    _service = service


def core_discovery_snapshot() -> dict[str, Any]:
    """What the connection page shows about this Core (works before wiring too)."""
    if _service is not None:
        return _service.snapshot()
    return {
        "core_id": _core_id(),
        "name": _core_name(),
        "port": wifi_link_port(),
        "service": SERVICE_TYPE,
        "bonjour": False,
        "bonjour_backend": "",
        "udp_port": discovery_udp_port(),
        "udp": False,
        "answered": 0,
    }


__all__ = [
    "DISCOVERY_UDP_PORT",
    "DISCOVER_REPLY_TYPE",
    "DISCOVER_REQUEST_TYPE",
    "PROTOCOL_VERSION",
    "SERVICE_TYPE",
    "BonjourAdvertiser",
    "CoreDiscoveryService",
    "DiscoveryResponder",
    "build_discover_reply",
    "core_discovery_snapshot",
    "discovery_udp_port",
    "install_core_discovery",
    "parse_discover_request",
]
