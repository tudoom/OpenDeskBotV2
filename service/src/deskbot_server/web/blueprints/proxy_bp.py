from __future__ import annotations

import json
import logging
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from flask import Blueprint, Response, jsonify, request

from deskbot_server.auth.api_key_service import read_free_api_key_raw
from deskbot_server.web.helpers import deskbot_upstream_base
from deskbot_server.web.session_device import get_current_device_id

logger = logging.getLogger("deskbot-server")

bp = Blueprint("proxy", __name__, url_prefix="/proxy/deskbot")

_DEVICE_PARAM_KEYS = ("device_id", "deviceid", "device", "id")

# The Flask process holds a credential that the realtime process trusts.  This
# route must therefore be an explicit capability bridge, never an open-ended
# authenticated HTTP proxy.
_PROXY_METHODS: dict[str, frozenset[str]] = {
    "/api/devices": frozenset({"GET"}),
    "/api/pipeline_recent": frozenset({"GET"}),
    "/api/device_servo": frozenset({"POST"}),
    "/api/device_tts": frozenset({"POST"}),
    "/api/control_operation": frozenset({"GET"}),
    "/api/device_pb_scene": frozenset({"POST"}),
    "/api/device_pb_anim": frozenset({"POST"}),
    "/api/device_pb_expr_scene": frozenset({"POST"}),
    "/api/pb_scenes": frozenset({"GET"}),
    "/api/face_catalog": frozenset({"GET"}),
    "/api/device_face_play": frozenset({"POST"}),
    "/api/expression_runtime": frozenset({"GET"}),
    "/api/servo_contract": frozenset({"GET"}),
    "/api/servo_config": frozenset({"GET", "POST"}),
    "/api/scene_playbooks": frozenset({"GET", "POST"}),
    "/api/scene_playbook/run": frozenset({"POST"}),
    # Process-wide emergency/debug switch for the loopback-only console.
    "/api/asr_auto_reply": frozenset({"GET", "POST"}),
}

_DEVICE_SCOPED_PATHS = frozenset(
    {
        "/api/pipeline_recent",
        "/api/device_servo",
        "/api/device_tts",
        "/api/control_operation",
        "/api/device_pb_scene",
        "/api/device_pb_anim",
        "/api/device_pb_expr_scene",
        "/api/device_face_play",
        "/api/expression_runtime",
        "/api/scene_playbook/run",
    }
)


def _extract_device_id_from_request() -> str | None:
    for key in _DEVICE_PARAM_KEYS:
        val = (request.args.get(key) or "").strip()
        if val:
            return val
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            val = str(payload.get("device_id") or "").strip()
            if val:
                return val
    return None


def _requires_device_id(path: str) -> bool:
    return path in _DEVICE_SCOPED_PATHS


@bp.route("/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def proxy_deskbot(subpath: str):
    path = "/" + subpath.lstrip("/")
    method = request.method.upper()

    allowed_methods = _PROXY_METHODS.get(path)
    if allowed_methods is None:
        return jsonify({"ok": False, "error": "proxy_path_not_allowed"}), 404
    if method == "OPTIONS":
        return Response(status=204)
    if method not in allowed_methods:
        response = jsonify({"ok": False, "error": "proxy_method_not_allowed"})
        response.status_code = 405
        response.headers["Allow"] = ", ".join(sorted(allowed_methods))
        return response
    if path == "/api/devices":
        return _forward(method, path)

    device_id = _extract_device_id_from_request()
    if not device_id and _requires_device_id(path):
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            return jsonify(
                {
                    "ok": False,
                    "error": "device_id_required",
                    "message": "写操作必须明确指定目标设备",
                }
            ), 400
        current = get_current_device_id()
        if not current:
            return jsonify({"ok": False, "error": "请先选择设备"}), 400
        device_id = current

    return _forward(method, path, device_id=device_id)


def _upstream_auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    upstream_key = read_free_api_key_raw()
    if upstream_key:
        headers["X-API-Key"] = upstream_key
    return headers


def _forward(
    method: str,
    path: str,
    *,
    device_id: str | None = None,
) -> Response:
    base = deskbot_upstream_base().rstrip("/")
    query = request.query_string.decode("utf-8")
    if device_id and "device_id=" not in query:
        extra = urlparse.urlencode({"device_id": device_id})
        query = f"{query}&{extra}" if query else extra
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query}"

    headers = _upstream_auth_headers()
    data = None
    if method in ("POST", "PUT", "PATCH"):
        if request.is_json:
            payload = request.get_json(silent=True)
            if isinstance(payload, dict) and device_id and not payload.get("device_id"):
                payload = {**payload, "device_id": device_id}
            data = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = request.get_data()
            ctype = request.content_type
            if ctype:
                headers["Content-Type"] = ctype

    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=120) as resp:
            body = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "application/json")
    except urlerror.HTTPError as exc:
        body = exc.read()
        status = exc.code
        ctype = exc.headers.get("Content-Type", "application/json")
    except Exception as exc:
        logger.warning("proxy 转发失败 %s %s: %s", method, url, exc)
        return jsonify({"ok": False, "error": f"upstream error: {exc}"}), 502

    return Response(body, status=status, content_type=ctype)
