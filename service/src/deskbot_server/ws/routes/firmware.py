"""firmware 路由（从 ws/http_api.py 的巨型 handler 原样搬出，逻辑未改）。"""

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

    if path_only in {
        "/api/firmware_manifest",
        "/api/firmware_update",
        "/api/firmware_update_status",
    }:
        from deskbot_server import firmware_update as fw

        manager = (
            serial_manager_provider()
            if callable(serial_manager_provider)
            else None
        )
        if manager is None:
            return _json_resp(
                503, {"ok": False, "error": "serial_manager_unavailable"}
            )
        if path_only == "/api/firmware_update_status":
            return _json_resp(
                200, {"ok": True, "job": fw.firmware_update_job.status()}
            )
        if path_only == "/api/firmware_manifest":
            dev = str(qargs.get("device_id") or "").strip()
            return _json_resp(200, fw.manifest_response(manager, dev))
        if method != "POST":
            return _json_resp(
                405, {"ok": False, "error": "firmware update requires POST"}
            )
        try:
            raw_body = (
                getattr(request, "body", None) or b""
            ).decode("utf-8")
            payload = json.loads(raw_body) if raw_body.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_resp(
                400, {"ok": False, "error": "invalid JSON body"}
            )
        if not isinstance(payload, dict):
            payload = {}
        dev = str(payload.get("device_id") or "").strip()
        if not dev:
            return _json_resp(
                400, {"ok": False, "error": "missing device_id"}
            )
        # 设备归属校验由 start_update 的会话查找承担；
        # _device_route_error 是 handler 靠后才定义的局部函数，此处不可用。
        status_code, body = await fw.start_update(manager, dev)
        logger.info(
            "[HTTP] POST /api/firmware_update peer=%s device_id=%s -> %d %s",
            peer,
            dev,
            status_code,
            body.get("error") or body.get("state") or "",
        )
        return _json_resp(status_code, body)
    return _json_resp(404, {"ok": False, "error": "not_found"})


for _path in ['/api/firmware_manifest', '/api/firmware_update', '/api/firmware_update_status']:
    ROUTES[_path] = handle
