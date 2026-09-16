"""控制台文字对话的拍照工具。

文字对话（``/api/llm/chat``）跑在 Flask 进程里，手上没有 Core 的 USB hub 与相机
帧缓存，而同步执行器 ``execute_llm_tools`` 也没有 ``capture_camera`` 分支——模型
一请求拍照就拿到「未知工具」，只能回答"看不到画面"；语音链路走 RTC 的
``capture_and_describe``（Core 内抓帧）所以正常。

这里把拍照工具改为向 Core 拉一帧（``GET /api/camera_snapshot``，一次性抓拍），
再按 ``_frame_to_capture_result`` 同样的瞬态图片格式回灌，下一轮模型就能看图。
其它工具原样交给 ``execute_llm_tools``。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from deskbot_server.auth.api_key_service import read_free_api_key_raw
from deskbot_server.llm.vision_input import (
    TRANSIENT_VISION_IMAGE_KEY,
    VisionImageValidationError,
    make_transient_vision_image,
)

logger = logging.getLogger("deskbot-server")

CAPTURE_TOOLS = frozenset({"capture_camera", "capture_and_describe"})
_SNAPSHOT_TIMEOUT_SEC = 14.0
# 节拍切换后首帧可能晚于 Core 的等待窗口（OV3660 实测 4–6s）；第一次抓空时
# 节拍已经踢起来了，隔一会儿再抓一次通常就有。
_SCENE_TIMEOUT_SEC = 120.0  # 一段表演可能十几秒；与 Core 路由同口径
_SNAPSHOT_ATTEMPTS = 2
_SNAPSHOT_RETRY_DELAY_SEC = 1.5

Fetcher = Callable[[str], tuple[int, str, bytes]]


def is_capture_tool(name: str) -> bool:
    return str(name or "").strip().lower() in CAPTURE_TOOLS


def _upstream_base() -> str:
    # 应用层唯一允许触碰 web 层的点（test_layering 白名单），两个 Core 调用共用
    from deskbot_server.web.helpers import deskbot_upstream_base

    return deskbot_upstream_base()


def _default_post(url: str, body: bytes) -> tuple[int, str, bytes]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = read_free_api_key_raw()
    if key:
        headers["X-API-Key"] = key
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=_SCENE_TIMEOUT_SEC) as resp:
            return int(resp.status), str(resp.headers.get("Content-Type") or ""), resp.read()
    except urlerror.HTTPError as exc:
        return int(exc.code), str(exc.headers.get("Content-Type") or ""), exc.read()


def run_core_scene(
    device_id: str,
    name: str,
    *,
    base_url: str | None = None,
    fetch: Callable[[str], tuple[int, str, bytes]] | None = None,
    post: Callable[[str, bytes], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """Flask 进程里的 perform_scene：转 Core ``/api/scene_playbook/run``，演完再返回。

    ``fetch`` 只为与相机保持同一调用签名（执行器统一透传）；实际用 ``post``。
    """
    del fetch
    dev = str(device_id or "").strip()
    key = str(name or "").strip()
    if not dev:
        return {"tool": "perform_scene", "ok": False, "error": "请先在控制台选择设备，再让我表演"}
    if not key:
        return {"tool": "perform_scene", "ok": False, "error": "perform_scene 需要 name"}
    if base_url is None:
        base_url = _upstream_base()
    url = f"{base_url.rstrip('/')}/api/scene_playbook/run"
    body = json.dumps({"device_id": dev, "name": key}).encode("utf-8")
    try:
        status, _ctype, raw = (post or _default_post)(url, body)
    except Exception as exc:  # noqa: BLE001
        return {"tool": "perform_scene", "ok": False, "error": f"表演请求失败：{str(exc)[:160]}"}
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        return {"tool": "perform_scene", "ok": True, "name": key, "status": "played"}
    err = (data.get("error") if isinstance(data, dict) else None) or f"HTTP {status}"
    if status == 404:
        err = f"没有叫「{key}」的表演"
    return {"tool": "perform_scene", "ok": False, "error": str(err)[:200]}


def _core_post_json(
    path: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    post: Callable[[str, bytes], tuple[int, str, bytes]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """向 Core 发一个 JSON POST，返回 (status, 解析后的 body)。网络异常 → (0, {"error": …})。"""
    if base_url is None:
        base_url = _upstream_base()
    url = f"{base_url.rstrip('/')}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        status, _ctype, raw = (post or _default_post)(url, body)
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"Core 请求失败：{str(exc)[:160]}"}
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def run_core_move_head(
    device_id: str,
    arguments: dict[str, Any],
    *,
    base_url: str | None = None,
    post: Callable[[str, bytes], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """Flask 进程里的 move_head：预设或自编步骤都转 Core ``/api/device_servo``（与语音链路同一限位/包络）。"""
    dev = str(device_id or "").strip()
    if not dev:
        return {"tool": "move_head", "ok": False, "error": "请先在控制台选择设备，再让我做动作"}
    move = str(arguments.get("move") or arguments.get("preset") or "").strip()
    steps = arguments.get("steps")
    body: dict[str, Any] = {"device_id": dev}
    if move:
        body["preset"] = move
        try:
            duration_ms = int(arguments.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        if duration_ms > 0:
            body["duration_ms"] = max(200, min(8000, duration_ms))
    elif isinstance(steps, list) and steps:
        clean: list[dict[str, Any]] = []
        for raw in steps[:5]:
            if not isinstance(raw, dict):
                return {"tool": "move_head", "ok": False, "error": "steps 里每一步都要是对象"}
            clean.append(
                {
                    "x": raw.get("x", 90),
                    "y": raw.get("y", 90),
                    "xm": raw.get("xm", 0),
                    "ym": raw.get("ym", raw.get("xm", 0)),
                    "ms": raw.get("ms", 400),
                }
            )
        body["steps"] = clean
    else:
        return {"tool": "move_head", "ok": False, "error": "move_head 需要 move（预设 id）或 steps"}
    status, data = _core_post_json("/api/device_servo", body, base_url=base_url, post=post)
    if status == 200 and data.get("ok", True) and not data.get("error"):
        return {"tool": "move_head", "ok": True, "status": "moved", "move": move or "composed", "steps": len(body.get("steps") or []) or None}
    err = data.get("error") or f"HTTP {status}"
    return {"tool": "move_head", "ok": False, "status": "invalid_move" if status == 400 else "failed", "error": str(err)[:200]}


def run_core_play_expression(
    device_id: str,
    arguments: dict[str, Any],
    *,
    base_url: str | None = None,
    post: Callable[[str, bytes], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """Flask 进程里的 play_expression：转 Core ``/api/device_face_play``（表情库里的表情按 name 播）。"""
    dev = str(device_id or "").strip()
    name = str(arguments.get("name") or arguments.get("expression") or "").strip()
    if not dev:
        return {"tool": "play_expression", "ok": False, "error": "请先在控制台选择设备，再让我做表情"}
    if not name:
        return {"tool": "play_expression", "ok": False, "error": "play_expression 需要 name"}
    query = urlparse.urlencode({"device_id": dev, "kind": "emotion", "name": name})
    status, data = _core_post_json(f"/api/device_face_play?{query}", {}, base_url=base_url, post=post)
    if status == 200 and data.get("ok", True) and not data.get("error"):
        return {"tool": "play_expression", "ok": True, "status": "accepted", "name": name}
    err = str(data.get("error") or f"HTTP {status}")
    valid = data.get("valid_names")
    if isinstance(valid, list) and valid:
        err += "；可用：" + "、".join(str(v) for v in valid[:30])
    return {"tool": "play_expression", "ok": False, "error": err[:400]}


def run_core_apply_default_expression(
    device_id: str,
    *,
    base_url: str | None = None,
    post: Callable[[str, bytes], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """Flask 进程里让设备立刻换上新的表情映射：转 Core ``/api/expression_apply_default``。"""
    dev = str(device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "没有目标设备"}
    status, data = _core_post_json("/api/expression_apply_default", {"device_id": dev}, base_url=base_url, post=post)
    if status == 200:
        return {"ok": True, **{k: v for k, v in data.items() if k != "device_id"}}
    return {"ok": False, "error": str(data.get("error") or f"HTTP {status}")[:200]}


def _default_fetch(url: str) -> tuple[int, str, bytes]:
    headers = {"Accept": "image/jpeg, application/json"}
    key = read_free_api_key_raw()
    if key:
        headers["X-API-Key"] = key
    req = urlrequest.Request(url, headers=headers, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=_SNAPSHOT_TIMEOUT_SEC) as resp:
            return int(resp.status), str(resp.headers.get("Content-Type") or ""), resp.read()
    except urlerror.HTTPError as exc:
        return int(exc.code), str(exc.headers.get("Content-Type") or ""), exc.read()


def fetch_core_camera_capture(
    device_id: str,
    *,
    base_url: str | None = None,
    fetch: Fetcher | None = None,
) -> dict[str, Any]:
    """向 Core 抓一帧，返回与 ``capture_camera`` 工具一致的结果字典。"""
    dev = str(device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "请先在控制台选择设备，再让我看画面"}
    if base_url is None:
        base_url = _upstream_base()
    url = f"{base_url.rstrip('/')}/api/camera_snapshot?" + urlparse.urlencode(
        {"device_id": dev}
    )
    status, content_type, body = 0, "", b""
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        try:
            status, content_type, body = (fetch or _default_fetch)(url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"相机抓拍请求失败：{str(exc)[:160]}"}
        if status == 200 or attempt == _SNAPSHOT_ATTEMPTS - 1:
            break
        time.sleep(_SNAPSHOT_RETRY_DELAY_SEC)
    if status == 200 and content_type.lower().startswith("image/"):
        try:
            image = make_transient_vision_image(body)
        except VisionImageValidationError as exc:
            return {"ok": False, "error": f"相机帧无效：{exc}"}
        return {
            "ok": True,
            "ts": time.time(),
            "captured_at": time.time(),
            "source": "core_snapshot",
            "jpeg_bytes": len(body),
            "display_requested": False,
            TRANSIENT_VISION_IMAGE_KEY: image,
        }
    detail = ""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
        detail = str(payload.get("error") or "")
    except (ValueError, AttributeError):
        pass
    return {"ok": False, "error": detail or f"相机抓拍失败 HTTP {status}"}


def execute_web_chat_tools(
    tools: list[dict[str, Any]],
    *,
    device_id: str | None,
    fetch: Fetcher | None = None,
    user_confirmed: bool = False,
) -> list[dict[str, Any]]:
    """Flask 进程一轮工具执行——薄封装，实现见 ``application.tool_executor``。"""
    from deskbot_server.application.tool_executor import ToolChannel, execute_tools_sync

    return execute_tools_sync(
        tools,
        channel=ToolChannel.WEB,
        device_id=device_id,
        user_confirmed=user_confirmed,
        fetch=fetch,
    )


__all__ = [
    "CAPTURE_TOOLS",
    "execute_web_chat_tools",
    "fetch_core_camera_capture",
    "is_capture_tool",
    "run_core_apply_default_expression",
    "run_core_move_head",
    "run_core_play_expression",
    "run_core_scene",
]
