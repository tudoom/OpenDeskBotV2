"""PC-local conversation history stored under ``data/local/session``.

There is one conversation namespace per PC. Hardware IDs are transport
addresses and never select, filter, or own conversation data.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from deskbot_server.atomic_store import atomic_write_json, file_lock
from deskbot_server.device_data import local_data_dir

SESSION_IDLE_SECONDS = 10 * 60
_MAX_HISTORY_TURNS = 30
_MAX_HISTORY_CHARS = 32_000
_MAX_MESSAGES_PER_SESSION = 200
_MAX_MESSAGE_CHARS = 16_000
_MAX_SESSIONS = 100
_SESSION_RETENTION_SECONDS = 90 * 24 * 60 * 60
_MAX_TITLE_LEN = 48
_META_FILENAME = "_meta.json"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VALID_DELIVERY_STATUSES = frozenset(
    {
        "generated",
        "synthesized",
        "enqueued",
        "accepted",
        "played",
        "failed",
        "cancelled",
        "not_requested",
    }
)


def _session_root() -> Path:
    return local_data_dir() / "session"


def _normalise_session_id(session_id: object) -> str:
    sid = str(session_id or "").strip()
    if not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("invalid session_id")
    return sid


def _session_path(session_id: object) -> Path:
    return _session_root() / f"{_normalise_session_id(session_id)}.json"


def _meta_path() -> Path:
    return _session_root() / _META_FILENAME


def _now_ts() -> float:
    return time.time()


def _truncate_title(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return "新对话"
    if len(raw) <= _MAX_TITLE_LEN:
        return raw
    return raw[: _MAX_TITLE_LEN - 1] + "…"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        atomic_write_json(path, data)


def _load_meta() -> dict[str, Any]:
    return _load_json(_meta_path()) or {}


def _save_meta(*, session_id: str | None, updated_at: float) -> None:
    path = _meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        payload: dict[str, Any] = {}
        if session_id:
            payload = {
                "current_session_id": _normalise_session_id(session_id),
                "current_updated_at": float(updated_at),
            }
        atomic_write_json(path, payload)


def _normalize_message(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    role = str(raw.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    message = str(raw.get("message") or raw.get("content") or "").strip()
    if not message:
        return None
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[: _MAX_MESSAGE_CHARS - 1] + "…"
    try:
        timestamp = float(raw.get("ts"))
    except (TypeError, ValueError):
        timestamp = _now_ts()
    out: dict[str, Any] = {"role": role, "message": message, "ts": timestamp}
    if role == "assistant":
        delivery_status = str(raw.get("delivery_status") or "").strip().lower()
        if delivery_status in _VALID_DELIVERY_STATUSES:
            out["delivery_status"] = delivery_status
        request_id = str(raw.get("request_id") or "").strip()
        if request_id:
            out["request_id"] = request_id[:128]
        delivery_error = str(raw.get("delivery_error") or "").strip()
        if delivery_error:
            out["delivery_error"] = delivery_error[:1000]
    return out


def load_session(session_id: str) -> dict[str, Any] | None:
    sid = _normalise_session_id(session_id)
    raw = _load_json(_session_path(sid))
    if not raw:
        return None
    messages = [
        normalised
        for item in raw.get("messages") or []
        if (normalised := _normalize_message(item)) is not None
    ]
    return {
        "session_id": sid,
        "title": str(raw.get("title") or "新对话"),
        "created_at": float(raw.get("created_at") or 0),
        "updated_at": float(raw.get("updated_at") or 0),
        "messages": messages[-_MAX_MESSAGES_PER_SESSION:],
    }


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    sid = _normalise_session_id(session.get("session_id"))
    messages = [
        normalised
        for item in list(session.get("messages") or [])
        if (normalised := _normalize_message(item)) is not None
    ]
    return {
        "session_id": sid,
        "title": _truncate_title(str(session.get("title") or "")),
        "created_at": float(session.get("created_at") or _now_ts()),
        "updated_at": float(session.get("updated_at") or _now_ts()),
        "messages": messages[-_MAX_MESSAGES_PER_SESSION:],
    }


def save_session(session: dict[str, Any], *, make_current: bool = True) -> None:
    payload = _session_payload(session)
    _save_json(_session_path(payload["session_id"]), payload)
    if make_current:
        _save_meta(
            session_id=payload["session_id"],
            updated_at=payload["updated_at"],
        )


def create_session(
    *,
    title: str | None = None,
    now: float | None = None,
    make_current: bool = True,
) -> dict[str, Any]:
    timestamp = float(now if now is not None else _now_ts())
    session = {
        "session_id": uuid.uuid4().hex[:16],
        "title": _truncate_title(title or ""),
        "created_at": timestamp,
        "updated_at": timestamp,
        "messages": [],
    }
    save_session(session, make_current=make_current)
    prune_sessions(now=timestamp)
    return session


def _session_is_idle(session: dict[str, Any], *, now: float) -> bool:
    updated_at = float(session.get("updated_at") or 0)
    return updated_at <= 0 or now - updated_at > SESSION_IDLE_SECONDS


def ensure_active_session(
    *,
    user_text: str | None = None,
    now: float | None = None,
    make_current: bool = True,
) -> dict[str, Any]:
    """Return the PC's active conversation, rotating it after idle timeout."""

    timestamp = float(now if now is not None else _now_ts())
    session = get_current_session()
    if session is None or _session_is_idle(session, now=timestamp):
        return create_session(
            title=_truncate_title(user_text or "") if user_text else "新对话",
            now=timestamp,
            make_current=make_current,
        )
    return session


