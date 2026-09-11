"""舵机到位释放开关（固件 ≥0.0.49）。

GET  /api/servo_relax?device_id=…          读设备当前设置与状态
POST /api/servo_relax {device_id, ms}      ms=0 关闭（始终保持力矩），默认 800
"""

from __future__ import annotations

import json

from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest

DEFAULT_RELAX_MS = 800
MAX_RELAX_MS = 60000


async def handle(ctx: RouteContext, req: RouteRequest):
    provider = ctx.serial_manager_provider
    manager = provider() if callable(provider) else None
    if manager is None:
        return ctx.json_resp(503, {"ok": False, "error": "serial_manager_unavailable"})
    payload: dict = {}
    if req.method == "POST":
        raw = (getattr(req.request, "body", None) or b"").decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return ctx.json_resp(400, {"ok": False, "error": "invalid JSON body"})
        if not isinstance(payload, dict):
            payload = {}
    elif req.method != "GET":
        return ctx.json_resp(405, {"ok": False, "error": "GET or POST"})
    dev = str(payload.get("device_id") or req.qargs.get("device_id") or "").strip()
    if not dev:
        return ctx.json_resp(400, {"ok": False, "error": "missing device_id"})
    session = manager.session_for_device(dev)
    if session is None:
        return ctx.json_resp(409, {"ok": False, "error": "device_not_connected"})
    ms = None
    if req.method == "POST":
        try:
            ms = int(payload.get("ms", DEFAULT_RELAX_MS))
        except (TypeError, ValueError):
            return ctx.json_resp(400, {"ok": False, "error": "ms must be an integer"})
        ms = max(0, min(MAX_RELAX_MS, ms))
    ack = await session.servo_relax_request(ms)
    if ack is None:
        return ctx.json_resp(
            200,
            {"ok": False, "supported": False, "error": "firmware_too_old", "requested_ms": ms},
        )
    return ctx.json_resp(
        200,
        {
            "ok": True,
            "supported": True,
            "ms": int(ack.get("ms") or 0),
            "relaxed": bool(ack.get("relaxed")),
            "relax_count": int(ack.get("relax_count") or 0),
        },
    )


ROUTES_API_KEY["/api/servo_relax"] = handle
