"""Bounded durable receipts for terminal device playback acknowledgements."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from deskbot_server.core.clock import as_utc as _as_utc
from deskbot_server.core.clock import utcnow as _utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import PlaybackReceipt

logger = logging.getLogger("deskbot-server")
_cleanup_lock = threading.Lock()
_next_cleanup_monotonic = 0.0


@dataclass(frozen=True)
class PlaybackReceiptView:
    device_id: str
    request_id: str
    source: str
    played_at: datetime
    expires_at: datetime


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def playback_receipt_retention_days() -> float:
    return _env_float(
        "DESKBOT_PLAYBACK_RECEIPT_RETENTION_DAYS",
        30.0,
        minimum=1.0,
        maximum=3650.0,
    )


def playback_receipt_cleanup_interval_seconds() -> float:
    return _env_float(
        "DESKBOT_PLAYBACK_RECEIPT_CLEANUP_INTERVAL_SEC",
        3600.0,
        minimum=60.0,
        maximum=86_400.0,
    )


def _normalize_device_id(device_id: str) -> str:
    value = str(device_id or "").strip()
    if not value:
        raise ValueError("device_id is required")
    if len(value) > 128:
        raise ValueError("device_id is too long")
    return value


def _normalize_request_ids(request_ids: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in request_ids:
        value = str(raw or "").strip()
        if not value:
            continue
        if len(value) > 128:
            raise ValueError("request_id is too long")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("at least one request_id is required")
    return normalized


def _view(row: PlaybackReceipt) -> PlaybackReceiptView:
    return PlaybackReceiptView(
        device_id=row.device_id,
        request_id=row.request_id,
        source=row.source,
        played_at=_as_utc(row.played_at),
        expires_at=_as_utc(row.expires_at),
    )


def record_playback_receipts(
    device_id: str,
    request_ids: Iterable[str],
    *,
    source: str = "pb_ack",
    played_at: datetime | None = None,
    retention_days: float | None = None,
) -> list[PlaybackReceiptView]:
    """Persist one or more immutable, idempotent played receipts atomically.

    A repeated terminal ACK does not extend retention indefinitely. An expired
    key may be reused and starts a new retention window.
    """

    device = _normalize_device_id(device_id)
    requests = _normalize_request_ids(request_ids)
    current = _as_utc(played_at) if played_at is not None else _utcnow()
    days = (
        playback_receipt_retention_days()
        if retention_days is None
        else max(0.0, min(3650.0, float(retention_days)))
    )
    expires_at = current + timedelta(days=days)
    receipt_source = str(source or "pb_ack").strip()[:32] or "pb_ack"
    maybe_purge_expired_playback_receipts()

    session = get_session()
    try:
        # Retention makes request-id reuse well-defined: remove an expired
        # exact key before the conflict-safe insert.
        session.execute(
            delete(PlaybackReceipt).where(
                PlaybackReceipt.device_id == device,
                PlaybackReceipt.request_id.in_(requests),
                PlaybackReceipt.expires_at <= current,
            )
        )
        session.execute(
            sqlite_insert(PlaybackReceipt)
            .values(
                [
                    {
                        "device_id": device,
                        "request_id": request_id,
                        "source": receipt_source,
                        "played_at": current,
                        "expires_at": expires_at,
                    }
                    for request_id in requests
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["device_id", "request_id"]
            )
        )
        rows = list(
            session.scalars(
                select(PlaybackReceipt).where(
                    PlaybackReceipt.device_id == device,
                    PlaybackReceipt.request_id.in_(requests),
                )
            )
        )
        session.commit()
        by_request = {row.request_id: _view(row) for row in rows}
        return [by_request[request_id] for request_id in requests]
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_playback_receipt(
    device_id: str,
    request_id: str,
    **kwargs,
) -> PlaybackReceiptView:
    return record_playback_receipts(
        device_id,
        [request_id],
        **kwargs,
    )[0]


def has_playback_receipt(
    device_id: str,
    request_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    device = _normalize_device_id(device_id)
    request = _normalize_request_ids([request_id])[0]
    current = _as_utc(now) if now is not None else _utcnow()
    session = get_session()
    try:
        receipt_id = session.scalar(
            select(PlaybackReceipt.id).where(
                PlaybackReceipt.device_id == device,
                PlaybackReceipt.request_id == request,
                PlaybackReceipt.expires_at > current,
            )
        )
        return receipt_id is not None
    finally:
        session.close()


def has_any_playback_receipt(
    request_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether any physical route confirmed this PC-local request."""

    request = _normalize_request_ids([request_id])[0]
    current = _as_utc(now) if now is not None else _utcnow()
    session = get_session()
    try:
        receipt_id = session.scalar(
            select(PlaybackReceipt.id)
            .where(
                PlaybackReceipt.request_id == request,
                PlaybackReceipt.expires_at > current,
            )
            .limit(1)
        )
        return receipt_id is not None
    finally:
        session.close()


def get_playback_receipt(
    device_id: str,
    request_id: str,
) -> PlaybackReceiptView | None:
    device = _normalize_device_id(device_id)
    request = _normalize_request_ids([request_id])[0]
    session = get_session()
    try:
        row = session.scalar(
            select(PlaybackReceipt).where(
                PlaybackReceipt.device_id == device,
                PlaybackReceipt.request_id == request,
            )
        )
        return _view(row) if row is not None else None
    finally:
        session.close()


def purge_expired_playback_receipts(
    *,
    now: datetime | None = None,
) -> int:
    current = _as_utc(now) if now is not None else _utcnow()
    session = get_session()
    try:
        result = session.execute(
            delete(PlaybackReceipt).where(
                PlaybackReceipt.expires_at <= current
            )
        )
        session.commit()
        return int(result.rowcount or 0)
    finally:
        session.close()


def maybe_purge_expired_playback_receipts(*, force: bool = False) -> int:
    """Run retention cleanup at most once per configured process interval."""

    global _next_cleanup_monotonic
    monotonic_now = time.monotonic()
    if not force and monotonic_now < _next_cleanup_monotonic:
        return 0
    if not _cleanup_lock.acquire(blocking=False):
        return 0
    try:
        monotonic_now = time.monotonic()
        if not force and monotonic_now < _next_cleanup_monotonic:
            return 0
        try:
            removed = purge_expired_playback_receipts()
        except Exception:
            logger.exception("playback receipt retention cleanup failed")
            removed = 0
        _next_cleanup_monotonic = (
            monotonic_now + playback_receipt_cleanup_interval_seconds()
        )
        return removed
    finally:
        _cleanup_lock.release()
