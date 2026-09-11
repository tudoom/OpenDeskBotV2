"""LiveKit function tools that proxy into the authenticated Deskbot Core."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from functools import wraps
from typing import Any

_BRIDGE_URL_ENV = "DESKBOT_RTC_TOOL_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "DESKBOT_RTC_TOOL_BRIDGE_TOKEN"
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DEVICE_IDENTITY_PREFIX = "deskbot-usb-"
_HTTP_TIMEOUT_SECONDS = 15.0
# 网络类工具要等外部站点/联网搜索（方舟 web_search 实测 6-18s），单独放宽。
_TOOL_HTTP_TIMEOUT_SECONDS = {"websearch": 45.0, "webfetch": 30.0}
_RTC_VISION_IMAGE_B64_KEY = "_rtc_vision_image_b64"
_RTC_MOVE_MIN_MS = 200
_RTC_MOVE_MAX_MS = 8000
# 自编排上限：五步足够表达点头、张望、犹豫这类动作，再多就该沉淀成预设。
_RTC_MOVE_MAX_STEPS = 5
_RTC_MOVE_MIN_STEP_MS = 50


def _object_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = list(required)
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
    }


_BASE_RTC_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    _object_schema(
        "play_expression",
        "Show one named expressive face only when the user explicitly asks "
        "the robot to display an emotion or expression. Do not call this for "
        "ordinary conversation or lifecycle states such as idle, listening, "
        "thinking, or speaking; those are automatic.",
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "duration_ms": {
                "type": "integer",
                "minimum": 100,
                "maximum": 30000,
            },
        },
        required=("name",),
    ),
    _object_schema(
        "move_head",
        "Run one configured expressive head-motion preset such as nod or shake.",
        {
            "move": {"type": "string", "minLength": 1, "maxLength": 80},
            "duration_ms": {
                "type": "integer",
                "minimum": 200,
                "maximum": 8000,
            },
        },
        required=("move",),
    ),
    _object_schema(
        "capture_camera",
        "Request one fresh camera frame and return capture metadata only. "
        "This tool cannot answer questions about what is visible.",
        {},
    ),
    _object_schema(
        "capture_and_describe",
        "Take one fresh camera photo and answer a question about that exact image. "
        "Use this whenever the user asks what the robot can see.",
        {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            },
        },
        required=("question",),
    ),
    _object_schema(
        "user_note",
        "Read or update User.md — your own running understanding of the person "
        "you live with: their preferences, habits, situation and what they seem "
        "to care about. Use action='append' after learning something about them "
        "that is worth carrying forward, and action='replace' only to rewrite "
        "the whole note when it has become repetitive or out of date. This is "
        "not a place for one-off events; those belong in memory_add.",
        {
            "action": {"type": "string", "enum": ["read", "append", "replace"]},
            "text": {
                "type": "string",
                "maxLength": 4000,
                "description": "Required for append and replace.",
            },
        },
        required=("action",),
    ),
    _object_schema(
        "memory_add",
        "Save a durable memory in this PC's Deskbot profile. When saving how the user wants "
        "to be addressed, a one-character name (e.g. “小”) or a cut-off phrase is almost always a "
        "truncated transcript: do not save it, ask the user to confirm the full name first.",
        {"text": {"type": "string", "minLength": 1, "maxLength": 2000}},
        required=("text",),
    ),
    _object_schema(
        "memory_delete",
        "Request deletion of one memory by id. Core requires explicit confirmation.",
        {"id": {"type": "string", "minLength": 1, "maxLength": 128}},
        required=("id",),
    ),
    _object_schema(
        "schedule_task",
        "Create, list, read, update, or request deletion of a reminder. "
        "Use Beijing time; destructive actions require confirmation.",
        {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "update", "delete"],
            },
            "id": {"type": "string", "maxLength": 128},
            "task": {"type": "string", "maxLength": 500},
            "scene": {"type": "string", "maxLength": 80},
            "task_kind": {"type": "string", "enum": ["once", "recurring"]},
            "cron": {"type": "string", "maxLength": 80},
            "delay_minutes": {"type": "number", "minimum": 0},
            "enabled": {"type": "boolean"},
        },
        required=("action",),
    ),
    _object_schema(
        "session",
        "Read the current or recent conversation session, or search the whole "
        "archive with action='search'. Search is how you recall something said "
        "days ago — do not page through sessions one by one. Queries shorter "
        "than three characters fall back to a slower scan, so prefer a phrase.",
        {
            "action": {
                "type": "string",
                "enum": ["current", "list", "get", "search"],
            },
            "session_id": {"type": "string", "maxLength": 128},
            "query": {
                "type": "string",
                "maxLength": 200,
                "description": "Search text; required when action='search'.",
            },
            "role": {"type": "string", "enum": ["user", "assistant"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        required=("action",),
    ),
    _object_schema(
        "miot",
        "Read or control Xiaomi Home (米家) devices. action=set writes one property "
        "(key/value, e.g. key='on' value=true to switch a light); action=action calls a "
        "device action (key + args); action=run_scene runs a scene by scene_name. "
        "For a Xiaoai speaker (小爱音箱) song/radio/voice-command requests ALWAYS use "
        "key='execute-text-directive' with args=[<what the user would say to Xiaoai>, true] "
        "(e.g. args=['播放周杰伦的晴天', true]); never use play-text for songs (it only reads "
        "the text aloud). Playback control: key='pause'/'next'/'previous' with empty args. "
        "Do not call spec first for these. Pick the device by name (add room if ambiguous).",
        {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "sync",
                    "list",
                    "spec",
                    "props",
                    "get",
                    "set",
                    "action",
                    "run_scene",
                ],
            },
            "name": {"type": "string", "maxLength": 120},
            "did": {"type": "string", "maxLength": 120},
            "room": {"type": "string", "maxLength": 120},
            "keys": {
                "type": "array",
                "items": {"type": "string", "maxLength": 120},
                "maxItems": 32,
            },
            "key": {
                "type": "string",
                "maxLength": 120,
                "description": "Property key for set (e.g. 'on', 'brightness') or action key.",
            },
            "value": {
                "type": ["boolean", "number", "string"],
                "description": "New property value for action=set.",
            },
            "args": {
                "type": "array",
                "items": {"type": ["boolean", "number", "string"]},
                "maxItems": 8,
                "description": "Positional arguments for action=action.",
            },
            "scene_name": {"type": "string", "maxLength": 120},
        },
        required=("action",),
    ),
    _object_schema(
        "websearch",
        "Search the web and return a small text summary.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("query",),
    ),
    _object_schema(
        "webfetch",
        "Fetch readable text from one public HTTP or HTTPS page.",
        {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
        required=("url",),
    ),
    _object_schema(
        "read",
        "Read a UTF-8 file from the current device's isolated tmp directory.",
        {"path": {"type": "string", "minLength": 1, "maxLength": 240}},
        required=("path",),
    ),
    _object_schema(
        "write",
        "Request writing a UTF-8 file in the current device's isolated tmp directory. "
        "Core requires explicit confirmation.",
        {
            "path": {"type": "string", "minLength": 1, "maxLength": 240},
            "content": {"type": "string", "maxLength": 20000},
        },
        required=("path", "content"),
    ),
    _object_schema(
        "register_face",
        "Request registering the currently captured face under a name. "
        "Core requires explicit confirmation.",
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "face_id": {"type": "integer", "minimum": 0},
        },
        required=("name",),
    ),
    # 剧本任务（Quest）：任务定义与可用 task_id 由 system prompt 附录给出。
    _object_schema(
        "update_task_result",
        "Mark the running quest task as success or unmet once you have talked it through: "
        "judge by its success condition and what the user replied. Call it every time you finish "
        "a step, even when unmet; the scene only moves on to the next step after a result is "
        "recorded (a few quiet seconds later). Only use task ids listed under the current quest tasks.",
        {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "status": {"type": "string", "enum": ["success", "unmet"]},
            "result": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        required=("task_id", "status", "result"),
    ),
    _object_schema(
        "update_task_strategy",
        "Update how a running quest task should be handled after the user gives "
        "feedback (for example: stop asking, keep it short).",
        {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "strategy": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        required=("task_id", "strategy"),
    ),
    _object_schema(
        "perform_scene",
        "Perform one pre-arranged show (speech + expression + motion sequence) by its name "
        "when the user asks for a performance, celebration or greeting routine. Names are "
        "listed in the system prompt. At most one per turn.",
        {"name": {"type": "string", "minLength": 1, "maxLength": 80}},
        required=("name",),
    ),
    _object_schema(
        "skip_goal",
        "Skip the current running quest goal when the user says they do not want it "
        "(for example: stop asking, never mind). Marks it as skipped, never asks again, and moves on.",
        {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "reason": {"type": "string", "maxLength": 200},
        },
        required=("task_id",),
    ),
    _object_schema(
        "propose_goal",
        "Propose one new long-term companion goal after the user verbally agrees "
        "(for example caring about their sleep). It waits for the user's approval "
        "in the console before it starts. Propose at most one at a time.",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": 40},
            "goal": {"type": "string", "minLength": 1, "maxLength": 400},
            "strategy": {"type": "string", "maxLength": 400},
            "success_condition": {"type": "string", "maxLength": 400},
            "schedule_time": {"type": "string", "maxLength": 5},
            "after": {"type": "string", "maxLength": 64},
        },
        required=("title", "goal"),
    ),
)


def _play_expression_schema() -> dict[str, Any]:
    """Build the model-facing enum from the same catalog Core will resolve."""

    try:
        from deskbot_server.application.expression_runtime import (
            expression_tool_catalog,
        )

        catalog = expression_tool_catalog()
    except Exception:
        # An empty enum is safer than inviting the model to invent a name when
        # the canonical local face file cannot be read in the worker process.
        catalog = {"values": [], "aliases": {}, "expressions": []}
    values = [str(value) for value in catalog.get("values") or [] if str(value)]
    aliases = {
        str(alias): str(target)
        for alias, target in (catalog.get("aliases") or {}).items()
        if str(alias) and str(target)
    }
    names = [
        str(row.get("name") or "")
        for row in catalog.get("expressions") or []
        if isinstance(row, dict) and str(row.get("name") or "")
    ]
    alias_note = ", ".join(
        f"{alias}->{target}" for alias, target in aliases.items()
    )
    description = (
        "Show one expression configured in this PC's live Deskbot face "
        "library only when the user explicitly requests a visible expression. "
        "Do not use this for ordinary replies, inferred sentiment, idle, "
        "listening, thinking, or speaking; those lifecycle faces are automatic. "
        "Choose only a value from the name enum; never invent an "
        "expression name. Configured expressions: "
        + (", ".join(names) if names else "none")
        + (f". Accepted aliases/state mappings: {alias_note}" if alias_note else "")
        + "."
    )
    return _object_schema(
        "play_expression",
        description,
        {
            "name": {
                "type": "string",
                "enum": values,
                "description": "A configured expression name or advertised alias.",
            },
            "duration_ms": {
                "type": "integer",
                "minimum": 100,
                "maximum": 30000,
            },
        },
        required=("name",),
    )


def _move_head_schema() -> dict[str, Any] | None:
    """Build the model-facing motion enum from Core's normalized catalog."""

    try:
        from deskbot_server.servo_config_store import (
            clamp_servo_step,
            load_servo_cfg_file,
        )
        from deskbot_server.servo_protocol import (
            SERVO_MIN_SEGMENT_DURATION_MS,
        )

        cfg = load_servo_cfg_file() or {}
        presets = [
            preset
            for preset in (cfg.get("presets") or [])
            if isinstance(preset, dict) and bool(preset.get("exposeToModel"))
        ]
    except Exception:
        cfg = {}
        presets = []
    values: list[str] = []
    labels: list[str] = []
    minimums: list[int] = []
    for preset in presets:
        preset_id = str(preset.get("id") or "").strip()
        if not preset_id:
            continue
        try:
            steps = [
                clamp_servo_step(step, limits=cfg)
                for step in (preset.get("steps") or [])
            ]
            minimum_ms = len(steps) * SERVO_MIN_SEGMENT_DURATION_MS
        except Exception:
            continue
        if not steps or minimum_ms > _RTC_MOVE_MAX_MS:
            continue
        values.append(preset_id)
        labels.append(
            f"{preset_id} ({preset.get('label') or preset_id}, "
            f"representable minimum {minimum_ms} ms)"
        )
        minimums.append(minimum_ms)
    envelope = {
        "xMin": int(cfg.get("xMin", 10) or 10),
        "xMax": int(cfg.get("xMax", 170) or 170),
        "yMin": int(cfg.get("yMin", 70) or 70),
        "yMax": int(cfg.get("yMax", 110) or 110),
    }
    duration_min = max(_RTC_MOVE_MIN_MS, max(minimums)) if minimums else _RTC_MOVE_MIN_MS
    return _object_schema(
        "move_head",
        "Move the head. Prefer a configured preset with `move` — those are "
        "tuned and always safe. Only when nothing in the catalog fits what "
        "was asked, compose the motion yourself with `steps` (at most "
        f"{_RTC_MOVE_MAX_STEPS}), and never pass both. Configured motions: "
        + (", ".join(labels) if labels else "none")
        + f". Angles are clamped to x {envelope['xMin']}-{envelope['xMax']}, "
        f"y {envelope['yMin']}-{envelope['yMax']}; x turns left/right and y "
        "tilts up/down, with higher y looking up.",
        {
            # 目录里没有可见预设时不放 move：`"enum": []` 是非法 schema，模型
            # 也无从选择；此时工具只剩自由组合的 steps。
            **(
                {
                    "move": {
                        "type": "string",
                        "enum": values,
                        "description": "A model-visible preset from the local motion catalog.",
                    }
                }
                if values
                else {}
            ),
            "steps": {
                "type": "array",
                "maxItems": _RTC_MOVE_MAX_STEPS,
                "description": (
                    "Composed motion, used only when no preset fits. Each step "
                    "moves both axes together and then holds for its duration."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "x": {
                            "type": "integer",
                            "description": "Horizontal angle, or offset when xm=1.",
                        },
                        "y": {
                            "type": "integer",
                            "description": "Vertical angle, or offset when ym=1.",
                        },
                        "xm": {
                            "type": "integer",
                            "enum": [0, 1, 2],
                            "description": "0 absolute, 1 relative, 2 hold.",
                        },
                        "ym": {
                            "type": "integer",
                            "enum": [0, 1, 2],
                            "description": "0 absolute, 1 relative, 2 hold.",
                        },
                        "ms": {
                            "type": "integer",
                            "minimum": _RTC_MOVE_MIN_STEP_MS,
                            "maximum": _RTC_MOVE_MAX_MS,
                        },
                    },
                    "required": ["ms"],
                },
            },
            "duration_ms": {
                "type": "integer",
                "minimum": duration_min,
                "maximum": _RTC_MOVE_MAX_MS,
                "description": "Total duration; applies to `move` only.",
            },
        },
    )
