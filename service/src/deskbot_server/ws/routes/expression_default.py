"""表情映射改了之后让设备立刻换上（对话 Agent 的生成工具在 Flask 进程时经这里到 Core）。

POST /api/expression_apply_default {device_id} → 按当前生命周期状态重推一次表情；映射没变则 unchanged。
"""

from __future__ import annotations

import json

from deskbot_server.application.agent_generation import apply_default_expression_for_device
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest


async def handle_apply_default(ctx: RouteContext, req: RouteRequest):
    if req.method != "POST":
        return ctx.json_resp(405, {"ok": False, "error": "POST only"})
    raw = (getattr(req.request, "body", None) or b"").decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return ctx.json_resp(400, {"ok": False, "error": "invalid JSON body"})
    if not isinstance(payload, dict):
        payload = {}
    dev = str(payload.get("device_id") or req.qargs.get("device_id") or "").strip()
    if not dev:
        return ctx.json_resp(400, {"ok": False, "error": "missing device_id"})
    if callable(req.device_route_error):
        denied = req.device_route_error(dev)
        if denied is not None:
            return denied
    out = await apply_default_expression_for_device(dev)
    return ctx.json_resp(200 if out.get("ok") else 409, {"device_id": dev, **out})


ROUTES_API_KEY["/api/expression_apply_default"] = handle_apply_default
