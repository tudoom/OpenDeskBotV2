from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select, text

from deskbot_server.db.engine import get_session
from deskbot_server.db.models import Device

_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")
_RESERVED_DEVICE_IDS = frozenset(
    {
        "deskbot_000000000000",
        "deskbot_ffffffffffff",
        "deskbot_unavailable",
    }
)


def normalize_device_id(device_id: str) -> str:
    return (device_id or "").strip()


def validate_device_id(device_id: str) -> bool:
    """Validate a physical transport identity reported by local hardware."""

    normalized = normalize_device_id(device_id)
    return bool(_DEVICE_ID_RE.fullmatch(normalized)) and (
        normalized.lower() not in _RESERVED_DEVICE_IDS
    )


def list_devices() -> list[Device]:
    """Return hardware previously observed by this PC, newest first."""

    session = get_session()
    try:
        rows = session.scalars(
            select(Device).order_by(
                Device.last_seen_at.desc(),
                Device.created_at.desc(),
            )
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)
    finally:
        session.close()


def ensure_local_device(
    device_id: str,
    *,
    seen_at: datetime | None = None,
) -> Device:
    """Record hardware after the USB bridge validates its framed hello.

    This catalogue entry is only a connection/routing record. It never creates
    a device-specific data directory. Replacing the USB hardware therefore
    keeps the same PC-local product data automatically.
    """

    normalized = normalize_device_id(device_id)
    if not validate_device_id(normalized):
        raise ValueError("device_id 格式无效（仅允许字母、数字、_、.、-）")
    observed_at = seen_at or datetime.now(timezone.utc)

    session = get_session()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.scalar(
            select(Device).where(Device.device_id == normalized)
        )
        if row is None:
            row = Device(
                device_id=normalized,
                display_name=normalized,
                last_seen_at=observed_at,
            )
            session.add(row)
        else:
            row.last_seen_at = observed_at
            if not str(row.display_name or "").strip():
                row.display_name = normalized
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def get_device_by_device_id(device_id: str) -> Device | None:
    """Look up one physical hardware routing record."""

    normalized = normalize_device_id(device_id)
    if not validate_device_id(normalized):
        return None
    session = get_session()
    try:
        row = session.scalar(
            select(Device).where(Device.device_id == normalized)
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def mark_device_seen(device_id: str, *, at: datetime | None = None) -> bool:
    """Update activity for a known physical transport route."""

    normalized = normalize_device_id(device_id)
    if not validate_device_id(normalized):
        return False
    session = get_session()
    try:
        row = session.scalar(
            select(Device).where(Device.device_id == normalized)
        )
        if row is None:
            return False
        row.last_seen_at = at or datetime.now(timezone.utc)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def device_ids() -> set[str]:
    """Return known hardware transport identities."""

    return {device.device_id for device in list_devices()}