def session_history_for_llm(
    session_id: str,
    *,
    max_turns: int = _MAX_HISTORY_TURNS,
) -> list[dict[str, str]]:
    session = load_session(session_id)
    if not session:
        return []
    rows = list(session.get("messages") or [])
    cap = max(0, int(max_turns)) * 2
    if cap:
        rows = rows[-cap:]
    output_reversed: list[dict[str, str]] = []
    used_chars = 0
    for row in reversed(rows):
        role = str(row.get("role") or "").strip()
        message = str(row.get("message") or "").strip()
        if (
            role == "assistant"
            and row.get("delivery_status")
            and row.get("delivery_status") != "played"
        ):
            continue
        if role not in {"user", "assistant"} or not message:
            continue
        remaining = _MAX_HISTORY_CHARS - used_chars
        if remaining <= 0:
            break
        if len(message) > remaining:
            message = message[-remaining:]
        output_reversed.append({"role": role, "content": message})
        used_chars += len(message)
    return list(reversed(output_reversed))


def append_turn(
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    now: float | None = None,
    make_current: bool = True,
    request_id: str | None = None,
    assistant_delivery_status: str = "played",
) -> dict[str, Any]:
    sid = _normalise_session_id(session_id)
    timestamp = float(now if now is not None else _now_ts())
    path = _session_path(sid)
    with file_lock(path):
        session = load_session(sid)
        if session is None:
            raise FileNotFoundError(f"session not found: {sid}")
        user_message = str(user_text or "").strip()
        assistant_message = str(assistant_text or "").strip()
        if user_message:
            session["messages"].append(
                {"role": "user", "message": user_message, "ts": timestamp}
            )
        if assistant_message:
            delivery = str(assistant_delivery_status or "played").strip().lower()
            if delivery not in _VALID_DELIVERY_STATUSES:
                raise ValueError(f"unsupported delivery status: {delivery}")
            assistant_row: dict[str, Any] = {
                "role": "assistant",
                "message": assistant_message,
                "ts": timestamp,
                "delivery_status": delivery,
            }
            request = str(request_id or "").strip()
            if request:
                assistant_row["request_id"] = request[:128]
            session["messages"].append(assistant_row)
        session["updated_at"] = timestamp
        if len(session["messages"]) <= 2 and user_message:
            session["title"] = _truncate_title(user_message)
        payload = _session_payload(session)
        atomic_write_json(path, payload)
    if make_current:
        _save_meta(session_id=sid, updated_at=timestamp)
    return payload


def update_assistant_delivery(
    session_id: str,
    request_id: str,
    delivery_status: str,
    *,
    error: str | None = None,
    now: float | None = None,
) -> bool:
    try:
        sid = _normalise_session_id(session_id)
    except ValueError:
        return False
    request = str(request_id or "").strip()
    status = str(delivery_status or "").strip().lower()
    if not request:
        return False
    if status not in _VALID_DELIVERY_STATUSES:
        raise ValueError(f"unsupported delivery status: {status}")
    path = _session_path(sid)
    with file_lock(path):
        session = load_session(sid)
        if session is None:
            return False
        for row in reversed(session.get("messages") or []):
            if row.get("role") != "assistant" or row.get("request_id") != request:
                continue
            row["delivery_status"] = status
            if error:
                row["delivery_error"] = str(error)[:1000]
            else:
                row.pop("delivery_error", None)
            session["updated_at"] = float(now if now is not None else _now_ts())
            atomic_write_json(path, _session_payload(session))
            return True
    return False


