from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from deskbot_server.auth.api_key_service import (
    ApiKeyAuth,
    QuotaExceededError,
    api_key_auth_is_current,
    authenticate_api_key,
    extract_api_key_from_headers,
    extract_api_key_from_query,
)
from deskbot_server.auth.debug_ws_token import (
    extract_debug_token_from_headers,
    extract_debug_token_from_query,
    verify_debug_ws_token,
)

logger = logging.getLogger("deskbot-server")

DEFAULT_LLM_TTS_RESERVATION_BYTES = 1_048_576
MAX_LLM_TTS_RESERVATION_BYTES = 64 * 1_048_576


@dataclass(frozen=True)
class DebugSubscriberAuth:
    """Revocable local credential retained by a debug observer WebSocket."""

    mode: str
    device_id: str
    raw_credential: str = field(repr=False)
    api_auth: ApiKeyAuth | None = None


QUOTA_MESSAGE = "今日本机配额已用完（1GB/日），请明日再试"


def _query_api_keys_enabled() -> bool:
    return (os.environ.get("DESKBOT_ALLOW_API_KEY_IN_QUERY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _raw_api_key(*, qargs: dict, headers) -> str | None:
    from_query = extract_api_key_from_query(qargs) if _query_api_keys_enabled() else None
    return from_query or extract_api_key_from_headers(headers)


def resolve_api_key(*, qargs: dict, headers) -> ApiKeyAuth | None:
    raw = _raw_api_key(qargs=qargs, headers=headers)
    if not raw:
        return None
    return authenticate_api_key(raw)


def ws_auth_failure_response(*, reason: str, message: str) -> tuple[int, list, bytes]:
    body = json.dumps({"ok": False, "error": reason, "message": message}, ensure_ascii=False)
    return (
        401,
        [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")],
        body.encode("utf-8"),
    )




async def ws_require_debug_subscriber_auth(
    websocket,
    qargs: dict,
    *,
    device_id: str | None = None,
    require_device: bool = False,
) -> DebugSubscriberAuth | None:
    """Authenticate a debug observer and retain revocable authorization."""
    headers = getattr(websocket, "request", None)
    hdr_map = headers.headers if headers is not None else {}
    raw_api_key = _raw_api_key(qargs=qargs, headers=hdr_map) or ""
    auth = authenticate_api_key(raw_api_key) if raw_api_key else None
    if auth is not None:
        try:
            from deskbot_server.auth.api_key_service import check_quota

            check_quota(auth.api_key_id, 0)
        except QuotaExceededError:
            await websocket.close(code=1008, reason="quota_exhausted")
            return None
        try:
            require_routed_device_id(auth, device_id, require_device=require_device)
        except PermissionError as exc:
            await websocket.close(code=1008, reason=str(exc) or "invalid_device_route")
            return None
        return DebugSubscriberAuth(
            mode="api_key",
            device_id=str(device_id or "").strip(),
            raw_credential=raw_api_key,
            api_auth=auth,
        )

    raw_token = extract_debug_token_from_headers(hdr_map) or extract_debug_token_from_query(qargs)
    if not raw_token:
        logger.warning("debug subscriber WS rejected: auth_required device_id=%s", device_id)
        await websocket.close(code=1008, reason="auth_required")
        return None

    if not verify_debug_ws_token(raw_token):
        logger.warning("debug subscriber WS rejected: invalid_debug_token device_id=%s", device_id)
        await websocket.close(code=1008, reason="invalid_debug_token")
        return None

    did = str(device_id or "").strip()
    if require_device and not did:
        logger.warning("debug subscriber WS rejected: device_id_required")
        await websocket.close(code=1008, reason="device_id_required")
        return None

    logger.info("local debug-token WS accepted device_id=%s", did or None)
    return DebugSubscriberAuth(
        mode="local_token",
        device_id=did,
        raw_credential=raw_token,
    )


def debug_subscriber_auth_is_current(auth: DebugSubscriberAuth) -> bool:
    """Revalidate the observer's local credential."""

    try:
        if auth.mode == "api_key":
            expected = auth.api_auth
            if expected is None or not api_key_auth_is_current(
                auth.raw_credential,
                expected,
            ):
                return False
            require_routed_device_id(
                expected,
                auth.device_id,
                require_device=True,
            )
            return True
        if auth.mode == "local_token":
            return bool(auth.device_id and verify_debug_ws_token(auth.raw_credential))
    except Exception:
        logger.exception(
            "debug subscriber authorization refresh failed "
            "mode=%s device_id=%s",
            auth.mode,
            auth.device_id,
        )
    return False


async def run_with_debug_subscriber_auth_watchdog(
    websocket,
    auth: DebugSubscriberAuth,
    awaitable,
    *,
    poll_interval: float = 2.0,
):
    """Stop an observer promptly when its token or key changes."""

    handler = asyncio.create_task(awaitable)
    interval = max(0.05, float(poll_interval))
    try:
        while not handler.done():
            done, _pending = await asyncio.wait(
                {handler},
                timeout=interval,
            )
            if done:
                return await handler
            current = await asyncio.to_thread(
                debug_subscriber_auth_is_current,
                auth,
            )
            if current:
                continue
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "credential_revoked",
                            "reason": "authorization_changed",
                        },
                        ensure_ascii=False,
                    )
                )
            except Exception:
                pass
            await websocket.close(
                code=1008,
                reason="authorization_revoked",
            )
            handler.cancel()
            try:
                await handler
            except (asyncio.CancelledError, Exception):
                pass
            return None
        return await handler
    finally:
        if not handler.done():
            handler.cancel()


