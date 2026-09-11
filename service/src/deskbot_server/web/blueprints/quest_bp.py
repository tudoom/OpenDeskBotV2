"""主动陪伴控制台：页面 ``/quest`` 与 ``/api/quest/*`` JSON API。

页面只有三层：总开关 + 当前场景、小目标清单（一条线按顺序排布，每个带状态小标，可拖动排序）、
小目标编辑抽屉。概念折叠在 ``application/quest_console``：没有绑定、没有沙箱、没有分数、没有连线，
顺序就是编排，不论达成与否都进入下一个。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Blueprint, Response, jsonify, render_template, request

from deskbot_server.application import quest_console as qc
from deskbot_server.application import quest_service
from deskbot_server.application.persona_generation import GenerationError
from deskbot_server.application.quest_service import QuestError
from deskbot_server.auth.api_key_service import read_free_api_key_raw
from deskbot_server.quest_playbooks_store import ensure_default_playbook
from deskbot_server.web.helpers import deskbot_upstream_base

bp = Blueprint("quest", __name__)
logger = logging.getLogger("deskbot-server")

_CORE_STATUS_TIMEOUT = 1.5
_CORE_SPEAK_TIMEOUT = 6.0


def _payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return dict(payload) if isinstance(payload, dict) else {}


def _err(exc: Exception, status: int = 400):
    return jsonify({"ok": False, "error": str(exc)}), status


# ── Core 进程（另一个进程）里的主动推进循环 ────────────────────


def _core_call(
    method: str, path: str, *, timeout: float, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    """调用 Core 的 loopback HTTP 路由；连不上 → (0, {})。"""
    try:
        url = deskbot_upstream_base().rstrip("/") + path
    except Exception:  # noqa: BLE001
        return 0, {}
    headers = {"Accept": "application/json"}
    key = read_free_api_key_raw()
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urlerror.HTTPError as exc:
        status, body = exc.code, exc.read()
    except Exception:  # noqa: BLE001 —— Core 没起 / 端口未就绪，页面照常可编辑
        return 0, {}
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _core_snapshot() -> dict[str, Any] | None:
    status, data = _core_call("GET", "/api/quest_proactive", timeout=_CORE_STATUS_TIMEOUT)
    if status != 200 or not data.get("ok") or not data.get("running"):
        return None
    snap = data.get("snapshot")
    return dict(snap) if isinstance(snap, dict) else None


def _core_live() -> dict[str, Any] | None:
    """待机张望状态也在 Core 进程里（block_reason / 次数 / 下一次时刻）。"""
    status, data = _core_call("GET", "/api/live_behavior", timeout=_CORE_STATUS_TIMEOUT)
    if status != 200 or not data.get("ok"):
        return None
    live = data.get("live")
    return dict(live) if isinstance(live, dict) else None


def _overview() -> dict[str, Any]:
    return qc.overview(_core_snapshot(), live=_core_live())


def _ok(**extra: Any):
    return jsonify({"ok": True, **extra, **_overview()})


@bp.get("/quest")
def page():
    ensure_default_playbook()
    return render_template("app2c/quest.html", active_nav="quest")


# ── 概览 / 设置 ───────────────────────────────────────────────


@bp.get("/api/quest/overview")
def api_overview():
    try:
        return _ok()
    except QuestError as exc:
        return _err(exc)


@bp.put("/api/quest/settings")
def api_settings():
    """PUT {enabled?, playbook?, idle_sec?, daily_limit?, idle_live?, idle_motion?, wander_per_min?, wander_idle_sec?, …}"""
    try:
        qc.save_settings(_payload())
        return _ok()
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/speak_now")
def api_speak_now():
    """现在就让小歪按剧场说一句（跳过冷场 / 人脸 / 每日上限）：转给 Core 进程的循环。"""
    return _speak(None)


@bp.post("/api/quest/tasks/<task_id>/speak")
def api_task_speak(task_id: str):
    """「触发一次」：就这个进行中的小目标立刻开口。"""
    return _speak(task_id)


def _speak(task_id: str | None):
    body = {"task_id": task_id} if task_id else {}
    status, data = _core_call("POST", "/api/quest_proactive/speak_now", timeout=_CORE_SPEAK_TIMEOUT, body=body)
    if status == 0:
        return jsonify({"ok": False, "error": "小歪服务没有运行，稍后再试", "reason": "core_unreachable"}), 409
    if status == 200 and data.get("ok"):
        return jsonify({"ok": True, "started": True, "pending": bool(data.get("pending"))})
    return (
        jsonify({"ok": False, "error": str(data.get("error") or "现在还不能开口"), "reason": data.get("reason")}),
        409,
    )


@bp.get("/api/quest/prompt_preview")
def api_prompt_preview():
    """高级：当前会注入给模型的剧情附录（空串 = 不注入）。"""
    return jsonify({"ok": True, "appendix": quest_service.quest_prompt_appendix()})


# ── 剧本 ──────────────────────────────────────────────────────


@bp.post("/api/quest/playbooks")
def api_playbook_create():
    body = _payload()
    try:
        pb = qc.create_playbook(str(body.get("title") or ""), str(body.get("template") or qc.TEMPLATE_BLANK))
        if body.get("select", True):
            qc.select_playbook(pb["name"])
        return _ok(playbook=pb)
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/playbooks/generate")
def api_playbook_generate():
    """POST {description, title?, select?}：用一句话让小歪（带人设）生成一个陪伴场景。"""
    body = _payload()
    try:
        out = qc.generate_playbook(
            str(body.get("description") or ""), title=str(body.get("title") or ""), select=bool(body.get("select", True))
        )
        pb = out.get("playbook") or {}
        generated = {k: v for k, v in out.items() if k != "playbook"}
        # 新场景的名字单独给：响应里的 playbook 键是总览的「当前在用的场景」，不是这次生成的
        generated["playbook_name"] = str(pb.get("name") or "") or None
        generated["playbook_title"] = str(pb.get("title") or pb.get("name") or "") or None
        return _ok(generated=generated)
    except GenerationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except QuestError as exc:
        return _err(exc)


@bp.put("/api/quest/playbooks/<name>")
def api_playbook_rename(name: str):
    """PUT {title?, check_interval_hours?} 场景名 / 目标检测间隔（小时）。"""
    try:
        qc.update_playbook_settings(name, _payload())
        return _ok()
    except QuestError as exc:
        return _err(exc)


@bp.delete("/api/quest/playbooks/<name>")
def api_playbook_delete(name: str):
    try:
        if quest_service.get_playbook(name) is None:
            return jsonify({"ok": False, "error": f"场景不存在: {name}"}), 404
        quest_service.delete_playbook(name)
        return _ok()
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/playbooks/<name>/reset")
def api_playbook_reset(name: str):
    """把某个剧场（不一定是当前的）的进度清零。"""
    try:
        return _ok(result=qc.reset_playbook_progress(name))
    except QuestError as exc:
        return _err(exc)


@bp.get("/api/quest/playbooks/<name>/export")
def api_playbook_export(name: str):
    try:
        pb = quest_service.get_playbook(name)
    except QuestError as exc:
        return _err(exc)
    if pb is None:
        return jsonify({"ok": False, "error": f"场景不存在: {name}"}), 404
    resp: Response = jsonify(pb)
    resp.headers["Content-Disposition"] = f'attachment; filename="quest_{name}.json"'
    return resp


@bp.post("/api/quest/playbooks/import")
def api_playbook_import():
    """导入为一个新剧本：body 是导出的剧本 JSON（含 tasks）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "文件不是场景 JSON"}), 400
    try:
        title = str(data.get("title") or data.get("name") or "导入的场景").strip()[:40]
        pb = qc.create_playbook(title, qc.TEMPLATE_BLANK)
        data = dict(data)
        data["title"] = title
        quest_service.save_playbook(pb["name"], data)
        qc.select_playbook(pb["name"])
        return _ok()
    except QuestError as exc:
        return _err(exc)


