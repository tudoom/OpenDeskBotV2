from __future__ import annotations

import io
import json
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from deskbot_server.device_preferences import load_preferences, update_preferences
from deskbot_server.scheduled_task_service import (
    create_scheduled_task,
    get_scheduled_task,
    retry_failed_scheduled_task,
    update_scheduled_task,
)
from deskbot_server.session_store import (
    clear_current_session,
    count_sessions,
    create_session,
    delete_session,
    list_current_sessions,
    list_recent_sessions,
    load_session,
    set_current_session,
)

bp = Blueprint("management", __name__)


def _payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return dict(payload) if isinstance(payload, dict) else {}


@bp.get("/app/api/preferences")
def preferences_get():
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "preferences": load_preferences(),
        }
    )


@bp.patch("/app/api/preferences")
def preferences_patch():
    body = _payload()
    patch = body.get("preferences")
    if patch is None:
        patch = {
            key: value
            for key, value in body.items()
            if key != "expected_revision"
        }
    if not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "preferences must be an object"}), 400
    expected = body.get("expected_revision")
    try:
        saved = update_preferences(
            patch,
            expected_revision=int(expected) if expected is not None else None,
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "preferences": saved,
            "revision": saved["revision"],
            "applied_revision": saved["revision"],
        }
    )


@bp.post("/app/api/scheduled-tasks")
def scheduled_task_create():
    body = _payload()
    try:
        task = create_scheduled_task(
            str(body.get("description") or body.get("title") or "").strip(),
            cron=str(body.get("cron") or body.get("cron_expr") or "").strip()
            or None,
            task_kind=str(body.get("task_kind") or "once"),
            run_at=body.get("run_at"),
            delay_seconds=body.get("delay_seconds"),
            delay_minutes=body.get("delay_minutes"),
            session_id=None,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "scope": "local", "task": task}), 201


@bp.patch("/app/api/scheduled-tasks/<task_id>")
def scheduled_task_patch(task_id: str):
    body = _payload()
    if get_scheduled_task(task_id) is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    try:
        task = update_scheduled_task(
            task_id,
            description=body.get("description") if "description" in body else None,
            cron=body.get("cron") if "cron" in body else body.get("cron_expr"),
            task_kind=body.get("task_kind") if "task_kind" in body else None,
            enabled=body.get("enabled") if "enabled" in body else None,
            run_at=body.get("run_at") if "run_at" in body else None,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "scope": "local", "task": task})


@bp.post("/app/api/scheduled-tasks/<task_id>/<action>")
def scheduled_task_action(task_id: str, action: str):
    try:
        if action == "pause":
            task = update_scheduled_task(task_id, enabled=False)
        elif action == "resume":
            task = update_scheduled_task(task_id, enabled=True)
        elif action == "retry":
            task = retry_failed_scheduled_task(task_id)
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    if task is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "scope": "local", "task": task})


@bp.get("/app/api/sessions")
def sessions_list():
    try:
        page = max(1, int(request.args.get("page") or 1))
        per_page = max(1, min(int(request.args.get("per_page") or 20), 100))
    except ValueError:
        return jsonify({"ok": False, "error": "分页参数无效"}), 400
    total = count_sessions()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = list_recent_sessions(
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    current_ids = {
        str(current.get("session_id") or "")
        for current in list_current_sessions()
        if current.get("session_id")
    }
    for row in rows:
        row["is_current"] = row["session_id"] in current_ids
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "sessions": rows,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "current_sessions": list_current_sessions(),
        }
    )


@bp.post("/app/api/sessions")
def sessions_create():
    body = _payload()
    try:
        session = create_session(title=str(body.get("title") or "新对话"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    session["is_current"] = True
    return jsonify({"ok": True, "scope": "local", "session": session}), 201


@bp.get("/app/api/sessions/<session_id>")
def sessions_get(session_id: str):
    session = load_session(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    current_ids = {
        str(current.get("session_id") or "")
        for current in list_current_sessions()
    }
    session["is_current"] = session["session_id"] in current_ids
    return jsonify({"ok": True, "scope": "local", "session": session})


@bp.post("/app/api/sessions/<session_id>/activate")
def sessions_activate(session_id: str):
    session = load_session(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    session = set_current_session(session_id)
    if session is not None:
        session["is_current"] = True
    return jsonify({"ok": True, "scope": "local", "session": session})


@bp.post("/app/api/sessions/clear-current")
def sessions_clear_current():
    return jsonify(
        {"ok": True, "scope": "local", "cleared": clear_current_session()}
    )


@bp.delete("/app/api/sessions/<session_id>")
def sessions_delete(session_id: str):
    if not delete_session(session_id):
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    return jsonify({"ok": True, "scope": "local"})


@bp.get("/app/api/sessions/<session_id>/export")
def sessions_export(session_id: str):
    session = load_session(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    encoded = json.dumps(session, ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        io.BytesIO(encoded),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"deskbot-session-{session_id}.json",
        max_age=0,
    )
