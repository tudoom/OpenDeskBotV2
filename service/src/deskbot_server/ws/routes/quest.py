"""主动陪伴（Quest）Core 侧路由：控制台是另一个进程，循环状态与「现在试一句」只能经这里拿。

GET  /api/quest_proactive            → 循环快照（running / today_count / last_spoke_at / last_skip_reason …）
POST /api/quest_proactive/speak_now  → 跳过冷场 / 人脸 / 每日上限，立刻发起一轮；最多等 3 s，
                                       没说完也按已发起返回（不取消对话轮）
"""

from __future__ import annotations

import asyncio
import json

from deskbot_server.application import quest_proactive
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest

_SPEAK_WAIT_SEC = 3.0


async def handle(ctx: RouteContext, req: RouteRequest):
    if req.path_only == "/api/quest_proactive":
        if req.method != "GET":
            return ctx.json_resp(405, {"ok": False, "error": "GET only"})
        snap = quest_proactive.status_snapshot()
        return ctx.json_resp(200, {"ok": True, "running": bool(snap), "snapshot": snap or {}})
    if req.path_only == "/api/quest_proactive/speak_now":
        if req.method != "POST":
            return ctx.json_resp(405, {"ok": False, "error": "POST only"})
        raw = (getattr(req.request, "body", None) or b"")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        task_id = str((payload or {}).get("task_id") or "").strip() if isinstance(payload, dict) else ""
        return ctx.json_resp(*await _speak_now(task_id or None))
    return ctx.json_resp(404, {"ok": False, "error": "not_found"})


async def _speak_now(task_id: str | None = None) -> tuple[int, dict]:
    lp = quest_proactive.active_loop()
    if lp is None:
        return 409, {"ok": False, "error": "主动陪伴循环还没启动，稍后再试", "reason": "not_started"}
    task = asyncio.ensure_future(lp.trigger_now(task_id=task_id))
    done, _pending = await asyncio.wait({task}, timeout=_SPEAK_WAIT_SEC)
    if task not in done:
        # 一轮对话可能持续十几秒：提交成功但还没说完，按已发起处理（不取消）
        return 200, {"ok": True, "started": True, "pending": True}
    try:
        started = bool(task.result())
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "error": str(exc)}
    if not started:
        reason = lp.last_runner_skip_reason()
        return 409, {"ok": False, "error": quest_proactive.skip_reason_text(reason), "reason": reason}
    return 200, {"ok": True, "started": True}


for _path in ("/api/quest_proactive", "/api/quest_proactive/speak_now"):
    ROUTES_API_KEY[_path] = handle
