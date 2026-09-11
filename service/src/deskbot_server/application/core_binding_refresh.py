"""Keep the robot's idea of "my Core" fresh on every USB session.

The robot stores {host, port, core_id, token} in NVS.  The host goes stale
whenever DHCP moves the PC; before 0.0.49 the only fix was re-running WiFi
sync by hand.  Now every USB session compares what the robot remembers with
what the Core is right now and pushes a host-only ``wifi_config`` (no SSID,
no reboot) when they differ.  Firmware that predates ``core_id`` in
``wifi_status`` is left alone — it would reject the empty SSID.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("deskbot-server")

REFRESH_DELAY_SEC = 3.0
_STATUS_TIMEOUT_SEC = 4.0
_CONFIG_TIMEOUT_SEC = 6.0


def desired_binding(*, host: str, port: int, core_id: str, token: str) -> dict[str, Any]:
    return {"host": str(host or ""), "port": int(port), "core_id": str(core_id or ""), "token": str(token or "")}


def binding_needs_refresh(status: dict[str, Any] | None, desired: dict[str, Any]) -> tuple[bool, str]:
    """(refresh?, reason) from the robot's wifi_status and the Core's current identity."""
    if not isinstance(status, dict):
        return False, "no_status"
    if "core_id" not in status:
        return False, "firmware_too_old"
    if not status.get("configured"):
        return False, "unprovisioned"
    if not desired["host"]:
        return False, "pc_lan_ip_unavailable"
    if str(status.get("core_id") or "") != desired["core_id"]:
        return True, "core_id"
    if str(status.get("host") or "") != desired["host"]:
        return True, "host"
    try:
        if int(status.get("port") or 0) != desired["port"]:
            return True, "port"
    except (TypeError, ValueError):
        return True, "port"
    return False, "up_to_date"


async def refresh_core_binding(
    session: Any,
    *,
    desired: dict[str, Any],
    token_provider: Callable[[str], Awaitable[str] | str] | None = None,
) -> dict[str, Any]:
    """Compare and, when needed, push the Core's current address/identity.

    ``session`` is a DeviceSession-like object: ``transport``, ``device_id``,
    ``request_wifi_status()``, ``send_wifi_config(dict)``.
    """
    device_id = str(getattr(session, "device_id", "") or "")
    if getattr(session, "transport", "") != "usb_cdc":
        return {"action": "skipped", "reason": "not_usb"}
    try:
        status = await session.request_wifi_status(timeout=_STATUS_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 - never let this break a session
        return {"action": "failed", "reason": f"status_error:{exc}"}
    refresh, reason = binding_needs_refresh(status, desired)
    if not refresh:
        return {"action": "skipped", "reason": reason}
    payload = dict(desired)
    if token_provider is not None:
        try:
            token = token_provider(device_id)
            if asyncio.iscoroutine(token):
                token = await token
            payload["token"] = str(token or payload.get("token") or "")
        except Exception as exc:  # noqa: BLE001
            return {"action": "failed", "reason": f"token_error:{exc}"}
    try:
        reply = await session.send_wifi_config(payload, timeout=_CONFIG_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001
        return {"action": "failed", "reason": f"config_error:{exc}"}
    if not isinstance(reply, dict):
        return {"action": "failed", "reason": "no_reply", "why": reason}
    applied = str(reply.get("host") or "") == desired["host"] and str(reply.get("core_id") or "") == desired["core_id"]
    logger.info(
        "[core_binding] %s device_id=%s why=%s host=%s->%s core=%s->%s",
        "refreshed" if applied else "rejected",
        device_id,
        reason,
        (status or {}).get("host"),
        desired["host"],
        (status or {}).get("core_id"),
        desired["core_id"],
    )
    return {"action": "refreshed" if applied else "failed", "reason": reason, "wifi": reply}


__all__ = ["REFRESH_DELAY_SEC", "binding_needs_refresh", "desired_binding", "refresh_core_binding"]
