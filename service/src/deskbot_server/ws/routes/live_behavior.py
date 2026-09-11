"""空闲待机张望 Core 侧路由：控制台是另一个进程，「现在为什么没动」只能经这里拿。

GET /api/live_behavior → {"ok": true, "live": {mode, block_reason, block_text, next_slot_in,
                          wander, heat_skips, last_motion_ts, last_motion_source, …}}
2026-09-10 教训：之前控制台在自己进程里调 live_behavior_service().any_stats()，永远是空的，
状态行显示「张望 0 次」，把人带偏了。
"""

from __future__ import annotations

from deskbot_server.application.live_behavior import live_behavior_service
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest


async def handle(ctx: RouteContext, req: RouteRequest):
    if req.method != "GET":
        return ctx.json_resp(405, {"ok": False, "error": "GET only"})
    return ctx.json_resp(200, {"ok": True, "live": live_behavior_service().any_stats()})


ROUTES_API_KEY["/api/live_behavior"] = handle
