from __future__ import annotations

import asyncio
import copy
import hmac
import ipaddress
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import urlencode

from deskbot_server.application.turn_arbiter import (
    PRIORITY_MANUAL,
    device_turn_arbiter,
)
from deskbot_server.auto_reply import get_asr_voice_auto_reply_enabled
from deskbot_server.constants import (
    FACE_DESIGN_FILE,
    SCENE_PLAYBOOKS_FILE,
    SERVO_CFG_FILE,
)
from deskbot_server.debug_prefs_store import (
    debug_prefs_snapshot,
    persist_asr_auto_reply,
)
from deskbot_server.device_data import resolve_json_path
from deskbot_server.face_expr_scenes_store import (
    design_frames_to_pb_chain,
    find_design_scene_by_name,
    load_face_expr_scenes_file,
)
from deskbot_server.log_privacy import safe_log_content
from deskbot_server.pb.llm_plan import expand_llm_moves
from deskbot_server.pb.scenes import (
    _pb_scene_entry_by_name,
    _pb_scene_keys_sorted,
    _prepare_pb_scene_chain_frames,
)
from deskbot_server.pb.shapes import (
    PB_ACTION_APPEND,
    PB_ACTION_DEFAULT,
    PB_ACTION_REPLACE,
    PB_LEVEL_DEBUG,
    PB_LEVEL_TASK,
)
from deskbot_server.servo_config_store import (
    VIEWER_LR_SWAP,
    clamp_servo_step,
    load_servo_cfg_file,
    normalize_servo_document,
    normalize_servo_step,
    resolve_move_for_perspective,
    save_servo_cfg_file,
    servo_preset_catalog,
)
from deskbot_server.servo_protocol import (
    SERVO_HARDWARE_ENVELOPE,
    SERVO_MAX_BATCH_DURATION_MS,
    SERVO_MAX_DEGREES_PER_TICK,
    SERVO_MAX_PLAN_DURATION_MS,
    SERVO_MAX_PLAN_STEPS,
    SERVO_MAX_SEGMENTS_PER_PB,
    SERVO_MIN_SEGMENT_DURATION_MS,
    SERVO_TICK_MS,
    ServoProtocolError,
    pb_sequence_completion_budget_ms,
    validate_servo_steps,
)
from deskbot_server.util import _extract_device_id, _parse_query
from deskbot_server.ws.registry import DeviceRegistry

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.asr_chat_hub import AsrChatHub
    from deskbot_server.ws.device_pipeline import DevicePipelineBroker

logger = logging.getLogger("deskbot-server")


async def _run_manual_device_action(
    device_id: str,
    source: str,
    factory: Callable[[], Awaitable[Any]],
) -> Any:
    return await device_turn_arbiter.run(
        device_id,
        factory,
        source=source,
        priority=PRIORITY_MANUAL,
        preempt_lower=True,
    )


async def _run_pb_control_sequences(
    *,
    websocket,
    settings,
    device_pipeline_broker,
    device_id: str,
    source: str,
    sequences: list[tuple[str, list[tuple[dict[str, Any], list[bytes]]]]],
    durable_replay: bool = True,
    servo_fail_safe_timeout: bool = False,
):
    """Send durable manual PB controls and require terminal ``played`` ACKs."""
    from deskbot_server.application.chat_flow import _send_pb_pairs
    from deskbot_server.core.types import ChatTurnResult
    from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter

    downlink = WsDownlinkAdapter(
        websocket,
        settings=settings,
        device_id=device_id,
        dp_broker=device_pipeline_broker,
    )

    async def _send():
        result = ChatTurnResult()
        async with downlink.pb_serial_chain():
            for request_id, pairs in sequences:
                result.playback_request_ids.append(request_id)
                result.playback_status = "enqueued"
                delivery = await _send_pb_pairs(
                    downlink,
                    pairs=pairs,
                    pb_req=request_id,
                    device_id=device_id,
                    n_pb=len(pairs),
                    durable_replay=durable_replay,
                    played_timeout_sec=(
                        max(
                            2.0,
                            pb_sequence_completion_budget_ms(
                                message for message, _binaries in pairs
                            )
                            / 1000.0
                            + 1.5,
                        )
                        if servo_fail_safe_timeout
                        else None
                    ),
                )
                result.playback_status = delivery
                if delivery != "played":
                    result.status = "error"
                    result.error = "playback delivery failed or terminal played ACK timed out"
                    return result
        return result

    return await _run_manual_device_action(device_id, source, _send)


class _ControlOperationLeaseLost(RuntimeError):
    pass


