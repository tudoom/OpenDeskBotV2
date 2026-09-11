"""rtc_health 路由（从 ws/http_api.py 的巨型 handler 原样搬出，逻辑未改）。"""

from __future__ import annotations

import asyncio  # noqa: F401 —— 原分支代码可能用到
import json  # noqa: F401
import time  # noqa: F401

from deskbot_server.ws.routes import ROUTES, RouteContext, RouteRequest

logger = None  # 由 ctx.logger 覆盖，保持原代码里的 logger 名字可用


async def handle(ctx: RouteContext, req: RouteRequest):
    global logger
    logger = ctx.logger
    # 原分支引用的名字一律从 ctx / req 绑定，代码主体保持逐字一致
    registry = ctx.registry  # noqa: F841
    asr_chat_hub = ctx.asr_chat_hub  # noqa: F841
    serial_manager_provider = ctx.serial_manager_provider  # noqa: F841
    rtc_status_provider = ctx.rtc_status_provider  # noqa: F841
    _json_resp = ctx.json_resp  # noqa: F841
    _cors_headers = ctx.cors_headers  # noqa: F841
    _no_content = ctx.no_content  # noqa: F841
    _connection_is_loopback = ctx.connection_is_loopback  # noqa: F841
    Response = ctx.Response  # noqa: F841
    Headers = ctx.Headers  # noqa: F841
    path_only, method, qargs, peer = req.path_only, req.method, req.qargs, req.peer  # noqa: F841
    request, connection = req.request, req.connection  # noqa: F841

    if path_only == "/api/rtc_health":
        snapshot = (
            rtc_status_provider()
            if callable(rtc_status_provider)
            else {"status": "unavailable"}
        )
        return _json_resp(200, {"ok": True, "rtc": snapshot})
    return _json_resp(404, {"ok": False, "error": "not_found"})


for _path in ['/api/rtc_health']:
    ROUTES[_path] = handle
