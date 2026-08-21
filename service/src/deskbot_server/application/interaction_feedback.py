"""PB servo payload helpers for explicit interaction actions."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from deskbot_server.pb.llm_plan import expand_llm_moves
from deskbot_server.pb.shapes import PB_ACTION_REPLACE, PB_LEVEL_TASK
from deskbot_server.servo_protocol import (
    ServoProtocolError,
    servo_sequence_completion_budget_ms,
)
from deskbot_server.ws.asr_chat_hub import AsrChatHub

logger = logging.getLogger("deskbot-server")


@dataclass(frozen=True)
class ServoMoveDelivery:
    """Transport and device-playback outcome for one servo-only PB request."""

    request_id: str
    delivered: int
    accepted: bool
    played: bool
    status: str

    @property
    def ok(self) -> bool:
        return self.played


def build_servo_only_pb_payload(
    moves: list[dict[str, Any]],
    *,
    request_id: Optional[str] = None,
) -> Optional[tuple[dict[str, Any], str]]:
    """Convert LLM-style moves to a servo-only ``pb_single`` payload."""
    try:
        steps = expand_llm_moves(moves)
    except (ServoProtocolError, TypeError, ValueError):
        logger.warning(
            "[interaction_feedback] rejected invalid servo plan",
            exc_info=True,
        )
        return None
    if not steps:
        return None
    req_id = request_id or uuid.uuid4().hex[:16]
    chunk_ms = sum(max(1, int(s.get("ms") or 0)) for s in steps)
    payload: dict[str, Any] = {
        "type": "pb_single",
        "req": req_id,
        "idx": 0,
        "chunk_ms": chunk_ms,
        "pb_ver": 2,
        "action": PB_ACTION_REPLACE,
        "level": PB_LEVEL_TASK,
        "servo": [dict(step) for step in steps],
    }
    return payload, req_id


async def send_servo_moves_and_wait(
    hub: AsrChatHub,
    device_id: str,
    moves: list[dict[str, Any]],
    *,
    source: str,
    summary: str,
    request_id: Optional[str] = None,
    accepted_timeout: Optional[float] = None,
    played_timeout: Optional[float] = None,
) -> ServoMoveDelivery:
    """Send one servo-only PB and require both accepted and played ACKs."""
    from deskbot_server.ws.pb_ack_waiter import (
        pb_ack_gate,
        pb_wait_ack_timeout_sec,
        pb_wait_played_timeout_sec,
    )

    built = build_servo_only_pb_payload(moves, request_id=request_id)
    if built is None:
        return ServoMoveDelivery(
            request_id=str(request_id or ""),
            delivered=0,
            accepted=False,
            played=False,
            status="invalid_move",
        )
    payload, req_id = built
    try:
        await pb_ack_gate.begin_req(device_id, req_id)
    except RuntimeError:
        logger.warning(
            "[interaction_feedback] duplicate active request "
            "device_id=%s req=%s source=%s",
            device_id,
            req_id,
            source,
        )
        return ServoMoveDelivery(
            request_id=req_id,
            delivered=0,
            accepted=False,
            played=False,
            status="request_active",
        )

    delivered = 0
    status = "send_failed"
    accepted = False
    played = False
    accepted_task = asyncio.create_task(
        pb_ack_gate.wait_accepted_result(
            device_id,
            req_id,
            int(payload.get("idx") or 0),
            timeout=(
                pb_wait_ack_timeout_sec()
                if accepted_timeout is None
                else accepted_timeout
            ),
        )
    )
    # Let the waiter capture the request state before a synchronously failing
    # transport can remove it during ``hub.send``.
    await asyncio.sleep(0)
    try:
        try:
            delivered = await hub.send(device_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[interaction_feedback] servo send failed "
                "device_id=%s req=%s source=%s",
                device_id,
                req_id,
                source,
            )
            delivered = 0
        if delivered <= 0:
            status = "device_unavailable"
            if not accepted_task.done():
                accepted_task.cancel()
            await asyncio.gather(accepted_task, return_exceptions=True)
        else:
            accepted_result = await accepted_task
            accepted = accepted_result.accepted_idx >= int(
                payload.get("idx") or 0
            )
            status = accepted_result.status
            if accepted_result.ok:
                playback_ms = servo_sequence_completion_budget_ms(
                    payload.get("servo") or [],
                    context="interaction feedback servo",
                )
                played_result = await pb_ack_gate.wait_played_result(
                    device_id,
                    req_id,
                    int(payload.get("idx") or 0),
                    timeout=(
                        pb_wait_played_timeout_sec(playback_ms)
                        if played_timeout is None
                        else played_timeout
                    ),
                )
                played = played_result.ok
                status = played_result.status
    finally:
        if not accepted_task.done():
            accepted_task.cancel()
            await asyncio.gather(accepted_task, return_exceptions=True)
        await pb_ack_gate.end_req(device_id, req_id)

    from deskbot_server.ws.device_pipeline import publish_auto_dispatch_event

    await publish_auto_dispatch_event(
        hub.pipeline_broker,
        device_id=device_id,
        request_id=req_id,
        source=source,
        summary=summary,
        status="ok" if played else "error",
        error=None if played else status,
    )
    logger.info(
        "[interaction_feedback] terminal servo delivery "
        "device_id=%s req=%s delivered=%s accepted=%s played=%s status=%s",
        device_id,
        req_id,
        delivered,
        accepted,
        played,
        status,
    )
    return ServoMoveDelivery(
        request_id=req_id,
        delivered=delivered,
        accepted=accepted,
        played=played,
        status=status,
    )