def build_rtc_tool_schemas() -> tuple[dict[str, Any], ...]:
    """Refresh file-backed tool instructions for each new Agent session."""

    refreshed: list[dict[str, Any]] = []
    for schema in _BASE_RTC_TOOL_SCHEMAS:
        name = str(schema.get("name") or "")
        if name == "play_expression":
            refreshed.append(_play_expression_schema())
        elif name == "move_head":
            move_schema = _move_head_schema()
            if move_schema is not None:
                refreshed.append(move_schema)
        else:
            refreshed.append(schema)
    return tuple(refreshed)


# Compatibility snapshot for callers that introspect exported tool names.
# Actual Agent construction calls ``build_rtc_tool_schemas`` again.
RTC_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = build_rtc_tool_schemas()


def _room_from_context(ctx: Any) -> Any:
    session = getattr(ctx, "session", None)
    room_io = getattr(session, "room_io", None)
    room = getattr(room_io, "room", None)
    if room is None:
        raise RuntimeError("RTC tool context has no LiveKit room")
    return room


def _device_id_from_context(ctx: Any) -> str:
    room = _room_from_context(ctx)
    participants = getattr(room, "remote_participants", None)
    if isinstance(participants, dict):
        rows = participants.values()
    else:
        rows = participants or ()
    device_ids: list[str] = []
    for participant in rows:
        identity = str(getattr(participant, "identity", "") or "").strip()
        if not identity.startswith(_DEVICE_IDENTITY_PREFIX):
            continue
        device_id = identity[len(_DEVICE_IDENTITY_PREFIX) :]
        if _DEVICE_ID_RE.fullmatch(device_id):
            device_ids.append(device_id)
    unique = sorted(set(device_ids))
    if len(unique) != 1:
        raise RuntimeError("RTC room does not contain exactly one Deskbot device")
    device_id = unique[0]
    room_name = str(getattr(room, "name", "") or "").strip()
    if room_name and not room_name.startswith(f"deskbot-{device_id}-"):
        raise RuntimeError("RTC room and device identity do not match")
    return device_id


