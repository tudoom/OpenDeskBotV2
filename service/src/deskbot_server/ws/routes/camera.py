"""camera 路由（从 ws/http_api.py 的巨型 handler 原样搬出，逻辑未改）。"""

from __future__ import annotations

import asyncio  # noqa: F401 —— 原分支代码可能用到
import json  # noqa: F401
import time  # noqa: F401

from deskbot_server.telemetry import emit
from deskbot_server.ws.routes import ROUTES, RouteContext, RouteRequest

logger = None  # 由 ctx.logger 覆盖，保持原代码里的 logger 名字可用

# 对话拍照工具用的"上一帧"（max_age 命中时直接给）。设备帧缓存（device_camera_frame_store）
# 的 TTL 只有 5s，它服务的是视觉新鲜度语义，不能为了 UI 拉长。首页预览已改走
# /camera_view 实时流，不再轮询这里。
_PREVIEW_TTL_S = 300.0
_preview_cache: dict[str, tuple[float, bytes, str]] = {}


def _preview_get(device_id: str, max_age_s: float):
    row = _preview_cache.get(device_id)
    if not row:
        return None
    ts, jpeg, captured_at = row
    age = time.time() - ts
    if age > min(max_age_s, _PREVIEW_TTL_S):
        return None
    return jpeg, captured_at, age


def _preview_put(device_id: str, jpeg: bytes, captured_at: str) -> None:
    now = time.time()
    _preview_cache[device_id] = (now, bytes(jpeg), captured_at)
    for key, (ts, _b, _c) in list(_preview_cache.items()):
        if now - ts > _PREVIEW_TTL_S:
            _preview_cache.pop(key, None)


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

    if path_only == "/api/camera_snapshot":
        # 一次一帧（image/jpeg）：对话里的拍照工具用。抓不到帧时给出可读原因（设备离线 / 相机未就绪）。
        dev = str(qargs.get("device_id") or "").strip()
        if not dev:
            return _json_resp(400, {"ok": False, "error": "missing device_id"})
        from deskbot_server.device_camera_frame_store import (
            capture_camera_for_device_async,
        )
        from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY

        # max_age=N：接受最近 N 秒内的缓存帧，有就立刻返回（OV3660 换节拍后首帧要 4–6s）。
        try:
            max_age = float(qargs.get("max_age") or 0)
        except (TypeError, ValueError):
            max_age = 0.0
        max_age = min(max(max_age, 0.0), 300.0)
        if max_age > 0:
            hit = _preview_get(dev, max_age)
            if hit is not None:
                jpeg, captured_at, age = hit
                headers = _cors_headers()
                headers["Content-Type"] = "image/jpeg"
                headers["Content-Length"] = str(len(jpeg))
                headers["Cache-Control"] = "no-store"
                headers["X-Deskbot-Captured-At"] = captured_at
                headers["X-Deskbot-Frame-Age"] = f"{age:.1f}"
                return Response(200, "OK", headers, jpeg)

        try:
            # OV3660 走 legacy DMA 时，节拍切换后首帧要 4–6s（实测），4s 窗口必然抓空，给 8s。
            # 回退节拍用 2fps：只要一帧，别用 5fps 突发。
            _t0 = time.monotonic()
            result = await capture_camera_for_device_async(
                dev,
                hub=asr_chat_hub,
                cam_fps=2,
                require_fresh=True,
                wait_timeout_s=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            return _json_resp(503, {"ok": False, "error": str(exc)[:200]})
        try:
            emit(
                "camera.snapshot",
                device_id=dev,
                ok=bool(result.get("ok")) if isinstance(result, dict) else False,
                ms=int((time.monotonic() - _t0) * 1000),
                cached_ok=max_age > 0,
                error=str(result.get("error") or "")[:120] if isinstance(result, dict) else "",
            )
        except Exception:  # noqa: BLE001
            logger.debug("camera.snapshot telemetry failed", exc_info=True)
        image = result.get(TRANSIENT_VISION_IMAGE_KEY) if isinstance(result, dict) else None
        jpeg = image.get("bytes") if isinstance(image, dict) else None
        if not result.get("ok") or not jpeg:
            return _json_resp(
                503,
                {
                    "ok": False,
                    "error": str(result.get("error") or "暂无相机帧")[:200],
                    "stale": bool(result.get("stale")),
                },
            )
        headers = _cors_headers()
        headers["Content-Type"] = "image/jpeg"
        headers["Content-Length"] = str(len(jpeg))
        headers["Cache-Control"] = "no-store"
        headers["X-Deskbot-Captured-At"] = str(result.get("captured_at") or "")
        _preview_put(dev, bytes(jpeg), str(result.get("captured_at") or ""))
        return Response(200, "OK", headers, bytes(jpeg))
    return _json_resp(404, {"ok": False, "error": "not_found"})


for _path in ['/api/camera_snapshot']:
    ROUTES[_path] = handle
