"""Cron 定时任务 CRUD 与调度。

时间口径（core.clock 约定）：所有落库字段统一 naive-UTC（写入前 ``as_utc``，
回读的 naive 值按 UTC 解释）；cron 表达式仍按东八区（北京时间）墙钟语义计算，
序列化边界统一转成北京时间字符串。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter
from sqlalchemy import func, or_, select, update

from deskbot_server.core.clock import CST, as_utc, to_local, utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import ScheduledTask, _new_id

_STATUS_ACTIVE = "active"
_STATUS_RUNNING = "running"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"
_STATUS_PAUSED = "paused"
_SCHEDULED_TASK_STATUSES = frozenset(
    {
        _STATUS_ACTIVE,
        _STATUS_RUNNING,
        _STATUS_COMPLETED,
        _STATUS_FAILED,
        _STATUS_PAUSED,
    }
)

_TASK_ONCE = "once"
_TASK_RECURRING = "recurring"

_CRON_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$"
)

def _parse_cron_int(token: str) -> int | None:
    tok = str(token or "").strip()
    if not tok.isdigit():
        return None
    return int(tok)


def _repair_llm_cron_parts(parts: list[str]) -> list[str]:
    """纠正 LLM 常见误写（如 ``0 49 15 6 12`` → ``49 15 12 6 *``）。"""
    if len(parts) != 5:
        return parts
    mn, hr, dom, mon, dow = parts
    hr_i = _parse_cron_int(hr)
    mn_i = _parse_cron_int(mn)
    mon_i = _parse_cron_int(mon)
    dom_i = _parse_cron_int(dom)
    dow_i = _parse_cron_int(dow)

    # 0 49 15 6 12：前导 0 + 分/时/月/日顺序颠倒
    if (
        hr_i is not None
        and hr_i > 23
        and hr_i <= 59
        and mn_i is not None
        and mn_i <= 23
        and mon_i is not None
        and 1 <= mon_i <= 12
        and dow_i is not None
        and 1 <= dow_i <= 31
        and (dom_i is None or 0 <= dom_i <= 23)
    ):
        hour = dom_i if dom_i is not None else 0
        return [str(hr_i), str(hour), str(dow_i), str(mon_i), "*"]

    # 49 15 12 6 2026：末尾误加年份
    if (
        dow_i is not None
        and dow_i >= 1970
        and dom_i is not None
        and 1 <= dom_i <= 31
        and mon_i is not None
        and 1 <= mon_i <= 12
        and hr_i is not None
        and 0 <= hr_i <= 23
        and mn_i is not None
        and 0 <= mn_i <= 59
    ):
        return [mn, hr, dom, mon, "*"]

    return parts


def format_cst(dt: datetime | None) -> str | None:
    """序列化边界：把落库时间（naive=UTC）转成北京时间字符串。"""
    if dt is None:
        return None
    return to_local(dt, CST).strftime("%Y-%m-%d %H:%M:%S")


def normalize_cron_expr(expr: str) -> str:
    raw = " ".join(str(expr or "").strip().split())
    parts = raw.split()
    if len(parts) == 6 and parts[0] in ("0", "00"):
        parts = parts[1:]
    if len(parts) == 6:
        raise ValueError(
            f"cron 表达式无效（须 5 段：分 时 日 月 周；勿含秒/年）: {expr!r}"
        )
    if len(parts) != 5:
        raise ValueError(f"cron 表达式无效（须 5 段：分 时 日 月 周）: {expr!r}")
    parts = _repair_llm_cron_parts(parts)
    raw = " ".join(parts)
    if not _CRON_RE.match(raw):
        raise ValueError(f"cron 表达式无效（须 5 段：分 时 日 月 周）: {expr!r}")
    mn_i = _parse_cron_int(parts[0])
    hr_i = _parse_cron_int(parts[1])
    if mn_i is not None and not (0 <= mn_i <= 59):
        raise ValueError(f"cron 分钟无效: {parts[0]!r}")
    if hr_i is not None and not (0 <= hr_i <= 23):
        raise ValueError(
            f"cron 小时无效: {parts[1]!r}（须 0–23；一次性任务格式为「分 时 日 月 *」，"
            f"例如 15:49 在 6 月 12 日应写 49 15 12 6 *）"
        )
    return raw


def validate_task_kind(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k in ("once", "onetime", "one_time", "one-time", "一次性", "单次"):
        return _TASK_ONCE
    if k in ("recurring", "repeat", "cron", "周期", "周期性", "循环"):
        return _TASK_RECURRING
    raise ValueError("task_kind 须为 once（一次性）或 recurring（周期性）")


def datetime_to_once_cron(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    local = dt.astimezone(CST)
    return f"{local.minute} {local.hour} {local.day} {local.month} *"




def parse_run_at(raw: str | datetime) -> datetime:
    """Parse an absolute one-shot time without dropping seconds or the year."""
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("run_at cannot be empty")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "run_at must be ISO-8601, for example 2026-07-27T15:49:30+08:00"
            ) from exc
    return _as_cst(value)


def _as_cst(dt: datetime) -> datetime:
    """用户输入口径：naive 视为北京时间墙钟（与落库口径 naive=UTC 不同）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def compute_next_run(cron_expr: str, *, base: datetime | None = None) -> datetime:
    expr = normalize_cron_expr(cron_expr)
    ref = _as_cst(base if base is not None else utcnow())
    itr = croniter(expr, ref)
    nxt = itr.get_next(datetime)
    return _as_cst(nxt)