# 指令驱动的那一轮里，这些工具执行完不需要模型再说一遍：结果只是记账
logger = logging.getLogger("deskbot-server")

_QUIET_AFTER_INSTRUCTION_TOOLS = frozenset({"update_task_result", "skip_goal", "update_task_strategy"})


def _in_instruction_turn(ctx: Any) -> bool:
    """当前工具调用是否发生在 Core 指令（generate_reply(instructions=…)）生成的那一轮。"""
    session = getattr(ctx, "session", None)
    ids = getattr(session, "_deskbot_instruction_speech_ids", None)
    handle = getattr(ctx, "speech_handle", None)
    speech_id = getattr(handle, "id", None)
    return bool(ids and speech_id and str(speech_id) in ids)


async def _call_core_tool(
    ctx: Any,
    tool: str,
    raw_arguments: dict[str, Any],
) -> dict[str, Any]:
    bridge_url = str(os.environ.get(_BRIDGE_URL_ENV) or "").strip()
    token = str(os.environ.get(_BRIDGE_TOKEN_ENV) or "").strip()
    if not bridge_url or not token:
        raise RuntimeError("Deskbot RTC tool bridge is not configured")
    device_id = _device_id_from_context(ctx)
    call = getattr(ctx, "function_call", None)
    call_id = str(
        getattr(call, "call_id", "") or getattr(call, "id", "") or ""
    ).strip()
    import httpx

    verify_tls = not bridge_url.lower().startswith("https://")
    async with httpx.AsyncClient(
        timeout=_TOOL_HTTP_TIMEOUT_SECONDS.get(tool, _HTTP_TIMEOUT_SECONDS),
        trust_env=False,
        verify=verify_tls,
    ) as client:
        response = await client.post(
            bridge_url,
            headers={"X-Deskbot-RTC-Bridge": token},
            json={
                "device_id": device_id,
                "tool": tool,
                "arguments": dict(raw_arguments or {}),
                "call_id": call_id,
            },
        )
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("error") or "")
        except Exception:
            detail = ""
        raise RuntimeError(
            f"Deskbot Core rejected {tool}: HTTP {response.status_code}"
            + (f" ({detail})" if detail else "")
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Deskbot Core returned an invalid tool response")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Deskbot Core tool response is missing result")
    return result


