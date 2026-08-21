from __future__ import annotations

import json
import os
import socket
from urllib.parse import urlsplit

from flask import request

from deskbot_server.config import load_config as _load_config_yaml
from deskbot_server.config import save_config as _save_config_yaml
from deskbot_server.core.clock import utcnow
from deskbot_server.paths import DEFAULT_CONFIG_PATH

CONFIG_PATH = DEFAULT_CONFIG_PATH


def load_config():
    return _load_config_yaml(str(CONFIG_PATH))


def save_config(cfg):
    _save_config_yaml(cfg, CONFIG_PATH)


def tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _prefer_access_host(raw_host: str) -> str:
    host = (raw_host or "").strip()
    if host and host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        return host
    req_host = (request.host or "").split(":", 1)[0].strip()
    if req_host and req_host not in ("0.0.0.0", "::", "localhost"):
        return req_host
    return "127.0.0.1"


def deskbot_ws_base() -> tuple[str, int]:
    cfg = load_config()
    srv = cfg.get("server") or {}
    public = (os.environ.get("DESKBOT_WEB_PUBLIC_HOST") or "").strip() or str(
        srv.get("web_public_host") or ""
    ).strip()
    if public:
        host = public
    else:
        host = _prefer_access_host(
            os.environ.get("DESKBOT_SERVER_HOST") or srv.get("host", "127.0.0.1")
        )
    port = int(os.environ.get("DESKBOT_SERVER_PORT") or srv.get("port", 9000))
    return host, port


def _deskbot_public_ws_origin() -> str:
    configured = (os.environ.get("DESKBOT_WS_PUBLIC_BASE") or "").strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DESKBOT_WS_PUBLIC_BASE must be a ws:// or wss:// origin")
        return configured

    secure = bool(
        request.is_secure
        or (os.environ.get("DESKBOT_SERVER_TLS_CERT") or "").strip()
        or (os.environ.get("DESKBOT_TLS_TERMINATED_BY_PROXY") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    host, port = deskbot_ws_base()
    return f"{'wss' if secure else 'ws'}://{host}:{port}"


def deskbot_ws_default() -> str:
    cfg = load_config()
    ws_path = os.environ.get("DESKBOT_WS_PATH") or cfg.get("server", {}).get("ws_path", "/asr_chat")
    if not str(ws_path).startswith("/"):
        ws_path = f"/{ws_path}"
    return f"{_deskbot_public_ws_origin()}{ws_path}"


def device_pipeline_ws_base() -> str:
    return f"{_deskbot_public_ws_origin()}/device_pipeline"


def camera_view_ws_base() -> str:
    return f"{_deskbot_public_ws_origin()}/camera_view"


def deskbot_http_base() -> str:
    return "/proxy/deskbot"


def _fetch_upstream_device_list(
    path: str,
    *,
    timeout: float = 1.5,
) -> tuple[list[dict], str | None]:
    """Fetch a realtime device list without collapsing transport failure.

    The web console needs to distinguish a healthy registry containing no connection
    from an unreachable :9000 process.  Returning an error code (rather than the raw
    exception) keeps the response useful without leaking hosts, tokens or credentials.
    """
    from urllib import error as urlerror
    from urllib import request as urlrequest

    base = deskbot_upstream_base().rstrip("/")
    normalized_path = "/" + str(path or "").strip().lstrip("/")
    url = f"{base}{normalized_path}"
    headers = {"Accept": "application/json"}
    from deskbot_server.auth.api_key_service import read_free_api_key_raw

    upstream_key = read_free_api_key_raw()
    if upstream_key:
        headers["X-API-Key"] = upstream_key
    req = urlrequest.Request(url, headers=headers, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        return [], f"upstream_http_{exc.code}"
    except (urlerror.URLError, OSError, TimeoutError):
        return [], "upstream_unreachable"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], "upstream_invalid_response"
    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, list):
        return [], "upstream_invalid_response"
    return [d for d in devices if isinstance(d, dict)], None


def _fetch_upstream_devices_snapshot(
    *, timeout: float = 1.5
) -> tuple[list[dict], str | None]:
    return _fetch_upstream_device_list(
        "/api/devices",
        timeout=timeout,
    )


def fetch_attached_usb_snapshot(*, timeout: float = 1.5) -> dict:
    """Return USB CDC devices physically attached to this service host.

    The upstream endpoint returns online ``usb_cdc`` producers and is reached
    with the PC-local service key. No browser or user identity is forwarded.
    """

    devices, error = _fetch_upstream_device_list(
        "/api/usb-devices",
        timeout=timeout,
    )
    return {
        "ok": error is None,
        "observed_at": utcnow().isoformat(),
        "error": error,
        "devices": devices,
    }




def fetch_live_device_snapshot(*, timeout: float = 1.5) -> dict:
    """Return registry reachability and device presence from one observation."""
    devices, error = _fetch_upstream_devices_snapshot(timeout=timeout)
    out: dict[str, dict] = {}
    for row in devices:
        did = str(row.get("device_id") or "").strip()
        if not did:
            continue
        out[did] = {
            "online": bool(row.get("online")),
            "last_seen": str(row.get("last_seen") or "").strip() or None,
            "interaction_state": str(row.get("interaction_state") or "").strip() or None,
            "transport": str(row.get("transport") or "").strip() or None,
            "session_generation": row.get("session_generation"),
            "microphone_health": (
                dict(row.get("microphone_health"))
                if isinstance(row.get("microphone_health"), dict)
                else None
            ),
            "channels": (
                dict(row.get("channels"))
                if isinstance(row.get("channels"), dict)
                else {}
            ),
        }
    return {
        "ok": error is None,
        "observed_at": utcnow().isoformat(),
        "error": error,
        "devices": out,
    }


def fetch_live_device_details(*, timeout: float = 1.5) -> dict[str, dict]:
    """查询 deskbot-server 内存注册表，返回 device_id -> {online, last_seen}。"""
    return fetch_live_device_snapshot(timeout=timeout)["devices"]




def deskbot_upstream_base() -> str:
    configured = (os.environ.get("DESKBOT_SERVER_INTERNAL_BASE") or "").strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("DESKBOT_SERVER_INTERNAL_BASE must be an http(s) URL")
        return configured
    cfg = load_config()
    srv = cfg.get("server") or {}
    host = os.environ.get("DESKBOT_SERVER_HOST") or srv.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = int(os.environ.get("DESKBOT_SERVER_PORT") or srv.get("port", 9000))
    direct_tls = bool((os.environ.get("DESKBOT_SERVER_TLS_CERT") or "").strip())
    return f"{'https' if direct_tls else 'http'}://{host}:{port}"


ALLOWED_LLM_ROLES = {"system", "user", "assistant"}