def list_recent_sessions(
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[dict[str, Any]]:
    root = _session_root()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        if path.name == _META_FILENAME:
            continue
        try:
            session = load_session(path.stem)
        except ValueError:
            continue
        if session:
            rows.append(_session_summary(session, include_messages=False))
    rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    start = max(0, int(offset))
    cap = max(1, min(int(limit), 100))
    return rows[start : start + cap]


def get_current_session() -> dict[str, Any] | None:
    meta = _load_meta()
    sid = str(meta.get("current_session_id") or "").strip()
    if not sid:
        return None
    try:
        session = load_session(sid)
    except ValueError:
        return None
    return session


def list_current_sessions(*, now: float | None = None) -> list[dict[str, Any]]:
    session = get_current_session()
    if session is None:
        return []
    timestamp = float(now if now is not None else _now_ts())
    if _session_is_idle(session, now=timestamp):
        return []
    return [_session_summary(session, include_messages=False)]


def set_current_session(session_id: str) -> dict[str, Any] | None:
    session = load_session(session_id)
    if session is None:
        return None
    _save_meta(
        session_id=session["session_id"],
        updated_at=float(session.get("updated_at") or _now_ts()),
    )
    return session


def clear_current_session() -> bool:
    path = _meta_path()
    with file_lock(path):
        current = _load_json(path) or {}
        changed = bool(str(current.get("current_session_id") or "").strip())
        if changed:
            atomic_write_json(path, {})
        return changed


def delete_session(session_id: str) -> bool:
    sid = _normalise_session_id(session_id)
    path = _session_path(sid)
    with file_lock(path):
        if load_session(sid) is None:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    meta = _load_meta()
    if str(meta.get("current_session_id") or "").strip() == sid:
        _save_meta(session_id=None, updated_at=_now_ts())
    return True


def count_sessions() -> int:
    root = _session_root()
    if not root.is_dir():
        return 0
    return sum(
        1
        for path in root.glob("*.json")
        if path.name != _META_FILENAME and path.is_file()
    )


def prune_sessions(
    *,
    now: float | None = None,
    max_sessions: int = _MAX_SESSIONS,
    retention_seconds: float = _SESSION_RETENTION_SECONDS,
) -> int:
    root = _session_root()
    if not root.is_dir():
        return 0
    timestamp = float(now if now is not None else _now_ts())
    current = get_current_session()
    current_id = str((current or {}).get("session_id") or "")
    rows: list[tuple[float, str]] = []
    for path in root.glob("*.json"):
        if path.name == _META_FILENAME:
            continue
        try:
            session = load_session(path.stem)
        except ValueError:
            session = None
        rows.append(
            (
                float((session or {}).get("updated_at") or 0),
                path.stem,
            )
        )
    rows.sort(reverse=True)
    keep_count = max(1, int(max_sessions))
    removed = 0
    for index, (updated_at, sid) in enumerate(rows):
        expired = updated_at <= 0 or timestamp - updated_at > max(
            60.0, float(retention_seconds)
        )
        over_count = index >= keep_count
        if sid == current_id or (not expired and not over_count):
            continue
        path = _session_path(sid)
        with file_lock(path):
            latest = load_session(sid)
            latest_updated = float((latest or {}).get("updated_at") or 0)
            still_expired = latest_updated <= 0 or timestamp - latest_updated > max(
                60.0, float(retention_seconds)
            )
            if not over_count and not still_expired:
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def execute_session_tool(raw: dict[str, Any]) -> dict[str, Any]:
    action = str(raw.get("action") or raw.get("op") or "current").strip().lower()
    if action in {"current", "now", "active"}:
        session = get_current_session()
        return {
            "tool": "session",
            "action": "current",
            "ok": True,
            "session": (
                _session_summary(session, include_messages=True)
                if session is not None
                else None
            ),
        }
    if action in {"list", "ls", "recent"}:
        sessions = list_recent_sessions(limit=int(raw.get("limit") or raw.get("max") or 10))
        return {
            "tool": "session",
            "action": "list",
            "ok": True,
            "sessions": sessions,
            "count": len(sessions),
        }
    session_id = str(raw.get("session_id") or raw.get("id") or "").strip()
    if action in {"get", "read", "query"}:
        session = load_session(session_id) if session_id else get_current_session()
        if session_id and session is None:
            raise ValueError(f"session not found: {session_id}")
        return {
            "tool": "session",
            "action": "get",
            "ok": True,
            "session": (
                _session_summary(session, include_messages=True)
                if session is not None
                else None
            ),
        }
    raise ValueError(f"unknown session action: {action}")


def _session_summary(
    session: dict[str, Any],
    *,
    include_messages: bool,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "session_id": session.get("session_id"),
        "title": session.get("title"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(session.get("messages") or []),
    }
    if include_messages:
        output["messages"] = list(session.get("messages") or [])
    return output