async def _await_control_action_with_lease(
    *,
    operation_id: str,
    lease_token: str,
    timeout: float,
    action: Callable[[], Awaitable[Any]],
) -> Any:
    from deskbot_server.application.control_operations import (
        control_operation_heartbeat_seconds,
        heartbeat_control_operation,
    )

    lease_lost = asyncio.Event()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(control_operation_heartbeat_seconds())
            try:
                # The lease write commits through the shared SQLite ledger;
                # it must not block the realtime loop.  A failed or lost
                # heartbeat still cancels the device action through
                # ``lease_lost`` exactly as before.
                current = await asyncio.to_thread(
                    heartbeat_control_operation,
                    operation_id=operation_id,
                    lease_token=lease_token,
                )
            except Exception:
                logger.exception(
                    "[HTTP] control operation heartbeat failed operation_id=%s",
                    operation_id,
                )
                lease_lost.set()
                return
            if not current:
                lease_lost.set()
                return

    heartbeat_task = asyncio.create_task(_heartbeat())
    action_task = asyncio.create_task(action())
    lease_wait_task = asyncio.create_task(lease_lost.wait())
    try:
        done, _pending = await asyncio.wait(
            {action_task, lease_wait_task},
            timeout=max(0.0, timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if action_task in done:
            return await action_task
        if lease_wait_task in done and lease_lost.is_set():
            raise _ControlOperationLeaseLost("control operation worker lease was lost")
        raise asyncio.TimeoutError
    finally:
        for task in (action_task, lease_wait_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            action_task,
            lease_wait_task,
            heartbeat_task,
            return_exceptions=True,
        )


async def _run_persisted_control_operation(
    *,
    operation_id: str,
    device_id: str,
    kind: str,
    action: Callable[[], Awaitable[Any]],
) -> None:
    from deskbot_server.application.control_operations import (
        finish_control_operation,
        mark_control_operation_running,
        seconds_until_deadline,
    )
    from deskbot_server.application.turn_arbiter import TurnInterrupted

    running = await asyncio.to_thread(
        mark_control_operation_running,
        operation_id=operation_id,
    )
    if running is None or not running.lease_token:
        logger.warning(
            "[HTTP] control operation did not acquire accepted lease "
            "device_id=%s kind=%s operation_id=%s",
            device_id,
            kind,
            operation_id,
        )
        return
    lease_token = running.lease_token

    async def _finish(
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                finish_control_operation,
                operation_id=operation_id,
                lease_token=lease_token,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            # The persisted lease remains recoverable as ``timeout`` if the DB
            # is temporarily unavailable during terminalization.
            logger.exception(
                "[HTTP] control operation terminal write failed "
                "device_id=%s kind=%s operation_id=%s status=%s",
                device_id,
                kind,
                operation_id,
                status,
            )

    try:
        turn = await _await_control_action_with_lease(
            operation_id=operation_id,
            lease_token=lease_token,
            timeout=seconds_until_deadline(running),
            action=action,
        )
    except TurnInterrupted as exc:
        await _finish(
            "cancelled",
            result={"terminal_reason": "turn_interrupted", "reason": exc.reason},
            error_code="turn_interrupted",
            error_message=str(exc),
        )
        return
    except _ControlOperationLeaseLost as exc:
        await _finish(
            "timeout",
            result={"terminal_reason": "worker_lease_lost"},
            error_code="worker_lease_lost",
            error_message=str(exc),
        )
        return
    except asyncio.TimeoutError:
        await _finish(
            "timeout",
            result={"terminal_reason": "operation_deadline_exceeded"},
            error_code="operation_deadline_exceeded",
            error_message="control operation exceeded its absolute deadline",
        )
        return
    except asyncio.CancelledError:
        await _finish(
            "cancelled",
            result={"terminal_reason": "worker_cancelled"},
            error_code="worker_cancelled",
            error_message="control operation worker was cancelled",
        )
        raise
    except Exception as exc:
        logger.exception(
            "[HTTP] control operation failed device_id=%s kind=%s operation_id=%s",
            device_id,
            kind,
            operation_id,
        )
        await _finish(
            "failed",
            result={"terminal_reason": "exception"},
            error_code="control_exception",
            error_message=str(exc),
        )
        return

    turn_status = str(getattr(turn, "status", "ok") or "ok").strip().lower()
    turn_error = str(getattr(turn, "error", "") or "").strip()
    playback_status = str(getattr(turn, "playback_status", "none") or "none").strip().lower()
    result = {
        "request_id": running.request_id,
        "turn_status": turn_status,
        "playback_status": playback_status,
        "playback_request_ids": list(getattr(turn, "playback_request_ids", []) or []),
    }
    expression_result = getattr(turn, "expression_result", None)
    if isinstance(expression_result, dict):
        # This is persisted with the control operation so a mismatched device
        # face can be traced back to its exact source, resolution and lease.
        result["expression"] = dict(expression_result)
    if turn_status != "ok" or turn_error or playback_status != "played":
        await _finish(
            "failed",
            result=result,
            error_code=(
                "playback_failed"
                if playback_status == "failed"
                else (
                    "playback_unconfirmed"
                    if turn_status == "ok" and not turn_error
                    else "control_result_error"
                )
            ),
            error_message=(
                turn_error
                or (
                    "device playback did not reach a terminal played ACK"
                    if playback_status != "played"
                    else "control operation returned an error"
                )
            ),
        )
        return
    await _finish("completed", result=result)


def _fail_accepted_control_operation(
    operation,
    *,
    error_code: str,
    error_message: str,
):
    """Synchronous ledger writes; async callers dispatch via asyncio.to_thread."""
    from deskbot_server.application.control_operations import (
        finish_control_operation,
        get_control_operation,
        mark_control_operation_running,
    )

    if operation.status == "accepted":
        running = mark_control_operation_running(
            operation_id=operation.operation_id,
        )
        if running is not None and running.lease_token:
            finish_control_operation(
                operation_id=operation.operation_id,
                lease_token=running.lease_token,
                status="failed",
                result={"terminal_reason": error_code},
                error_code=error_code,
                error_message=error_message,
            )
    return (
        get_control_operation(
            operation_id=operation.operation_id,
        )
        or operation
    )


def _scene_playbook_operation_payload(
    playbook: dict[str, Any],
) -> dict[str, Any]:
    """Return the behavior-only payload used to bind an idempotency key.

    ``normalize_playbook`` generates a random ID when a caller omits a chunk
    ID.  Chunk IDs are editor/debug metadata and do not change what the device
    plays, so including generated IDs in the fingerprint would make an
    otherwise identical retry conflict with its first request.
    """

    canonical = copy.deepcopy(playbook)
    chunks = canonical.get("chunks")
    if isinstance(chunks, list):
        for chunk in chunks:
            if isinstance(chunk, dict):
                chunk.pop("id", None)
    return {"playbook": canonical}


def _build_http_request_handler(
    device_pipeline_broker: DevicePipelineBroker,
    registry: DeviceRegistry,
    *,
    asr_chat_hub: "AsrChatHub",
    chat: "ChatService",
    camera_image_broker: Any | None = None,
    rtc_tool_token: str = "",
    rtc_status_provider: Any | None = None,
):
    """在同端口之上提供极简 HTTP 接口（不新增依赖）。

    - GET /api/devices：返回当前注册的设备列表（源自 DeviceRegistry）
    - GET /api/pipeline_recent?device_id=xxx&limit=100：返回滚动窗口内的事件
    - POST /api/device_servo?device_id=…&dyaw=…&dpitch=…&ms=…&xm=…&ym=…&action=…&level=…：向该设备
      已连接的 USB CDC 会话下发仅含 ``servo`` 的 **单片** ``pb_single``。``xm``/``ym`` 缺省
      ``0``（绝对，x/y 钳位 0–180）；``xm=ym=1`` 时 dyaw/dpitch 为相对增量（±90）。可选 ``action``：``replace`` / ``append`` / ``default``，缺省
      ``replace``。可选 ``level``：``0``–``3``，缺省 ``3``（调试态）。可选 ``with_scene`` / ``append_scene``：
      先确认舵机动作完成，再把该场景的精确帧交给统一表情运行时；表情不再混入舵机 PB，
      因而不会绕过 Web / RTC / Agent 的显示所有权仲裁。
    - GET /api/pb_scenes：列出本机表情文件中 ``emotions`` 的场景 name。
    - GET /api/face_catalog：列出本机表情文件的音素与情绪摘要。
    - POST /api/device_face_play?device_id=…&kind=phoneme|emotion&name=…：向设备下发对应表情链（无音频）。
    - POST /api/device_pb_scene?device_id=…&scene=<id>：按 ``deskbot-face.json`` 向该设备
      USB CDC 会话**顺序**下发 ``pb_start`` → ``pb_chunk``* → ``pb_end``（含 ``anim``+``servo``，无音频）。
      ``scene`` 与文件中的 key **不区分大小写**。
    - GET /api/asr_auto_reply：读取自动应答开关；POST 并带
      ``?enabled=1|0|true|false`` 时写入开关（调试页「启用自动应答」）。
    - POST /api/device_tts：JSON ``device_id`` + ``text``，跳过 LLM 直接音素 TTS 并下发 pb；
      可选 ``scene``：与 TTS 在同一条 pb 链锁内顺序追加该场景帧（先语音后场景）。
    - GET /api/control_operation?device_id=…&operation_id=…：查询异步控制终态。
    - GET /api/servo_contract：只读舵机契约单一源——硬件包络、PB 段数/时长/步进限制、
      观众视角 left/right 对调表与当前 ``presets`` catalog；前端不得再硬编码这些数值。
    - GET/POST /api/servo_config：读取/写入 ``data/local/servo.json`` 舵机限位/反向与 ``presets`` 动作预设。
    - POST /api/device_pb_anim：向设备下发仅含 ``anim`` 的单片 ``pb_single``（调试口型预览）。
    - POST /api/device_pb_expr_scene：按设备表情设计场景下发 pb 链。
    - GET /health：存活探针
    - 其它 /api/* 返回 404；非 WS 升级请求才会进入该分支。
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def _cors_headers() -> Headers:
        headers = Headers()
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        headers["Cache-Control"] = "no-store"
        return headers

    def _json_resp(status: int, payload: object) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = _cors_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))
        reason = {
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "OK")
        return Response(status, reason, headers, body)

    def _no_content() -> Response:
        headers = _cors_headers()
        headers["Content-Length"] = "0"
        return Response(204, "No Content", headers, b"")

    def _control_operation_response(
        operation,
        *,
        created: bool,
        extra: dict[str, Any] | None = None,
    ) -> Response:
        operation_payload = operation.as_dict()
        status = operation.status
        in_flight = status in {"accepted", "running"}
        response_payload: dict[str, Any] = {
            "ok": status not in {"failed", "cancelled", "timeout"},
            "accepted": in_flight,
            "created": bool(created),
            "idempotent_replay": not created,
            "operation_id": operation.operation_id,
            "device_id": operation.device_id,
            "kind": operation.kind,
            "status": status,
            "terminal": operation_payload["terminal"],
            "req": operation.request_id,
            "status_url": "/api/control_operation?"
            + urlencode(
                {
                    "device_id": operation.device_id,
                    "operation_id": operation.operation_id,
                }
            ),
            "operation": operation_payload,
            "t": time.time(),
        }
        if extra:
            response_payload.update(extra)
        return _json_resp(202 if in_flight else 200, response_payload)

    def _peer_from_conn(connection) -> str:
        try:
            peer = getattr(connection, "remote_address", None)
            if peer and isinstance(peer, tuple) and len(peer) >= 2:
                return f"{peer[0]}:{peer[1]}"
        except Exception:
            pass
        return "?"

    def _connection_is_loopback(connection) -> bool:
        remote = getattr(connection, "remote_address", None)
        remote_host = str(remote[0] if isinstance(remote, tuple) and remote else "")
        host = remote_host.strip()
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        host = host.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        if address.is_loopback:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(mapped and mapped.is_loopback)

    async def handler(connection, request):
        upgrade = (request.headers.get("Upgrade") or "").strip().lower()
        if upgrade == "websocket":
            return None

        raw_path = request.path or ""
        path_only, _, query = raw_path.partition("?")
        qargs = _parse_query(query)
        peer = _peer_from_conn(connection)
        method = (getattr(request, "method", None) or "GET").upper()

        if path_only == "/health":
            logger.info("[HTTP] GET /health peer=%s -> 200", peer)
            return _json_resp(200, {"ok": True})

        if path_only.startswith("/api/") and not _connection_is_loopback(connection):
            return _json_resp(403, {"ok": False, "error": "loopback_required"})
        if method == "OPTIONS" and path_only.startswith("/api/"):
            return _no_content()

        if path_only == "/api/rtc_health":
            snapshot = (
                rtc_status_provider()
                if callable(rtc_status_provider)
                else {"status": "unavailable"}
            )
            return _json_resp(200, {"ok": True, "rtc": snapshot})

        if path_only == "/internal/rtc/tools":
            if method != "POST":
                return _json_resp(405, {"ok": False, "error": "method_not_allowed"})
            if not _connection_is_loopback(connection):
                return _json_resp(403, {"ok": False, "error": "loopback_required"})
            expected_token = str(rtc_tool_token or "")
            supplied_token = str(request.headers.get("X-Deskbot-RTC-Bridge") or "")
            if not expected_token:
                return _json_resp(
                    503,
                    {"ok": False, "error": "rtc_tool_bridge_unavailable"},
                )
            if not supplied_token or not hmac.compare_digest(
                supplied_token,
                expected_token,
            ):
                return _json_resp(
                    401,
                    {"ok": False, "error": "rtc_tool_bridge_unauthorized"},
                )
            raw_body_bytes = getattr(request, "body", None) or b""
            if len(raw_body_bytes) > 65536:
                return _json_resp(
                    400,
                    {"ok": False, "error": "request_too_large"},
                )
            try:
                raw_body = raw_body_bytes.decode("utf-8")
                body = json.loads(raw_body) if raw_body.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _json_resp(400, {"ok": False, "error": "invalid_json"})
            if not isinstance(body, dict):
                return _json_resp(400, {"ok": False, "error": "invalid_json"})
            device_id = str(body.get("device_id") or "").strip()
            tool = str(body.get("tool") or "").strip()
            arguments = body.get("arguments")
            call_id = str(body.get("call_id") or "").strip()[:128]
            if not isinstance(arguments, dict):
                return _json_resp(
                    400,
                    {"ok": False, "error": "arguments_must_be_object"},
                )
            from deskbot_server.application.rtc_tool_service import (
                execute_rtc_tool,
                rtc_tool_names,
            )
            from deskbot_server.hardware_catalog import validate_device_id

            if not validate_device_id(device_id) or tool not in rtc_tool_names():
                return _json_resp(
                    400,
                    {"ok": False, "error": "unsupported_tool_request"},
                )
            if await asr_chat_hub.first_ws(device_id) is None:
                return _json_resp(
                    409,
                    {"ok": False, "error": "device_not_connected"},
                )
            try:
                device_context = await registry.pb_ack_llm_context(device_id)
                result = await execute_rtc_tool(
                    device_id=device_id,
                    tool=tool,
                    arguments=arguments,
                    call_id=call_id,
                    asr_chat_hub=asr_chat_hub,
                    device_context=device_context,
                )
            except Exception:
                logger.exception(
                    "[rtc-tools] Core execution failed device_id=%s tool=%s",
                    device_id,
                    tool,
                )
                return _json_resp(
                    500,
                    {"ok": False, "error": "rtc_tool_execution_failed"},
                )
            logger.info(
                "[rtc-tools] executed device_id=%s tool=%s ok=%s",
                device_id,
                tool,
                bool(result.get("ok")),
            )
            return _json_resp(
                200,
                {"ok": True, "device_id": device_id, "result": result},
            )

        api_auth = None
        if path_only.startswith("/api/"):
            from deskbot_server.auth.api_key_service import QuotaExceededError
            from deskbot_server.ws.api_key_gate import QUOTA_MESSAGE, http_require_api_key

            try:
                api_auth = http_require_api_key(qargs, request.headers)
            except QuotaExceededError:
                return _json_resp(
                    429,
                    {
                        "ok": False,
                        "error": "quota_exhausted",
                        "message": QUOTA_MESSAGE,
                    },
                )
            except PermissionError:
                return _json_resp(
                    401,
                    {"ok": False, "error": "api_key_required", "message": "缺少或无效的 API Key"},
                )

        def _device_route_error(device_id: str | None):
            from deskbot_server.ws.api_key_gate import http_require_device_route

            try:
                http_require_device_route(api_auth, device_id)
            except PermissionError as exc:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": str(exc) or "device_route_invalid",
                        "message": "缺少有效的设备通信路由",
                    },
                )
            return None

        async def _start_pb_control_operation(
            *,
            device_id: str,
            kind: str,
            operation_payload: object,
            requested_operation_id: str,
            source: str,
            build_sequences: Callable[
                [str],
                list[
                    tuple[
                        str,
                        list[tuple[dict[str, Any], list[bytes]]],
                    ]
                ],
            ],
            extra: dict[str, Any] | None = None,
            durable_replay: bool = True,
            servo_fail_safe_timeout: bool = False,
        ):
            from deskbot_server.application.control_operations import (
                ControlOperationConflict,
                accept_control_operation,
            )

            try:
                operation, created = await asyncio.to_thread(
                    accept_control_operation,
                    device_id=device_id,
                    kind=kind,
                    payload=operation_payload,
                    operation_id=requested_operation_id or None,
                    request_id=uuid.uuid4().hex[:16],
                )
            except ControlOperationConflict as exc:
                return _json_resp(
                    409,
                    {
                        "ok": False,
                        "error": "operation_id_conflict",
                        "message": str(exc),
                    },
                )
            except (TypeError, ValueError) as exc:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_control_operation",
                        "message": str(exc),
                    },
                )

            response_extra = dict(extra or {})
            if created:
                ws = await asr_chat_hub.first_ws(device_id)
                if ws is None:
                    operation = await asyncio.to_thread(
                        _fail_accepted_control_operation,
                        operation,
                        error_code="device_offline",
                        error_message="device USB session is not connected",
                    )
                    response_extra.update(
                        {
                            "delivered": 0,
                            "hint": "device USB session is not connected",
                        }
                    )
                    return _control_operation_response(
                        operation,
                        created=True,
                        extra=response_extra,
                    )
                try:
                    sequences = build_sequences(operation.request_id)
                    if not sequences or any(
                        not request_id or not pairs for request_id, pairs in sequences
                    ):
                        raise ValueError("control produced an empty PB sequence")
                except Exception as exc:
                    logger.exception(
                        "[HTTP] PB control build failed device_id=%s kind=%s operation_id=%s",
                        device_id,
                        kind,
                        operation.operation_id,
                    )
                    operation = await asyncio.to_thread(
                        _fail_accepted_control_operation,
                        operation,
                        error_code="control_build_failed",
                        error_message=str(exc),
                    )
                    return _control_operation_response(
                        operation,
                        created=True,
                        extra=response_extra,
                    )

                async def _action():
                    if kind == "device_servo" and isinstance(
                        operation_payload, dict
                    ):
                        from deskbot_server.core.types import ChatTurnResult

                        servo_turn = await _run_pb_control_sequences(
                            websocket=ws,
                            settings=chat.settings,
                            device_pipeline_broker=device_pipeline_broker,
                            device_id=device_id,
                            source=source,
                            sequences=sequences,
                            durable_replay=durable_replay,
                            servo_fail_safe_timeout=servo_fail_safe_timeout,
                        )
                        scene_frames = operation_payload.get("scene_frames")
                        if (
                            str(getattr(servo_turn, "playback_status", ""))
                            != "played"
                            or not isinstance(scene_frames, list)
                            or not scene_frames
                        ):
                            return servo_turn

                        from deskbot_server.application.expression_runtime import (
                            play_web_expression_frames,
                        )

                        expression = await play_web_expression_frames(
                            device_id,
                            scene_frames,
                            name=operation_payload.get("with_scene"),
                            title=operation_payload.get("scene_title"),
                            hold_ms=None,
                            reason="http_device_servo_with_scene",
                            operation_id=operation.operation_id,
                        )
                        servo_turn.playback_request_ids.append(
                            f"{operation.request_id}-expression"
                        )
                        servo_turn.expression_result = expression.as_tool_result()
                        if not expression.ok:
                            servo_turn.status = "error"
                            servo_turn.playback_status = "failed"
                            servo_turn.error = (
                                expression.error or "servo expression playback failed"
                            )
                        return servo_turn
                    if kind in {
                        "device_pb_anim",
                        "device_face_play",
                        "device_pb_scene",
                        "device_pb_expr_scene",
                    }:
                        from deskbot_server.application.expression_runtime import (
                            play_web_expression_frames,
                            play_web_expression_scene,
                        )

                        frames = (
                            operation_payload.get("frames")
                            or operation_payload.get("anim")
                            if isinstance(operation_payload, dict)
                            else None
                        )
                        if isinstance(frames, list):
                            arbitrated = await play_web_expression_frames(
                                device_id,
                                frames,
                                name=operation_payload.get("expression_name"),
                                title=operation_payload.get("expression_title"),
                                # Web preview is authoritative until the next
                                # real lifecycle or explicit expression event.
                                # An arbitrary timeout made the device jump to
                                # its configured idle face after a correct send.
                                hold_ms=None,
                                reason=source,
                                operation_id=operation.operation_id,
                            )
                            from deskbot_server.core.types import ChatTurnResult

                            result = ChatTurnResult()
                            result.playback_request_ids.append(
                                operation.request_id
                            )
                            result.playback_status = (
                                "played" if arbitrated.ok else "failed"
                            )
                            result.status = "ok" if arbitrated.ok else "error"
                            if arbitrated.error:
                                result.error = arbitrated.error
                            result.expression_result = arbitrated.as_tool_result()
                            return result
                        elif (
                            kind in {
                                "device_face_play",
                                "device_pb_scene",
                                "device_pb_expr_scene",
                            }
                            and isinstance(operation_payload, dict)
                        ):
                            scene_name = str(
                                operation_payload.get("name")
                                or operation_payload.get("scene")
                                or ""
                            ).strip()
                            if scene_name:
                                arbitrated = await play_web_expression_scene(
                                    device_id,
                                    scene_name,
                                    hold_ms=None,
                                    reason=source,
                                    operation_id=operation.operation_id,
                                )
                                from deskbot_server.core.types import ChatTurnResult

                                result = ChatTurnResult()
                                result.playback_request_ids.append(
                                    operation.request_id
                                )
                                result.playback_status = (
                                    "played" if arbitrated.ok else "failed"
                                )
                                result.status = (
                                    "ok" if arbitrated.ok else "error"
                                )
                                if arbitrated.error:
                                    result.error = arbitrated.error
                                result.expression_result = arbitrated.as_tool_result()
                                return result
                        from deskbot_server.core.types import ChatTurnResult

                        result = ChatTurnResult()
                        result.status = "error"
                        result.playback_status = "failed"
                        result.error = (
                            "expression control has no exact frames or scene; "
                            "direct PB fallback is disabled"
                        )
                        return result
                    # Only non-display controls may use the generic PB path.
                    # Every display control above returns through the USB
                    # expression runtime, including runtime-unavailable errors.
                    return await _run_pb_control_sequences(
                        websocket=ws,
                        settings=chat.settings,
                        device_pipeline_broker=device_pipeline_broker,
                        device_id=device_id,
                        source=source,
                        sequences=sequences,
                        durable_replay=durable_replay,
                        servo_fail_safe_timeout=servo_fail_safe_timeout,
                    )

                asyncio.create_task(
                    _run_persisted_control_operation(
                        operation_id=operation.operation_id,
                        device_id=device_id,
                        kind=kind,
                        action=_action,
                    ),
                    name=f"control-operation:{operation.operation_id}",
                )

            logger.info(
                "[HTTP] PB control status=%s created=%s peer=%s "
                "device_id=%s kind=%s req=%s operation_id=%s",
                operation.status,
                created,
                peer,
                device_id,
                kind,
                operation.request_id,
                operation.operation_id,
            )
            return _control_operation_response(
                operation,
                created=created,
                extra=response_extra,
            )

        if path_only == "/api/control_operation":
            if method != "GET":
                return _json_resp(
                    405,
                    {"ok": False, "error": "control operation query requires GET"},
                )
            operation_id = str(qargs.get("operation_id") or "").strip()
            if not operation_id:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": "operation_id is required",
                    },
                )
            from deskbot_server.application.control_operations import (
                get_control_operation,
            )

            operation = await asyncio.to_thread(
                get_control_operation,
                operation_id=operation_id,
            )
            if operation is None:
                return _json_resp(
                    404,
                    {"ok": False, "error": "control_operation_not_found"},
                )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "operation_id": operation.operation_id,
                    "device_id": operation.device_id,
                    "kind": operation.kind,
                    "status": operation.status,
                    "terminal": operation.status in {"completed", "failed", "cancelled", "timeout"},
                    "operation": operation.as_dict(),
                    "t": time.time(),
                },
            )

        if path_only == "/api/expression_runtime":
            if method != "GET":
                return _json_resp(
                    405,
                    {"ok": False, "error": "expression runtime query requires GET"},
                )
            dev = (qargs.get("device_id") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            from deskbot_server.application.expression_runtime import (
                get_expression_runtime,
            )

            expression_runtime = get_expression_runtime(dev)
            if expression_runtime is None:
                return _json_resp(
                    503,
                    {
                        "ok": False,
                        "error": "expression_runtime_unavailable",
                        "device_id": dev,
                        "t": time.time(),
                    },
                )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "runtime": expression_runtime.snapshot(),
                    "t": time.time(),
                },
            )

        if path_only == "/api/usb-devices":
            # Physical IDs identify live USB routes only; they do not own data.
            attached = []
            for row in registry.snapshot():
                channels = row.get("channels")
                usb_channel_count = (
                    int(channels.get("usb_cdc") or 0) if isinstance(channels, dict) else 0
                )
                if (
                    not bool(row.get("online"))
                    or str(row.get("transport") or "") != "usb_cdc"
                    or usb_channel_count <= 0
                ):
                    continue
                attached.append(
                    {
                        "device_id": str(row.get("device_id") or ""),
                        "online": True,
                        "transport": "usb_cdc",
                        "session_generation": row.get("session_generation"),
                        "interaction_state": row.get("interaction_state"),
                        "microphone_health": (
                            dict(row.get("microphone_health"))
                            if isinstance(
                                row.get("microphone_health"),
                                dict,
                            )
                            else None
                        ),
                        "last_seen": row.get("last_seen"),
                    }
                )
            return _json_resp(
                200,
                {
                    "devices": attached,
                    "t": time.time(),
                },
            )

        if path_only == "/api/devices":
            snap = registry.snapshot()
            device_ids = [d.get("device_id") for d in snap]
            logger.info(
                "[HTTP] GET /api/devices peer=%s -> %d 台设备 device_ids=%s",
                peer,
                len(snap),
                device_ids,
            )
            return _json_resp(
                200,
                {
                    "devices": snap,
                    "t": time.time(),
                },
            )

        if path_only == "/api/asr_auto_reply":
            raw_e = qargs.get("enabled")
            if method not in {"GET", "POST"} or (raw_e is not None and method != "POST"):
                return _json_resp(
                    405,
                    {"ok": False, "error": "mutations require POST"},
                )
            if method == "POST" and raw_e is None:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing enabled"},
                )
            if raw_e is None:
                logger.info(
                    "[HTTP] GET /api/asr_auto_reply -> enabled=%s peer=%s",
                    get_asr_voice_auto_reply_enabled(),
                    peer,
                )
            if raw_e is not None:
                se = str(raw_e).strip().lower()
                if se in ("1", "true", "yes", "on"):
                    persist_asr_auto_reply(True)
                elif se in ("0", "false", "no", "off"):
                    persist_asr_auto_reply(False)
                else:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": "invalid enabled; use 1/0 or true/false",
                        },
                    )
                logger.info(
                    "[HTTP] /api/asr_auto_reply set enabled=%s peer=%s (已写入 debug_prefs.json)",
                    get_asr_voice_auto_reply_enabled(),
                    peer,
                )
            return _json_resp(
                200,
                {"ok": True, "enabled": get_asr_voice_auto_reply_enabled()},
            )

        if path_only == "/api/debug_prefs":
            raw_ar = qargs.get("asr_auto_reply")
            if method not in {"GET", "POST"} or (raw_ar is not None and method != "POST"):
                return _json_resp(
                    405,
                    {"ok": False, "error": "mutations require POST"},
                )
            if method == "POST" and raw_ar is None:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing mutation parameter"},
                )
            if raw_ar is None:
                return _json_resp(200, {"ok": True, **debug_prefs_snapshot()})
            normalized = str(raw_ar).strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                persist_asr_auto_reply(True)
            elif normalized in ("0", "false", "no", "off"):
                persist_asr_auto_reply(False)
            else:
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid asr_auto_reply"},
                )
            return _json_resp(200, {"ok": True, **debug_prefs_snapshot()})
        if path_only == "/api/pipeline_recent":
            dev = _extract_device_id(qargs)
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            try:
                limit = int(qargs.get("limit") or str(device_pipeline_broker.max_events))
            except ValueError:
                limit = device_pipeline_broker.max_events
            limit = max(1, min(device_pipeline_broker.max_events, limit))
            items = device_pipeline_broker.snapshot_events(dev, limit)
            logger.info(
                "[HTTP] GET /api/pipeline_recent peer=%s device_id=%s limit=%d -> %d 条",
                peer,
                dev,
                limit,
                len(items),
            )
            return _json_resp(
                200,
                {
                    "items": items,
                    "device_id": dev,
                    "limit": limit,
                    "max_events": device_pipeline_broker.max_events,
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_servo":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST"},
                )
            raw_body_bytes = getattr(request, "body", None) or b""
            body_supplied = bool(raw_body_bytes.strip())
            if body_supplied:
                try:
                    body = json.loads(raw_body_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return _json_resp(
                        400,
                        {"ok": False, "error": "invalid JSON body", "t": time.time()},
                    )
                if not isinstance(body, dict):
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": "JSON body must be an object",
                            "t": time.time(),
                        },
                    )
            else:
                body = {}
            dev = str(body.get("device_id") or qargs.get("device_id") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            try:
                dyaw = float(qargs.get("dyaw") or 0.0)
                dpitch = float(qargs.get("dpitch") or 0.0)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {"error": "invalid dyaw or dpitch", "t": time.time()},
                )
            try:
                ms = int(qargs.get("ms") or 400)
            except (TypeError, ValueError):
                ms = 400
            try:
                # 固定镜头默认绝对定位；显式传 xm=1 时为相对增量
                xm = int(qargs.get("xm") if qargs.get("xm") is not None else 0)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {
                        "error": "invalid xm (use 0=absolute|1=relative|2=hold)",
                        "t": time.time(),
                    },
                )
            try:
                ym = int(qargs.get("ym") if qargs.get("ym") is not None else xm)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {
                        "error": "invalid ym (use 0=absolute|1=relative|2=hold)",
                        "t": time.time(),
                    },
                )
            if xm not in (0, 1, 2) or ym not in (0, 1, 2):
                return _json_resp(
                    400,
                    {
                        "error": (
                            "invalid xm/ym "
                            "(debug UI: 0=absolute, 1=relative, 2=hold)"
                        ),
                        "t": time.time(),
                    },
                )
            try:
                logical_step = normalize_servo_step(
                    {
                        "xm": xm,
                        "ym": ym,
                        "x": int(round(dyaw)),
                        "y": int(round(dpitch)),
                        "ms": ms,
                    }
                )
                servo_step = clamp_servo_step(logical_step)
            except (OverflowError, TypeError, ValueError):
                return _json_resp(
                    400,
                    {
                        "error": "invalid servo coordinates or duration",
                        "t": time.time(),
                    },
                )
            action_raw = (
                body.get("action") if body_supplied else qargs.get("action")
            )
            act = str(action_raw or PB_ACTION_REPLACE).strip().lower()
            if act not in (PB_ACTION_REPLACE, PB_ACTION_APPEND, PB_ACTION_DEFAULT):
                return _json_resp(
                    400,
                    {
                        "error": "invalid action (use replace|append|default)",
                        "t": time.time(),
                    },
                )
            try:
                level_raw = (
                    body.get("level", PB_LEVEL_DEBUG)
                    if body_supplied
                    else qargs.get("level", PB_LEVEL_DEBUG)
                )
                pb_level = int(level_raw)
            except (TypeError, ValueError):
                pb_level = -1
            if pb_level not in (0, 1, 2, 3):
                return _json_resp(
                    400,
                    {
                        "error": "invalid level (use 0|1|2|3)",
                        "t": time.time(),
                    },
                )
            scene_raw = (
                body.get("with_scene")
                if body_supplied
                else qargs.get("with_scene") or qargs.get("append_scene")
            )
            with_scene_durable = str(scene_raw or "").strip()
            scene_frame_count = 0
            scene_frames_durable: list[dict[str, Any]] = []
            scene_title_durable = ""
            if with_scene_durable:
                scene_entry = _pb_scene_entry_by_name(
                    with_scene_durable,
                )
                if scene_entry is None:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": (f"unknown or empty scene: {with_scene_durable!r}"),
                            "t": time.time(),
                        },
                    )
                with_scene_durable = str(scene_entry.get("name") or with_scene_durable).strip()
                scene_title_durable = str(
                    scene_entry.get("title") or with_scene_durable
                ).strip()
                scene_frames_durable = copy.deepcopy(scene_entry.get("frames") or [])
                scene_frame_count = len(
                    _prepare_pb_scene_chain_frames(
                        with_scene_durable,
                        runtime_req="validation",
                    )
                )
                if scene_frame_count <= 0:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": (f"unknown or empty scene: {with_scene_durable!r}"),
                            "t": time.time(),
                        },
                    )

            durable_servo = [servo_step]
            preset_name = ""
            if body_supplied:
                steps_raw = body.get("steps")
                preset_name = str(body.get("preset") or "").strip()
                if steps_raw is not None and preset_name:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": "provide either steps or preset, not both",
                            "t": time.time(),
                        },
                    )
                if steps_raw is not None:
                    if not isinstance(steps_raw, list) or not steps_raw:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": "steps must be a non-empty array",
                                "t": time.time(),
                            },
                        )
                    if len(steps_raw) > 32:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": "steps exceeds device limit of 32",
                                "t": time.time(),
                            },
                        )
                    durable_servo = []
                    try:
                        for raw_step in steps_raw:
                            if not isinstance(raw_step, dict):
                                raise ValueError("step must be an object")
                            if raw_step.get("x") is None or raw_step.get("y") is None:
                                raise ValueError("step requires x and y")
                            step_xm = raw_step.get("xm", 0)
                            step_ym = raw_step.get("ym", step_xm)
                            if step_xm not in (0, 1, 2) or step_ym not in (0, 1, 2):
                                raise ValueError("step xm/ym must be 0, 1 or 2")
                            logical_step = normalize_servo_step(
                                {
                                    "xm": step_xm,
                                    "ym": step_ym,
                                    "x": raw_step["x"],
                                    "y": raw_step["y"],
                                    "ms": raw_step.get("ms", 400),
                                }
                            )
                            durable_servo.append(clamp_servo_step(logical_step))
                    except (OverflowError, TypeError, ValueError) as exc:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": f"invalid servo step: {exc}",
                                "t": time.time(),
                            },
                        )
                elif preset_name:
                    resolved_preset = resolve_move_for_perspective(preset_name)
                    try:
                        cfg = load_servo_cfg_file() or {}
                    except (OSError, ValueError):
                        cfg = {}
                    preset = next(
                        (
                            row
                            for row in cfg.get("presets") or []
                            if isinstance(row, dict)
                            and str(row.get("id") or "").strip().lower()
                            == resolved_preset.lower()
                        ),
                        None,
                    )
                    if preset is None:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": f"unknown servo preset: {preset_name!r}",
                                "t": time.time(),
                            },
                        )
                    preset_steps = preset.get("steps") or []
                    if len(preset_steps) > 32:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": "preset exceeds device limit of 32 steps",
                                "t": time.time(),
                            },
                        )
                    default_ms = sum(
                        max(1, int(step.get("ms") or 400))
                        for step in preset_steps
                        if isinstance(step, dict)
                    )
                    target_ms = body.get("duration_ms", body.get("ms", default_ms))
                    if isinstance(target_ms, bool) or not isinstance(target_ms, int):
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": "invalid preset duration",
                                "t": time.time(),
                            },
                        )
                    try:
                        durable_servo = expand_llm_moves(
                            [{"move": preset_name, "ms": target_ms}]
                        )
                    except (ServoProtocolError, TypeError, ValueError) as exc:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": f"invalid servo preset: {exc}",
                                "t": time.time(),
                            },
                        )
                    if not durable_servo:
                        return _json_resp(
                            400,
                            {
                                "ok": False,
                                "error": f"invalid servo preset: {preset_name!r}",
                                "t": time.time(),
                            },
                        )
                else:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": "JSON body requires steps or preset",
                            "t": time.time(),
                        },
                    )
            try:
                checked_servo, ms = validate_servo_steps(
                    durable_servo,
                    context="HTTP servo sequence",
                )
                durable_servo = [dict(step) for step in checked_servo]
            except ServoProtocolError as exc:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                        "t": time.time(),
                    },
                )

            def _build_servo_sequences(request_id: str):
                durable_payload = {
                    "type": "pb_single",
                    "req": request_id,
                    "idx": 0,
                    "chunk_ms": ms,
                    "pb_ver": 2,
                    "action": act,
                    "level": pb_level,
                    "servo": copy.deepcopy(durable_servo),
                }
                sequences = [(request_id, [(durable_payload, [])])]
                return sequences

            return await _start_pb_control_operation(
                device_id=dev,
                kind="device_servo",
                operation_payload={
                    "servo": durable_servo,
                    "action": act,
                    "level": pb_level,
                    "with_scene": with_scene_durable or None,
                    "scene_title": scene_title_durable or None,
                    "scene_frames": scene_frames_durable,
                },
                requested_operation_id=str(
                    (
                        body.get("operation_id")
                        if body_supplied
                        else qargs.get("operation_id")
                    )
                    or ""
                ).strip(),
                source="http_device_servo",
                build_sequences=_build_servo_sequences,
                durable_replay=False,
                servo_fail_safe_timeout=True,
                extra={
                    "type": "pb_single",
                    "action": act,
                    "level": pb_level,
                    "servo": durable_servo,
                    "preset": preset_name or None,
                    "with_scene": with_scene_durable or None,
                    "scene_frames": scene_frame_count,
                },
            )

        if path_only == "/api/pb_scenes":
            if method != "GET":
                return _json_resp(
                    405,
                    {"ok": False, "error": "method_not_allowed", "t": time.time()},
                )
            keys = _pb_scene_keys_sorted()
            logger.info(
                "[HTTP] GET /api/pb_scenes peer=%s -> %d scene(s)",
                peer,
                len(keys),
            )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "scenes": keys,
                    "file": os.path.basename(FACE_DESIGN_FILE),
                    "t": time.time(),
                },
            )

        if path_only == "/api/face_catalog":
            from deskbot_server.face_design_store import build_face_expression_catalog

            if method != "GET":
                return _json_resp(
                    405,
                    {"ok": False, "error": "method_not_allowed", "t": time.time()},
                )
            try:
                catalog = build_face_expression_catalog()
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return _json_resp(
                    500,
                    {"ok": False, "error": str(exc), "t": time.time()},
                )
            logger.info(
                "[HTTP] GET /api/face_catalog peer=%s phonemes=%d emotions=%d",
                peer,
                len(catalog.get("phonemes") or []),
                len(catalog.get("emotions") or []),
            )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "phonemes": catalog.get("phonemes") or [],
                    "emotions": catalog.get("emotions") or [],
                    "file": os.path.basename(FACE_DESIGN_FILE),
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_face_play":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST", "t": time.time()},
                )
            from deskbot_server.face_design_store import (
                _load_face_design_cached,
                build_face_expression_catalog,
                ensure_face_design_file,
                resolve_face_expression,
            )

            dev = (qargs.get("device_id") or "").strip()
            kind_q = (qargs.get("kind") or "").strip().lower()
            name_q = (qargs.get("name") or qargs.get("scene") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if kind_q not in ("phoneme", "emotion"):
                return _json_resp(
                    400,
                    {"ok": False, "error": "kind must be phoneme or emotion", "t": time.time()},
                )
            if not name_q:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing name", "t": time.time()},
                )
            try:
                doc = _load_face_design_cached()
                if not isinstance(doc, dict):
                    doc = ensure_face_design_file()
                ent = resolve_face_expression(doc, kind=kind_q, name=name_q)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return _json_resp(
                    500,
                    {"ok": False, "error": str(exc), "t": time.time()},
                )
            if ent is None:
                catalog = build_face_expression_catalog()
                if kind_q == "phoneme":
                    valid = [
                        str(x.get("name") or "")
                        for x in catalog.get("phonemes") or []
                        if isinstance(x, dict) and x.get("name")
                    ]
                else:
                    valid = [
                        str(x.get("name") or "")
                        for x in catalog.get("emotions") or []
                        if isinstance(x, dict) and x.get("name")
                    ]
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": f"unknown {kind_q}: {name_q!r}",
                        "valid_names": sorted(set(valid)),
                        "t": time.time(),
                    },
                )
            durable_expr_name = str(ent.get("name") or name_q).strip()
            durable_frame_count = len(ent.get("frames") or [])

            def _build_face_sequences(request_id: str):
                durable_pairs = design_frames_to_pb_chain(
                    ent.get("frames") or [],
                    runtime_req=request_id,
                )
                return [(request_id, durable_pairs)]

            return await _start_pb_control_operation(
                device_id=dev,
                kind="device_face_play",
                operation_payload={
                    "kind": kind_q,
                    "name": durable_expr_name,
                    # Persist the exact route-resolved frames. The operation
                    # must not re-resolve this name later against a different
                    # catalog category/revision (especially phonemes).
                    "frames": copy.deepcopy(ent.get("frames") or []),
                },
                requested_operation_id=str(qargs.get("operation_id") or "").strip(),
                source="http_device_face_play",
                build_sequences=_build_face_sequences,
                extra={
                    "expression_kind": kind_q,
                    "name": durable_expr_name,
                    "frames": durable_frame_count,
                },
            )

        if path_only == "/api/device_tts":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST"},
                )
            from deskbot_server.application.chat_flow import run_device_tts_only
            from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter
            from deskbot_server.ws.chat_turn import publish_ws_chat_turn

            dev = ""
            text = ""
            scene_q = ""
            requested_operation_id = ""
            try:
                raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                payload = json.loads(raw_body) if raw_body.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid JSON body", "t": time.time()},
                )
            if isinstance(payload, dict):
                dev = str(payload.get("device_id") or "").strip()
                text = str(payload.get("text") or "").strip()
                scene_q = str(payload.get("scene") or "").strip()
                requested_operation_id = str(payload.get("operation_id") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if not text:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing text", "t": time.time()},
                )
            from deskbot_server.application.control_operations import (
                ControlOperationConflict,
                accept_control_operation,
            )

            proposed_req_id = uuid.uuid4().hex[:16]
            try:
                operation, created = await asyncio.to_thread(
                    accept_control_operation,
                    device_id=dev,
                    kind="device_tts",
                    payload={"text": text, "scene": scene_q or None},
                    operation_id=requested_operation_id or None,
                    request_id=proposed_req_id,
                )
            except ControlOperationConflict as exc:
                return _json_resp(
                    409,
                    {
                        "ok": False,
                        "error": "operation_id_conflict",
                        "message": str(exc),
                    },
                )
            except (TypeError, ValueError) as exc:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_control_operation",
                        "message": str(exc),
                    },
                )
            req_id = operation.request_id
            ws = await asr_chat_hub.first_ws(dev) if created else None
            if created and ws is None:
                operation = await asyncio.to_thread(
                    _fail_accepted_control_operation,
                    operation,
                    error_code="device_offline",
                    error_message="device USB session is not connected",
                )
                return _control_operation_response(
                    operation,
                    created=True,
                    extra={
                        "text": text,
                        "scene": scene_q or None,
                        "delivered": 0,
                        "hint": "请确认设备已通过 USB 连接且服务已识别相同 device_id",
                    },
                )
            settings = chat.settings
            broker = device_pipeline_broker

            async def _device_tts_action():
                from deskbot_server.application.turn_arbiter import (
                    PRIORITY_MANUAL,
                    device_turn_arbiter,
                )

                downlink = WsDownlinkAdapter(
                    ws,
                    settings=settings,
                    device_id=dev,
                    dp_broker=broker,
                )

                async def _run_manual_tts():
                    return await run_device_tts_only(
                        downlink,
                        chat,
                        text,
                        request_id=req_id,
                        device_id=dev,
                        scenes=[scene_q] if scene_q else None,
                        durable_replay=True,
                    )

                turn = await device_turn_arbiter.run(
                    dev,
                    _run_manual_tts,
                    source="http_device_tts",
                    priority=PRIORITY_MANUAL,
                )
                try:
                    await publish_ws_chat_turn(
                        broker,
                        registry,
                        dev,
                        source="device_tts",
                        asr_text=None,
                        t_asr_start=turn.t_llm_end,
                        t_asr_text=turn.t_llm_end,
                        flow=turn.as_dict(),
                        request_id=req_id,
                    )
                except Exception:
                    logger.exception(
                        "[HTTP] /api/device_tts telemetry publish failed device_id=%s req=%s",
                        dev,
                        req_id,
                    )
                ok = (turn.status or "ok") == "ok" and not turn.error
                logger.info(
                    "[HTTP] /api/device_tts job done device_id=%s req=%s "
                    "operation_id=%s content=%s ok=%s playback=%s err=%s",
                    dev,
                    req_id,
                    operation.operation_id,
                    safe_log_content(text),
                    ok,
                    turn.playback_status,
                    safe_log_content(turn.error) if turn.error else None,
                )
                return turn

            if created:
                asyncio.create_task(
                    _run_persisted_control_operation(
                        operation_id=operation.operation_id,
                        device_id=dev,
                        kind="device_tts",
                        action=_device_tts_action,
                    ),
                    name=f"control-operation:{operation.operation_id}",
                )
            logger.info(
                "[HTTP] %s /api/device_tts status=%s created=%s peer=%s "
                "device_id=%s req=%s operation_id=%s content=%s",
                method,
                operation.status,
                created,
                peer,
                dev,
                req_id,
                operation.operation_id,
                safe_log_content(text),
            )
            return _control_operation_response(
                operation,
                created=created,
                extra={
                    "text": text,
                    "scene": scene_q or None,
                },
            )

        if path_only == "/api/scene_playbooks":
            from deskbot_server.scene_playbooks_store import (
                collect_missing_servo_presets,
                load_scene_playbooks_file,
                normalize_scene_playbooks,
                save_scene_playbooks_file,
            )

            cfg_path = resolve_json_path(SCENE_PLAYBOOKS_FILE)
            if method == "GET":
                try:
                    rows = load_scene_playbooks_file(
                        seed_if_missing=True,
                    )
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    return _json_resp(
                        500,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                return _json_resp(
                    200,
                    {
                        "ok": True,
                        "config": rows or [],
                        "exists": os.path.isfile(cfg_path),
                        "file": os.path.basename(cfg_path),
                        "scope": "local",
                        "t": time.time(),
                    },
                )
            if method == "POST":
                try:
                    raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                    payload = json.loads(raw_body) if raw_body.strip() else []
                    rows = normalize_scene_playbooks(payload)
                    save_scene_playbooks_file(rows)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    return _json_resp(
                        400,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                except OSError as exc:
                    return _json_resp(
                        500,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                payload_out: dict[str, Any] = {
                    "ok": True,
                    "config": rows,
                    "file": os.path.basename(cfg_path),
                    "scope": "local",
                    "t": time.time(),
                }
                missing = collect_missing_servo_presets(rows)
                if missing:
                    payload_out["missing_servo_presets"] = missing
                return _json_resp(200, payload_out)
            return _json_resp(
                405,
                {"ok": False, "error": "method not allowed", "t": time.time()},
            )

        if path_only == "/api/scene_playbook/run":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST"},
                )
            from deskbot_server.application.chat_flow import run_device_playbook
            from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter
            from deskbot_server.scene_playbooks_store import (
                find_playbook_by_name,
                load_scene_playbooks_file,
                normalize_playbook,
            )

            dev = ""
            playbook_raw: object = None
            playbook_name = ""
            requested_operation_id = ""
            try:
                raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                payload = json.loads(raw_body) if raw_body.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid JSON body", "t": time.time()},
                )
            if isinstance(payload, dict):
                dev = str(payload.get("device_id") or "").strip()
                requested_operation_id = str(payload.get("operation_id") or "").strip()
                if "playbook" in payload:
                    playbook_raw = payload.get("playbook")
                elif payload.get("name"):
                    playbook_name = str(payload.get("name") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if playbook_raw is None and playbook_name:
                try:
                    rows = (
                        load_scene_playbooks_file(
                            seed_if_missing=False,
                        )
                        or []
                    )
                    playbook_raw = find_playbook_by_name(rows, playbook_name)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    return _json_resp(
                        500,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
            if not playbook_raw:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing playbook or unknown name", "t": time.time()},
                )
            try:
                playbook = normalize_playbook(playbook_raw)
            except ValueError as exc:
                return _json_resp(
                    400,
                    {"ok": False, "error": str(exc), "t": time.time()},
                )
            from deskbot_server.application.control_operations import (
                ControlOperationConflict,
                accept_control_operation,
            )

            proposed_req_id = uuid.uuid4().hex[:16]
            try:
                operation, created = await asyncio.to_thread(
                    accept_control_operation,
                    device_id=dev,
                    kind="scene_playbook",
                    payload=_scene_playbook_operation_payload(playbook),
                    operation_id=requested_operation_id or None,
                    request_id=proposed_req_id,
                )
            except ControlOperationConflict as exc:
                return _json_resp(
                    409,
                    {
                        "ok": False,
                        "error": "operation_id_conflict",
                        "message": str(exc),
                    },
                )
            except (TypeError, ValueError) as exc:
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_control_operation",
                        "message": str(exc),
                    },
                )
            req_id = operation.request_id
            ws = await asr_chat_hub.first_ws(dev) if created else None
            if created and ws is None:
                operation = await asyncio.to_thread(
                    _fail_accepted_control_operation,
                    operation,
                    error_code="device_offline",
                    error_message="device USB session is not connected",
                )
                return _control_operation_response(
                    operation,
                    created=True,
                    extra={
                        "name": playbook.get("name"),
                        "interleaved": True,
                        "delivered": 0,
                        "hint": "请确认设备已通过 USB 连接且服务已识别相同 device_id",
                    },
                )
            settings = chat.settings
            broker = device_pipeline_broker

            async def _playbook_action():
                from deskbot_server.application.turn_arbiter import (
                    PRIORITY_MANUAL,
                    device_turn_arbiter,
                )

                downlink = WsDownlinkAdapter(
                    ws,
                    settings=settings,
                    device_id=dev,
                    dp_broker=broker,
                )

                async def _run_manual_playbook():
                    return await run_device_playbook(
                        downlink,
                        chat,
                        playbook,
                        request_id=req_id,
                        device_id=dev,
                        durable_replay=True,
                    )

                turn = await device_turn_arbiter.run(
                    dev,
                    _run_manual_playbook,
                    source="http_scene_playbook",
                    priority=PRIORITY_MANUAL,
                )
                ok = (turn.status or "ok") == "ok" and not turn.error
                logger.info(
                    "[HTTP] /api/scene_playbook/run done device_id=%s req=%s "
                    "operation_id=%s name=%s ok=%s playback=%s err=%s",
                    dev,
                    req_id,
                    operation.operation_id,
                    playbook.get("name"),
                    ok,
                    turn.playback_status,
                    safe_log_content(turn.error) if turn.error else None,
                )
                return turn

            if created:
                asyncio.create_task(
                    _run_persisted_control_operation(
                        operation_id=operation.operation_id,
                        device_id=dev,
                        kind="scene_playbook",
                        action=_playbook_action,
                    ),
                    name=f"control-operation:{operation.operation_id}",
                )
            logger.info(
                "[HTTP] %s /api/scene_playbook/run status=%s created=%s "
                "peer=%s device_id=%s req=%s operation_id=%s name=%s",
                method,
                operation.status,
                created,
                peer,
                dev,
                req_id,
                operation.operation_id,
                playbook.get("name"),
            )
            return _control_operation_response(
                operation,
                created=created,
                extra={
                    "name": playbook.get("name"),
                    "interleaved": True,
                },
            )

        if path_only == "/api/device_pb_scene":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST"},
                )
            dev = (qargs.get("device_id") or "").strip()
            scene_q = (qargs.get("scene") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if not scene_q:
                return _json_resp(
                    400,
                    {"error": "missing scene", "t": time.time()},
                )
            ent = _pb_scene_entry_by_name(scene_q)
            if ent is None:
                valid = _pb_scene_keys_sorted()
                return _json_resp(
                    400,
                    {
                        "error": f"unknown or empty scene: {scene_q!r}",
                        "valid_scenes": valid,
                        "t": time.time(),
                    },
                )
            durable_pb_scene_name = str(ent.get("name") or scene_q).strip()
            validation_pairs = design_frames_to_pb_chain(
                ent.get("frames") or [],
                runtime_req="validation",
            )
            if not validation_pairs:
                return _json_resp(
                    500,
                    {"error": "empty frames", "t": time.time()},
                )

            def _build_pb_scene_sequences(request_id: str):
                durable_pairs = design_frames_to_pb_chain(
                    ent.get("frames") or [],
                    runtime_req=request_id,
                )
                return [(request_id, durable_pairs)]

            return await _start_pb_control_operation(
                device_id=dev,
                kind="device_pb_scene",
                operation_payload={
                    "scene": durable_pb_scene_name,
                    "frames": copy.deepcopy(ent.get("frames") or []),
                },
                requested_operation_id=str(qargs.get("operation_id") or "").strip(),
                source="http_device_pb_scene",
                build_sequences=_build_pb_scene_sequences,
                extra={
                    "scene": durable_pb_scene_name,
                    "frames": len(validation_pairs),
                },
            )

        if path_only == "/api/servo_contract":
            if method != "GET":
                return _json_resp(
                    405,
                    {"ok": False, "error": "method not allowed", "t": time.time()},
                )
            try:
                presets = servo_preset_catalog()
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "[HTTP] GET /api/servo_contract preset read failed peer=%s err=%s",
                    peer,
                    exc,
                )
                presets = []
            return _json_resp(
                200,
                {
                    "ok": True,
                    "envelope": dict(SERVO_HARDWARE_ENVELOPE),
                    "limits": {
                        "maxSegmentsPerPb": SERVO_MAX_SEGMENTS_PER_PB,
                        "maxBatchDurationMs": SERVO_MAX_BATCH_DURATION_MS,
                        "minSegmentDurationMs": SERVO_MIN_SEGMENT_DURATION_MS,
                        "maxPlanSteps": SERVO_MAX_PLAN_STEPS,
                        "maxPlanDurationMs": SERVO_MAX_PLAN_DURATION_MS,
                        "maxDegreesPerTick": SERVO_MAX_DEGREES_PER_TICK,
                        "tickMs": SERVO_TICK_MS,
                    },
                    "viewerLrSwap": dict(VIEWER_LR_SWAP),
                    "presets": presets,
                    "scope": "local",
                    "t": time.time(),
                },
            )

        if path_only == "/api/servo_config":
            cfg_path = resolve_json_path(SERVO_CFG_FILE)
            if method == "GET":
                try:
                    cfg = load_servo_cfg_file()
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "[HTTP] GET /api/servo_config read failed peer=%s err=%s",
                        peer,
                        exc,
                    )
                    return _json_resp(
                        500,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                if cfg is None:
                    return _json_resp(
                        200,
                        {
                            "ok": True,
                            "exists": False,
                            "file": os.path.basename(cfg_path),
                            "scope": "local",
                            "t": time.time(),
                        },
                    )
                logger.info(
                    "[HTTP] GET /api/servo_config peer=%s -> %s",
                    peer,
                    os.path.basename(cfg_path),
                )
                return _json_resp(
                    200,
                    {
                        "ok": True,
                        "exists": True,
                        "config": cfg,
                        "file": os.path.basename(cfg_path),
                        "scope": "local",
                        "t": time.time(),
                    },
                )
            if method == "POST":
                try:
                    raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                    payload = json.loads(raw_body) if raw_body.strip() else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return _json_resp(
                        400,
                        {"ok": False, "error": "invalid JSON body", "t": time.time()},
                    )
                try:
                    cfg = normalize_servo_document(payload, require_presets=True)
                    save_servo_cfg_file(cfg)
                except ValueError as exc:
                    return _json_resp(
                        400,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                except OSError as exc:
                    logger.warning(
                        "[HTTP] POST /api/servo_config write failed peer=%s err=%s",
                        peer,
                        exc,
                    )
                    return _json_resp(
                        500,
                        {"ok": False, "error": str(exc), "t": time.time()},
                    )
                logger.info(
                    "[HTTP] POST /api/servo_config peer=%s -> %s",
                    peer,
                    cfg_path,
                )
                return _json_resp(
                    200,
                    {
                        "ok": True,
                        "config": cfg,
                        "file": os.path.basename(cfg_path),
                        "scope": "local",
                        "t": time.time(),
                    },
                )
            return _json_resp(
                405,
                {"ok": False, "error": "method not allowed", "t": time.time()},
            )

        if path_only == "/api/device_pb_anim":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST"},
                )
            anim: Optional[dict[str, Any]] = None
            dev = ""
            chunk_ms = 500
            act = PB_ACTION_REPLACE
            pb_level = PB_LEVEL_TASK

            try:
                raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                body = json.loads(raw_body) if raw_body.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid JSON body", "t": time.time()},
                )
            if not isinstance(body, dict):
                return _json_resp(
                    400,
                    {"ok": False, "error": "JSON body must be an object", "t": time.time()},
                )
            dev = str(body.get("device_id") or "").strip()
            anim = body.get("anim")
            expression_name = str(
                body.get("expression_name") or body.get("name") or ""
            ).strip()
            expression_title = str(
                body.get("expression_title") or body.get("title") or ""
            ).strip()
            try:
                chunk_ms = int(body.get("chunk_ms", 500))
            except (TypeError, ValueError):
                chunk_ms = 500
            act = str(body.get("action") or PB_ACTION_REPLACE).strip().lower()
            try:
                pb_level = int(body.get("level", PB_LEVEL_TASK))
            except (TypeError, ValueError):
                pb_level = PB_LEVEL_TASK

            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if not isinstance(anim, (dict, list)):
                return _json_resp(
                    400,
                    {"ok": False, "error": "anim must be array or object", "t": time.time()},
                )
            if isinstance(anim, dict):
                if not isinstance(anim.get("elements"), dict):
                    return _json_resp(
                        400,
                        {"ok": False, "error": "anim.elements required", "t": time.time()},
                    )
                anim_list = [{"elements": copy.deepcopy(anim["elements"]), "ms": chunk_ms}]
            else:
                anim_list = []
                for one in anim:
                    if not isinstance(one, dict) or not isinstance(one.get("elements"), dict):
                        return _json_resp(
                            400,
                            {"ok": False, "error": "anim[] items need elements", "t": time.time()},
                        )
                    try:
                        item_ms = int(one.get("ms") or chunk_ms)
                    except (TypeError, ValueError):
                        item_ms = chunk_ms
                    item: dict[str, Any] = {
                        "elements": copy.deepcopy(one["elements"]),
                        "ms": max(1, item_ms),
                    }
                    ph = str(one.get("phoneme") or "").strip()
                    if ph:
                        item["phoneme"] = ph
                    anim_list.append(item)
                if not anim_list:
                    return _json_resp(
                        400,
                        {"ok": False, "error": "anim[] must not be empty", "t": time.time()},
                    )
                chunk_ms = sum(int(x["ms"]) for x in anim_list)
            if act not in (PB_ACTION_REPLACE, PB_ACTION_APPEND, PB_ACTION_DEFAULT):
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid action", "t": time.time()},
                )
            if pb_level not in (0, 1, 2, 3):
                return _json_resp(
                    400,
                    {"ok": False, "error": "invalid level", "t": time.time()},
                )

            try:
                validation_pairs = design_frames_to_pb_chain(
                    anim_list,
                    runtime_req="validation",
                )
            except (TypeError, ValueError) as exc:
                return _json_resp(
                    400,
                    {"ok": False, "error": str(exc), "t": time.time()},
                )
            if not validation_pairs:
                return _json_resp(
                    400,
                    {"ok": False, "error": "anim produced no PB frames", "t": time.time()},
                )
            total_ms = sum(
                max(1, int(payload.get("chunk_ms") or 0))
                for payload, _binary in validation_pairs
            )

            def _build_anim_sequences(request_id: str):
                durable_pairs = design_frames_to_pb_chain(
                    anim_list,
                    runtime_req=request_id,
                )
                for payload, _binary in durable_pairs:
                    payload["action"] = act
                    payload["level"] = pb_level
                return [(request_id, durable_pairs)]

            return await _start_pb_control_operation(
                device_id=dev,
                kind="device_pb_anim",
                operation_payload={
                    "chunk_ms": total_ms,
                    "action": act,
                    "level": pb_level,
                    "anim": anim_list,
                    "expression_name": expression_name or "adhoc_web",
                    "expression_title": expression_title or expression_name or "Web 预览",
                },
                requested_operation_id=str(body.get("operation_id") or "").strip(),
                source="http_device_pb_anim",
                build_sequences=_build_anim_sequences,
                extra={
                    "chunk_ms": total_ms,
                    "action": act,
                    "level": pb_level,
                    "frames": len(anim_list),
                    "chunks": len(validation_pairs),
                },
            )

        if path_only == "/api/device_pb_expr_scene":
            if method != "POST":
                return _json_resp(
                    405,
                    {"ok": False, "error": "device control requires POST", "t": time.time()},
                )
            dev = (qargs.get("device_id") or "").strip()
            scene_q = (qargs.get("scene") or qargs.get("name") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            denied = _device_route_error(dev)
            if denied is not None:
                return denied
            if not scene_q:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing scene", "t": time.time()},
                )
            try:
                rows = load_face_expr_scenes_file(seed_if_missing=False) or []
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return _json_resp(
                    500,
                    {"ok": False, "error": str(exc), "t": time.time()},
                )
            ent = find_design_scene_by_name(rows, scene_q)
            if ent is None:
                valid = sorted({str(r.get("name") or "") for r in rows if r.get("name")})
                return _json_resp(
                    400,
                    {
                        "ok": False,
                        "error": f"unknown scene: {scene_q!r}",
                        "valid_scenes": valid,
                        "t": time.time(),
                    },
                )
            durable_scene_name = str(ent.get("name") or scene_q).strip()
            validation_pairs = design_frames_to_pb_chain(
                ent.get("frames") or [],
                runtime_req="validation",
            )
            if not validation_pairs:
                return _json_resp(
                    500,
                    {"error": "empty frames", "t": time.time()},
                )

            def _build_scene_sequences(request_id: str):
                durable_pairs = design_frames_to_pb_chain(
                    ent.get("frames") or [],
                    runtime_req=request_id,
                )
                return [(request_id, durable_pairs)]

            return await _start_pb_control_operation(
                device_id=dev,
                kind="device_pb_expr_scene",
                operation_payload={
                    "scene": durable_scene_name,
                    "frames": copy.deepcopy(ent.get("frames") or []),
                },
                requested_operation_id=str(qargs.get("operation_id") or "").strip(),
                source="http_device_pb_expr_scene",
                build_sequences=_build_scene_sequences,
                extra={
                    "scene": durable_scene_name,
                    "frames": len(validation_pairs),
                },
            )

        if path_only.startswith("/api/"):
            logger.warning("[HTTP] 未知 API peer=%s path=%s -> 404", peer, path_only)
            return _json_resp(404, {"error": "not found", "path": path_only})

        logger.debug("[HTTP] 非 API 请求忽略 peer=%s path=%s", peer, path_only)
        return _no_content()

    return handler
