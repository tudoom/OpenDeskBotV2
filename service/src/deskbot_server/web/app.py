from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from deskbot_server.db import init_database, remove_session
from deskbot_server.device_data import ensure_local_data_initialized
from deskbot_server.env import load_dotenv
from deskbot_server.web.blueprints.app2c_bp import bp as app2c_bp
from deskbot_server.web.blueprints.app_bp import bp as app_bp
from deskbot_server.web.blueprints.asr_bp import bp as asr_bp
from deskbot_server.web.blueprints.debug_bp import bp as debug_bp
from deskbot_server.web.blueprints.management_bp import bp as management_bp
from deskbot_server.web.blueprints.proxy_bp import bp as proxy_bp
from deskbot_server.web.blueprints.site import bp as site_bp
from deskbot_server.ws.tls import is_loopback_host, validate_web_proxy_tls

_WEB_DIR = Path(__file__).resolve().parent
_DEV_SECRET = secrets.token_urlsafe(48)


def _local_request_host_is_valid(raw_host: str) -> bool:
    """Accept only an unambiguous localhost/loopback Host header."""

    value = str(raw_host or "").strip()
    if not value or any(char in value for char in "@/\\, \t\r\n"):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        # Accessing ``port`` validates its syntax and numeric range.
        parsed.port
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        return False
    return is_loopback_host(parsed.hostname)


def web_debug_enabled() -> bool:
    """Return whether Flask's development debugger was explicitly enabled."""

    raw = (os.environ.get("DESKBOT_WEB_DEBUG") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def create_app() -> Flask:
    load_dotenv()
    host = (os.environ.get("DESKBOT_WEB_HOST") or "127.0.0.1").strip()
    if not is_loopback_host(host):
        raise RuntimeError("the local console must bind to a loopback host")
    validate_web_proxy_tls(host)
    init_database()
    ensure_local_data_initialized()

    app = Flask(
        __name__,
        template_folder=str(_WEB_DIR / "templates"),
        static_folder=str(_WEB_DIR / "static"),
    )
    app.config["LOCAL_CONSOLE_ENABLED"] = True
    secret = (os.environ.get("DESKBOT_WEB_SECRET_KEY") or "").strip()
    if not secret:
        secret = _DEV_SECRET
        # The session only stores local UI state (for example the selected USB
        # connection), but it still needs an unpredictable signing key.
        os.environ["DESKBOT_WEB_SECRET_KEY"] = secret
    app.config["SECRET_KEY"] = secret
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = False
    app.jinja_env.auto_reload = True

    app.register_blueprint(site_bp)
    app.register_blueprint(app_bp)
    app.register_blueprint(asr_bp)
    app.register_blueprint(app2c_bp)
    app.register_blueprint(management_bp)
    app.register_blueprint(debug_bp)
    app.register_blueprint(proxy_bp)

    @app.before_request
    def protect_local_console():
        # Passwordless is safe here only because the process and every accepted
        # request are constrained to this PC.
        if not _local_request_host_is_valid(request.host):
            return jsonify({"ok": False, "error": "invalid_local_host"}), 421
        if request.method == "OPTIONS":
            return None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = (request.headers.get("Sec-Fetch-Site") or "").lower()
            if fetch_site == "cross-site":
                return jsonify(
                    {"ok": False, "error": "cross_site_request_rejected"}
                ), 403
            origin = (request.headers.get("Origin") or "").strip()
            if origin:
                try:
                    origin_host = urlsplit(origin).netloc.lower()
                except ValueError:
                    origin_host = ""
                if not origin_host or origin_host != request.host.lower():
                    return jsonify({"ok": False, "error": "origin_mismatch"}), 403
        return None

    @app.after_request
    def apply_security_headers(resp):
        if (request.path or "").startswith("/debug/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # script-src 兜底存储型 XSS：外部脚本一律只允许同源（第三方 CDN 被
        # 拒绝）。模板目前包含大量内联 <script> 且 Vue 全量构建在运行时用
        # new Function 编译模板，因此暂保留 'unsafe-inline' / 'unsafe-eval'；
        # 目标是把内联脚本迁到 static/ 并换用运行时版 Vue 后逐步移除两者，
        # 收敛到 script-src 'self'。frame-ancestors 与 X-Frame-Options DENY 对齐。
        resp.headers.setdefault(
            "Content-Security-Policy",
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )
        resp.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(self), geolocation=()",
        )
        return resp

    @app.teardown_appcontext
    def shutdown_session(_exc=None):
        remove_session()

    @app.context_processor
    def inject_globals():
        from deskbot_server.web.session_device import get_current_device_id

        return {
            "nav_current_device_id": get_current_device_id(),
        }

    return app


app = create_app()


if __name__ == "__main__":
    host = (os.environ.get("DESKBOT_WEB_HOST") or "127.0.0.1").strip()
    port = int(os.environ.get("DESKBOT_WEB_PORT") or "5050")
    app.run(host=host, port=port, debug=web_debug_enabled())
