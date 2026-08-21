from __future__ import annotations

import json
import math
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, text

from deskbot_server.core.clock import as_utc as _as_utc
from deskbot_server.core.clock import utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import VoiceCloneJob, VoiceClonePollThrottle

_MAX_PROVIDER_RESPONSE_CHARS = 64 * 1024
_MAX_ERROR_CHARS = 2_000
_POLL_THROTTLE_RETENTION = timedelta(days=7)


def _poll_interval_seconds(name: str, default: float, *, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(0.25, min(value, maximum))


def claim_voice_clone_status_poll(
    *,
    job_id: str,
) -> int:
    """Atomically reserve one provider status poll.

    Returns zero when the caller may contact the provider, otherwise the
    number of whole seconds the client must wait. SQLite's immediate write
    reservation makes the decision consistent across Flask workers.
    """

    job_key = str(job_id or "").strip()[:128]
    if not job_key:
        raise ValueError("job_id is required")
    interval = _poll_interval_seconds(
        "DESKBOT_VOICE_CLONE_STATUS_JOB_INTERVAL_SEC",
        5.0,
        maximum=300.0,
    )
    now = utcnow()
    session = get_session()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        session.execute(
            delete(VoiceClonePollThrottle).where(
                VoiceClonePollThrottle.last_polled_at
                < now - _POLL_THROTTLE_RETENTION
            )
        )
        row = session.scalar(
            select(VoiceClonePollThrottle).where(
                VoiceClonePollThrottle.job_key == job_key,
            )
        )
        if row is not None:
            elapsed = (now - _as_utc(row.last_polled_at)).total_seconds()
            if elapsed < interval:
                session.rollback()
                return max(1, math.ceil(interval - elapsed))
            row.last_polled_at = now
        else:
            session.add(
                VoiceClonePollThrottle(
                    job_key=job_key,
                    last_polled_at=now,
                )
            )
        session.commit()
        return 0
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _provider_json(raw: dict[str, Any] | None) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    return value[:_MAX_PROVIDER_RESPONSE_CHARS]


def _detach(session, row: VoiceCloneJob) -> VoiceCloneJob:
    session.refresh(row)
    session.expunge(row)
    return row


def create_voice_clone_job(
    *,
    speaker_id: str,
    display_name: str,
) -> VoiceCloneJob:
    row = VoiceCloneJob(
        speaker_id=str(speaker_id),
        display_name=str(display_name).strip()[:128],
        state="submitting",
    )
    session = get_session()
    try:
        session.add(row)
        session.commit()
        return _detach(session, row)
    finally:
        session.close()


def update_voice_clone_job_result(job_id: str, result) -> VoiceCloneJob:
    session = get_session()
    try:
        row = session.get(VoiceCloneJob, str(job_id))
        if row is None:
            raise ValueError("voice clone job not found")
        row.speaker_id = str(result.speaker_id or row.speaker_id)
        row.provider_status = result.status
        row.status_label = str(result.status_label or "")[:64] or None
        row.ready = bool(result.ready)
        row.model_type = result.model_type
        row.state = (
            "ready"
            if result.ready
            else ("failed" if result.status == 3 else "training")
        )
        row.provider_response_json = _provider_json(result.raw)
        row.last_error = None
        session.commit()
        return _detach(session, row)
    finally:
        session.close()


def mark_voice_clone_job_failed(job_id: str, error: BaseException | str) -> None:
    session = get_session()
    try:
        row = session.get(VoiceCloneJob, str(job_id))
        if row is None:
            return
        row.state = "failed"
        row.ready = False
        row.last_error = str(error)[:_MAX_ERROR_CHARS]
        session.commit()
    finally:
        session.close()


def get_voice_clone_job(
    *,
    job_id: str,
) -> VoiceCloneJob | None:
    session = get_session()
    try:
        row = session.scalar(
            select(VoiceCloneJob).where(
                VoiceCloneJob.id == str(job_id),
            )
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def list_voice_clone_jobs(
    *,
    limit: int = 20,
) -> list[VoiceCloneJob]:
    safe_limit = max(1, min(int(limit), 100))
    session = get_session()
    try:
        rows = list(
            session.scalars(
                select(VoiceCloneJob)
                .order_by(VoiceCloneJob.created_at.desc())
                .limit(safe_limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def voice_clone_job_payload(row: VoiceCloneJob) -> dict[str, Any]:
    return {
        "job_id": row.id,
        "speaker_id": row.speaker_id,
        "display_name": row.display_name,
        "state": row.state,
        "status": row.provider_status,
        "status_label": row.status_label or "未知",
        "ready": bool(row.ready),
        "model_type": row.model_type,
        "error": row.last_error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