# ── 小目标（定义）────────────────────────────────────────────


@bp.post("/api/quest/playbooks/<name>/tasks")
def api_task_create(name: str):
    body = _payload()
    try:
        task = qc.add_task(name, body, after=str(body.get("after") or "") or None)
        return _ok(task=task)
    except QuestError as exc:
        return _err(exc)


@bp.put("/api/quest/playbooks/<name>/order")
def api_task_order(name: str):
    """PUT {ids: [...]} 拖动排序后的完整顺序（主线小目标）。"""
    body = _payload()
    try:
        qc.reorder_tasks(name, body.get("ids") if isinstance(body.get("ids"), list) else [])
        return _ok()
    except QuestError as exc:
        return _err(exc)


@bp.put("/api/quest/playbooks/<name>/tasks/<task_id>")
def api_task_update(name: str, task_id: str):
    try:
        task = qc.update_task(name, task_id, _payload())
        return _ok(task=task)
    except QuestError as exc:
        return _err(exc)


@bp.delete("/api/quest/playbooks/<name>/tasks/<task_id>")
def api_task_delete(name: str, task_id: str):
    try:
        qc.delete_task(name, task_id)
        return _ok()
    except QuestError as exc:
        return _err(exc)


# ── 小歪自己提的小目标 ────────────────────────────────────────


