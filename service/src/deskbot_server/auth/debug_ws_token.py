"""Short-lived bearer token for the local debug console WebSockets."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "deskbot-debug-ws-v1"
DEBUG_WS_SUBPROTOCOL = "deskbot.debug.v1"
DEBUG_WS_TOKEN_PROTOCOL_PREFIX = "deskbot.debug.auth."


def select_debug_ws_subprotocol(_connection, offered_subprotocols) -> str | None:
    """Select only the public protocol marker, never the bearer-token protocol."""
    if DEBUG_WS_SUBPROTOCOL in offered_subprotocols:
        return DEBUG_WS_SUBPROTOCOL
    # Keep header- and opt-in query-auth clients compatible. Authentication is
    # still enforced by the route after the WebSocket handshake.
    return None


def debug_ws_server_options() -> dict[str, object]:
    """Return the optional server negotiation used by browser debug sockets."""
    return {
        "subprotocols": [DEBUG_WS_SUBPROTOCOL],
        "select_subprotocol": select_debug_ws_subprotocol,
    }


def _default_ttl_seconds() -> int:
    raw = (os.environ.get("DESKBOT_DEBUG_WS_TOKEN_SECONDS") or "600").strip()
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 600
    # Browser tokens are bearer credentials.  Keep them long enough for a
    # debugging session, but never turn a copied value into a multi-day key.
    return max(60, min(seconds, 3600))


def _secret_key() -> str:
    secret = (os.environ.get("DESKBOT_WEB_SECRET_KEY") or "").strip()
    if len(secret) < 32:
        raise RuntimeError(
            "DESKBOT_WEB_SECRET_KEY must be set to at least 32 characters "
            "before debug WebSocket tokens can be issued"
        )
    return secret


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


@dataclass(frozen=True)
class DebugWsTokenInfo:
    token: str
    expires_in: int


def issue_debug_ws_token() -> DebugWsTokenInfo:
    """Issue a short-lived local-console token."""
    serializer = _serializer()
    expires_in = _default_ttl_seconds()
    token = serializer.dumps(
        {
            "aud": "debug_ws",
            "nonce": secrets.token_urlsafe(8),
        }
    )
    return DebugWsTokenInfo(token=token, expires_in=expires_in)


def verify_debug_ws_token(token: str) -> bool:
    raw = str(token or "").strip()
    if not raw:
        return False
    try:
        data = _serializer().loads(raw, max_age=_default_ttl_seconds())
    except (BadSignature, SignatureExpired, RuntimeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("aud") != "debug_ws":
        return False
    return bool(str(data.get("nonce") or "").strip())


def debug_ws_subprotocols(token: str) -> list[str]:
    raw = str(token or "").strip()
    if not raw:
        return [DEBUG_WS_SUBPROTOCOL]
    return [DEBUG_WS_SUBPROTOCOL, f"{DEBUG_WS_TOKEN_PROTOCOL_PREFIX}{raw}"]


def extract_debug_token_from_headers(headers) -> str | None:
    if headers is None:
        return None
    for key in ("X-Deskbot-Debug-Token", "X-Deskbot-Web-Token"):
        val = str(headers.get(key) or "").strip()
        if val:
            return val
    requested = str(headers.get("Sec-WebSocket-Protocol") or "")
    for item in requested.split(","):
        protocol = item.strip()
        if protocol.startswith(DEBUG_WS_TOKEN_PROTOCOL_PREFIX):
            token = protocol[len(DEBUG_WS_TOKEN_PROTOCOL_PREFIX) :].strip()
            if token:
                return token
    return None


def _query_tokens_enabled() -> bool:
    return (os.environ.get("DESKBOT_ALLOW_DEBUG_TOKEN_IN_QUERY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def extract_debug_token_from_query(qargs: dict) -> str | None:
    if not _query_tokens_enabled():
        return None
    for key in ("debug_token", "debugtoken", "ws_token"):
        val = str(qargs.get(key) or "").strip()
        if val:
            return val
    return None
