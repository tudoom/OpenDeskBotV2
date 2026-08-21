from __future__ import annotations

from flask import session

from deskbot_server.hardware_catalog import get_device_by_device_id

SESSION_DEVICE_KEY = "current_device_id"


def get_current_device_id() -> str | None:
    raw = session.get(SESSION_DEVICE_KEY)
    if not raw:
        return None
    device_id = str(raw).strip()
    if not device_id:
        return None
    if get_device_by_device_id(device_id) is None:
        clear_current_device()
        return None
    return device_id


def set_current_device_id(device_id: str) -> None:
    session[SESSION_DEVICE_KEY] = device_id.strip()
    session.modified = True


def clear_current_device() -> None:
    session.pop(SESSION_DEVICE_KEY, None)
    session.modified = True