def _consume_rtc_vision_jpeg(result: dict[str, Any]) -> bytes | None:
    """Pop and validate the bridge-only image before any tool output exists."""

    encoded = result.pop(_RTC_VISION_IMAGE_B64_KEY, None)
    if encoded is None:
        return None
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("Deskbot Core returned an invalid transient camera image")
    from deskbot_server.llm.vision_input import MAX_VISION_IMAGE_BYTES

    max_encoded = ((MAX_VISION_IMAGE_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded:
        raise RuntimeError("Deskbot Core returned an oversized camera image")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Deskbot Core returned invalid camera image base64") from exc

    from deskbot_server.llm.vision_input import validate_jpeg_bytes

    try:
        return validate_jpeg_bytes(decoded)
    except Exception as exc:
        raise RuntimeError("Deskbot Core returned an invalid camera JPEG") from exc


def _start_same_session_vision_reply(
    ctx: Any,
    *,
    question: str,
    jpeg: bytes,
) -> bool:
    """Generate from a transient multimodal copy of this exact Agent history."""

    from livekit.agents import llm

    session = getattr(ctx, "session", None)
    history = getattr(session, "history", None)
    generate_reply = getattr(session, "generate_reply", None)
    if history is None or not callable(generate_reply):
        return False

    encoded = base64.b64encode(jpeg).decode("ascii")
    transient_ctx = history.copy()
    transient_ctx.add_message(
        role="user",
        content=[
            question,
            llm.ImageContent(
                image=f"data:image/jpeg;base64,{encoded}",
                mime_type="image/jpeg",
                inference_detail="low",
            ),
        ],
    )
    try:
        handle = generate_reply(chat_ctx=transient_ctx, tool_choice="none")
    except Exception:
        transient_ctx.items.clear()
        raise
    add_done_callback = getattr(handle, "add_done_callback", None)
    if callable(add_done_callback):
        add_done_callback(lambda _handle: transient_ctx.items.clear())
    else:
        # A nonstandard SDK without SpeechHandle lifecycle hooks must not keep
        # the image-bearing context alive after this callback returns.
        transient_ctx.items.clear()
    return True


def build_livekit_deskbot_tools() -> list[Any]:
    from livekit.agents import RunContext, function_tool

    from deskbot_server.rtc_llm_adapter import (
        ark_web_search_active,
        build_ark_web_search_tool,
    )

    tools: list[Any] = []
    builtin_search = ark_web_search_active()
    if builtin_search:
        # 主模型走方舟 Responses 时，联网由服务端内置工具在回答那一轮直接完成；
        # Core 的 websearch 函数工具会多一整轮往返，且与内置搜索语义重叠，撤掉。
        tools.append(build_ark_web_search_tool())
    for schema in build_rtc_tool_schemas():
        tool_name = str(schema["name"])
        if builtin_search and tool_name == "websearch":
            continue

        async def _callback(
            raw_arguments: dict[str, Any],
            ctx: RunContext,
            *,
            _tool_name: str = tool_name,
        ) -> dict[str, Any] | None:
            result = await _call_core_tool(ctx, _tool_name, raw_arguments)
            if _tool_name in _QUIET_AFTER_INSTRUCTION_TOOLS and _in_instruction_turn(ctx):
                # 主动陪伴 / 催标结果的那一轮：模型已经按指令说过话了，工具再返回内容 LiveKit 就会
                # 拿着同一条指令再生成一轮，把打招呼原样念第二遍（2026-09-10 20:15、21:00 两次都是）。
                logger.info("[rtc-tools] %s in instruction turn: no follow-up reply", _tool_name)
                return None
            if _tool_name != "capture_and_describe":
                return result

            jpeg = _consume_rtc_vision_jpeg(result)
            if jpeg is None:
                return result
            question = str(raw_arguments.get("question") or "").strip()
            if not question:
                raise RuntimeError("capture_and_describe requires question")
            try:
                if _start_same_session_vision_reply(
                    ctx,
                    question=question,
                    jpeg=jpeg,
                ):
                    # The explicit same-session reply owns the assistant turn.
                    # Returning the Core metadata would trigger a redundant
                    # text-only follow-up and could expose implementation data.
                    return None
            except Exception:
                return {
                    "tool": "capture_and_describe",
                    "ok": False,
                    "error": "The current RTC Agent could not accept the camera image.",
                }
            return {
                "tool": "capture_and_describe",
                "ok": False,
                "error": "The current RTC Agent does not support camera images.",
            }

        # ``from __future__ import annotations`` stores ``RunContext`` as a
        # string, but RunContext is intentionally imported inside this
        # optional-dependency factory.  LiveKit later calls get_type_hints()
        # with the module globals and cannot resolve that local-only name,
        # aborting the whole reply whenever the model selects a tool.  Pin the
        # runtime type object onto the callback before registration.
        _callback.__annotations__["ctx"] = RunContext
        tools.append(function_tool(raw_schema=schema)(_callback))
    return tools


def install_lampgo_deskbot_tools() -> bool:
    """Inject Core-backed tools into LampGo's otherwise tool-less Agent."""

    bridge_url = str(os.environ.get(_BRIDGE_URL_ENV) or "").strip()
    token = str(os.environ.get(_BRIDGE_TOKEN_ENV) or "").strip()
    if not bridge_url or not token:
        return False

    from livekit.agents import Agent

    current_init = Agent.__init__
    if getattr(current_init, "_deskbot_rtc_tools", False):
        return True

    @wraps(current_init)
    def _deskbot_agent_init(self, *args, **kwargs):
        supplied = kwargs.get("tools")
        deskbot_tools = build_livekit_deskbot_tools()
        if supplied is None:
            kwargs["tools"] = deskbot_tools
        else:
            existing = list(supplied)
            existing_ids = {
                str(getattr(tool, "id", "") or "")
                for tool in existing
            }
            kwargs["tools"] = existing + [
                tool for tool in deskbot_tools if tool.id not in existing_ids
            ]
        return current_init(self, *args, **kwargs)

    setattr(_deskbot_agent_init, "_deskbot_rtc_tools", True)
    Agent.__init__ = _deskbot_agent_init
    return True


__all__ = [
    "RTC_TOOL_SCHEMAS",
    "build_rtc_tool_schemas",
    "build_livekit_deskbot_tools",
    "install_lampgo_deskbot_tools",
]
