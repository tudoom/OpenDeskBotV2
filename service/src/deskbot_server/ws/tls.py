from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass

_PRODUCTION_ENVS = {"prod", "production"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ServerTls:
    context: ssl.SSLContext | None
    scheme: str
    terminated_by_proxy: bool


def _enabled(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUE_VALUES


def _native_tls_configured() -> bool:
    """Whether a complete native WebSocket TLS identity is configured."""

    cert_file = (os.environ.get("DESKBOT_SERVER_TLS_CERT") or "").strip()
    key_file = (os.environ.get("DESKBOT_SERVER_TLS_KEY") or "").strip()
    return bool(cert_file and key_file)


def secure_deployment_required(host: str) -> bool:
    """Return whether production security invariants must fail closed.

    A loopback bind is common behind a TLS reverse proxy, so host inspection
    alone cannot identify production. Either proxy-termination flag or a
    complete native WebSocket certificate/key pair is an explicit production
    signal even when an operator accidentally omits
    ``DESKBOT_ENV=production``. A partial pair is deliberately not treated as
    a valid signal; ``build_server_tls`` rejects that configuration directly.
    """

    production = (
        (os.environ.get("DESKBOT_ENV") or "").strip().lower()
        in _PRODUCTION_ENVS
    )
    proxy_terminated = _enabled(
        "DESKBOT_TLS_TERMINATED_BY_PROXY"
    ) or _enabled("DESKBOT_WEB_TLS_TERMINATED_BY_PROXY")
    if production or proxy_terminated or _native_tls_configured():
        return True
    if is_loopback_host(host):
        return False
    return not _enabled("DESKBOT_ALLOW_INSECURE_LOCAL")


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def build_server_tls(host: str) -> ServerTls:
    """Build the WebSocket TLS policy and fail closed in production.

    A TLS-terminating reverse proxy is accepted only when the Python process
    listens on loopback.  This prevents an accidental public plaintext socket
    from bypassing the proxy.
    """

    cert_file = (os.environ.get("DESKBOT_SERVER_TLS_CERT") or "").strip()
    key_file = (os.environ.get("DESKBOT_SERVER_TLS_KEY") or "").strip()
    proxy_tls = _enabled("DESKBOT_TLS_TERMINATED_BY_PROXY")
    production = secure_deployment_required(host)

    if bool(cert_file) != bool(key_file):
        raise RuntimeError(
            "DESKBOT_SERVER_TLS_CERT and DESKBOT_SERVER_TLS_KEY must be set together"
        )

    if cert_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return ServerTls(context=context, scheme="wss", terminated_by_proxy=False)

    if production:
        if not proxy_tls:
            raise RuntimeError(
                "production WebSocket service requires a TLS certificate/key or "
                "DESKBOT_TLS_TERMINATED_BY_PROXY=1"
            )
        if not is_loopback_host(host):
            raise RuntimeError(
                "a proxy-terminated plaintext WebSocket listener must bind to "
                "localhost/loopback in production"
            )

    return ServerTls(
        context=None,
        scheme="wss" if proxy_tls else "ws",
        terminated_by_proxy=proxy_tls,
    )


def validate_web_proxy_tls(host: str) -> None:
    """Require HTTPS proxy termination for the Flask console in production."""
    if not secure_deployment_required(host):
        return
    if not _enabled("DESKBOT_WEB_TLS_TERMINATED_BY_PROXY"):
        raise RuntimeError(
            "production Web console requires "
            "DESKBOT_WEB_TLS_TERMINATED_BY_PROXY=1"
        )
    if not is_loopback_host(host):
        raise RuntimeError(
            "a proxy-terminated Flask listener must bind to "
            "localhost/loopback in production"
        )
