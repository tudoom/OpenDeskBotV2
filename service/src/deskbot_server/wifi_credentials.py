"""Read the PC's currently connected WiFi SSID and key.

Windows: ``netsh wlan show interfaces`` / ``show profile … key=clear``.
Revealing ``key=clear`` may require an elevated console on some builds.

macOS: ``ipconfig getsummary`` / ``networksetup`` for the SSID and the login
keychain (``security find-generic-password``) for the key.  Since macOS 14 the
system treats the SSID as location data: every CLI prints ``<redacted>`` (and
CoreWLAN returns nil) unless the caller holds Location Services permission,
which a background service never has.  In that case we can still tell that the
PC *is* on WiFi and list the preferred networks so the page can offer a picker.

Callers must treat a missing key as "ask the user to type it", never as an
error that blocks provisioning.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field

logger = logging.getLogger("deskbot-server")

_NETSH_TIMEOUT_SECONDS = 10.0
_DARWIN_CMD_TIMEOUT_SECONDS = 10.0
# 钥匙串读取会弹系统授权框，给用户留时间点「允许」。
_DARWIN_KEYCHAIN_TIMEOUT_SECONDS = 45.0
_REDACTED_MARKERS = ("<redacted>", "redacted")


@dataclass
class PcWifiStatus:
    """What the PC knows about its own WiFi, with graceful degradation."""

    associated: bool = False
    ssid: str = ""
    ssid_hidden_by_os: bool = False
    candidates: list[str] = field(default_factory=list)


def is_darwin() -> bool:
    return sys.platform == "darwin"


# ---------------------------------------------------------------- Windows


def _run_netsh(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            timeout=_NETSH_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[wifi] netsh %s failed: %s", " ".join(args), exc)
        return ""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return completed.stdout.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return completed.stdout.decode("utf-8", errors="replace")


def _first_value(text: str, keys: tuple[str, ...]) -> str:
    """netsh output is localized; match on any known label for the field."""
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        normalized = re.sub(r"\s+", "", label).lower()
        if any(key in normalized for key in keys):
            candidate = value.strip()
            if candidate:
                return candidate
    return ""


def _windows_current_ssid() -> str:
    text = _run_netsh(["wlan", "show", "interfaces"])
    if not text:
        return ""
    # 排除 BSSID 行（同样含 "ssid"）；先精确找独立的 SSID 标签。
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        if re.sub(r"\s+", "", label).lower() == "ssid":
            candidate = value.strip()
            if candidate:
                return candidate
    return ""


def wifi_profile_details(ssid: str) -> tuple[str, bool]:
    """(key, is_enterprise) for one profile; key is empty when not revealable.

    802.1X networks have no PSK at all — the caller must steer the user to a
    different network instead of asking for a password that does not exist.
    """
    name = str(ssid or "").strip()
    if not name:
        return "", False
    if is_darwin():
        return _darwin_wifi_password(name), False
    text = _run_netsh(
        ["wlan", "show", "profile", f"name={name}", "key=clear"]
    )
    if not text:
        return "", False
    auth = _first_value(text, ("authentication", "身份验证")).lower()
    enterprise = "enterprise" in auth or "802.1x" in auth
    # 中文系统为「关键内容」，英文为 "Key Content"。
    key = _first_value(text, ("keycontent", "关键内容"))
    return key, enterprise


# ------------------------------------------------------------------ macOS


def _run_darwin(args: list[str], *, timeout: float = _DARWIN_CMD_TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[wifi] %s failed: %s", " ".join(args[:2]), exc)
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


def _darwin_wifi_interface() -> str:
    """The BSD name of the Wi-Fi port (usually en0/en1)."""
    text = _run_darwin(["networksetup", "-listallhardwareports"])
    port = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.partition(":")[2].strip()
        elif line.startswith("Device:") and port.lower() in {"wi-fi", "airport", "wlan"}:
            return line.partition(":")[2].strip()
    return ""


def _is_redacted(value: str) -> bool:
    return value.strip().lower() in _REDACTED_MARKERS


def _darwin_ssid_via_getsummary(iface: str) -> tuple[bool, str]:
    """(associated, ssid). ssid is "" when the OS redacts it."""
    text = _run_darwin(["ipconfig", "getsummary", iface])
    for line in text.splitlines():
        label, _, value = line.partition(":")
        if label.strip() == "SSID":
            value = value.strip()
            if not value:
                return False, ""
            return True, "" if _is_redacted(value) else value
    return False, ""


def _darwin_ssid_via_networksetup(iface: str) -> str:
    text = _run_darwin(["networksetup", "-getairportnetwork", iface])
    marker = "Current Wi-Fi Network:"
    for line in text.splitlines():
        if marker in line:
            value = line.split(marker, 1)[1].strip()
            return "" if _is_redacted(value) else value
    return ""


def _darwin_preferred_networks(iface: str) -> list[str]:
    text = _run_darwin(["networksetup", "-listpreferredwirelessnetworks", iface])
    names: list[str] = []
    for line in text.splitlines():
        if line.startswith("\t") or line.startswith("    "):
            name = line.strip()
            if name and name not in names:
                names.append(name)
    return names


def _darwin_wifi_password(ssid: str) -> str:
    """Login/System keychain lookup; the OS shows an allow dialog to the user."""
    text = _run_darwin(
        [
            "security",
            "find-generic-password",
            "-D",
            "AirPort network password",
            "-wa",
            ssid,
        ],
        timeout=_DARWIN_KEYCHAIN_TIMEOUT_SECONDS,
    )
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _darwin_status() -> PcWifiStatus:
    iface = _darwin_wifi_interface()
    if not iface:
        return PcWifiStatus()
    associated, ssid = _darwin_ssid_via_getsummary(iface)
    if not ssid:
        ssid = _darwin_ssid_via_networksetup(iface)
    if ssid:
        associated = True
    status = PcWifiStatus(associated=associated, ssid=ssid)
    if associated and not ssid:
        status.ssid_hidden_by_os = True
        status.candidates = _darwin_preferred_networks(iface)
    return status


# ------------------------------------------------------------------ public


def current_wifi_ssid() -> str:
    if is_darwin():
        return _darwin_status().ssid
    return _windows_current_ssid()


def pc_wifi_status() -> PcWifiStatus:
    """Platform-neutral view used by the provisioning route."""
    if is_darwin():
        return _darwin_status()
    ssid = _windows_current_ssid()
    return PcWifiStatus(associated=bool(ssid), ssid=ssid)


def pc_wifi_credentials() -> tuple[PcWifiStatus, str, bool]:
    """One probe: (status, key, is_enterprise).

    Platform probing (several subprocesses on macOS) happens exactly once; the
    keychain / netsh key lookup runs only when an SSID is actually visible.
    """
    status = pc_wifi_status()
    if not status.ssid:
        return status, "", False
    key, enterprise = wifi_profile_details(status.ssid)
    return status, key, enterprise


def current_wifi_credentials() -> tuple[str, str, bool]:
    """(ssid, key, is_enterprise); parts may be empty when unavailable."""
    status, key, enterprise = pc_wifi_credentials()
    return status.ssid, key, enterprise
