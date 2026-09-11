"""wifi 路由（从 ws/http_api.py 的巨型 handler 原样搬出，逻辑未改）。"""

from __future__ import annotations

import asyncio  # noqa: F401 —— 原分支代码可能用到
import json  # noqa: F401
import time  # noqa: F401

from deskbot_server.infrastructure.net.core_discovery import core_discovery_snapshot
from deskbot_server.infrastructure.net.core_identity import core_id
from deskbot_server.infrastructure.net.wifi_link import (
    ensure_link_token,
    pc_lan_ip,
    wifi_link_port,
)
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

    if path_only in {"/api/wifi_status", "/api/wifi_provision"}:
        manager = (
            serial_manager_provider()
            if callable(serial_manager_provider)
            else None
        )
        if manager is None:
            return _json_resp(
                503, {"ok": False, "error": "serial_manager_unavailable"}
            )

        if path_only == "/api/wifi_status":
            dev = str(qargs.get("device_id") or "").strip()
            if not dev:
                return _json_resp(
                    400, {"ok": False, "error": "missing device_id"}
                )
            # 连接页要展示「本机 Core 是谁、机器人记住的是谁」：身份 + 当前地址。
            core = core_discovery_snapshot()
            core["host"] = await asyncio.to_thread(pc_lan_ip)
            session = manager.session_for_device(dev)
            if session is None:
                return _json_resp(
                    200,
                    {
                        "ok": True,
                        "connected": False,
                        "transport": None,
                        "wifi": None,
                        "core": core,
                    },
                )
            status = await session.request_wifi_status()
            return _json_resp(
                200,
                {
                    "ok": True,
                    "connected": True,
                    "transport": session.transport,
                    "wifi": status or session.wifi_status,
                    "stale": status is None,
                    "core": core,
                    "t": time.time(),
                },
            )

        if method != "POST":
            return _json_resp(
                405, {"ok": False, "error": "provision requires POST"}
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
        session = manager.session_for_device(dev)
        if session is None:
            return _json_resp(
                409, {"ok": False, "error": "device_not_connected"}
            )
        ssid = str(payload.get("ssid") or "").strip()
        password = str(payload.get("password") or "")
        enterprise = False
        if not ssid:
            from deskbot_server.wifi_credentials import pc_wifi_credentials

            # 一次探测同时给出 SSID/密码与"在 WiFi 上但名字被系统隐藏"
            # 的候选列表，不再把同一组系统命令跑两遍。
            status, extracted, enterprise = await asyncio.to_thread(
                pc_wifi_credentials
            )
            ssid = status.ssid
            if not password:
                password = extracted
            if not ssid:
                # macOS 14+ 把 SSID 当定位数据，后台服务读到的全是
                # <redacted>；PC 明明在 WiFi 上却拿不到名字时，把首选
                # 网络列表交给页面让用户选/填，而不是误报"没连 WiFi"。
                if status.associated and status.ssid_hidden_by_os:
                    return _json_resp(
                        200,
                        {
                            "ok": False,
                            "needs_password": True,
                            "needs_ssid": True,
                            "enterprise": False,
                            "ssid": "",
                            "candidates": list(status.candidates),
                            "message": (
                                "macOS 不允许后台程序读取当前 WiFi 名称，"
                                "请选择或输入 PC 正在用的 WiFi 并填写密码"
                            ),
                        },
                    )
        if not ssid:
            return _json_resp(
                409,
                {
                    "ok": False,
                    "error": "pc_not_on_wifi",
                    "message": "PC 当前没有连接 WiFi，无法同步",
                },
            )
        if not password:
            # 企业级 802.1X 网络根本没有 PSK；普通网络则可能是系统
            # 拒绝透出密码（需要管理员权限的机器）。两种情况都让页面
            # 回落到手输，而不是把配网整个卡死。
            return _json_resp(
                200,
                {
                    "ok": False,
                    "needs_password": True,
                    "enterprise": enterprise,
                    "ssid": ssid,
                    "message": (
                        "该网络是企业级认证，机器人无法加入，请改用普通"
                        "密码的 WiFi"
                        if enterprise
                        else "无法自动读取 WiFi 密码，请手动输入"
                    ),
                },
            )
        # VPN 会把默认路由抢走，自动选址会选到 VPN 接口的地址；
        # 请求方可显式指定 PC 在目标网段的地址。
        host_ip = str(payload.get("host") or "").strip() or pc_lan_ip()
        if not host_ip:
            return _json_resp(
                500, {"ok": False, "error": "pc_lan_ip_unavailable"}
            )
        token = await asyncio.to_thread(ensure_link_token, dev)
        status = await session.send_wifi_config(
            {
                "ssid": ssid,
                "password": password,
                "host": host_ip,
                "port": wifi_link_port(),
                "token": token,
                # 0.0.49+ 设备记住的是「哪台 Core」，地址只是当前快照。
                "core_id": core_id(),
            }
        )
        return _json_resp(
            200,
            {
                "ok": status is not None,
                "applied": status is not None,
                "ssid": ssid,
                "host": host_ip,
                "port": wifi_link_port(),
                "transport": session.transport,
                "wifi": status,
                "t": time.time(),
            },
        )
    return _json_resp(404, {"ok": False, "error": "not_found"})


for _path in ['/api/wifi_status', '/api/wifi_provision']:
    ROUTES[_path] = handle
