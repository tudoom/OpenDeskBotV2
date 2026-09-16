"""Core HTTP 的路由表。

``ws/http_api.py`` 里的 handler 曾是一个 2500+ 行、29 个 ``if path_only`` 分支的
函数。这里提供一张 ``{path: handler}`` 表与两个数据类：路由模块把分支原样搬出
来注册到表里，handler 只做鉴权与分发。分支代码逐字未改，只是换了住处——
新路由请直接在这里登记，不要再往 handler 里加 if。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class RouteContext:
    registry: Any
    asr_chat_hub: Any
    serial_manager_provider: Any
    rtc_status_provider: Any
    json_resp: Callable[[int, object], Any]
    cors_headers: Callable[[], Any]
    no_content: Callable[[], Any]
    connection_is_loopback: Callable[[Any], bool]
    Response: Any
    Headers: Any
    logger: Any


@dataclass(frozen=True)
class RouteRequest:
    path_only: str
    method: str
    qargs: dict[str, str]
    peer: str
    request: Any
    connection: Any
    # 仅门禁后的路由可用：已鉴权的 API Key 上下文与设备归属校验器
    api_auth: Any = None
    device_route_error: Any = None


RouteHandler = Callable[[RouteContext, RouteRequest], Awaitable[Any]]
# 两张表对应 handler 里两个分发点：
# - ROUTES：只要求 loopback（原本位于 API Key 门禁之前的分支：rtc_health /
#   camera_snapshot / firmware / wifi）
# - ROUTES_API_KEY：还必须过本机 API Key 门禁（原本位于门禁之后的分支）
# 新路由默认登记到 ROUTES_API_KEY，除非有明确理由不需要 key。
ROUTES: dict[str, RouteHandler] = {}
ROUTES_API_KEY: dict[str, RouteHandler] = {}


def _load_route_modules() -> None:
    # 导入即注册；显式列出便于 grep
    from deskbot_server.ws.routes import (  # noqa: F401
        camera,
        device_power,
        devices,
        expression_default,
        face_store,
        firmware,
        live_behavior,
        quest,
        rtc_health,
        servo_relax,
        telemetry,
        usb_devices,
        wifi,
    )


_load_route_modules()

__all__ = ["ROUTES", "ROUTES_API_KEY", "RouteContext", "RouteHandler", "RouteRequest"]