@bp.post("/api/quest/proposals/<task_id>/approve")
def api_proposal_approve(task_id: str):
    """POST {kind?: care|story, repeat_interval_sec?, trigger_hint?, after?} 同意小歪提的小目标。
    默认作为日常关心（频率 + 条件）；kind=story 时插到 after 之后。"""
    body = _payload()
    try:
        playbook = quest_service.care_playbook()
        if not playbook:
            raise QuestError("「日常关心」场景不可用")
        interval = body.get("repeat_interval_sec")
        task = qc.approve_proposal(
            playbook,
            task_id,
            kind=str(body.get("kind") or "care"),
            after=(str(body.get("after") or "") if "after" in body else None),
            repeat_interval_sec=int(interval) if interval not in (None, "") else None,
            trigger_hint=(str(body.get("trigger_hint") or "") if "trigger_hint" in body else None),
            schedule_time=(str(body.get("schedule_time") or "") if "schedule_time" in body else None),
        )
        return _ok(task=task)
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/proposals/<task_id>/reject")
def api_proposal_reject(task_id: str):
    try:
        playbook = quest_service.care_playbook()
        if not playbook:
            raise QuestError("「日常关心」场景不可用")
        qc.delete_task(playbook, task_id)
        return _ok()
    except QuestError as exc:
        return _err(exc)


# ── 小目标（真实进度）────────────────────────────────────────


@bp.post("/api/quest/tasks/<task_id>/restart")
def api_task_restart(task_id: str):
    try:
        task = qc.restart_task(task_id)
        return _ok(task=task)
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/tasks/<task_id>/start_from")
def api_task_start_from(task_id: str):
    """「从这个开始」：它前面还没结果的都记为未达成（主人跳过），它成为当前。"""
    try:
        return _ok(**qc.start_from(task_id))
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/tasks/<task_id>/mark")
def api_task_mark(task_id: str):
    """POST {status: success|unmet|skipped, note?} 手动记成 达成 / 未达成 / 跳过。"""
    body = _payload()
    try:
        out = qc.mark_task(task_id, str(body.get("status") or ""), str(body.get("note") or ""))
        return _ok(**out)
    except QuestError as exc:
        return _err(exc)


@bp.post("/api/quest/runtime/reset")
def api_runtime_reset():
    try:
        return _ok(result=qc.reset_progress())
    except QuestError as exc:
        return _err(exc)
