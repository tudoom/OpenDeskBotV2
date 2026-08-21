"""Durable idempotency and terminal states for asynchronous HTTP controls."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from deskbot_server.core.clock import as_utc as _as_utc
from deskbot_server.core.clock import utcnow as _utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import ControlOperation

CONTROL_OPERATION_KINDS = frozenset(
    {
        "device_tts",
        "scene_playbook",
        "device_servo",
        "device_face_play",
        "device_pb_scene",
        "device_pb_anim",
        "device_pb_expr_scene",
    }
)
CONTROL_OPERATION_IN_FLIGHT = frozenset({"accepted", "running"})
CONTROL_OPERATION_TERMINAL = frozenset(
    {"completed", "failed", "cancelled", "timeout"}
)
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
logger = logging.getLogger("deskbot-server")
_cleanup_lock = threading.Lock()
_next_cleanup_monotonic = 0.0


class ControlOperationConflict(ValueError):
    """An idempotency key was already bound to a different control."""


@dataclass(frozen=True)
class ControlOperationView:
    operation_id: str
    device_id: str
    kind: str
    payload_hash: str
    request_id: str
    status: str
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    deadline_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_token: str | None = None

    def as_dict(self) -> dict[str, Any]:
        error = None
        if self.error_code or self.error_message:
            error = {
                "code": self.error_code or "control_operation_failed",
                "message": self.error_message or "",
            }
        return {
            "operation_id": self.operation_id,
            "device_id": self.device_id,
            "kind": self.kind,
            "payload_hash": self.payload_hash,
            "request_id": self.request_id,
            "status": self.status,
            "terminal": self.status in CONTROL_OPERATION_TERMINAL,
            "result": self.result,
            "error": error,
            "deadline_at": _iso(self.deadline_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _env_seconds(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def control_operation_timeout_seconds() -> float:
    return _env_seconds(
        "DESKBOT_CONTROL_OPERATION_TIMEOUT_SEC",
        900.0,
        1.0,
        3600.0,
    )


def control_operation_lease_seconds() -> float:
    return _env_seconds(
        "DESKBOT_CONTROL_OPERATION_LEASE_SEC",
        45.0,
        3.0,
        300.0,
    )


def control_operation_heartbeat_seconds() -> float:
    lease = control_operation_lease_seconds()
    return max(1.0, min(10.0, lease / 3.0))


def control_operation_retention_days() -> float:
    return _env_seconds(
        "DESKBOT_CONTROL_OPERATION_RETENTION_DAYS",
        30.0,
        1.0,
        3650.0,
    )


def control_operation_cleanup_interval_seconds() -> float:
    return _env_seconds(
        "DESKBOT_CONTROL_OPERATION_CLEANUP_INTERVAL_SEC",
        3600.0,
        60.0,
        86_400.0,
    )


def canonical_payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_operation_id(operation_id: str | None) -> str:
    value = str(operation_id or "").strip()
    if not value:
        return uuid.uuid4().hex
    if not _OPERATION_ID_RE.fullmatch(value):
        raise ValueError(
            "operation_id must be 1-128 characters using letters, digits, '.', '_', ':' or '-'"
        )
    return value


def _decode_result(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"unreadable": True}
    return value if isinstance(value, dict) else {"value": value}


def _view(row: ControlOperation) -> ControlOperationView:
    return ControlOperationView(
        operation_id=row.operation_id,
        device_id=row.device_id,
        kind=row.kind,
        payload_hash=row.payload_hash,
        request_id=row.request_id,
        status=row.status,
        result=_decode_result(row.result_json),
        error_code=row.error_code,
        error_message=row.error_message,
        deadline_at=_as_utc(row.deadline_at),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        started_at=_as_utc(row.started_at) if row.started_at else None,
        completed_at=_as_utc(row.completed_at) if row.completed_at else None,
        lease_token=row.lease_token,
    )


def _operation_by_identity(session, operation_id: str):
    return session.scalar(
        select(ControlOperation).where(
            ControlOperation.operation_id == operation_id,
        )
    )


def _binding_matches(
    row: ControlOperation,
    *,
    device_id: str,
    kind: str,
    payload_hash: str,
) -> bool:
    return (
        row.device_id == device_id
        and row.kind == kind
        and row.payload_hash == payload_hash
    )


def purge_expired_control_operations(
    *,
    now: datetime | None = None,
    retention_days: float | None = None,
) -> int:
    """Delete expired terminal ledger rows without touching in-flight work."""

    current = _as_utc(now) if now is not None else _utcnow()
    days = (
        control_operation_retention_days()
        if retention_days is None
        else max(0.0, float(retention_days))
    )
    cutoff = current - timedelta(days=days)
    session = get_session()
    try:
        completed = session.execute(
            delete(ControlOperation).where(
                ControlOperation.status.in_(CONTROL_OPERATION_TERMINAL),
                ControlOperation.completed_at.is_not(None),
                ControlOperation.completed_at <= cutoff,
            )
        )
        updated_only = session.execute(
            delete(ControlOperation).where(
                ControlOperation.status.in_(CONTROL_OPERATION_TERMINAL),
                ControlOperation.completed_at.is_(None),
                ControlOperation.updated_at <= cutoff,
            )
        )
        session.commit()
        return int(completed.rowcount or 0) + int(updated_only.rowcount or 0)
    finally:
        session.close()


def maybe_purge_expired_control_operations(*, force: bool = False) -> int:
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
            removed = purge_expired_control_operations()
        except Exception:
            logger.exception("control operation retention cleanup failed")
            removed = 0
        _next_cleanup_monotonic = (
            monotonic_now + control_operation_cleanup_interval_seconds()
        )
        return removed
    finally:
        _cleanup_lock.release()


def accept_control_operation(
    *,
    device_id: str,
    kind: str,
    payload: object,
    operation_id: str | None = None,
    request_id: str | None = None,
) -> tuple[ControlOperationView, bool]:
    device = str(device_id or "").strip()
    normalized_kind = str(kind or "").strip()
    if not device or len(device) > 128:
        raise ValueError("valid device_id is required")
    if normalized_kind not in CONTROL_OPERATION_KINDS:
        raise ValueError(f"unsupported control operation kind: {normalized_kind}")
    op_id = normalize_operation_id(operation_id)
    req_id = str(request_id or uuid.uuid4().hex[:16]).strip()[:64]
    fingerprint = canonical_payload_hash(payload)

    # Retention cleanup is deliberately NOT run inline here.  It issues
    # several DELETE commits and used to piggyback on the first accept after
    # the cleanup interval, stalling that control request.  Core now runs
    # ``maybe_purge_expired_control_operations`` from a dedicated background
    # task (see deskbot_server.main).
    recover_stale_control_operations(
        operation_id=op_id,
    )
    now = _utcnow()
    row = ControlOperation(
        operation_id=op_id,
        device_id=device,
        kind=normalized_kind,
        payload_hash=fingerprint,
        request_id=req_id,
        status="accepted",
        lease_expires_at=now + timedelta(seconds=control_operation_lease_seconds()),
        deadline_at=now + timedelta(seconds=control_operation_timeout_seconds()),
        created_at=now,
        updated_at=now,
    )
    session = get_session()
    try:
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            existing = _operation_by_identity(session, op_id)
            if existing is None:
                raise
            if not _binding_matches(
                existing,
                device_id=device,
                kind=normalized_kind,
                payload_hash=fingerprint,
            ):
                raise ControlOperationConflict(
                    "operation_id is already bound to another device, kind or payload"
                ) from exc
            return _view(existing), False
        session.refresh(row)
        return _view(row), True
    finally:
        session.close()


def recover_stale_control_operations(
    *,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> int:
    current = _as_utc(now) if now is not None else _utcnow()
    predicates = [
        ControlOperation.status.in_(CONTROL_OPERATION_IN_FLIGHT),
        or_(
            ControlOperation.deadline_at <= current,
            ControlOperation.lease_expires_at.is_(None),
            ControlOperation.lease_expires_at <= current,
        ),
    ]
    op_id = str(operation_id or "").strip()
    if op_id:
        predicates.append(ControlOperation.operation_id == op_id)

    session = get_session()
    try:
        deadline_expired = ControlOperation.deadline_at <= current
        result = session.execute(
            update(ControlOperation)
            .where(*predicates)
            .values(
                status="timeout",
                error_code=case(
                    (
                        deadline_expired,
                        "operation_deadline_exceeded",
                    ),
                    else_="worker_lease_expired",
                ),
                error_message=case(
                    (
                        deadline_expired,
                        "control operation exceeded its absolute deadline",
                    ),
                    else_=(
                        "control operation worker lease expired before a "
                        "terminal result"
                    ),
                ),
                result_json=case(
                    (
                        deadline_expired,
                        '{"terminal_reason": "operation_deadline_exceeded"}',
                    ),
                    else_='{"terminal_reason": "worker_lease_expired"}',
                ),
                lease_token=None,
                lease_expires_at=None,
                updated_at=current,
                completed_at=current,
            )
        )
        session.commit()
        return int(result.rowcount or 0)
    finally:
        session.close()


def get_control_operation(
    *,
    operation_id: str,
) -> ControlOperationView | None:
    op_id = str(operation_id or "").strip()
    if not op_id:
        return None
    maybe_purge_expired_control_operations()
    recover_stale_control_operations(
        operation_id=op_id,
    )
    session = get_session()
    try:
        row = session.scalar(
            select(ControlOperation).where(
                ControlOperation.operation_id == op_id,
            )
        )
        return _view(row) if row is not None else None
    finally:
        session.close()


def mark_control_operation_running(
    *,
    operation_id: str,
) -> ControlOperationView | None:
    op_id = str(operation_id or "").strip()
    recover_stale_control_operations(
        operation_id=op_id,
    )
    now = _utcnow()
    lease_token = uuid.uuid4().hex
    session = get_session()
    try:
        result = session.execute(
            update(ControlOperation)
            .where(
                ControlOperation.operation_id == op_id,
                ControlOperation.status == "accepted",
                ControlOperation.deadline_at > now,
                ControlOperation.lease_expires_at > now,
            )
            .values(
                status="running",
                lease_token=lease_token,
                lease_expires_at=now
                + timedelta(seconds=control_operation_lease_seconds()),
                started_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        row = _operation_by_identity(session, op_id)
        return _view(row) if row is not None else None
    finally:
        session.close()


def heartbeat_control_operation(
    *,
    operation_id: str,
    lease_token: str,
) -> bool:
    now = _utcnow()
    session = get_session()
    try:
        result = session.execute(
            update(ControlOperation)
            .where(
                ControlOperation.operation_id == operation_id,
                ControlOperation.status == "running",
                ControlOperation.lease_token == lease_token,
                ControlOperation.deadline_at > now,
            )
            .values(
                lease_expires_at=now
                + timedelta(seconds=control_operation_lease_seconds()),
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1
    finally:
        session.close()


def seconds_until_deadline(operation: ControlOperationView) -> float:
    return max(0.0, (_as_utc(operation.deadline_at) - _utcnow()).total_seconds())


def finish_control_operation(
    *,
    operation_id: str,
    lease_token: str,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ControlOperationView | None:
    terminal_status = str(status or "").strip()
    if terminal_status not in CONTROL_OPERATION_TERMINAL:
        raise ValueError(f"invalid terminal control operation status: {terminal_status}")
    now = _utcnow()
    predicates = [
        ControlOperation.operation_id == operation_id,
        ControlOperation.status == "running",
        ControlOperation.lease_token == lease_token,
    ]
    if terminal_status != "timeout":
        predicates.extend(
            [
                ControlOperation.deadline_at > now,
                ControlOperation.lease_expires_at > now,
            ]
        )
    result_json = (
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if result is not None
        else None
    )
    message = str(error_message or "").strip()[:2048] or None
    session = get_session()
    needs_recovery = False
    view: ControlOperationView | None = None
    try:
        updated = session.execute(
            update(ControlOperation)
            .where(*predicates)
            .values(
                status=terminal_status,
                result_json=result_json,
                error_code=str(error_code or "").strip()[:64] or None,
                error_message=message,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                completed_at=now,
            )
        )
        if updated.rowcount != 1:
            session.rollback()
            needs_recovery = True
        else:
            session.commit()
            row = _operation_by_identity(session, operation_id)
            view = _view(row) if row is not None else None
    finally:
        session.close()
    if needs_recovery:
        recover_stale_control_operations(
            operation_id=operation_id,
            now=now,
        )
        session = get_session()
        try:
            row = _operation_by_identity(session, operation_id)
            return _view(row) if row is not None else None
        finally:
            session.close()
    return view
