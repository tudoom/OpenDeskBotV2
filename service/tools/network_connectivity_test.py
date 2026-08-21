"""USB-only Deskbot service diagnostic.

The historical filename is retained for operator compatibility, but this tool
never opens a serial port and never impersonates a device over WebSocket. It
talks only to the PC service HTTP API and verifies that the requested robot is
represented by a live ``usb_cdc`` session.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the service API key on the explicitly selected service origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback_host(hostname: str) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_http_base(
    value: str,
    *,
    allow_insecure_http: bool = False,
) -> str:
    """Validate a credential-bearing PC service origin."""

    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("service URL must use http:// or https:// and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials must not be embedded in the service URL")
    if parsed.query or parsed.fragment:
        raise ValueError("service URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("service URL must be an origin without an API path")
    if (
        parsed.scheme == "http"
        and not _is_loopback_host(parsed.hostname)
        and not allow_insecure_http
    ):
        raise ValueError(
            "plaintext http:// is allowed only for loopback development; use https://"
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "", "", "")
    ).rstrip("/")


def _api_headers(api_key: str | None) -> dict[str, str]:
    key = str(api_key or "").strip()
    if not key:
        return {}
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError("invalid API key")
    return {"X-API-Key": key}


def _http_json(
    url: str,
    timeout: float = 10.0,
    *,
    api_key: str | None = None,
    method: str = "GET",
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json", **_api_headers(api_key)}
    normalized_method = str(method or "GET").upper()
    request = urllib.request.Request(
        url,
        data=b"" if normalized_method == "POST" else None,
        headers=headers,
        method=normalized_method,
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=max(0.1, float(timeout))) as response:
            status = int(response.status)
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PC service is unreachable: {exc.reason}") from exc

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {
            "ok": False,
            "error": "invalid_json_response",
            "body": raw[:500],
        }
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "invalid_json_response", "value": payload}
    return status, payload


def _find_device(payload: dict[str, Any], device_id: str) -> dict[str, Any] | None:
    rows = payload.get("devices")
    if not isinstance(rows, list):
        return None
    expected = str(device_id or "").strip()
    for row in rows:
        if isinstance(row, dict) and str(row.get("device_id") or "").strip() == expected:
            return row
    return None


def _is_live_usb_session(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    channels = row.get("channels")
    try:
        usb_count = (
            int(channels.get("usb_cdc") or 0) if isinstance(channels, dict) else 0
        )
    except (TypeError, ValueError):
        return False
    return (
        bool(row.get("online"))
        and str(row.get("transport") or "") == "usb_cdc"
        and usb_count > 0
    )


@dataclass
class TestReport:
    device_id: str
    base_url: str
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    control_latencies_ms: list[float] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.checks.append(str(message))

    def fail(self, message: str) -> None:
        self.failures.append(str(message))

    def summary(self) -> str:
        lines = [
            "",
            "=== Deskbot USB service diagnostic ===",
            f"device_id: {self.device_id}",
            f"service: {self.base_url}",
        ]
        lines.extend(f"PASS: {message}" for message in self.checks)
        lines.extend(f"FAIL: {message}" for message in self.failures)
        if self.control_latencies_ms:
            lines.append(
                "control played latency: "
                f"p50={statistics.median(self.control_latencies_ms):.0f}ms "
                f"max={max(self.control_latencies_ms):.0f}ms"
            )
        lines.append("全部通过" if not self.failures else f"失败项: {len(self.failures)}")
        return "\n".join(lines)


async def _send_pb_servo(
    base_url: str,
    device_id: str,
    dyaw: float,
    dpitch: float,
    ms: int = 150,
    *,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> tuple[float, dict[str, Any]]:
    """Submit a small control and wait for its terminal device ``played`` ACK."""

    operation_id = f"usb-servo:{uuid.uuid4().hex}"
    query = urllib.parse.urlencode(
        {
            "device_id": device_id,
            "dyaw": dyaw,
            "dpitch": dpitch,
            "ms": ms,
            "xm": 1,
            "ym": 1,
            "operation_id": operation_id,
        }
    )
    started = time.monotonic()
    status, submitted = await asyncio.to_thread(
        _http_json,
        f"{base_url.rstrip('/')}/api/device_servo?{query}",
        timeout,
        api_key=api_key,
        method="POST",
    )
    if status not in {200, 202}:
        raise RuntimeError(f"device_servo HTTP {status}: {submitted}")

    operation_id = str(submitted.get("operation_id") or operation_id)
    terminal = submitted
    deadline = time.monotonic() + max(0.1, float(timeout))
    while not bool(terminal.get("terminal")):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"device_servo operation timeout operation_id={operation_id}"
            )
        await asyncio.sleep(0.1)
        operation_query = urllib.parse.urlencode(
            {"device_id": device_id, "operation_id": operation_id}
        )
        status, terminal = await asyncio.to_thread(
            _http_json,
            f"{base_url.rstrip('/')}/api/control_operation?{operation_query}",
            min(8.0, timeout),
            api_key=api_key,
        )
        if status != 200:
            raise RuntimeError(f"control_operation HTTP {status}: {terminal}")

    operation = terminal.get("operation")
    operation_payload = operation if isinstance(operation, dict) else {}
    final_status = str(
        operation_payload.get("status") or terminal.get("status") or ""
    ).strip()
    if final_status != "completed":
        detail = (
            operation_payload.get("error_message")
            or operation_payload.get("error")
            or terminal.get("error")
            or final_status
        )
        raise RuntimeError(
            f"device_servo did not complete operation_id={operation_id}: {detail}"
        )
    result = dict(submitted)
    result.update(
        {
            "operation_id": operation_id,
            "status": final_status,
            "terminal": True,
            "operation": operation_payload,
        }
    )
    return started, result


async def run_tests(args: argparse.Namespace) -> TestReport:
    base_url = validate_http_base(
        args.base_url,
        allow_insecure_http=bool(getattr(args, "allow_insecure_transport", False)),
    )
    device_id = str(args.device_id or "").strip()
    if not device_id:
        raise ValueError("device_id is required")
    api_key = str(getattr(args, "api_key", "") or "").strip()
    if not api_key:
        raise ValueError("service API Key is required; set DESKBOT_API_KEY")

    timeout = max(0.1, float(getattr(args, "timeout", 10.0)))
    report = TestReport(device_id=device_id, base_url=base_url)

    status, health = await asyncio.to_thread(
        _http_json,
        f"{base_url}/health",
        timeout,
        api_key=api_key,
    )
    if status == 200 and bool(health.get("ok")):
        report.ok("PC service health endpoint is ready")
    else:
        report.fail(f"health HTTP {status}: {health}")

    status, devices = await asyncio.to_thread(
        _http_json,
        f"{base_url}/api/devices",
        timeout,
        api_key=api_key,
    )
    if status != 200:
        report.fail(f"device registry HTTP {status}: {devices}")
        print(report.summary())
        return report

    row = _find_device(devices, device_id)
    if row is None:
        report.fail("device is not listed by the PC service")
    elif not _is_live_usb_session(row):
        report.fail(
            "device exists but is not a live USB CDC session "
            f"(online={row.get('online')!r}, transport={row.get('transport')!r}, "
            f"channels={row.get('channels')!r})"
        )
    else:
        report.ok(
            "live USB CDC session detected "
            f"(generation={row.get('session_generation')!r}, "
            f"state={row.get('interaction_state')!r})"
        )

    control_rounds = max(0, int(getattr(args, "control_rounds", 0)))
    if row is not None and _is_live_usb_session(row):
        for index in range(control_rounds):
            try:
                started, _result = await _send_pb_servo(
                    base_url,
                    device_id,
                    float(getattr(args, "dyaw", 1.0)),
                    float(getattr(args, "dpitch", 0.0)),
                    int(getattr(args, "control_ms", 100)),
                    api_key=api_key,
                    timeout=timeout,
                )
            except Exception as exc:
                report.fail(f"control round {index + 1}: {exc}")
                continue
            latency_ms = (time.monotonic() - started) * 1000.0
            report.control_latencies_ms.append(latency_ms)
            report.ok(
                f"control round {index + 1} reached terminal played ACK "
                f"in {latency_ms:.0f}ms"
            )

    print(report.summary())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Deskbot USB CDC session through the PC service"
    )
    parser.add_argument("--device-id", required=True, help="device ID shown by the PC service")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:9000",
        help="PC service HTTP origin",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DESKBOT_API_KEY", ""),
        help="service API Key; prefer the DESKBOT_API_KEY environment variable",
    )
    parser.add_argument(
        "--allow-insecure-transport",
        action="store_true",
        help="allow plaintext HTTP to a non-loopback host in an isolated network",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--control-rounds",
        type=int,
        default=0,
        help="number of small servo controls to verify (default: read-only)",
    )
    parser.add_argument("--dyaw", type=float, default=1.0)
    parser.add_argument("--dpitch", type=float, default=0.0)
    parser.add_argument("--control-ms", type=int, default=100)
    args = parser.parse_args()

    report = asyncio.run(run_tests(args))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
