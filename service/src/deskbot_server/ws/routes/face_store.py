"""待机卡通脸的设备持久化（固件 ≥0.0.57）。

设备把 PC 用 face_keep 标记的待机位图表情留在 PSRAM，断开电脑后自己循环播放；这里再让它
写进 FFat，断电、重启、换电脑都保持最后设定的表情。语音运行时在待机链被接收后也会自动
调用同一套控制帧（expression_runtime._persist_standby_face），本路由给表情页"设为默认"用。

GET  /api/face_status   ?device_id=  → {ok, supported, tag, persisted, hello_tag}
POST /api/face_persist  {device_id}  → {ok, supported, tag, bytes|already}
POST /api/face_clear    {device_id}  → {ok, supported}（删盘上的 + PSRAM 副本，回内建矢量待机脸）
"""

from __future__ import annotations

from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest
from deskbot_server.ws.routes.device_power import _session


def _runtime(device_id: str):
    from deskbot_server.application.expression_runtime import get_expression_runtime

    return get_expression_runtime(device_id)


def _ack_or_unsupported(ctx: RouteContext, ack: dict | None, **extra):
    if ack is None:
        return ctx.json_resp(200, {"ok": False, "supported": False, "error": "firmware_too_old", **extra})
    body = {k: v for k, v in ack.items() if k != "type"}
    ok = body.pop("ok", True) is not False
    return ctx.json_resp(200, {"ok": ok, "supported": True, **body, **extra})


async def handle_face_status(ctx: RouteContext, req: RouteRequest):
    if req.method != "GET":
        return ctx.json_resp(405, {"ok": False, "error": "GET only"})
    session, error, _payload = await _session(ctx, req)
    if error is not None:
        return error
    hello = getattr(session, "hello_info", None)
    hello_tag = str(getattr(hello, "face_tag", "") or "")
    ack = await session.face_status_request()
    return _ack_or_unsupported(ctx, ack, hello_tag=hello_tag)


async def handle_face_persist(ctx: RouteContext, req: RouteRequest):
    if req.method != "POST":
        return ctx.json_resp(405, {"ok": False, "error": "POST only"})
    session, error, payload = await _session(ctx, req)
    if error is not None:
        return error
    ack = await session.face_persist_request()
    if ack is not None and ack.get("ok") is True:
        runtime = _runtime(str(payload.get("device_id") or ""))
        if runtime is not None:
            runtime.note_face_persisted(str(ack.get("tag") or ""))
    return _ack_or_unsupported(ctx, ack)


async def handle_face_clear(ctx: RouteContext, req: RouteRequest):
    if req.method != "POST":
        return ctx.json_resp(405, {"ok": False, "error": "POST only"})
    session, error, payload = await _session(ctx, req)
    if error is not None:
        return error
    ack = await session.face_clear_request()
    if ack is not None:
        runtime = _runtime(str(payload.get("device_id") or ""))
        if runtime is not None:
            runtime.note_face_persisted("")
    return _ack_or_unsupported(ctx, ack)


ROUTES_API_KEY["/api/face_status"] = handle_face_status
ROUTES_API_KEY["/api/face_persist"] = handle_face_persist
ROUTES_API_KEY["/api/face_clear"] = handle_face_clear