def _coerce_delay_minutes(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _coerce_delay_seconds(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def create_scheduled_task(
    description: str,
    *,
    cron: str | None = None,
    cron_expr: str | None = None,
    task_kind: str = _TASK_ONCE,
    delay_minutes: float | None = None,
    delay_seconds: float | None = None,
    run_at: str | datetime | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    desc = str(description or "").strip()
    if not desc:
        raise ValueError("任务描述不能为空")

    kind = validate_task_kind(task_kind)
    expr_raw = str(cron or cron_expr or "").strip()
    exact_run_at: datetime | None = None
    if run_at is not None:
        exact_run_at = parse_run_at(run_at)
        kind = _TASK_ONCE
    elif delay_minutes is not None or delay_seconds is not None:
        exact_run_at = utcnow()
        if delay_minutes is not None:
            exact_run_at += timedelta(minutes=float(delay_minutes))
        else:
            exact_run_at += timedelta(seconds=float(delay_seconds or 0))
        kind = _TASK_ONCE

    if exact_run_at is not None:
        if exact_run_at <= utcnow():
            raise ValueError("run_at must be in the future")
        expr = datetime_to_once_cron(exact_run_at)
        nxt = exact_run_at
    else:
        if not expr_raw:
            raise ValueError("需要 cron、run_at 或 delay_minutes/delay_seconds")
        expr = normalize_cron_expr(expr_raw)
        nxt = compute_next_run(expr)
        if nxt <= utcnow():
            nxt = compute_next_run(expr, base=utcnow() + timedelta(seconds=1))

    session = get_session()
    try:
        # A reminder is an independent notification. Persisting the chat
        # session that created it could later roll the active context back.
        sid = None
        row = ScheduledTask(
            id=_new_id(),
            description=desc,
            cron_expr=expr,
            task_kind=kind,
            enabled=True,
            next_run_at=as_utc(nxt),
            session_id=sid,
            status=_STATUS_ACTIVE,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _task_to_dict(row)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_scheduled_task(task_id: str) -> dict[str, Any] | None:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if row is None:
            return None
        return _task_to_dict(row)
    finally:
        session.close()


def _normalize_status_filter(status: str | None) -> str | None:
    value = str(status or "").strip().lower()
    if not value:
        return None
    if value not in _SCHEDULED_TASK_STATUSES:
        allowed = ", ".join(sorted(_SCHEDULED_TASK_STATUSES))
        raise ValueError(f"status must be one of: {allowed}")
    return value


def list_scheduled_tasks(
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    normalized_status = _normalize_status_filter(status)
    session = get_session()
    try:
        filters = []
        if normalized_status is not None:
            filters.append(ScheduledTask.status == normalized_status)
        rows = session.scalars(
            select(ScheduledTask)
            .where(*filters)
            .order_by(ScheduledTask.next_run_at.asc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), 500)))
        ).all()
        return [_task_to_dict(r) for r in rows]
    finally:
        session.close()


def count_scheduled_tasks(
    *,
    status: str | None = None,
) -> int:
    normalized_status = _normalize_status_filter(status)
    session = get_session()
    try:
        filters = []
        if normalized_status is not None:
            filters.append(ScheduledTask.status == normalized_status)
        return int(
            session.scalar(
                select(func.count())
                .select_from(ScheduledTask)
                .where(*filters)
            )
            or 0
        )
    finally:
        session.close()


def update_scheduled_task(
    task_id: str,
    *,
    description: str | None = None,
    cron: str | None = None,
    cron_expr: str | None = None,
    task_kind: str | None = None,
    enabled: bool | None = None,
    run_at: str | datetime | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    tid = str(task_id or "").strip()
    if not tid:
        raise ValueError("id 不能为空")
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if row is None:
            return None

        if description is not None:
            desc = str(description).strip()
            if not desc:
                raise ValueError("任务描述不能为空")
            row.description = desc
        expr_in = str(cron or cron_expr or "").strip()
        if run_at is not None:
            exact = parse_run_at(run_at)
            if exact <= utcnow():
                raise ValueError("run_at must be in the future")
            row.task_kind = _TASK_ONCE
            row.cron_expr = datetime_to_once_cron(exact)
            row.next_run_at = as_utc(exact)
            row.status = _STATUS_ACTIVE
            row.enabled = True
            row.executed_at = None
            row.result_summary = None
            row.attempt_count = 0
            row.offline_wait_count = 0
            row.first_due_at = None
            row.lease_expires_at = None
            row.lease_token = None
            row.occurrence_id = _new_id()
        elif expr_in:
            row.cron_expr = normalize_cron_expr(expr_in)
            row.next_run_at = as_utc(compute_next_run(row.cron_expr))
            row.attempt_count = 0
            row.offline_wait_count = 0
            row.first_due_at = None
            row.occurrence_id = _new_id()
        if task_kind is not None:
            row.task_kind = validate_task_kind(task_kind)
        if enabled is not None:
            row.enabled = bool(enabled)
            if row.enabled and row.status in (
                _STATUS_COMPLETED,
                _STATUS_FAILED,
                _STATUS_PAUSED,
            ):
                row.status = _STATUS_ACTIVE
                row.executed_at = None
                row.result_summary = None
                row.attempt_count = 0
                row.offline_wait_count = 0
                row.first_due_at = None
                row.lease_expires_at = None
                row.lease_token = None
                row.occurrence_id = _new_id()
            elif not row.enabled and row.status in (_STATUS_ACTIVE, _STATUS_RUNNING):
                row.status = _STATUS_PAUSED
                row.lease_expires_at = None
                row.lease_token = None
        if session_id is not None:
            row.session_id = str(session_id).strip() or None
        if row.enabled and row.status == _STATUS_ACTIVE:
            if as_utc(row.next_run_at) <= utcnow():
                row.next_run_at = as_utc(
                    compute_next_run(
                        row.cron_expr,
                        base=utcnow() + timedelta(seconds=1),
                    )
                )
        session.commit()
        session.refresh(row)
        return _task_to_dict(row)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def retry_failed_scheduled_task(
    task_id: str,
    *,
    delay_seconds: float = 1.0,
) -> dict[str, Any] | None:
    """Explicit user retry for a failed/completed reminder.

    This differs from the scheduler's transient retry: it re-enables the row
    and starts a fresh attempt/grace window.
    """
    tid = str(task_id or "").strip()
    if not tid:
        return None
    session = get_session()
    try:
        row = session.scalar(
            select(ScheduledTask).where(ScheduledTask.id == tid)
        )
        if row is None:
            return None
        if row.status == _STATUS_RUNNING:
            raise ValueError("任务正在执行，不能重复重试")
        row.enabled = True
        row.status = _STATUS_ACTIVE
        row.next_run_at = utcnow() + timedelta(
            seconds=max(1.0, float(delay_seconds))
        )
        row.executed_at = None
        row.lease_expires_at = None
        row.lease_token = None
        row.attempt_count = 0
        row.offline_wait_count = 0
        row.first_due_at = None
        row.result_summary = None
        row.occurrence_id = _new_id()
        session.commit()
        session.refresh(row)
        return _task_to_dict(row)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def delete_scheduled_task(task_id: str) -> bool:
    tid = str(task_id or "").strip()
    if not tid:
        return False
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def execute_schedule_task_tool(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """LLM ``schedule_task`` 工具：增删改查。"""
    action = str(raw.get("action") or raw.get("op") or "create").strip().lower()
    if action in ("create", "add", "new"):
        entry = create_scheduled_task(
            str(raw.get("task") or raw.get("description") or raw.get("text") or "").strip(),
            cron=str(raw.get("cron") or raw.get("cron_expr") or "").strip() or None,
            task_kind=str(raw.get("task_kind") or raw.get("kind") or _TASK_ONCE),
            delay_minutes=_coerce_delay_minutes(
                raw.get("delay_minutes")
                or raw.get("minutes")
                or raw.get("delay_minute")
                or raw.get("minute")
            ),
            delay_seconds=_coerce_delay_seconds(
                raw.get("delay_seconds") or raw.get("seconds") or raw.get("second")
            ),
            run_at=raw.get("run_at") or raw.get("datetime") or raw.get("at"),
            session_id=None,
        )
        return {"tool": "schedule_task", "action": "create", "ok": True, **entry}
    if action in ("list", "ls", "query_all"):
        tasks = list_scheduled_tasks()
        return {"tool": "schedule_task", "action": "list", "ok": True, "tasks": tasks, "count": len(tasks)}
    tid = str(raw.get("id") or raw.get("task_id") or "").strip()
    if action in ("get", "read", "query"):
        if not tid:
            raise ValueError("get 需要 id")
        row = get_scheduled_task(tid)
        if row is None:
            raise ValueError(f"未找到任务 id={tid}")
        return {"tool": "schedule_task", "action": "get", "ok": True, "task": row}
    if action in ("update", "edit", "modify"):
        if not tid:
            raise ValueError("update 需要 id")
        row = update_scheduled_task(
            tid,
            description=raw.get("task") or raw.get("description") or raw.get("text"),
            cron=str(raw.get("cron") or raw.get("cron_expr") or "").strip() or None,
            task_kind=raw.get("task_kind") or raw.get("kind"),
            enabled=raw.get("enabled") if "enabled" in raw else None,
            session_id=None,
        )
        if row is None:
            raise ValueError(f"未找到任务 id={tid}")
        return {"tool": "schedule_task", "action": "update", "ok": True, **row}
    if action in ("delete", "remove", "del"):
        if not tid:
            raise ValueError("delete 需要 id")
        if not delete_scheduled_task(tid):
            raise ValueError(f"未找到任务 id={tid}")
        return {"tool": "schedule_task", "action": "delete", "ok": True, "id": tid}
    raise ValueError(f"未知 action: {action!r}")


def expire_overdue_active_tasks(*, lookback_minutes: float = 5.0) -> int:
    """Expire overdue tasks according to the PC-local offline policy.

    ``lookback_minutes`` remains the minimum scheduler recovery window, but it
    must not override a user's longer grace period.  In particular,
    ``deliver_when_online`` tasks remain claimable after a service restart
    instead of being silently failed by the old fixed five-minute cutoff.
    """
    now = utcnow()
    cutoff = now - timedelta(minutes=max(1.0, float(lookback_minutes)))
    session = get_session()
    try:
        rows = session.scalars(
            select(ScheduledTask).where(
                ScheduledTask.enabled.is_(True),
                ScheduledTask.status == _STATUS_ACTIVE,
                ScheduledTask.next_run_at < cutoff,
            )
        ).all()
        n = 0
        prefs: dict[str, Any] = {}
        if rows:
            # One preference read per pass: the policy applies uniformly to
            # every overdue row, and re-reading the JSON file per row only
            # multiplied disk I/O inside the scheduler tick.
            from deskbot_server.device_preferences import load_preferences

            prefs = load_preferences()
        for row in rows:
            policy = str(
                prefs.get("offline_reminder_policy") or "retry_within_grace"
            )
            first_due = as_utc(row.first_due_at or row.next_run_at)
            if policy == "deliver_when_online":
                continue
            if policy == "retry_within_grace":
                # 0 is a legal value (expire immediately); load_preferences
                # always materialises the key, so the fallback only guards an
                # unexpectedly empty mapping and stays fail-safe at 0.
                grace_seconds = max(
                    0.0,
                    float(prefs.get("offline_reminder_grace_seconds", 0)),
                )
                if now - first_due <= timedelta(seconds=grace_seconds):
                    continue
            if row.task_kind == _TASK_RECURRING:
                row.next_run_at = as_utc(compute_next_run(row.cron_expr, base=now))
                row.first_due_at = None
                row.attempt_count = 0
                row.offline_wait_count = 0
                row.occurrence_id = _new_id()
                row.result_summary = "missed while device was offline"
            else:
                row.status = _STATUS_FAILED
                row.enabled = False
                row.executed_at = now
                row.result_summary = "超过设备离线补发窗口，提醒已过期"
            n += 1
        if n:
            session.commit()
        return n
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def claim_due_tasks(
    *,
    limit: int = 10,
    lookback_minutes: float = 5.0,
    lease_seconds: float = 120.0,
) -> list[dict[str, Any]]:
    now = utcnow()
    # Policy-aware expiry is handled separately.  Keeping a lower-bound here
    # made ``deliver_when_online`` reminders permanently invisible after a
    # restart because their original due time was older than five minutes.
    _ = lookback_minutes
    session = get_session()
    try:
        rows = session.scalars(
            select(ScheduledTask)
            .where(
                ScheduledTask.enabled.is_(True),
                ScheduledTask.status == _STATUS_ACTIVE,
                ScheduledTask.next_run_at <= now,
            )
            .order_by(ScheduledTask.next_run_at.asc())
            .limit(max(1, min(int(limit), 50)))
        ).all()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            lease_token = uuid.uuid4().hex
            result = session.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == row.id,
                    ScheduledTask.status == _STATUS_ACTIVE,
                    ScheduledTask.enabled.is_(True),
                )
                .values(status=_STATUS_RUNNING)
                .values(
                    lease_expires_at=now
                    + timedelta(seconds=max(10.0, float(lease_seconds))),
                    lease_token=lease_token,
                    first_due_at=func.coalesce(
                        ScheduledTask.first_due_at, ScheduledTask.next_run_at
                    ),
                )
            )
            if result.rowcount:
                session.commit()
                session.refresh(row)
                claimed_row = _task_to_dict(row)
                # The fencing token is deliberately only returned by the
                # internal claim API.  It is not included by _task_to_dict(),
                # so list/get endpoints cannot disclose it.
                claimed_row["_lease_token"] = lease_token
                claimed.append(claimed_row)
            else:
                session.rollback()
        return claimed
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def recover_expired_running_tasks() -> int:
    """Return abandoned running tasks to the queue after their lease expires."""
    now = utcnow()
    session = get_session()
    try:
        expired_predicates = (
            ScheduledTask.status == _STATUS_RUNNING,
            ScheduledTask.enabled.is_(True),
            or_(
                ScheduledTask.lease_expires_at.is_(None),
                ScheduledTask.lease_expires_at <= now,
            ),
        )
        # The common idle tick has nothing to recover.  A read-only probe
        # avoids opening a SQLite write transaction (and contending with the
        # Web process's write lock) every two seconds.  The race with a lease
        # expiring right after the probe is benign: the next tick recovers it.
        has_expired = session.scalar(
            select(ScheduledTask.id).where(*expired_predicates).limit(1)
        )
        if has_expired is None:
            return 0
        result = session.execute(
            update(ScheduledTask)
            .where(*expired_predicates)
            .values(
                status=_STATUS_ACTIVE,
                next_run_at=now,
                lease_expires_at=None,
                lease_token=None,
                result_summary="previous execution lease expired; retrying",
            )
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def renew_scheduled_task_lease(
    task_id: str,
    *,
    lease_token: str,
    lease_seconds: float = 120.0,
) -> bool:
    """Extend a live execution lease if and only if its fencing token matches."""

    tid = str(task_id or "").strip()
    token = str(lease_token or "").strip()
    if not tid or not token:
        return False
    now = utcnow()
    session = get_session()
    try:
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == tid,
                ScheduledTask.status == _STATUS_RUNNING,
                ScheduledTask.enabled.is_(True),
                ScheduledTask.lease_token == token,
                ScheduledTask.lease_expires_at.is_not(None),
                ScheduledTask.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now
                + timedelta(seconds=max(10.0, float(lease_seconds)))
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def mark_scheduled_task_delivery_attempt(
    task_id: str,
    *,
    lease_token: str,
) -> bool:
    """Count a real online delivery attempt without counting offline probes."""

    tid = str(task_id or "").strip()
    token = str(lease_token or "").strip()
    if not tid or not token:
        return False
    session = get_session()
    try:
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == tid,
                ScheduledTask.status == _STATUS_RUNNING,
                ScheduledTask.enabled.is_(True),
                ScheduledTask.lease_token == token,
            )
            .values(
                attempt_count=ScheduledTask.attempt_count + 1,
                offline_wait_count=0,
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def defer_offline_scheduled_task(
    task_id: str,
    *,
    summary: str,
    lease_token: str,
    base_delay_seconds: float,
    max_delay_seconds: float,
    grace_seconds: float | None,
) -> bool:
    """Requeue an offline reminder using persisted exponential backoff.

    Offline probes have their own counter and therefore do not inflate real
    delivery attempts. The fencing token prevents an old worker from changing
    a lease recovered by another scheduler.
    """

    tid = str(task_id or "").strip()
    token = str(lease_token or "").strip()
    if not tid or not token:
        return False
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if (
            row is None
            or not row.enabled
            or row.status != _STATUS_RUNNING
            or str(row.lease_token or "") != token
        ):
            return False
        now = utcnow()
        due_at = as_utc(row.first_due_at or row.next_run_at)
        grace_expired = (
            grace_seconds is not None
            and now - due_at
            > timedelta(seconds=max(0.0, float(grace_seconds)))
        )
        criteria = (
            ScheduledTask.id == tid,
            ScheduledTask.status == _STATUS_RUNNING,
            ScheduledTask.enabled.is_(True),
            ScheduledTask.lease_token == token,
        )
        if grace_expired:
            if row.task_kind == _TASK_RECURRING:
                values: dict[str, Any] = {
                    "status": _STATUS_ACTIVE,
                    "enabled": True,
                    "next_run_at": as_utc(
                        compute_next_run(
                            row.cron_expr,
                            base=now + timedelta(seconds=1),
                        )
                    ),
                    "executed_at": now,
                    "first_due_at": None,
                    "attempt_count": 0,
                    "offline_wait_count": 0,
                    "occurrence_id": _new_id(),
                    "lease_expires_at": None,
                    "lease_token": None,
                    "result_summary": summary[:4000],
                }
            else:
                values = {
                    "status": _STATUS_FAILED,
                    "enabled": False,
                    "executed_at": now,
                    "offline_wait_count": 0,
                    "lease_expires_at": None,
                    "lease_token": None,
                    "result_summary": summary[:4000],
                }
            session.execute(update(ScheduledTask).where(*criteria).values(**values))
            session.commit()
            return False

        next_wait_count = int(row.offline_wait_count or 0) + 1
        base_delay = max(1.0, float(base_delay_seconds))
        max_delay = max(base_delay, float(max_delay_seconds))
        exponent = min(next_wait_count - 1, 10)
        delay_seconds = min(max_delay, base_delay * (2**exponent))
        result = session.execute(
            update(ScheduledTask)
            .where(*criteria)
            .values(
                status=_STATUS_ACTIVE,
                next_run_at=now + timedelta(seconds=delay_seconds),
                offline_wait_count=next_wait_count,
                lease_expires_at=None,
                lease_token=None,
                result_summary=summary[:4000],
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def defer_quiet_hours_scheduled_task(
    task_id: str,
    *,
    lease_token: str,
    summary: str,
    delay_seconds: float,
) -> bool:
    """Requeue a claimed reminder untouched until the quiet window ends.

    Unlike the offline/failure requeues this is not an error path: delivery is
    simply not allowed to speak yet.  ``first_due_at`` and the attempt/offline
    counters are reset so that once quiet hours end, the offline grace window
    starts counting from the deferred due time instead of the original
    (suppressed) one — otherwise a reminder deferred overnight would expire
    the moment the device happened to be offline at wake-up.
    """

    tid = str(task_id or "").strip()
    token = str(lease_token or "").strip()
    if not tid or not token:
        return False
    session = get_session()
    try:
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == tid,
                ScheduledTask.status == _STATUS_RUNNING,
                ScheduledTask.enabled.is_(True),
                ScheduledTask.lease_token == token,
            )
            .values(
                status=_STATUS_ACTIVE,
                next_run_at=utcnow()
                + timedelta(seconds=max(1.0, float(delay_seconds))),
                first_due_at=None,
                attempt_count=0,
                offline_wait_count=0,
                lease_expires_at=None,
                lease_token=None,
                result_summary=str(summary or "")[:4000],
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def wake_deliver_when_online_tasks() -> int:
    """Make backoff-delayed reminders due immediately after device reconnect."""

    from deskbot_server.device_preferences import load_preferences

    preferences = load_preferences()
    if preferences.get("offline_reminder_policy") != "deliver_when_online":
        return 0
    session = get_session()
    try:
        result = session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.enabled.is_(True),
                ScheduledTask.status == _STATUS_ACTIVE,
                ScheduledTask.offline_wait_count > 0,
            )
            .values(next_run_at=utcnow())
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def retry_scheduled_task(
    task_id: str,
    *,
    summary: str,
    lease_token: str | None = None,
    retry_delay_seconds: float = 15.0,
    grace_seconds: float | None = 300.0,
    max_attempts: int | None = None,
) -> bool:
    """Requeue a transiently failed task while it remains inside its grace window."""
    tid = str(task_id or "").strip()
    if not tid:
        return False
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if row is None or not row.enabled or row.status != _STATUS_RUNNING:
            return False
        current_token = str(row.lease_token or "").strip()
        supplied_token = str(lease_token or "").strip()
        if current_token != supplied_token:
            return False
        now = utcnow()
        due_at = as_utc(row.first_due_at or row.next_run_at)
        attempts = int(row.attempt_count or 0)
        attempt_limit_reached = (
            max_attempts is not None and attempts >= max(1, int(max_attempts))
        )
        grace_expired = (
            grace_seconds is not None
            and now - due_at
            > timedelta(seconds=max(0.0, float(grace_seconds)))
        )
        criteria = [
            ScheduledTask.id == tid,
            ScheduledTask.status == _STATUS_RUNNING,
            ScheduledTask.enabled.is_(True),
        ]
        if current_token:
            criteria.append(ScheduledTask.lease_token == current_token)
        else:
            criteria.append(ScheduledTask.lease_token.is_(None))

        if attempt_limit_reached or grace_expired:
            if row.task_kind == _TASK_RECURRING:
                values = {
                    "status": _STATUS_ACTIVE,
                    "enabled": True,
                    "next_run_at": as_utc(
                        compute_next_run(
                            row.cron_expr,
                            base=now + timedelta(seconds=1),
                        )
                    ),
                    "executed_at": now,
                    "first_due_at": None,
                    "attempt_count": 0,
                    "lease_expires_at": None,
                    "lease_token": None,
                    "result_summary": summary[:4000],
                }
            else:
                values = {
                    "status": _STATUS_FAILED,
                    "enabled": False,
                    "executed_at": now,
                    "lease_expires_at": None,
                    "lease_token": None,
                    "result_summary": summary[:4000],
                }
            session.execute(
                update(ScheduledTask).where(*criteria).values(**values)
            )
            session.commit()
            return False

        result = session.execute(
            update(ScheduledTask)
            .where(*criteria)
            .values(
                status=_STATUS_ACTIVE,
                next_run_at=now
                + timedelta(seconds=max(1.0, float(retry_delay_seconds))),
                lease_expires_at=None,
                lease_token=None,
                result_summary=summary[:4000],
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def finish_scheduled_task(
    task_id: str,
    *,
    ok: bool,
    summary: str | None = None,
    lease_token: str | None = None,
) -> bool:
    tid = str(task_id or "").strip()
    if not tid:
        return False
    session = get_session()
    try:
        row = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid))
        if row is None or row.status != _STATUS_RUNNING:
            return False
        current_token = str(row.lease_token or "").strip()
        supplied_token = str(lease_token or "").strip()
        if current_token != supplied_token:
            return False
        now = utcnow()
        criteria = [
            ScheduledTask.id == tid,
            ScheduledTask.status == _STATUS_RUNNING,
        ]
        if current_token:
            criteria.append(ScheduledTask.lease_token == current_token)
        else:
            criteria.append(ScheduledTask.lease_token.is_(None))
        values: dict[str, Any] = {
            "executed_at": now,
            "lease_expires_at": None,
            "lease_token": None,
            "result_summary": (summary or "")[:4000] or None,
        }
        if row.task_kind == _TASK_RECURRING and row.enabled and ok:
            values.update(
                status=_STATUS_ACTIVE,
                next_run_at=as_utc(
                    compute_next_run(
                        row.cron_expr,
                        base=now + timedelta(seconds=1),
                    )
                ),
                first_due_at=None,
                attempt_count=0,
                offline_wait_count=0,
                occurrence_id=_new_id(),
            )
        else:
            values.update(
                status=_STATUS_COMPLETED if ok else _STATUS_FAILED,
                enabled=False,
            )
        result = session.execute(
            update(ScheduledTask).where(*criteria).values(**values)
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _task_to_dict(row: ScheduledTask) -> dict[str, Any]:
    return {
        "id": row.id,
        "description": row.description,
        "cron": row.cron_expr,
        "cron_expr": row.cron_expr,
        "task_kind": row.task_kind,
        "enabled": bool(row.enabled),
        "next_run_at": format_cst(row.next_run_at),
        "session_id": row.session_id,
        "status": row.status,
        "result_summary": row.result_summary,
        "created_at": format_cst(row.created_at),
        "executed_at": format_cst(row.executed_at),
        "lease_expires_at": format_cst(row.lease_expires_at),
        "attempt_count": int(row.attempt_count or 0),
        "offline_wait_count": int(row.offline_wait_count or 0),
        "first_due_at": format_cst(row.first_due_at),
        "occurrence_id": row.occurrence_id,
    }
