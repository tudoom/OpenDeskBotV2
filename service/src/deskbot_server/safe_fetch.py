"""SSRF-resistant HTTP(S) opening for user supplied URLs."""
from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.aws.internal",
}


def _normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _assert_public_ip(value: str) -> None:
    ip = _normalized_ip(value)
    if not ip.is_global:
        raise ValueError(f"URL resolves to a non-public address: {ip}")


def validate_public_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL host is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in URLs are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if (
        host in _BLOCKED_HOSTS
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        raise ValueError("local or metadata hosts are not allowed")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        _assert_public_ip(host)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    return parsed


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _url_origin(parsed: urllib.parse.SplitResult) -> str:
    host = (parsed.hostname or "").rstrip(".").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = {
        "http": 80,
        "https": 443,
        "ws": 80,
        "wss": 443,
    }.get(parsed.scheme.lower())
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{suffix}"


def _origin_is_explicitly_allowed(
    parsed: urllib.parse.SplitResult,
    *,
    env_name: str = "DESKBOT_PROVIDER_PRIVATE_ORIGINS",
) -> bool:
    wanted = _url_origin(parsed)
    for raw in str(os.environ.get(env_name) or "").split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            allowed = urllib.parse.urlsplit(candidate)
            if (
                allowed.hostname
                and allowed.username is None
                and allowed.password is None
                and not allowed.query
                and not allowed.fragment
                and allowed.path in ("", "/")
                and _url_origin(allowed) == wanted
            ):
                return True
        except (TypeError, ValueError):
            continue
    return False


def validate_provider_http_url(url: str) -> urllib.parse.SplitResult:
    """Validate a credential-bearing provider endpoint.

    Public providers must use HTTPS.  An operator may explicitly trust an exact
    private origin (for example a local Ollama service) through
    ``DESKBOT_PROVIDER_PRIVATE_ORIGINS``; user-submitted URLs alone can never
    opt themselves into private-network access.
    """

    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("provider URL must use https")
    if not parsed.hostname:
        raise ValueError("provider URL host is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in provider URLs are not allowed")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid provider URL port") from exc
    if _origin_is_explicitly_allowed(parsed):
        return parsed
    parsed = validate_public_url(url)
    if (
        parsed.scheme.lower() != "https"
        and not _env_enabled("DESKBOT_ALLOW_INSECURE_PROVIDER_HTTP")
    ):
        raise ValueError("provider URL must use HTTPS")
    return parsed


def validate_provider_websocket_url(
    url: str,
    *,
    resolve_dns: bool = True,
) -> urllib.parse.SplitResult:
    """Validate a TTS WebSocket endpoint before handing it to websockets."""

    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in ("ws", "wss"):
        raise ValueError("provider WebSocket URL must use wss")
    if not parsed.hostname:
        raise ValueError("provider WebSocket host is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in provider WebSocket URLs are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid provider WebSocket port") from exc
    if _origin_is_explicitly_allowed(parsed):
        return parsed
    if (
        parsed.scheme.lower() != "wss"
        and not _env_enabled("DESKBOT_ALLOW_INSECURE_PROVIDER_WS")
    ):
        raise ValueError("provider WebSocket URL must use WSS")
    http_scheme = "https" if parsed.scheme.lower() == "wss" else "http"
    public_equivalent = urllib.parse.urlunsplit(
        (http_scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
    validate_public_url(public_equivalent)
    if resolve_dns:
        _public_sockaddrs(
            parsed.hostname,
            port or (443 if parsed.scheme.lower() == "wss" else 80),
        )
    return parsed


def _public_sockaddrs(host: str, port: int) -> list[tuple]:
    rows = _resolved_sockaddrs(host, port)
    safe: list[tuple] = []
    for family, socktype, proto, canonname, sockaddr in rows:
        _assert_public_ip(sockaddr[0])
        safe.append((family, socktype, proto, canonname, sockaddr))
    return safe


def _resolved_sockaddrs(host: str, port: int) -> list[tuple]:
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not rows:
        raise OSError("host did not resolve")
    return rows


def _connect_sockaddrs(
    rows: list[tuple],
    timeout: float | None,
) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in rows:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("unable to connect")


def _connect_public(host: str, port: int, timeout: float | None) -> socket.socket:
    # Connect to the exact validated sockaddr; no second DNS lookup occurs.
    return _connect_sockaddrs(_public_sockaddrs(host, port), timeout)


def connect_provider_websocket_socket(
    url: str,
    *,
    timeout: float | None = None,
) -> tuple[urllib.parse.SplitResult, socket.socket]:
    """Connect a provider WebSocket to one validated DNS result.

    The returned socket is already connected and non-blocking.  Callers must
    pass it to the WebSocket implementation together with the original URI;
    this preserves the HTTP Host header and TLS SNI/certificate hostname while
    eliminating a second resolver lookup.
    """

    parsed = validate_provider_websocket_url(url, resolve_dns=False)
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if parsed.scheme.lower() == "wss" else 80)
    rows = (
        _resolved_sockaddrs(host, port)
        if _origin_is_explicitly_allowed(parsed)
        else _public_sockaddrs(host, port)
    )
    sock = _connect_sockaddrs(rows, timeout)
    sock.setblocking(False)
    return parsed, sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_public(self.host, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        raw = _connect_public(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        validate_public_url(req.full_url)
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        validate_public_url(req.full_url)
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "provider redirects are not allowed",
            headers,
            fp,
        )


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
    _ValidatedRedirectHandler(),
)

_PUBLIC_PROVIDER_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
    _RejectRedirectHandler(),
)

_EXPLICIT_PRIVATE_PROVIDER_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _RejectRedirectHandler(),
)


def safe_urlopen(request_or_url, *, timeout: float | None = None):
    url = request_or_url.full_url if hasattr(request_or_url, "full_url") else str(request_or_url)
    validate_public_url(url)
    return _OPENER.open(request_or_url, timeout=timeout)


def safe_provider_urlopen(request_or_url, *, timeout: float | None = None):
    """Open a provider API URL with HTTPS/private-origin and DNS pinning rules."""

    url = request_or_url.full_url if hasattr(request_or_url, "full_url") else str(request_or_url)
    parsed = validate_provider_http_url(url)
    opener = (
        _EXPLICIT_PRIVATE_PROVIDER_OPENER
        if _origin_is_explicitly_allowed(parsed)
        else _PUBLIC_PROVIDER_OPENER
    )
    return opener.open(request_or_url, timeout=timeout)
