"""Permanent identity of this PC Core.

The robot used to remember the PC by IP address.  DHCP hands the PC a new
address every so often (2026-09-08: the Mac moved from .163 to .63 and the
robot on wall power could not find the Core any more).  The identity below
never changes once minted, so the robot can remember *which* Core it belongs
to and look up that Core's current address by name (mDNS / UDP discovery).

Stored next to the link tokens under the local data dir so a reinstall of
the app keeps the same identity and no device needs re-pairing.
"""

from __future__ import annotations

import json
import platform
import secrets
import time

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.device_data import local_data_dir

IDENTITY_FILENAME = "core_identity.json"
CORE_ID_PREFIX = "core_"


def _identity_path():
    return local_data_dir() / IDENTITY_FILENAME


def _read_identity() -> dict:
    try:
        raw = json.loads(_identity_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def core_id() -> str:
    """Return this Core's permanent id, minting and persisting one on first use."""
    current = _read_identity().get("core_id")
    if isinstance(current, str) and current.startswith(CORE_ID_PREFIX) and len(current) > len(CORE_ID_PREFIX):
        return current
    path = _identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path)):
        data = _read_identity()
        current = data.get("core_id")
        if not (isinstance(current, str) and current.startswith(CORE_ID_PREFIX) and len(current) > len(CORE_ID_PREFIX)):
            current = CORE_ID_PREFIX + secrets.token_hex(6)
            data.update(core_id=current, created_at=time.time())
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return current


def core_name() -> str:
    """Human label shown next to the id (the PC's host name; never used for matching)."""
    name = ""
    try:
        name = platform.node().split(".")[0]
    except Exception:  # noqa: BLE001 - cosmetic only
        name = ""
    return name or "PC"


__all__ = ["CORE_ID_PREFIX", "IDENTITY_FILENAME", "core_id", "core_name"]