def http_require_api_key(qargs: dict, headers) -> ApiKeyAuth:
    auth = resolve_api_key(qargs=qargs, headers=headers)
    if auth is not None:
        from deskbot_server.auth.api_key_service import check_quota

        check_quota(auth.api_key_id, 0)
        return auth
    raise PermissionError("api_key_required")


def require_routed_device_id(
    auth: ApiKeyAuth | None,
    device_id: str | None,
    *,
    require_device: bool = False,
) -> None:
    """Validate only the routing requirement; keys never own devices."""
    did = str(device_id or "").strip()
    if not did:
        if require_device:
            raise PermissionError("device_id_required")
        return
    if auth is None:
        raise PermissionError("api_key_required")
    return


def http_require_device_route(
    auth: ApiKeyAuth | None,
    device_id: str | None,
    *,
    require_device: bool = False,
) -> None:
    require_routed_device_id(auth, device_id, require_device=require_device)


def record_turn_usage(
    api_key_id: str,
    *,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> None:
    from deskbot_server.auth.api_key_service import record_usage_batch_checked

    record_usage_batch_checked(
        api_key_id,
        asr_bytes=asr_bytes,
        face_bytes=face_bytes,
        llm_bytes=llm_bytes,
        tts_bytes=tts_bytes,
    )


def adjust_turn_usage(
    api_key_id: str,
    *,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> None:
    from deskbot_server.auth.api_key_service import adjust_usage_batch_checked

    adjust_usage_batch_checked(
        api_key_id,
        asr_bytes=asr_bytes,
        face_bytes=face_bytes,
        llm_bytes=llm_bytes,
        tts_bytes=tts_bytes,
    )


def llm_tts_reservation_bytes() -> int:
    """Return the per-turn generation budget reserved before provider calls."""

    raw = (
        os.environ.get("DESKBOT_LLM_TTS_RESERVE_BYTES")
        or str(DEFAULT_LLM_TTS_RESERVATION_BYTES)
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_LLM_TTS_RESERVATION_BYTES
    if value <= 0:
        value = DEFAULT_LLM_TTS_RESERVATION_BYTES
    return min(value, MAX_LLM_TTS_RESERVATION_BYTES)


async def enforce_turn_usage(
    websocket,
    api_key_id: str,
    *,
    device_id: str | None = None,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> bool:
    """Consume usage or terminate a long-lived producer connection.

    Quota and accounting-database failures are both fail-closed.  Returning
    ``False`` tells the caller to stop the current frame/turn immediately.
    """

    try:
        await asyncio.to_thread(
            record_turn_usage,
            api_key_id,
            asr_bytes=asr_bytes,
            face_bytes=face_bytes,
            llm_bytes=llm_bytes,
            tts_bytes=tts_bytes,
        )
        return True
    except QuotaExceededError:
        error = "quota_exhausted"
        message = QUOTA_MESSAGE
        close_code = 4008
        logger.warning(
            "API quota exhausted api_key_id=%s device_id=%s",
            api_key_id,
            device_id,
        )
    except Exception:
        error = "usage_accounting_unavailable"
        message = "用量计费暂不可用，连接已安全停止，请稍后重试"
        close_code = 1011
        logger.exception(
            "usage accounting failed closed api_key_id=%s device_id=%s",
            api_key_id,
            device_id,
        )
    try:
        await websocket.send(
            json.dumps(
                {
                    "type": "error",
                    "error": error,
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        pass
    try:
        await websocket.close(code=close_code, reason=error)
    except Exception:
        pass
    return False


async def settle_turn_usage(
    websocket,
    api_key_id: str,
    *,
    device_id: str | None = None,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> bool:
    """Apply signed settlement deltas, failing the producer connection closed."""

    try:
        await asyncio.to_thread(
            adjust_turn_usage,
            api_key_id,
            asr_bytes=asr_bytes,
            face_bytes=face_bytes,
            llm_bytes=llm_bytes,
            tts_bytes=tts_bytes,
        )
        return True
    except QuotaExceededError:
        error = "quota_exhausted"
        message = QUOTA_MESSAGE
        close_code = 4008
        logger.warning(
            "API quota exhausted during settlement api_key_id=%s device_id=%s",
            api_key_id,
            device_id,
        )
    except Exception:
        error = "usage_accounting_unavailable"
        message = (
            "Usage accounting is temporarily unavailable; "
            "the connection was stopped safely."
        )
        close_code = 1011
        logger.exception(
            "usage settlement failed closed api_key_id=%s device_id=%s",
            api_key_id,
            device_id,
        )
    try:
        await websocket.send(
            json.dumps(
                {
                    "type": "error",
                    "error": error,
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        pass
    try:
        await websocket.close(code=close_code, reason=error)
    except Exception:
        pass
    return False


@dataclass
class TurnUsageReservation:
    """One in-process, exactly-once handle for a generation reservation."""

    api_key_id: str
    device_id: str | None
    reserved_bytes: int
    _settled: bool = field(default=False, init=False, repr=False)

    async def settle(
        self,
        websocket,
        *,
        llm_bytes: int,
        tts_bytes: int,
    ) -> bool:
        if self._settled:
            return True
        self._settled = True
        return await settle_turn_usage(
            websocket,
            self.api_key_id,
            device_id=self.device_id,
            llm_bytes=max(0, int(llm_bytes)) - self.reserved_bytes,
            tts_bytes=max(0, int(tts_bytes)),
        )

    async def refund(self, websocket) -> bool:
        return await self.settle(websocket, llm_bytes=0, tts_bytes=0)


async def reserve_llm_tts_usage(
    websocket,
    api_key_id: str,
    *,
    device_id: str | None = None,
) -> TurnUsageReservation | None:
    """Reserve a bounded LLM/TTS turn before invoking external providers."""

    reserved = llm_tts_reservation_bytes()
    accepted = await enforce_turn_usage(
        websocket,
        api_key_id,
        device_id=device_id,
        llm_bytes=reserved,
    )
    if not accepted:
        return None
    return TurnUsageReservation(
        api_key_id=api_key_id,
        device_id=device_id,
        reserved_bytes=reserved,
    )


async def run_generation_with_reserved_quota(
    websocket,
    *,
    api_key_id: str | None,
    device_id: str | None,
    user_text: str,
    turn_factory: Callable[[], Awaitable[dict]],
) -> tuple[bool, dict | None]:
    """Run provider work only after an atomic LLM/TTS quota reservation."""

    if not api_key_id:
        return True, await turn_factory()
    reservation = await reserve_llm_tts_usage(
        websocket,
        api_key_id,
        device_id=device_id,
    )
    if reservation is None:
        return False, None
    try:
        generated = await turn_factory()
    except asyncio.CancelledError:
        # Keep the compensating refund alive when audio_cancel interrupts this
        # task. A repeated cancellation may stop this waiter, but the shielded
        # accounting task remains scheduled to completion.
        await asyncio.shield(reservation.refund(websocket))
        raise
    except Exception:
        await reservation.refund(websocket)
        raise

    if str(generated.get("status") or "ok") != "ok":
        return await reservation.refund(websocket), generated

    llm_out = generated.get("llm_raw") or generated.get("llm_text") or ""
    llm_bytes = len(user_text.encode("utf-8")) + len(
        str(llm_out).encode("utf-8")
    )
    tts_bytes = len(str(generated.get("llm_text") or "").encode("utf-8")) * 48
    settled = await reservation.settle(
        websocket,
        llm_bytes=llm_bytes,
        tts_bytes=tts_bytes,
    )
    return settled, generated
