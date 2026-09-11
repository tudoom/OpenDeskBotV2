"""devices 路由（从 ws/http_api.py 的巨型 handler 原样搬出，逻辑未改）。"""

from __future__ import annotations

import asyncio  # noqa: F401 —— 原分支代码可能用到
import json  # noqa: F401
import time  # noqa: F401

from deskbot_server.application.thermal_guard import thermal_guard
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest

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
    api_auth = req.api_auth  # noqa: F841
    _device_route_error = req.device_route_error  # noqa: F841

    if path_only == "/api/devices":
        snap = registry.snapshot()
        # 相机健康：固件 [CAMERA] 日志 + 最近一帧时间，控制台据此报准状态。
        try:
            from deskbot_server.device_camera_frame_store import get_device_camera_frame
            from deskbot_server.device_camera_health import snapshot as camera_health

            for row in snap:
                dev = str(row.get("device_id") or "")
                frame = get_device_camera_frame(dev) if dev else None
                last_frame_at = (
                    float(frame.get("captured_at") or frame.get("ts") or 0) or None
                    if frame
                    else None
                )
                row["camera_health"] = camera_health(dev, last_frame_at=last_frame_at)
                # 片内温度与告警等级（固件 ≥0.0.52 上报；旧固件为 unknown）
                row["thermal"] = thermal_guard().snapshot(dev)
        except Exception:  # noqa: BLE001 —— 遥测缺失不影响设备列表
            logger.debug("_action: swallowed exception", exc_info=True)
        device_ids = [d.get("device_id") for d in snap]
        logger.info(
            "[HTTP] GET /api/devices peer=%s -> %d 台设备 device_ids=%s",
            peer,
            len(snap),
            device_ids,
        )
        return _json_resp(
            200,
            {
                "devices": snap,
                "t": time.time(),
            },
        )
    return _json_resp(404, {"ok": False, "error": "not_found"})


for _path in ['/api/devices']:
    ROUTES_API_KEY[_path] = handle
