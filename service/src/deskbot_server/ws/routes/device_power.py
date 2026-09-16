"""参数设置页的设备端开关（固件 ≥0.0.53，静音 ≥0.0.54）。

上行模式与温度阈值保存在设备 NVS；静音以 PC 端偏好为准（默认 false），连上时推给设备，
读取时若设备残留的值与 PC 不一致就立刻纠正（2026-09-14）。

GET/POST /api/mic_uplink_mode  {device_id, mode: "vad"|"continuous"}（固件默认 continuous）
GET/POST /api/mic_mute         {device_id, muted: bool}（PC 端偏好，默认 false；true = 设备不再采集/上传音频）
GET/POST /api/thermal_cutoff   {device_id, c: 0..120}（0 = 关闭断电保护）
"""

from __future__ import annotations

import json

from deskbot_server.application.device_power_sync import pc_mic_muted
from deskbot_server.device_preferences import update_preferences
from deskbot_server.ws.routes import ROUTES_API_KEY, RouteContext, RouteRequest


async def _session(ctx: RouteContext, req: RouteRequest):
    provider = ctx.serial_manager_provider
    manager = provider() if callable(provider) else None
    if manager is None:
        return None, ctx.json_resp(503, {"ok": False, "error": "serial_manager_unavailable"}), {}
    payload: dict = {}
    if req.method == "POST":
        raw = (getattr(req.request, "body", None) or b"").decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None, ctx.json_resp(400, {"ok": False, "error": "invalid JSON body"}), {}
        if not isinstance(payload, dict):
            payload = {}
    elif req.method != "GET":
        return None, ctx.json_resp(405, {"ok": False, "error": "GET or POST"}), {}
    dev = str(payload.get("device_id") or req.qargs.get("device_id") or "").strip()
    if not dev:
        return None, ctx.json_resp(400, {"ok": False, "error": "missing device_id"}), {}
    session = manager.session_for_device(dev)
    if session is None:
        return None, ctx.json_resp(409, {"ok": False, "error": "device_not_connected"}), {}
    return session, None, payload


async def handle_mic_uplink(ctx: RouteContext, req: RouteRequest):
    session, error, payload = await _session(ctx, req)
    if error is not None:
        return error
    mode = None
    if req.method == "POST":
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in ("vad", "continuous"):
            return ctx.json_resp(400, {"ok": False, "error": "mode must be vad or continuous"})
    ack = await session.mic_uplink_mode_request(mode)
    if ack is None:
        return ctx.json_resp(200, {"ok": False, "supported": False, "error": "firmware_too_old"})
    return ctx.json_resp(200, {"ok": True, "supported": True, **{k: v for k, v in ack.items() if k != "type"}})


async def handle_mic_mute(ctx: RouteContext, req: RouteRequest):
    session, error, payload = await _session(ctx, req)
    if error is not None:
        return error
    muted = None
    if req.method == "POST":
        raw = payload.get("muted")
        if isinstance(raw, str):
            word = raw.strip().lower()
            raw = True if word in ("1", "true", "yes", "on") else False if word in ("0", "false", "no", "off") else None
        if not isinstance(raw, bool):
            return ctx.json_resp(400, {"ok": False, "error": "muted must be a boolean"})
        muted = raw
        update_preferences({"power": {"mic_muted": muted}})  # PC 端是正本
        ack = await session.mic_mute_request(muted)
    else:
        muted = pc_mic_muted()
        ack = await session.mic_mute_request(None)
        if ack is not None and bool(ack.get("muted")) != muted:
            # 设备 NVS 里残留的是别的值（比如上次在别的电脑上开过）：以 PC 为准，顺手纠正
            ack = await session.mic_mute_request(muted)
    if ack is None:
        return ctx.json_resp(200, {"ok": False, "supported": False, "error": "firmware_too_old"})
    fields = {k: v for k, v in ack.items() if k != "type"}
    fields["device_muted"] = bool(ack.get("muted"))
    fields["muted"] = muted
    return ctx.json_resp(200, {"ok": True, "supported": True, "source": "pc", **fields})


async def handle_thermal_cutoff(ctx: RouteContext, req: RouteRequest):
    session, error, payload = await _session(ctx, req)
    if error is not None:
        return error
    celsius = None
    if req.method == "POST":
        try:
            celsius = max(0, min(120, int(payload.get("c", 70))))
        except (TypeError, ValueError):
            return ctx.json_resp(400, {"ok": False, "error": "c must be an integer"})
    ack = await session.thermal_cutoff_request(celsius)
    if ack is None:
        return ctx.json_resp(200, {"ok": False, "supported": False, "error": "firmware_too_old"})
    return ctx.json_resp(200, {"ok": True, "supported": True, **{k: v for k, v in ack.items() if k != "type"}})


ROUTES_API_KEY["/api/mic_uplink_mode"] = handle_mic_uplink
ROUTES_API_KEY["/api/mic_mute"] = handle_mic_mute
ROUTES_API_KEY["/api/thermal_cutoff"] = handle_thermal_cutoff
