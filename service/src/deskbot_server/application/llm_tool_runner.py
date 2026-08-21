"""执行 LLM JSON 中的 ``tools`` 指令。"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from deskbot_server.application.face_registration import register_face_for_device
from deskbot_server.core.clock import as_utc, utcnow
from deskbot_server.device_tmp_store import read_local_tmp_file, write_local_tmp_file
from deskbot_server.memory_store import add_memory, delete_memory
from deskbot_server.miot_tools import execute_miot_tool
from deskbot_server.scheduled_task_service import execute_schedule_task_tool
from deskbot_server.session_store import execute_session_tool
from deskbot_server.web_tools import webfetch, websearch

logger = logging.getLogger("deskbot-server")

_CONFIRMATION_TTL_SECONDS = 120
_OPERATION_RUNNING_TTL_SECONDS = 300
_TOOL_ROUND_LEDGER_NAME = "__llm_tool_round__"

_IDEMPOTENT_SIDE_EFFECT_TOOLS = frozenset(
    {
        "register_face",
        "memory_add",
        "memory_delete",
        "schedule_task",
        "scheduled_task",
        "session",
        "miot",
        "mihome",
        "mijia",
        "write",
    }
)

_MIOT_READ_ACTIONS = frozenset(
    {
        "status",
        "auth_status",
        "sync",
        "refresh",
        "sync_homes",
        "list",
        "devices",
        "scenes",
        "list_scenes",
        "get",
        "device",
        "spec",
        "props",
        "get_props",
        "properties",
    }
)


def _requires_explicit_confirmation(tool: str, raw: dict[str, Any]) -> bool:
    """Return whether a tool can destroy data or affect the physical world."""
    if tool == "register_face":
        return True
    if tool == "memory_delete":
        return True
    if tool == "write":
        return True
    if tool == "session":
        action = str(raw.get("action") or raw.get("op") or "").strip().lower()
        return action in {"delete", "remove", "clear", "reset"}
    if tool in {"schedule_task", "scheduled_task"}:
        action = str(raw.get("action") or raw.get("op") or "create").strip().lower()
        return action in {"delete", "remove", "del"}
    if tool in {"miot", "mihome", "mijia"}:
        action = str(raw.get("action") or raw.get("op") or "list").strip().lower()
        return action not in _MIOT_READ_ACTIONS
    return False


def _confirmation_payload_hash(tool: str, raw: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in raw.items()
        if key not in {"operation_id", "confirmation_id"}
    }
    payload["tool"] = tool
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _issue_tool_confirmation(
    tool: str,
    raw: dict[str, Any],
) -> str:
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolConfirmation, _new_id

    now = utcnow()
    payload_hash = _confirmation_payload_hash(tool, raw)
    session = get_session()
    try:
        session.execute(
            delete(ToolConfirmation).where(
                ToolConfirmation.expires_at <= now,
            )
        )
        existing = session.scalar(
            select(ToolConfirmation)
            .where(
                ToolConfirmation.tool_name == tool,
                ToolConfirmation.payload_hash == payload_hash,
                ToolConfirmation.consumed_at.is_(None),
                ToolConfirmation.expires_at > now,
            )
            .order_by(ToolConfirmation.created_at.desc())
        )
        if existing is not None:
            session.commit()
            return existing.id
        confirmation_id = _new_id()
        session.add(
            ToolConfirmation(
                id=confirmation_id,
                tool_name=tool,
                payload_hash=payload_hash,
                expires_at=now + timedelta(seconds=_CONFIRMATION_TTL_SECONDS),
            )
        )
        session.commit()
        return confirmation_id
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _consume_tool_confirmation(
    tool: str,
    raw: dict[str, Any],
) -> str | None:
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolConfirmation

    now = utcnow()
    payload_hash = _confirmation_payload_hash(tool, raw)
    requested_id = str(raw.get("confirmation_id") or "").strip()
    session = get_session()
    try:
        query = select(ToolConfirmation).where(
            ToolConfirmation.tool_name == tool,
            ToolConfirmation.payload_hash == payload_hash,
            ToolConfirmation.expires_at > now,
        )
        if requested_id:
            query = query.where(ToolConfirmation.id == requested_id)
        else:
            query = (
                query.where(ToolConfirmation.consumed_at.is_(None))
                .order_by(ToolConfirmation.created_at.desc())
            )
        row = session.scalar(query)
        if row is None:
            return None
        # An explicit retransmission of the same confirmation is allowed to
        # reach the operation ledger.  The confirmation-derived operation_id
        # then returns the cached/unknown outcome and never repeats the effect.
        if requested_id and row.consumed_at is not None:
            return row.id
        result = session.execute(
            update(ToolConfirmation)
            .where(
                ToolConfirmation.id == row.id,
                ToolConfirmation.consumed_at.is_(None),
                ToolConfirmation.expires_at > now,
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return row.id if result.rowcount else None
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _operation_unknown_result(
    tool: str,
    operation_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "ok": False,
        "operation_id": operation_id,
        "operation_status": "unknown",
        "reconciliation_required": True,
        "retry_safe": False,
        "side_effect_may_have_succeeded": True,
        "error": reason,
        "idempotent_replay": True,
    }


def _operation_conflict_result(
    tool: str,
    operation_id: str,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "ok": False,
        "operation_id": operation_id,
        "operation_status": "idempotency_conflict",
        "reconciliation_required": True,
        "retry_safe": False,
        "error": (
            "operation_id is already bound to another payload; "
            "the operation was not executed"
        ),
        "idempotent_replay": True,
    }


def _claim_tool_operation(
    tool: str,
    operation_id: str,
    *,
    payload_hash: str | None = None,
    create_if_missing: bool = True,
) -> tuple[bool, dict[str, Any] | None]:
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation, _new_id

    def existing_result(existing: ToolOperation) -> dict[str, Any]:
        if (
            existing.payload_hash
            and payload_hash
            and existing.payload_hash != payload_hash
        ):
            return _operation_conflict_result(tool, operation_id)
        if existing.result_json:
            try:
                cached = dict(json.loads(existing.result_json))
                cached["idempotent_replay"] = True
                return cached
            except (TypeError, ValueError):
                pass
        created_at = as_utc(existing.created_at)
        now = utcnow()
        if (
            existing.status == "unknown"
            or now - created_at
            >= timedelta(seconds=_OPERATION_RUNNING_TTL_SECONDS)
        ):
            unknown = _operation_unknown_result(
                tool,
                operation_id,
                reason=(
                    "The previous worker stopped before recording the outcome. "
                    "Reconcile the provider or target state; automatic replay "
                    "is disabled."
                ),
            )
            existing.status = "unknown"
            existing.result_json = json.dumps(unknown, ensure_ascii=False)
            existing.completed_at = now
            return unknown
        return {
            "tool": tool,
            "ok": False,
            "operation_id": operation_id,
            "operation_status": "running",
            "reconciliation_required": False,
            "retry_safe": False,
            "error": "operation is already in progress",
            "idempotent_replay": True,
        }

    session = get_session()
    try:
        query = select(ToolOperation).where(
            ToolOperation.tool_name == tool,
            ToolOperation.operation_id == operation_id,
        )
        existing = session.scalar(query)
        if existing is not None:
            result = existing_result(existing)
            if existing.status == "unknown" and session.is_modified(existing):
                session.commit()
            return False, result
        if not create_if_missing:
            return False, None
        session.add(
            ToolOperation(
                id=_new_id(),
                tool_name=tool,
                operation_id=operation_id,
                payload_hash=payload_hash,
                status="running",
            )
        )
        try:
            session.commit()
            return True, None
        except IntegrityError:
            session.rollback()
            existing = session.scalar(query)
            if existing is None:
                return False, _operation_unknown_result(
                    tool,
                    operation_id,
                    reason=(
                        "The operation ledger conflicted but the existing row "
                        "could not be loaded; manual reconciliation is required."
                    ),
                )
            result = existing_result(existing)
            if existing.status == "unknown" and session.is_modified(existing):
                session.commit()
            return False, result
    finally:
        session.close()


def _finish_tool_operation(
    tool: str,
    operation_id: str,
    result: dict[str, Any],
) -> None:
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation

    session = get_session()
    try:
        row = session.scalar(
            select(ToolOperation).where(
                ToolOperation.tool_name == tool,
                ToolOperation.operation_id == operation_id,
            )
        )
        if row is None:
            return
        row.status = "completed" if result.get("ok") else "failed"
        row.result_json = json.dumps(result, ensure_ascii=False, default=str)
        row.completed_at = utcnow()
        session.commit()
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _mark_tool_operation_unknown(
    tool: str,
    operation_id: str,
    *,
    reason: str,
) -> None:
    """Persist a conservative terminal state after ledger finalisation fails."""
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation

    unknown = _operation_unknown_result(tool, operation_id, reason=reason)
    session = get_session()
    try:
        row = session.scalar(
            select(ToolOperation).where(
                ToolOperation.tool_name == tool,
                ToolOperation.operation_id == operation_id,
            )
        )
        if row is None or row.status != "running":
            return
        row.status = "unknown"
        row.result_json = json.dumps(unknown, ensure_ascii=False)
        row.completed_at = utcnow()
        session.commit()
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def _tool_round_payload_hash(tools: list[dict[str, Any]]) -> str:
    """Bind one client request/round to the exact model-authored tool plan."""

    plan = [
        {
            key: value
            for key, value in dict(raw).items()
            if key != "operation_id"
        }
        for raw in tools
        if isinstance(raw, dict)
    ]
    canonical = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_llm_tool_round(
    *,
    request_id: str,
    round_index: int,
    tools: list[dict[str, Any]],
) -> tuple[str, bool, list[dict[str, Any]] | None]:
    """Claim a deterministic request slot before any tool can run.

    The row is created even for a final empty-tool reply.  Consequently, a
    retry using the same client idempotency key cannot turn a previously
    harmless round into a new side effect merely because the LLM sampled a
    different payload.
    """

    req = str(request_id or "").strip()
    operation_id = f"turn:{req}:round:{max(0, int(round_index))}"[:128]
    claimed, cached = _claim_tool_operation(
        _TOOL_ROUND_LEDGER_NAME,
        operation_id,
        payload_hash=_tool_round_payload_hash(tools),
    )
    if claimed:
        return operation_id, True, None
    if cached is None:
        return operation_id, False, [
            _operation_unknown_result(
                _TOOL_ROUND_LEDGER_NAME,
                operation_id,
                reason="tool-round ledger could not be loaded",
            )
        ]
    cached_results = cached.get("tool_results")
    if isinstance(cached_results, list):
        replayed: list[dict[str, Any]] = []
        for raw in cached_results:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["idempotent_replay"] = True
            replayed.append(row)
        return operation_id, False, replayed
    return operation_id, False, [dict(cached)]


def finish_llm_tool_round(
    *,
    operation_id: str,
    tool_results: list[dict[str, Any]],
) -> None:
    result = {
        "tool": _TOOL_ROUND_LEDGER_NAME,
        "ok": all(bool(row.get("ok")) for row in tool_results),
        "tool_results": tool_results,
    }
    _finish_tool_operation(
        _TOOL_ROUND_LEDGER_NAME,
        operation_id,
        result,
    )


def mark_llm_tool_round_unknown(
    *,
    operation_id: str,
    reason: str,
) -> None:
    _mark_tool_operation_unknown(
        _TOOL_ROUND_LEDGER_NAME,
        operation_id,
        reason=reason,
    )



def execute_llm_tools(
    tools: list[dict[str, Any]],
    *,
    device_id: Optional[str] = None,
    user_confirmed: bool = False,
) -> list[dict[str, Any]]:
    """逐条执行工具，返回结果摘要（供日志与 pipeline 事件）。"""
    results: list[dict[str, Any]] = []
    dev = str(device_id or "").strip()
    confirmation_available = bool(user_confirmed)
    for raw in tools or []:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if not tool:
            continue
        operation_id = str(raw.get("operation_id") or "").strip()
        operation_claimed = False
        result_start = len(results)
        requires_confirmation = _requires_explicit_confirmation(tool, raw)
        confirmed_for_operation = False
        confirmation_id: str | None = None
        if requires_confirmation and confirmation_available:
            requested_confirmation_id = str(
                raw.get("confirmation_id") or ""
            ).strip()
            if requested_confirmation_id:
                replay_operation_id = (
                    f"confirm:{requested_confirmation_id}"[:128]
                )
                _claimed, replay = _claim_tool_operation(
                    tool,
                    replay_operation_id,
                    payload_hash=_confirmation_payload_hash(tool, raw),
                    create_if_missing=False,
                )
                if replay is not None:
                    confirmation_available = False
                    results.append(replay)
                    continue
            confirmation_id = _consume_tool_confirmation(tool, raw)
            if confirmation_id:
                confirmed_for_operation = True
                confirmation_available = False
                operation_id = f"confirm:{confirmation_id}"[:128]
                raw = dict(raw)
                raw["operation_id"] = operation_id
                raw["confirmation_id"] = confirmation_id
        pending_confirmation_id = None
        if requires_confirmation and not confirmed_for_operation:
            pending_confirmation_id = _issue_tool_confirmation(
                tool,
                raw,
            )
        if requires_confirmation and not confirmed_for_operation:
            results.append(
                {
                    "tool": tool,
                    "ok": False,
                    "confirmation_required": True,
                    "confirmation_id": pending_confirmation_id,
                    "confirmation_expires_in_seconds": _CONFIRMATION_TTL_SECONDS,
                    "error": "此操作会修改数据或实体设备，需要用户明确确认后才能执行",
                    "operation_id": operation_id or None,
                }
            )
            continue
        if tool in _IDEMPOTENT_SIDE_EFFECT_TOOLS and operation_id:
            operation_claimed, cached = _claim_tool_operation(
                tool,
                operation_id,
                payload_hash=_confirmation_payload_hash(tool, raw),
            )
            if not operation_claimed:
                results.append(cached or {"tool": tool, "ok": False})
                continue
        try:
            if tool == "register_face":
                name = str(raw.get("name") or raw.get("person_name") or "").strip()
                fid_raw = raw.get("face_id")
                face_id = int(fid_raw) if fid_raw is not None else None
                out = register_face_for_device(dev, name, face_id=face_id)
                results.append(
                    {
                        "tool": tool,
                        "ok": True,
                        "person_id": out["profile"].get("person_id"),
                        "name": out["profile"].get("name"),
                        "face_id": out.get("face_id"),
                    }
                )
            elif tool == "memory_add":
                text = str(raw.get("text") or raw.get("value") or "").strip()
                if not text:
                    raise ValueError("memory_add 需要 text")
                entry = add_memory(text)
                results.append({"tool": tool, "ok": True, "id": entry["id"], "text": entry["text"]})
            elif tool == "memory_delete":
                eid = str(raw.get("id") or "").strip()
                if not eid:
                    raise ValueError("memory_delete 需要 id")
                ok = delete_memory(eid)
                if not ok:
                    raise ValueError(f"未找到记忆 id={eid}")
                results.append({"tool": tool, "ok": True, "id": eid})
            elif tool in ("schedule_task", "scheduled_task"):
                out = execute_schedule_task_tool(raw)
                results.append(out)
            elif tool == "session":
                out = execute_session_tool(raw)
                results.append(out)
            elif tool in ("miot", "mihome", "mijia"):
                out = execute_miot_tool(raw)
                results.append(out)
            elif tool == "webfetch":
                url = str(raw.get("url") or "").strip()
                out = webfetch(url)
                results.append({"tool": tool, **out})
            elif tool == "websearch":
                query = str(raw.get("query") or raw.get("q") or "").strip()
                max_results = raw.get("max_results") or raw.get("limit") or 5
                out = websearch(query, max_results=int(max_results))
                results.append({"tool": tool, **out})
            elif tool == "read":
                path = str(raw.get("path") or raw.get("file") or "").strip()
                out = read_local_tmp_file(path)
                results.append({"tool": tool, "ok": True, **out})
            elif tool == "write":
                path = str(raw.get("path") or raw.get("file") or "").strip()
                content = str(raw.get("content") or raw.get("text") or raw.get("data") or "")
                out = write_local_tmp_file(path, content)
                results.append({"tool": tool, "ok": True, **out})
            else:
                results.append({"tool": tool, "ok": False, "error": f"未知工具: {tool}"})
        except Exception as exc:
            logger.warning("[LLM tools] %s 失败: %s", tool, exc)
            results.append({"tool": tool, "ok": False, "error": str(exc)})
        finally:
            if operation_claimed and len(results) > result_start:
                result = results[-1]
                result.setdefault("operation_id", operation_id)
                try:
                    _finish_tool_operation(tool, operation_id, result)
                except Exception:
                    reason = (
                        "The side effect returned but its durable outcome could "
                        "not be recorded. Automatic replay is disabled; reconcile "
                        "the provider or target state."
                    )
                    logger.exception(
                        "[LLM tools] ledger finalisation failed "
                        "device_id=%s tool=%s operation_id=%s",
                        dev,
                        tool,
                        operation_id,
                    )
                    result.update(
                        ok=False,
                        operation_status="unknown",
                        reconciliation_required=True,
                        retry_safe=False,
                        side_effect_may_have_succeeded=True,
                        error=reason,
                    )
                    try:
                        _mark_tool_operation_unknown(
                            tool,
                            operation_id,
                            reason=reason,
                        )
                    except Exception:
                        logger.exception(
                            "[LLM tools] failed to persist unknown operation "
                            "device_id=%s tool=%s operation_id=%s",
                            dev,
                            tool,
                            operation_id,
                        )
    return results
