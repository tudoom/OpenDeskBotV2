"""GET /api/telemetry/recent?limit=N&prefix=session. —— 结构化遥测最近事件。"""

from __future__ import annotations

from deskbot_server.telemetry import recent
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest


async def handle(ctx: RouteContext, req: RouteRequest):
    if req.method != "GET":
        return ctx.json_resp(405, {"ok": False, "error": "GET only"})
    try:
        limit = max(1, min(500, int(req.qargs.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    prefix = str(req.qargs.get("prefix") or "").strip() or None
    return ctx.json_resp(200, {"ok": True, "events": recent(limit, event_prefix=prefix)})


ROUTES_API_KEY["/api/telemetry/recent"] = handle
