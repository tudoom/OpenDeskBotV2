"""Core-owned execution boundary for tools requested by the RTC Agent worker."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from deskbot_server.application.expression_runtime import (
    play_rtc_expression,
)
from deskbot_server.application.interaction_feedback import send_servo_moves_and_wait
from deskbot_server.application.llm_tool_loop import (
    _tool_result_for_storage,
    execute_tools_round,
)
from deskbot_server.device_camera_frame_store import capture_camera_for_device_async
from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY
from deskbot_server.servo_config_store import (
    clamp_servo_step,
    load_servo_cfg_file,
)
from deskbot_server.servo_protocol import (
    SERVO_MIN_SEGMENT_DURATION_MS,
    ServoProtocolError,
)

if TYPE_CHECKING:
    from deskbot_server.ws.asr_chat_hub import AsrChatHub

logger = logging.getLogger("deskbot-server")

_CORE_TOOL_NAMES = frozenset(
    {
        "capture_camera",
        "capture_and_describe",
        "memory_add",
        "memory_delete",
        "schedule_task",
        "session",
        "miot",
        "webfetch",
        "websearch",
        "read",
        "write",
        "register_face",
    }
)
_RTC_TOOL_NAMES = _CORE_TOOL_NAMES | {
    "move_head",
    "play_expression",
}
_RTC_VISION_IMAGE_B64_KEY = "_rtc_vision_image_b64"
_MOVE_MS_MIN = 200
_MOVE_MS_MAX = 8000


def rtc_tool_names() -> frozenset[str]:
    return _RTC_TOOL_NAMES


def _bounded_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


async def _play_expression(
    *,
    device_id: str,
    arguments: dict[str, Any],
    asr_chat_hub: "AsrChatHub",
) -> dict[str, Any]:
    name = str(arguments.get("name") or arguments.get("expression") or "").strip()
    if not name:
        return {
            "tool": "play_expression",
            "ok": False,
            "error": "play_expression requires name",
        }
    # The authenticated bridge selects the device, while the registered RTC
    # expression runtime owns its exact current USB session and ordered PB
    # sender.  Never fan this tool out through a guessed/stale connection.
    del asr_chat_hub
    return await play_rtc_expression(
        device_id,
        name,
        duration_ms=arguments.get("duration_ms"),
    )


async def _move_head(
    *,
    device_id: str,
    arguments: dict[str, Any],
    asr_chat_hub: "AsrChatHub",
    call_id: str = "",
) -> dict[str, Any]:
    move = str(arguments.get("move") or arguments.get("preset") or "").strip()
    if not move:
        return {
            "tool": "move_head",
            "ok": False,
            "error": "move_head requires a configured move preset",
        }
    try:
        cfg = load_servo_cfg_file() or {}
        preset = next(
            (
                row
                for row in (cfg.get("presets") or [])
                if isinstance(row, dict)
                and bool(row.get("exposeToModel"))
                and str(row.get("id") or "").strip().casefold() == move.casefold()
            ),
            None,
        )
        if preset is None:
            raise ServoProtocolError("preset is not model-visible")
        canonical_move = str(preset.get("id") or "").strip()
        protocol_steps = [
            clamp_servo_step(step, limits=cfg)
            for step in (preset.get("steps") or [])
        ]
        minimum_duration_ms = len(protocol_steps) * SERVO_MIN_SEGMENT_DURATION_MS
        if not protocol_steps or minimum_duration_ms > _MOVE_MS_MAX:
            raise ServoProtocolError("preset exceeds RTC motion duration budget")
    except (OSError, TypeError, ValueError, ServoProtocolError):
        return {
            "tool": "move_head",
            "ok": False,
            "status": "invalid_move",
            "error": "move_head requires a model-visible configured preset",
        }
    move = canonical_move
    duration_ms = _bounded_int(
        arguments.get("duration_ms"),
        minimum=max(_MOVE_MS_MIN, minimum_duration_ms),
        maximum=_MOVE_MS_MAX,
        default=max(1000, minimum_duration_ms),
    )
    stable_request_id = None
    if str(call_id or "").strip():
        stable_request_id = "rtcm" + hashlib.sha256(
            str(call_id).encode("utf-8")
        ).hexdigest()[:12]
    try:
        delivery = await send_servo_moves_and_wait(
            asr_chat_hub,
            device_id,
            [{"move": move, "ms": duration_ms}],
            source="rtc_tool_move",
            summary=f"RTC move preset {move}",
            request_id=stable_request_id,
        )
    except ServoProtocolError:
        return {
            "tool": "move_head",
            "ok": False,
            "status": "invalid_move",
            "error": "move_head preset cannot be represented safely",
        }
    return {
        "tool": "move_head",
        "ok": delivery.ok,
        "status": "moved" if delivery.ok else delivery.status,
        "move": move,
        "duration_ms": duration_ms,
        "request_id": delivery.request_id,
        "delivered": delivery.delivered,
        "accepted": delivery.accepted,
        "played": delivery.played,
    }


async def _capture_and_describe(
    *,
    device_id: str,
    arguments: dict[str, Any],
    asr_chat_hub: "AsrChatHub",
) -> dict[str, Any]:
    """Return one validated JPEG only across the authenticated local bridge.

    The Worker consumes and removes the private base64 field before returning
    from the function tool. It must never enter a model tool result or durable
    chat history.
    """

    question = str(arguments.get("question") or "").strip()
    if not question:
        return {
            "tool": "capture_and_describe",
            "ok": False,
            "error": "capture_and_describe requires question",
        }
    result = await capture_camera_for_device_async(
        device_id,
        hub=asr_chat_hub,
        display=False,
        require_fresh=True,
    )
    image = result.pop(TRANSIENT_VISION_IMAGE_KEY, None)
    if not result.get("ok") or not isinstance(image, dict):
        return {"tool": "capture_and_describe", **_tool_result_for_storage(result)}
    jpeg = image.get("bytes")
    if not isinstance(jpeg, bytes) or not jpeg:
        return {
            "tool": "capture_and_describe",
            "ok": False,
            "error": "camera capture returned no validated JPEG",
        }
    result.pop("image_display", None)
    return {
        "tool": "capture_and_describe",
        **result,
        _RTC_VISION_IMAGE_B64_KEY: base64.b64encode(jpeg).decode("ascii"),
    }


async def execute_rtc_tool(
    *,
    device_id: str,
    tool: str,
    arguments: dict[str, Any],
    call_id: str,
    asr_chat_hub: "AsrChatHub",
    device_context: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one allow-listed RTC tool inside the Core process.

    The Worker is intentionally unable to import Core runtime state. This
    function is called only after the localhost bridge authenticates the
    Worker and verifies that ``device_id`` is currently connected.
    """

    dev = str(device_id or "").strip()
    name = str(tool or "").strip()
    if not dev or name not in _RTC_TOOL_NAMES:
        return {
            "tool": name,
            "ok": False,
            "status": "unsupported",
            "error": "unsupported RTC tool",
        }
    if name == "play_expression":
        return await _play_expression(
            device_id=dev,
            arguments=arguments,
            asr_chat_hub=asr_chat_hub,
        )
    if name == "move_head":
        return await _move_head(
            device_id=dev,
            arguments=arguments,
            asr_chat_hub=asr_chat_hub,
            call_id=call_id,
        )
    if name == "capture_and_describe":
        return await _capture_and_describe(
            device_id=dev,
            arguments=arguments,
            asr_chat_hub=asr_chat_hub,
        )
    raw = dict(arguments)
    raw["tool"] = name
    operation_id = str(call_id or "").strip()
    if operation_id:
        raw["operation_id"] = f"rtc:{operation_id}"[:128]
    results = await execute_tools_round(
        [raw],
        device_id=dev,
        asr_chat_hub=asr_chat_hub,
        user_confirmed=False,
        allow_camera_display=False,
        device_context=device_context,
    )
    if not results:
        return {
            "tool": name,
            "ok": False,
            "status": "no_result",
            "error": "RTC tool produced no result",
        }
    return _tool_result_for_storage(results[-1])


__all__ = ["execute_rtc_tool", "rtc_tool_names"]
