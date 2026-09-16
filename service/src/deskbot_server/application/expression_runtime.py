"""USB-owned facial-expression state, arbitration and playback runtime.

The RTC worker and the Core process intentionally share only the canonical
``data/local/deskbot-face.json`` library.  Core owns the physical USB session,
so all expression PB is resolved and written here rather than in the worker.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
import uuid
import zlib
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("deskbot-server")

# 位图（卡通）表情播完一条时间线后是否自动续发形成循环；默认关闭（设备持续解码 JPEG 有性能顾虑）。
BITMAP_IDLE_LOOP_RESEND = True

CANONICAL_EXPRESSION_STATES: tuple[str, ...] = (
    "idle",
    "listening",
    "thinking",
    "speaking",
    "happy",
    "sad",
    "angry",
    "surprised",
    "sleepy",
)

# Lifecycle faces are owned by the RTC state machine.  They are valid
# transition inputs, but advertising them to the model lets a tool call fight
# the same state machine that already displays them.
MODEL_EXPRESSION_STATES: tuple[str, ...] = (
    "happy",
    "sad",
    "angry",
    "surprised",
    "sleepy",
)

from deskbot_server.application.expression_catalog import (  # noqa: E402,F401 —— 再导出
    _DISPLAY_LAYER_ORDER,
    _DISPLAY_SHAPE_ENUM,
    _EXPRESSION_LEASE_MS_MAX,
    _EXPRESSION_MS_MAX,
    _EXPRESSION_MS_MIN,
    _EXPRESSION_TIMELINE_MS_MAX,
    _MODEL_BLOCKED_EXPRESSION_KEYS,
    _STATE_BY_KEY,
    EXPRESSION_STATE_ALIASES,
    ExpressionCatalog,
    ExpressionScene,
    _bounded_duration,
    _cpp_int,
    _cpp_lround,
    _display_primitive_state,
    _i16,
    _iter_scene_rows,
    _lease_duration_ms,
    _normalize_mapping_rows,
    _scale_messages,
    _scene_playback_ms,
    _stable_fingerprint,
    _state_entry_frames,
    build_expression_catalog,
    build_expression_pb_frames,
    display_semantic_crc32,
    expression_tool_catalog,
    fingerprint_expression_messages,
    fingerprint_pb_display_pairs,
    join_expression_pb_chains,
    load_expression_catalog,
    normalize_expression_state,
    standby_face_tag,
)


@dataclass(frozen=True, slots=True)
class ExpressionSendResult:
    requested: str
    resolved: str | None
    status: str
    frames: int = 0
    delivered: int = 0
    state: str | None = None
    source: str | None = None
    reason: str | None = None
    title: str | None = None
    frame_fingerprint: str | None = None
    final_frame_fingerprint: str | None = None
    final_frame_index: int | None = None
    frame_count: int | None = None
    timeline_ms: int | None = None
    operation_id: str | None = None
    expected_display_crc32: str | None = None
    device_display_crc32: str | None = None
    display_crc_match: bool | None = None
    previous_source: str | None = None
    preempt_reason: str | None = None
    lease_token: str | None = None
    coalesced: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"accepted", "played", "unchanged"}

    def as_tool_result(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": "play_expression",
            "ok": self.ok,
            "status": self.status,
            "expression": self.resolved or self.requested,
            "requested_expression": self.requested,
            "frames": self.frames,
            "delivered": self.delivered,
        }
        if self.state is not None:
            payload["state"] = self.state
        if self.source is not None:
            payload["source"] = self.source
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.previous_source is not None:
            payload["previous_source"] = self.previous_source
        if self.preempt_reason is not None:
            payload["preempt_reason"] = self.preempt_reason
        if self.title is not None:
            payload["title"] = self.title
        if self.frame_fingerprint is not None:
            payload["frame_fingerprint"] = self.frame_fingerprint
        if self.final_frame_fingerprint is not None:
            payload["final_frame_fingerprint"] = self.final_frame_fingerprint
        if self.final_frame_index is not None:
            payload["final_frame_index"] = self.final_frame_index
        if self.frame_count is not None:
            payload["frame_count"] = self.frame_count
        if self.timeline_ms is not None:
            payload["timeline_ms"] = self.timeline_ms
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
        if self.expected_display_crc32 is not None:
            payload["expected_display_crc32"] = self.expected_display_crc32
        if self.device_display_crc32 is not None:
            payload["device_display_crc32"] = self.device_display_crc32
        if self.display_crc_match is not None:
            payload["display_crc_match"] = self.display_crc_match
        if self.lease_token is not None:
            # The full token is an internal capability. Diagnostics only need
            # a short correlation id and must not expose the capability itself.
            payload["lease_id"] = self.lease_token[:8]
        if self.coalesced:
            payload["coalesced"] = True
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class _ExpressionRequest:
    requested: str
    state: str | None
    duration_ms: object
    restore_idle: bool
    force: bool
    reason: str
    source: str
    future: asyncio.Future[ExpressionSendResult]
    wait_for_played: bool = True
    operation_id: str | None = None
    scene_override: ExpressionScene | None = None
    lease_token: str | None = None
    kind: str = "state"
    state_generation: int = 0
    explicit_generation: int = 0
    cancel_generation: int = 0
    previous_source: str | None = None
    preempt_reason: str | None = None
    superseded: asyncio.Event = field(default_factory=asyncio.Event)
    request_id: str | None = None
    title: str | None = None
    fingerprint: dict[str, Any] = field(default_factory=dict)
    # 位图循环续发：以 append 接在当前链后面，不打断正在播放的时间线
    pb_append: bool = False


@dataclass(slots=True)
class _ExpressionLease:
    token: str
    source: str
    priority: int
    generation: int
    duration_ms: int | None = None
    expires_at: float | None = None
    acquired_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _DisplayedExpression:
    name: str
    title: str
    source: str
    reason: str
    status: str
    frame_fingerprint: str
    final_frame_fingerprint: str
    final_frame_index: int
    frame_count: int
    timeline_ms: int
    operation_id: str | None
    state: str | None
    voice_mouth: bool
    mouth_only: bool
    expected_display_crc32: str | None
    device_display_crc32: str | None
    display_crc_match: bool | None
    generation: int
    updated_at: float


@dataclass(frozen=True, slots=True)
class _ExpressionOperation:
    request_id: str
    name: str
    title: str
    source: str
    reason: str
    status: str
    frame_fingerprint: str
    final_frame_fingerprint: str
    final_frame_index: int
    frame_count: int
    timeline_ms: int
    operation_id: str | None
    state: str | None
    voice_mouth: bool
    mouth_only: bool
    expected_display_crc32: str | None
    device_display_crc32: str | None
    display_crc_match: bool | None
    updated_at: float


@asynccontextmanager
async def _ordered_session_chain(session: object):
    # Share the hub's whole-PB-chain lock as well as the USB adapter lock.  RTC
    # writes go to the exact session directly, but must not splice themselves
    # into a boot/TTS chain that happens to use AsrChatHub on that same object.
    try:
        from deskbot_server.ws.ws_send import _pb_ws_chain_serial_lock

        hub_chain_lock = _pb_ws_chain_serial_lock(session)
    except (ImportError, AttributeError, TypeError):
        hub_chain_lock = asyncio.Lock()
    chain_factory = getattr(session, "downlink_chain", None)
    async with hub_chain_lock:
        if callable(chain_factory):
            async with chain_factory():
                yield
            return
        else:
            yield


class RtcExpressionRuntime:
    """Latest-wins expression arbiter bound to one exact USB session.

    The historical class name is retained for compatibility.  Its lifecycle is
    USB-owned: RTC is only one state producer alongside Web, Agent and boot.
    """

    def __init__(
        self,
        device_id: str,
        device_session: object,
        *,
        catalog_loader: Callable[[], ExpressionCatalog] = load_expression_catalog,
    ) -> None:
        self.device_id = str(device_id or "").strip()
        self.device_session = device_session
        self._catalog_loader = catalog_loader
        self._queue_lock = asyncio.Lock()
        self._pending: list[_ExpressionRequest] = []
        self._active_request: _ExpressionRequest | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_scene_name: str | None = None
        self._displayed: _DisplayedExpression | None = None
        self._operations: deque[_ExpressionOperation] = deque(maxlen=32)
        # Keep enough ownership changes to diagnose a complete voice turn even
        # when listening/thinking/Agent/image events happen in quick succession.
        self._display_history: deque[_DisplayedExpression] = deque(maxlen=32)
        self._display_generation = 0
        self._accepted_generation = 0
        self._desired_state = "idle"
        self._mouth_overlay: dict[str, Any] | None = None
        self._lease: _ExpressionLease | None = None
        self._lease_generation = 0
        self._lease_timer_task: asyncio.Task[None] | None = None
        self._completion_tasks: set[asyncio.Task[None]] = set()
        self._state_generation = 0
        self._explicit_generation = 0
        self._cancel_generation = 0
        # 待机卡通脸（固件 ≥0.0.57）：设备 FFat 里已确认保存的内容标签；空 = 未知/没有
        self._persisted_face_tag = ""
        self._face_persist_task: asyncio.Task[None] | None = None
        self._face_clear_checked = False

    @property
    def last_scene_name(self) -> str | None:
        return self._last_scene_name

    @property
    def desired_state(self) -> str:
        return self._desired_state

    @property
    def active_source(self) -> str:
        return self._displayed.source if self._displayed is not None else "none"

    def _record_displayed(
        self,
        *,
        name: str,
        title: str,
        source: str,
        reason: str,
        status: str,
        fingerprint: dict[str, Any],
        operation_id: str | None = None,
        state: str | None = None,
        device_display_crc32: str | None = None,
    ) -> None:
        now = time.time()
        previous = self._displayed
        same_accepted_playback = bool(
            previous is not None
            and previous.status == "accepted"
            and status == "played"
            and previous.name == name
            and previous.source == source
            and previous.reason == reason
            and previous.operation_id == operation_id
            and previous.state == state
            and previous.frame_fingerprint
            == str(fingerprint["frame_fingerprint"])
            and previous.final_frame_fingerprint
            == str(fingerprint["final_frame_fingerprint"])
        )
        generation = previous.generation if same_accepted_playback else self._display_generation + 1
        displayed = _DisplayedExpression(
            name=name,
            title=title,
            source=source,
            reason=reason,
            status=status,
            frame_fingerprint=str(fingerprint["frame_fingerprint"]),
            final_frame_fingerprint=str(fingerprint["final_frame_fingerprint"]),
            final_frame_index=int(fingerprint["final_frame_index"]),
            frame_count=max(0, int(fingerprint.get("frame_count") or 0)),
            timeline_ms=max(0, int(fingerprint.get("timeline_ms") or 0)),
            operation_id=(str(operation_id).strip() or None) if operation_id else None,
            state=state,
            voice_mouth=bool(fingerprint.get("voice_mouth")),
            mouth_only=bool(fingerprint.get("mouth_only")),
            expected_display_crc32=(
                str(fingerprint.get("expected_display_crc32") or "") or None
            ),
            device_display_crc32=(
                str(device_display_crc32 or "").strip().lower() or None
            ),
            display_crc_match=(
                str(device_display_crc32 or "").strip().lower()
                == str(fingerprint.get("expected_display_crc32") or "").strip().lower()
                if device_display_crc32
                and fingerprint.get("expected_display_crc32")
                else None
            ),
            generation=generation,
            updated_at=now,
        )
        if same_accepted_playback:
            if self._display_history and self._display_history[-1].generation == generation:
                self._display_history.pop()
            self._display_history.append(displayed)
        else:
            self._display_generation = generation
            self._display_history.append(displayed)
        self._last_scene_name = name
        self._displayed = displayed

    def _record_operation(
        self,
        *,
        request_id: str,
        name: str,
        title: str,
        source: str,
        reason: str,
        status: str,
        fingerprint: dict[str, Any],
        operation_id: str | None = None,
        state: str | None = None,
        device_display_crc32: str | None = None,
    ) -> None:
        operation = _ExpressionOperation(
            request_id=request_id,
            name=name,
            title=title,
            source=source,
            reason=reason,
            status=status,
            frame_fingerprint=str(fingerprint.get("frame_fingerprint") or ""),
            final_frame_fingerprint=str(
                fingerprint.get("final_frame_fingerprint") or ""
            ),
            final_frame_index=int(fingerprint.get("final_frame_index") or 0),
            frame_count=max(0, int(fingerprint.get("frame_count") or 0)),
            timeline_ms=max(0, int(fingerprint.get("timeline_ms") or 0)),
            operation_id=(str(operation_id).strip() or None) if operation_id else None,
            state=state,
            voice_mouth=bool(fingerprint.get("voice_mouth")),
            mouth_only=bool(fingerprint.get("mouth_only")),
            expected_display_crc32=(
                str(fingerprint.get("expected_display_crc32") or "") or None
            ),
            device_display_crc32=(
                str(device_display_crc32 or "").strip().lower() or None
            ),
            display_crc_match=(
                str(device_display_crc32 or "").strip().lower()
                == str(fingerprint.get("expected_display_crc32") or "").strip().lower()
                if device_display_crc32
                and fingerprint.get("expected_display_crc32")
                else None
            ),
            updated_at=time.time(),
        )
        self._operations = deque(
            (item for item in self._operations if item.request_id != request_id),
            maxlen=32,
        )
        self._operations.append(operation)

    def _record_result_operation(
        self,
        request: _ExpressionRequest,
        *,
        result: ExpressionSendResult,
    ) -> None:
        """Keep every resolved send attempt attributable in diagnostics."""

        if not request.request_id:
            return
        self._record_operation(
            request_id=request.request_id,
            name=(
                request.scene_override.name
                if request.scene_override is not None
                else result.resolved or request.requested
            ),
            title=(
                request.title
                or (
                    request.scene_override.title
                    if request.scene_override is not None
                    else result.resolved or request.requested
                )
            ),
            source=request.source,
            reason=request.reason,
            status=result.status,
            fingerprint=request.fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=result.device_display_crc32,
        )

    @staticmethod
    def _displayed_payload(displayed: _DisplayedExpression) -> dict[str, Any]:
        return {
            "expression": displayed.name,
            "title": displayed.title,
            "source": displayed.source,
            "reason": displayed.reason,
            "status": displayed.status,
            "state": displayed.state,
            "voice_mouth": displayed.voice_mouth,
            "mouth_only": displayed.mouth_only,
            "expected_display_crc32": displayed.expected_display_crc32,
            "device_display_crc32": displayed.device_display_crc32,
            "display_crc_match": displayed.display_crc_match,
            "frame_fingerprint": displayed.frame_fingerprint,
            "final_frame_fingerprint": displayed.final_frame_fingerprint,
            "final_frame_index": displayed.final_frame_index,
            "frame_count": displayed.frame_count,
            "timeline_ms": displayed.timeline_ms,
            "operation_id": displayed.operation_id,
            "generation": displayed.generation,
            "displayed_at": displayed.updated_at,
        }

    @staticmethod
    def _operation_payload(operation: _ExpressionOperation) -> dict[str, Any]:
        return {
            "request_id": operation.request_id,
            "expression": operation.name,
            "title": operation.title,
            "source": operation.source,
            "reason": operation.reason,
            "status": operation.status,
            "state": operation.state,
            "voice_mouth": operation.voice_mouth,
            "mouth_only": operation.mouth_only,
            "frame_fingerprint": operation.frame_fingerprint,
            "final_frame_fingerprint": operation.final_frame_fingerprint,
            "final_frame_index": operation.final_frame_index,
            "frame_count": operation.frame_count,
            "timeline_ms": operation.timeline_ms,
            "operation_id": operation.operation_id,
            "expected_display_crc32": operation.expected_display_crc32,
            "device_display_crc32": operation.device_display_crc32,
            "display_crc_match": operation.display_crc_match,
            "updated_at": operation.updated_at,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return enough ownership state to identify every visible face."""

        lease = self._lease
        active = self._active_request
        displayed = self._displayed
        rtc_mouth_active = bool(
            displayed is not None
            and displayed.state == "speaking"
            and displayed.voice_mouth
            and self._desired_state == "speaking"
        )
        overlay = self._mouth_overlay
        mouth_overlay = (
            dict(overlay)
            if overlay is not None
            else {
                "active": rtc_mouth_active,
                "kind": "rtc_pcm" if rtc_mouth_active else None,
                "source": "rtc" if rtc_mouth_active else None,
                "reason": "agent_speaking" if rtc_mouth_active else None,
                "started_at": displayed.updated_at if rtc_mouth_active else None,
            }
        )
        return {
            "device_id": self.device_id,
            "displayed_expression": displayed.name if displayed is not None else None,
            "displayed_title": displayed.title if displayed is not None else None,
            "displayed_source": displayed.source if displayed is not None else None,
            "displayed_reason": displayed.reason if displayed is not None else None,
            "displayed_status": displayed.status if displayed is not None else None,
            "displayed_state": displayed.state if displayed is not None else None,
            "voice_mouth_enabled": (
                displayed.voice_mouth if displayed is not None else False
            ),
            "mouth_only": displayed.mouth_only if displayed is not None else False,
            "expected_display_crc32": (
                displayed.expected_display_crc32 if displayed is not None else None
            ),
            "device_display_crc32": (
                displayed.device_display_crc32 if displayed is not None else None
            ),
            "display_crc_match": (
                displayed.display_crc_match if displayed is not None else None
            ),
            "mouth_overlay": mouth_overlay,
            "frame_fingerprint": (
                displayed.frame_fingerprint if displayed is not None else None
            ),
            "final_frame_fingerprint": (
                displayed.final_frame_fingerprint if displayed is not None else None
            ),
            "final_frame_index": (
                displayed.final_frame_index if displayed is not None else None
            ),
            "frame_count": displayed.frame_count if displayed is not None else 0,
            "timeline_ms": displayed.timeline_ms if displayed is not None else 0,
            "displayed_operation_id": (
                displayed.operation_id if displayed is not None else None
            ),
            "display_generation": displayed.generation if displayed is not None else 0,
            "displayed_at": displayed.updated_at if displayed is not None else None,
            # Newest first: this makes a lifecycle sequence such as
            # listening -> thinking -> speaking -> idle directly auditable
            # instead of looking like unrelated faces chosen by the device.
            "history": [
                self._displayed_payload(item)
                for item in reversed(self._display_history)
            ],
            "operations": [
                self._operation_payload(item) for item in reversed(self._operations)
            ],
            "desired_state": self._desired_state,
            # Kept for API compatibility: it now means the source that last
            # reached the display, rather than whichever lease happens to live.
            "active_source": displayed.source if displayed is not None else "none",
            "lease": (
                {
                    "lease_id": lease.token[:8],
                    "source": lease.source,
                    "priority": lease.priority,
                    "duration_ms": lease.duration_ms,
                    "expires_at": lease.expires_at,
                    "reason": lease.acquired_reason,
                }
                if lease is not None
                else None
            ),
            "active_request": (
                {
                    "requested": active.requested,
                    "state": active.state,
                    "source": active.source,
                    "reason": active.reason,
                    "kind": active.kind,
                }
                if active is not None
                else None
            ),
            "pending": [
                {
                    "requested": request.requested,
                    "state": request.state,
                    "source": request.source,
                    "reason": request.reason,
                    "kind": request.kind,
                }
                for request in self._pending
            ],
        }

    def begin_mouth_overlay(
        self,
        *,
        source: str,
        reason: str,
        kind: str = "phoneme",
    ) -> str:
        """Expose a temporary mouth-only writer without treating it as a face."""

        token = uuid.uuid4().hex
        self._mouth_overlay = {
            "active": True,
            "kind": str(kind or "phoneme"),
            "source": str(source or "tts"),
            "reason": str(reason or "mouth_overlay"),
            "started_at": time.time(),
            "overlay_id": token[:8],
        }
        return token

    def end_mouth_overlay(self, token: str) -> None:
        overlay = self._mouth_overlay
        if overlay is None or overlay.get("overlay_id") != str(token or "")[:8]:
            return
        self._mouth_overlay = None

    async def transition(
        self,
        state: object,
        *,
        force: bool = False,
        reason: str = "agent_state",
    ) -> ExpressionSendResult:
        canonical = normalize_expression_state(state) or "idle"
        async with self._queue_lock:
            self._desired_state = canonical
            if self._lease is not None and not force:
                logger.info(
                    "[expression] deferred device_id=%s source=rtc "
                    "requested=%s resolved=%s owner=%s reason=%s",
                    self.device_id,
                    canonical,
                    self._last_scene_name or "",
                    self._lease.source,
                    reason,
                )
                return ExpressionSendResult(
                    requested=canonical,
                    resolved=self._last_scene_name,
                    status="deferred",
                    state=canonical,
                    source="rtc",
                    reason=reason,
                    previous_source=self._lease.source,
                )
            if not force:
                active = self._active_request
                duplicate_active = bool(
                    active is not None
                    and active.kind == "state"
                    and active.state == canonical
                    and not active.superseded.is_set()
                )
                duplicate_pending = any(
                    pending.kind == "state"
                    and pending.state == canonical
                    and not pending.superseded.is_set()
                    for pending in self._pending
                )
                # 同一状态正在播/排队，但映射已经换了场景（用户刚换了一套卡通表情）：
                # 不能按"重复生命周期"合并，否则第一次说话还停在旧套图的说话脸，
                # 要等位图循环下一轮续发才换过来（2026-09-07 真机复现）。
                mapping_changed = False
                if duplicate_active:
                    try:
                        target = self._catalog_loader().resolve_state(canonical)
                    except Exception:  # noqa: BLE001 - 目录不可用时按原逻辑合并
                        target = None
                    active_resolved = str((active.fingerprint or {}).get("resolved_scene") or "")
                    if target is not None and active_resolved and target.name != active_resolved:
                        mapping_changed = True
                        logger.info(
                            "[expression] lifecycle mapping changed device_id=%s state=%s "
                            "active=%s target=%s: resend instead of coalesce",
                            self.device_id,
                            canonical,
                            active_resolved,
                            target.name,
                        )
                if (duplicate_active or duplicate_pending) and not mapping_changed:
                    logger.info(
                        "[expression] coalesced duplicate lifecycle "
                        "device_id=%s state=%s reason=%s",
                        self.device_id,
                        canonical,
                        reason,
                    )
                    return ExpressionSendResult(
                        requested=canonical,
                        resolved=self._last_scene_name,
                        status="coalesced",
                        state=canonical,
                        source="rtc",
                        reason=reason,
                        coalesced=True,
                    )
        return await self._submit(
            requested=canonical,
            state=canonical,
            duration_ms=None,
            restore_idle=False,
            force=force,
            reason=reason,
            source="rtc",
            kind="cancel" if force and canonical == "idle" else "state",
        )

    set_state = transition

    async def restore_idle(
        self,
        *,
        reason: str,
        force: bool = False,
    ) -> ExpressionSendResult:
        return await self.transition("idle", force=force, reason=reason)

    async def play_expression(
        self,
        name: object,
        *,
        duration_ms: object = None,
        restore_idle: bool = True,
        reason: str = "tool",
        source: str = "agent",
        priority: int = 20,
        lease_ms: object = None,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        del restore_idle
        normalized_operation_id = str(operation_id or "").strip() or None
        duration = _bounded_duration(duration_ms)
        catalog = self._catalog_loader()
        scene = catalog.resolve_scene(name)
        if scene is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=None,
                status="not_found",
                source=source,
                reason=reason,
                operation_id=normalized_operation_id,
                error=f"unknown expression: {str(name or '').strip()}",
            )
        lease_duration = _lease_duration_ms(
            scene,
            duration_ms=duration,
            requested_lease_ms=lease_ms,
        )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=lease_duration,
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=str(name or "").strip(),
                state=None,
                duration_ms=duration,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def play_frames(
        self,
        frames: Iterable[dict[str, Any]],
        assets: Iterable[bytes] = (),
        *,
        name: object = None,
        title: object = None,
        source: str = "web",
        priority: int = 30,
        reason: str = "web_preview",
        hold_ms: object = None,
        persist_until_preempted: bool = False,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        """Play validated ad-hoc Web frames through the same source arbiter."""

        normalized_operation_id = str(operation_id or "").strip() or None
        copied = tuple(copy.deepcopy(list(frames)))
        scene_name = str(name or "").strip() or f"adhoc_{source}"
        scene_title = str(title or "").strip() or scene_name
        scene = ExpressionScene(
            name=scene_name,
            title=scene_title,
            aliases=(),
            frames=copied,
            assets=tuple(bytes(b) for b in assets),
        )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=(
                None
                if persist_until_preempted
                else _lease_duration_ms(
                    scene,
                    requested_lease_ms=hold_ms,
                )
            ),
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=scene.name,
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=scene.name,
                state=None,
                duration_ms=None,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def play_scene(
        self,
        name: object,
        *,
        source: str,
        priority: int,
        reason: str,
        hold_ms: object = None,
        persist_until_preempted: bool = False,
        wait_for_played: bool = True,
        operation_id: object = None,
    ) -> ExpressionSendResult:
        """Play a catalog scene under an explicit source lease."""

        normalized_operation_id = str(operation_id or "").strip() or None
        catalog = self._catalog_loader()
        scene = catalog.resolve_scene(name)
        if scene is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=None,
                status="not_found",
                source=source,
                reason=reason,
                operation_id=normalized_operation_id,
                error=f"unknown expression: {str(name or '').strip()}",
            )
        token = await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=(
                None
                if persist_until_preempted
                else _lease_duration_ms(
                    scene,
                    requested_lease_ms=hold_ms,
                )
            ),
            reason=reason,
        )
        if token is None:
            return ExpressionSendResult(
                requested=str(name or "").strip(),
                resolved=self._last_scene_name,
                status="deferred",
                source=source,
                reason=reason,
                previous_source=self.active_source,
                operation_id=normalized_operation_id,
                error="a higher-priority expression source is active",
            )
        try:
            result = await self._submit(
                requested=str(name or "").strip(),
                state=None,
                duration_ms=None,
                restore_idle=False,
                force=True,
                reason=reason,
                source=source,
                kind="explicit",
                wait_for_played=wait_for_played,
                operation_id=normalized_operation_id,
                scene_override=scene,
                lease_token=token,
            )
            if not result.ok:
                await self.release_lease(token, reason=f"{source}_failed")
            return result
        except asyncio.CancelledError:
            await self.release_lease(token, reason=f"{source}_cancelled")
            raise

    async def acquire_external_display(
        self,
        *,
        name: str,
        title: str,
        source: str,
        priority: int,
        reason: str,
    ) -> str | None:
        """Acquire the display lane for a PB writer that carries binary assets."""

        return await self.acquire_lease(
            source=source,
            priority=priority,
            duration_ms=None,
            reason=reason,
        )

    def external_display_matches(self, token: str) -> bool:
        lease = self._lease
        return bool(lease is not None and lease.token == str(token or ""))

    async def release_external_display(
        self,
        token: str,
        *,
        reason: str,
        restore: bool = True,
    ) -> ExpressionSendResult | None:
        """Release a binary display lease, optionally without a redundant redraw."""

        async with self._queue_lock:
            if self._lease is None or self._lease.token != token:
                return None
            lease = self._lease
            self._lease = None
            self._explicit_generation += 1
            timer = self._lease_timer_task
            self._lease_timer_task = None
            desired = self._desired_state
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        if not restore:
            logger.info(
                "[expression] external lease released device_id=%s source=%s "
                "restore=false reason=%s",
                self.device_id,
                lease.source,
                reason,
            )
            return None
        return await self._submit(
            requested=desired,
            state=desired,
            duration_ms=None,
            restore_idle=False,
            force=True,
            reason=f"restore_desired:{reason}",
            source="rtc",
            kind="state",
            previous_source=lease.source,
            preempt_reason=reason,
        )

    def record_external_display(
        self,
        token: str,
        *,
        name: str,
        title: str,
        source: str,
        reason: str,
        fingerprint: dict[str, Any],
    ) -> bool:
        """Record an externally-sent image only while its lease is current."""

        if not self.external_display_matches(token):
            return False
        self._record_displayed(
            name=str(name or "external_display"),
            title=str(title or name or "external display"),
            source=str(source or "external"),
            reason=str(reason or "external_display"),
            status="played",
            fingerprint=fingerprint,
        )
        logger.info(
            "[expression] external played device_id=%s source=%s name=%s "
            "fingerprint=%s reason=%s",
            self.device_id,
            source,
            name,
            str(fingerprint.get("frame_fingerprint") or "")[:12],
            reason,
        )
        return True

    async def preempt_lease(self, *, reason: str) -> None:
        """End any explicit lease without first restoring a stale state.

        权威语义 · 生命周期 preempt 矩阵（与 ``acquire_lease`` 的数字优先级
        表配对；正式文档由后续 docs 波次引用此处）：

        ============================  ============================================
        触发事件（rtc_runtime）        对显示权的影响
        ============================  ============================================
        barge_in                      ``preempt_lease`` → ``transition("listening")``
        user_speech_start             同上
        user_speech_confirmed_end     不 preempt；``transition("thinking")``
        agent_state（生命周期态）      不 preempt；``transition(state)``（持有租约
                                      期间仅记 ``_desired_state``，返回 deferred）
        ============================  ============================================

        **preempt 无视优先级**：无论当前租约是 10（voice_link_feedback）、
        20（agent）、25（manual_tts）还是 30（web），用户开口说话
        （barge_in / user_speech_start）都会立即终结租约——真人语音边沿
        是比任何显式表情源都高的事实输入，不参与数字比较。

        其余租约终点（对照）：

        - ``release_lease`` / 租约到期 ``_expire_lease``：恢复
          ``_desired_state``（``restore_desired:<reason>``）。
        - ``cancel``（会话取消/断开）：desired 置 idle 后走 release。
        - 同级或更高优先级新租约：``acquire_lease`` 直接替换旧租约。

        preempt 本身 **不下发** 新表情：只作废租约与 explicit 队列，随后的
        lifecycle ``transition`` 决定屏幕内容。
        """

        async with self._queue_lock:
            lease = self._lease
            if lease is None:
                return
            self._lease = None
            self._explicit_generation += 1
            timer = self._lease_timer_task
            self._lease_timer_task = None
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            stale = [
                request for request in self._pending if request.kind == "explicit"
            ]
            if stale:
                self._pending[:] = [
                    request for request in self._pending if request.kind != "explicit"
                ]
                self._coalesce_requests(stale)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        logger.info(
            "[expression] lease preempted device_id=%s source=%s reason=%s",
            self.device_id,
            lease.source,
            reason,
        )

    async def acquire_lease(
        self,
        *,
        source: str,
        priority: int,
        duration_ms: object = None,
        reason: str | None = None,
    ) -> str | None:
        """Take the explicit display lane, arbitrated by numeric priority.

        权威语义 · 数字优先级表（与 ``preempt_lease`` 的生命周期矩阵配对；
        正式文档由后续 docs 波次引用此处）：

        ====  ===================  ==========================================
        优先  source               入口 / 用途
        ====  ===================  ==========================================
        10    voice_link_feedback  「语音启动中…」冷启动反馈，绝不抢真实内容
        20    agent                LLM 工具 ``play_expression``
        25    manual_tts           手动 TTS 链路（chat_flow）的表情
        30    web                  Web 预览 / 下发（人工意图，最高）
        ====  ===================  ==========================================

        判定规则：

        - 仅当 ``priority < 当前租约.priority`` 时拒绝（返回 ``None``，
          调用方上报 ``deferred``）。
        - **同级替换**：``priority >= 当前租约.priority`` 即替换旧租约并作废
          其 explicit 队列（后到者胜，无排队等待）。
        - 生命周期状态机（source="rtc" 的 ``transition``）不走租约：持有
          租约期间它只更新 ``_desired_state``；租约结束（release/到期/
          preempt）后恢复该 desired 态。
        - 用户语音边沿通过 ``preempt_lease`` 无条件终结任何租约，不参与
          本表的数字比较。
        """
        try:
            duration = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None:
            duration = max(_EXPRESSION_MS_MIN, min(_EXPRESSION_LEASE_MS_MAX, duration))
        async with self._queue_lock:
            current = self._lease
            if current is not None and int(priority) < current.priority:
                logger.info(
                    "[expression] lease denied device_id=%s source=%s "
                    "priority=%d owner=%s owner_priority=%d reason=%s",
                    self.device_id,
                    source,
                    int(priority),
                    current.source,
                    current.priority,
                    reason or "",
                )
                return None
            replaced_source = current.source if current is not None else None
            self._lease_generation += 1
            self._explicit_generation += 1
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            replaced = [
                request
                for request in self._pending
                if request.kind == "explicit"
            ]
            if replaced:
                self._pending[:] = [
                    request
                    for request in self._pending
                    if request.kind != "explicit"
                ]
                self._coalesce_requests(replaced)
            token = uuid.uuid4().hex
            self._lease = _ExpressionLease(
                token=token,
                source=str(source or "explicit"),
                priority=int(priority),
                generation=self._lease_generation,
                duration_ms=duration,
                expires_at=None,
                acquired_reason=str(reason or "").strip() or None,
            )
            if self._lease_timer_task is not None:
                self._lease_timer_task.cancel()
                self._lease_timer_task = None
            logger.info(
                "[expression] lease acquired device_id=%s source=%s "
                "priority=%d previous_source=%s duration_ms=%s reason=%s token=%s",
                self.device_id,
                source,
                int(priority),
                replaced_source or "",
                "persistent" if duration is None else duration,
                reason or "",
                token[:8],
            )
            return token

    async def _arm_lease_timer(self, token: str) -> None:
        """Start the hold only after the target face reached the device."""

        loop = asyncio.get_running_loop()
        async with self._queue_lock:
            lease = self._lease
            if lease is None or lease.token != token:
                return
            duration = lease.duration_ms
            if duration is None:
                return
            previous = self._lease_timer_task
            lease.expires_at = loop.time() + duration / 1000.0
            timer = asyncio.create_task(
                self._expire_lease(token, duration / 1000.0),
                name=f"deskbot-expression-lease:{self.device_id}",
            )
            self._lease_timer_task = timer
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)

    async def _expire_lease(self, token: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            await self.release_lease(token, reason="lease_expired")
        except asyncio.CancelledError:
            return

    async def release_lease(self, token: str, *, reason: str) -> ExpressionSendResult | None:
        async with self._queue_lock:
            if self._lease is None or self._lease.token != token:
                return None
            lease = self._lease
            self._lease = None
            self._explicit_generation += 1
            if (
                self._active_request is not None
                and self._active_request.kind == "explicit"
            ):
                self._active_request.superseded.set()
            stale_explicit = [
                request
                for request in self._pending
                if request.kind == "explicit"
            ]
            if stale_explicit:
                self._pending[:] = [
                    request
                    for request in self._pending
                    if request.kind != "explicit"
                ]
                self._coalesce_requests(stale_explicit)
            timer = self._lease_timer_task
            self._lease_timer_task = None
            desired = self._desired_state
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        return await self._submit(
            requested=desired,
            state=desired,
            duration_ms=None,
            restore_idle=False,
            force=True,
            reason=f"restore_desired:{reason}",
            source="rtc",
            kind="state",
            previous_source=lease.source,
            preempt_reason=reason,
        )

    async def cancel(self, *, reason: str = "cancel") -> ExpressionSendResult:
        """Coalesce queued work and replace the display with idle."""
        async with self._queue_lock:
            self._desired_state = "idle"
            lease = self._lease
        if lease is not None:
            async with self._queue_lock:
                self._cancel_generation += 1
            released = await self.release_lease(lease.token, reason=reason)
            if released is not None:
                return released
        return await self.restore_idle(reason=reason, force=True)

    async def close(self, *, restore_idle: bool = True) -> None:
        if self._closed:
            return
        if restore_idle:
            try:
                await asyncio.wait_for(
                    self.cancel(reason="disconnect"),
                    timeout=2.0,
                )
            except TimeoutError:
                logger.info(
                    "[expression] timed out restoring idle during close "
                    "device_id=%s",
                    self.device_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "[expression] idle restore failed during close "
                    "device_id=%s",
                    self.device_id,
                    exc_info=True,
                )
        async with self._queue_lock:
            self._closed = True
            self._lease = None
            self._mouth_overlay = None
            lease_timer = self._lease_timer_task
            self._lease_timer_task = None
            face_persist = self._face_persist_task
            self._face_persist_task = None
            if face_persist is not None and not face_persist.done():
                face_persist.cancel()
            pending = list(self._pending)
            self._pending.clear()
            active = self._active_request
            if active is not None:
                active.superseded.set()
            for request in pending:
                request.superseded.set()
                if request.future.done():
                    continue
                request.future.set_result(
                    ExpressionSendResult(
                        requested=request.requested,
                        resolved=None,
                        status="closed",
                        state=request.state,
                        source=request.source,
                        reason=request.reason,
                        previous_source=request.previous_source,
                        preempt_reason=request.preempt_reason,
                        lease_token=request.lease_token,
                        operation_id=request.operation_id,
                    )
                )
            worker = self._worker_task
            completion_tasks = list(self._completion_tasks)
            self._completion_tasks.clear()
        if lease_timer is not None:
            lease_timer.cancel()
        for task in completion_tasks:
            task.cancel()
        if lease_timer is not None or completion_tasks:
            await asyncio.gather(
                *([lease_timer] if lease_timer is not None else []),
                *completion_tasks,
                return_exceptions=True,
            )
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(worker, return_exceptions=True),
                    timeout=0.5,
                )
            except TimeoutError:
                logger.info(
                    "[expression] worker did not stop before close bound "
                    "device_id=%s",
                    self.device_id,
                )

    async def _submit(
        self,
        *,
        pb_append: bool = False,
        requested: str,
        state: str | None,
        duration_ms: object,
        restore_idle: bool,
        force: bool,
        reason: str,
        source: str,
        kind: str,
        wait_for_played: bool = True,
        operation_id: str | None = None,
        scene_override: ExpressionScene | None = None,
        lease_token: str | None = None,
        previous_source: str | None = None,
        preempt_reason: str | None = None,
    ) -> ExpressionSendResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ExpressionSendResult] = loop.create_future()
        request = _ExpressionRequest(
            requested=requested,
            pb_append=pb_append,
            state=state,
            duration_ms=duration_ms,
            restore_idle=restore_idle,
            force=force,
            reason=reason,
            source=source,
            future=future,
            wait_for_played=wait_for_played,
            operation_id=operation_id,
            scene_override=scene_override,
            lease_token=lease_token,
            kind=kind,
            previous_source=previous_source,
            preempt_reason=preempt_reason,
            request_id=f"rtc-expr-{uuid.uuid4().hex[:12]}",
        )
        async with self._queue_lock:
            if self._closed:
                return ExpressionSendResult(
                    requested=requested,
                    resolved=None,
                    status="closed",
                    state=state,
                    source=source,
                    reason=reason,
                    previous_source=previous_source,
                    preempt_reason=preempt_reason,
                    lease_token=lease_token,
                    operation_id=operation_id,
                )
            if kind == "cancel":
                self._cancel_generation += 1
                self._state_generation += 1
                request.cancel_generation = self._cancel_generation
                request.state_generation = self._state_generation
                if self._active_request is not None:
                    self._active_request.superseded.set()
                self._coalesce_requests(self._pending, status="cancelled")
                self._pending.clear()
                self._pending.append(request)
            elif kind == "explicit":
                # Explicit tool calls outrank automatic participant state.
                # Preserve the order of other explicit tools, but invalidate
                # an active/queued state so it cannot delay this request.
                self._state_generation += 1
                request.state_generation = self._state_generation
                request.explicit_generation = self._explicit_generation
                request.cancel_generation = self._cancel_generation
                if (
                    self._active_request is not None
                    and self._active_request.kind == "state"
                ):
                    self._active_request.superseded.set()
                retained: list[_ExpressionRequest] = []
                removed: list[_ExpressionRequest] = []
                for queued in self._pending:
                    (removed if queued.kind == "state" else retained).append(queued)
                self._coalesce_requests(removed)
                self._pending[:] = [*retained, request]
            else:
                self._state_generation += 1
                request.state_generation = self._state_generation
                request.cancel_generation = self._cancel_generation
                if (
                    self._active_request is not None
                    and self._active_request.kind == "state"
                ):
                    self._active_request.superseded.set()
                if self._pending and self._pending[-1].kind == "state":
                    superseded = self._pending.pop()
                    self._coalesce_requests([superseded])
                self._pending.append(request)
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(
                    self._run_queue(),
                    name=f"deskbot-rtc-expression:{self.device_id}",
                )
        return await future

    def _coalesce_requests(
        self,
        requests: Iterable[_ExpressionRequest],
        *,
        status: str = "coalesced",
    ) -> None:
        for request in requests:
            request.superseded.set()
            if request.request_id:
                self._record_operation(
                    request_id=request.request_id,
                    name=(
                        request.scene_override.name
                        if request.scene_override is not None
                        else request.requested
                    ),
                    title=(
                        request.title
                        or (
                            request.scene_override.title
                            if request.scene_override is not None
                            else request.requested
                        )
                    ),
                    source=request.source,
                    reason=request.reason,
                    status=status,
                    fingerprint=request.fingerprint,
                    operation_id=request.operation_id,
                    state=request.state,
                )
            if request.future.done():
                continue
            request.future.set_result(
                ExpressionSendResult(
                    requested=request.requested,
                    resolved=None,
                    status=status,
                    state=request.state,
                    source=request.source,
                    reason=request.reason,
                    previous_source=request.previous_source,
                    preempt_reason=request.preempt_reason,
                    lease_token=request.lease_token,
                    operation_id=request.operation_id,
                    coalesced=status == "coalesced",
                )
            )

    async def _run_queue(self) -> None:
        while True:
            # Let callbacks already scheduled for this event-loop turn settle;
            # participant state commonly arrives as a short burst.
            await asyncio.sleep(0)
            async with self._queue_lock:
                request = self._pending.pop(0) if self._pending else None
                if request is None:
                    self._active_request = None
                    self._worker_task = None
                    return
                self._active_request = request
            try:
                result = await self._execute(request)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                logger.exception(
                    "[expression] transition failed device_id=%s "
                    "requested=%s reason=%s",
                    self.device_id,
                    request.requested,
                    request.reason,
                )
                result = ExpressionSendResult(
                    requested=request.requested,
                    resolved=None,
                    status="error",
                    state=request.state,
                    source=request.source,
                    reason=request.reason,
                    previous_source=request.previous_source,
                    preempt_reason=request.preempt_reason,
                    lease_token=request.lease_token,
                    operation_id=request.operation_id,
                    error=str(exc) or type(exc).__name__,
                )
            if not request.future.done():
                if result.status not in {"played", "accepted"}:
                    self._record_result_operation(request, result=result)
                request.future.set_result(result)

    def displayed_is_bitmap(self) -> bool:
        """屏幕上当前是不是位图（卡通）表情。

        说话链路据此决定 TTS 音频 PB 要不要带口型动画：位图脸上叠矢量口型会变成
        "黑脸一张嘴"（固件新 PB 到来时丢掉上一帧的 JPEG），所以位图脸下音频 PB 不带
        显示层，固件按"有无 anim"判定显示通道，卡通脸和它的说话循环得以保留。
        """
        try:
            catalog = self._catalog_loader()
            # _displayed 只在整条时间线 played 后更新，卡通一条 9s：刚切到卡通、用户马上开口时它还停在
            # 上一张矢量脸。正在播/刚被接收的活动请求解析到位图场景，也算位图脸（宁可少一句口型，
            # 不能把刚上屏的卡通顶成"只剩一张嘴"）。
            active = self._active_request
            if active is not None and not active.superseded.is_set():
                active_name = str((active.fingerprint or {}).get("resolved_scene") or "")
                if active_name:
                    active_scene = catalog.resolve_scene(active_name)
                    if active_scene is not None and active_scene.assets:
                        return True
            return self._displayed_is_bitmap(catalog)
        except Exception:  # noqa: BLE001 - 判断失败按矢量脸处理，走原有口型逻辑
            return False

    def _displayed_is_bitmap(self, catalog: ExpressionCatalog) -> bool:
        displayed = self._displayed
        if displayed is None or not displayed.name:
            return False
        scene = catalog.resolve_scene(displayed.name)
        return bool(scene is not None and scene.assets)

    def _schedule_bitmap_loop(self, request: _ExpressionRequest, scene: ExpressionScene, playback_ms: int) -> None:
        previous = getattr(self, "_bitmap_loop_task", None)
        if previous is not None and not previous.done():
            previous.cancel()
        # 在设备报 played（当前时间线播完）之后立刻以 append 接上一轮：不 replace、不打断，
        # 设备把下一轮排在当前链后面，动画连续；若屏幕已换成别的场景则不再续发。
        delay = 0.2

        async def _loop() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            displayed = self._displayed
            if displayed is None or displayed.name.casefold() != scene.name.casefold():
                logger.info("[expression] bitmap loop stop: displayed=%s scene=%s", displayed and displayed.name, scene.name)
                return
            if request.kind != "state" or self._lease is not None:
                # 只给状态推送续发；网页/Agent 持有显示租约时不插队，避免把它们的请求挤成 coalesced。
                logger.info("[expression] bitmap loop stop: kind=%s lease=%s", request.kind, bool(self._lease))
                return
            if self._is_superseded(request):
                logger.info("[expression] bitmap loop skipped (superseded) scene=%s", scene.name)
                return
            logger.info("[expression] bitmap loop append scene=%s state=%s", scene.name, request.state)
            try:
                await self._submit(
                    requested=request.requested,
                    state=request.state,
                    duration_ms=None,
                    restore_idle=request.restore_idle,
                    force=True,
                    reason="bitmap_loop",
                    source=request.source,
                    kind=request.kind,
                    wait_for_played=False,
                    scene_override=scene if request.kind == "explicit" else None,
                    lease_token=request.lease_token,
                    pb_append=True,
                )
            except Exception:  # noqa: BLE001 - 循环续发失败只记日志，不影响主流程
                logger.warning("[expression] bitmap loop resend failed", exc_info=True)

        self._bitmap_loop_task = asyncio.create_task(_loop())

    def _is_superseded(self, request: _ExpressionRequest) -> bool:
        if request.superseded.is_set():
            return True
        if request.cancel_generation != self._cancel_generation:
            return True
        return bool(
            (
                request.kind == "state"
                and request.state_generation != self._state_generation
            )
            or (
                request.kind == "explicit"
                and request.explicit_generation != self._explicit_generation
            )
        )

    def _superseded_result(
        self,
        request: _ExpressionRequest,
        *,
        resolved: str | None,
        frames: int,
        delivered: int,
        cancelled: bool = False,
    ) -> ExpressionSendResult:
        status = "cancelled" if cancelled else "coalesced"
        if request.request_id:
            self._record_operation(
                request_id=request.request_id,
                name=(
                    request.scene_override.name
                    if request.scene_override is not None
                    else resolved or request.requested
                ),
                title=(
                    request.title
                    or (
                        request.scene_override.title
                        if request.scene_override is not None
                        else resolved or request.requested
                    )
                ),
                source=request.source,
                reason=request.reason,
                status=status,
                fingerprint=request.fingerprint,
                operation_id=request.operation_id,
                state=request.state,
            )
        return ExpressionSendResult(
            requested=request.requested,
            resolved=resolved,
            status=status,
            frames=frames,
            delivered=delivered,
            state=request.state,
            source=request.source,
            reason=request.reason,
            previous_source=request.previous_source,
            preempt_reason=request.preempt_reason,
            lease_token=request.lease_token,
            operation_id=request.operation_id,
            coalesced=not cancelled,
        )

    async def _execute(self, request: _ExpressionRequest) -> ExpressionSendResult:
        result_title: str | None = None
        result_fingerprint: dict[str, Any] = {}

        def _result(
            *,
            resolved: str | None,
            status: str,
            frames: int = 0,
            delivered: int = 0,
            coalesced: bool = False,
            error: str | None = None,
            device_display_crc32: str | None = None,
        ) -> ExpressionSendResult:
            expected_crc = result_fingerprint.get("expected_display_crc32")
            normalized_device_crc = (
                str(device_display_crc32 or "").strip().lower() or None
            )
            return ExpressionSendResult(
                requested=request.requested,
                resolved=resolved,
                status=status,
                frames=frames,
                delivered=delivered,
                state=request.state,
                source=request.source,
                reason=request.reason,
                previous_source=request.previous_source,
                preempt_reason=request.preempt_reason,
                lease_token=request.lease_token,
                title=result_title,
                frame_fingerprint=result_fingerprint.get("frame_fingerprint"),
                final_frame_fingerprint=result_fingerprint.get(
                    "final_frame_fingerprint"
                ),
                final_frame_index=result_fingerprint.get("final_frame_index"),
                frame_count=result_fingerprint.get("frame_count"),
                timeline_ms=result_fingerprint.get("timeline_ms"),
                operation_id=request.operation_id,
                expected_display_crc32=expected_crc,
                device_display_crc32=normalized_device_crc,
                display_crc_match=(
                    normalized_device_crc == str(expected_crc).strip().lower()
                    if normalized_device_crc and expected_crc
                    else None
                ),
                coalesced=coalesced,
                error=error,
            )

        if self._is_superseded(request):
            return self._superseded_result(
                request,
                resolved=None,
                frames=0,
                delivered=0,
                cancelled=request.cancel_generation != self._cancel_generation,
            )
        catalog = self._catalog_loader()
        scene = request.scene_override or (
            catalog.resolve_state(request.state)
            if request.state is not None
            else catalog.resolve_scene(request.requested)
        )
        if scene is None:
            return _result(
                resolved=None,
                status="not_found",
                error=f"unknown expression: {request.requested}",
            )
        result_title = scene.title
        request.title = scene.title
        # 记下这个请求实际解析到的场景：transition() 判断"同状态是否真的重复"时用它比对当前映射。
        request.fingerprint["resolved_scene"] = scene.name
        if (
            request.state is not None
            and request.scene_override is None
            and not scene.assets
            and self._displayed_is_bitmap(catalog)
        ):
            # 卡通套装规则：屏幕上是位图（卡通）表情、而这个状态没有配卡通表情时，
            # 不用矢量表情去替换，保持当前动画不变（用户拍板：不要"沿用矢量"的回退）。
            logger.info(
                "[expression] keep current bitmap face; state=%s has no cartoon scene (would be %s)",
                request.state, scene.name,
            )
            return _result(resolved=scene.name, status="unchanged")
        if request.state is not None:
            scene = _state_entry_frames(scene, request.state)

        request_id = request.request_id or f"rtc-expr-{uuid.uuid4().hex[:12]}"
        request.request_id = request_id
        scene_binaries: list[list[bytes]] = []
        # 待机（idle）位图表情的首条 replace 链标记为待机脸：固件留在 PSRAM，电脑断开后自己循环；
        # 链被设备接收后再让它写进 FFat（重启也保持）。循环续发（append）不重复标记。
        face_keep = bool(
            scene.assets
            and request.kind == "state"
            and request.state == "idle"
            and request.duration_ms is None
            and not request.pb_append
        )
        face_tag = standby_face_tag(scene.frames, scene.assets) if face_keep else ""
        scene_messages = build_expression_pb_frames(
            scene,
            request_id=request_id,
            duration_ms=request.duration_ms,
            replace=not request.pb_append,
            voice_mouth=(request.kind == "state" and request.state == "speaking"),
            out_binaries=scene_binaries,
            face_keep=face_keep,
            face_tag=face_tag,
        )
        messages = join_expression_pb_chains(
            scene_messages,
            request_id=request_id,
            replace=not request.pb_append,
        )
        if not messages:
            return _result(
                resolved=scene.name,
                status="not_found",
                error=f"expression has no frames: {scene.name}",
            )
        result_fingerprint = fingerprint_expression_messages(
            messages,
            asset_crc32_by_message=[
                [(zlib.crc32(blob) & 0xFFFFFFFF) for blob in bins]
                for bins in scene_binaries
            ],
        )
        request.fingerprint = result_fingerprint
        if (
            request.state is not None
            and not request.force
            and self._displayed is not None
            and self._displayed.name.casefold() == scene.name.casefold()
            and self._displayed.frame_fingerprint
            == result_fingerprint["frame_fingerprint"]
        ):
            return _result(
                resolved=scene.name,
                status="unchanged",
            )

        delivered = 0
        final_idx = int(messages[-1].get("idx") or 0)
        from deskbot_server.constants import PB_WAIT_ACK
        from deskbot_server.servo_protocol import pb_sequence_completion_budget_ms
        from deskbot_server.ws.pb_ack_waiter import (
            pb_ack_gate,
            pb_wait_ack_timeout_sec,
            pb_wait_played_timeout_sec,
        )
        playback_ms = pb_sequence_completion_budget_ms(messages)

        try:
            async with _ordered_session_chain(self.device_session):
                ack_started = False
                try:
                    if self.device_id and PB_WAIT_ACK:
                        try:
                            # A fast Agent tool return transfers the played
                            # wait to a background task, so ACK ownership must
                            # not be tied to this queue-worker task.
                            await pb_ack_gate.begin_req(
                                self.device_id,
                                request_id,
                                owner_cleanup=False,
                            )
                            ack_started = True
                        except RuntimeError as exc:
                            return _result(
                                resolved=scene.name,
                                status="request_active",
                                frames=len(messages),
                                delivered=0,
                                error=str(exc),
                            )

                    for message_index, message in enumerate(messages):
                        if self._is_superseded(request):
                            return self._superseded_result(
                                request,
                                resolved=scene.name,
                                frames=len(messages),
                                delivered=delivered,
                                cancelled=(
                                    request.cancel_generation
                                    != self._cancel_generation
                                ),
                            )
                        # join_expression_pb_chains 按链顺序原样拼接：场景链在前，
                        # 第 i 条消息的附件就是 scene_binaries[i]（尾链无附件）。
                        message_binaries = (
                            scene_binaries[message_index]
                            if message_index < len(scene_binaries)
                            else []
                        )
                        if _message_declares_binary(message) and not message_binaries:
                            raise ValueError(
                                "RTC expression PB declares binary payloads without data"
                            )
                        wire = json.dumps(
                            message,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        sent = await self._send_frame_interruptibly(
                            request,
                            wire,
                            binaries=message_binaries,
                        )
                        if not sent or self._is_superseded(request):
                            return self._superseded_result(
                                request,
                                resolved=scene.name,
                                frames=len(messages),
                                delivered=delivered,
                                cancelled=(
                                    request.cancel_generation
                                    != self._cancel_generation
                                ),
                            )
                        delivered += 1

                    if not (self.device_id and PB_WAIT_ACK):
                        self._record_operation(
                            request_id=request_id,
                            name=scene.name,
                            title=scene.title,
                            source=request.source,
                            reason=request.reason,
                            status="accepted",
                            fingerprint=result_fingerprint,
                            operation_id=request.operation_id,
                            state=request.state,
                        )
                        if request.lease_token is not None:
                            await self._arm_lease_timer(request.lease_token)
                        self._sync_standby_face(face_keep, face_tag, scene)
                        return _result(
                            resolved=scene.name,
                            status="accepted",
                            frames=len(messages),
                            delivered=delivered,
                            error="PB terminal ACK waiting is disabled",
                        )

                    accepted_result = await self._wait_ack_interruptibly(
                        request,
                        pb_ack_gate.wait_accepted_result(
                            self.device_id,
                            request_id,
                            final_idx,
                            timeout=pb_wait_ack_timeout_sec(),
                        ),
                    )
                    if accepted_result is None:
                        return self._superseded_result(
                            request,
                            resolved=scene.name,
                            frames=len(messages),
                            delivered=delivered,
                            cancelled=(
                                request.cancel_generation != self._cancel_generation
                            ),
                        )
                    if not accepted_result.ok:
                        status = accepted_result.status
                        if status == "not_started" and ack_started:
                            status = "disconnected"
                        return _result(
                            resolved=scene.name,
                            status=status,
                            frames=len(messages),
                            delivered=delivered,
                            error=(
                                "device did not accept the complete expression PB chain"
                            ),
                        )

                    self._accepted_generation += 1
                    accepted_generation = self._accepted_generation
                    self._record_operation(
                        request_id=request_id,
                        name=scene.name,
                        title=scene.title,
                        source=request.source,
                        reason=request.reason,
                        status="accepted",
                        fingerprint=result_fingerprint,
                        operation_id=request.operation_id,
                        state=request.state,
                    )

                    # The lease starts when the complete chain is accepted,
                    # not after its terminal played ACK. This makes the lease
                    # cover the actual playback interval instead of adding a
                    # second full playback interval afterwards.
                    if request.lease_token is not None:
                        await self._arm_lease_timer(request.lease_token)
                    # 设备接收整条链时已把待机脸留在 PSRAM，此时就可以安排落盘。
                    self._sync_standby_face(face_keep, face_tag, scene)

                    if not request.wait_for_played:
                        completion = asyncio.create_task(
                            self._finish_played_ack(
                                request_id=request_id,
                                final_idx=final_idx,
                                playback_ms=playback_ms,
                                scene_name=scene.name,
                                scene_title=scene.title,
                                source=request.source,
                                reason=request.reason,
                                fingerprint=result_fingerprint,
                                operation_id=request.operation_id,
                                state=request.state,
                                accepted_generation=accepted_generation,
                                ack_started=ack_started,
                                lease_token=request.lease_token,
                            ),
                            name=f"deskbot-expression-played:{request_id}",
                        )
                        self._completion_tasks.add(completion)
                        completion.add_done_callback(self._completion_tasks.discard)
                        ack_started = False
                        return _result(
                            resolved=scene.name,
                            status="accepted",
                            frames=len(messages),
                            delivered=delivered,
                        )

                    played_result = await self._wait_ack_interruptibly(
                        request,
                        pb_ack_gate.wait_played_result(
                            self.device_id,
                            request_id,
                            final_idx,
                            timeout=pb_wait_played_timeout_sec(playback_ms),
                        ),
                    )
                    if played_result is None:
                        return self._superseded_result(
                            request,
                            resolved=scene.name,
                            frames=len(messages),
                            delivered=delivered,
                            cancelled=(
                                request.cancel_generation != self._cancel_generation
                            ),
                        )
                    if not played_result.ok:
                        status = played_result.status
                        if status == "not_started" and ack_started:
                            status = "disconnected"
                        self._record_operation(
                            request_id=request_id,
                            name=scene.name,
                            title=scene.title,
                            source=request.source,
                            reason=request.reason,
                            status=status,
                            fingerprint=result_fingerprint,
                            operation_id=request.operation_id,
                            state=request.state,
                        )
                        return _result(
                            resolved=scene.name,
                            status=status,
                            frames=len(messages),
                            delivered=delivered,
                            error="device did not complete the expression PB chain",
                        )
                    device_display_crc32 = played_result.display_crc32
                finally:
                    if ack_started:
                        await pb_ack_gate.end_req(self.device_id, request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "[expression] USB send failed device_id=%s expression=%s "
                "error=%s",
                self.device_id,
                scene.name,
                type(exc).__name__,
            )
            return _result(
                resolved=scene.name,
                status="device_unavailable",
                frames=len(messages),
                delivered=delivered,
                error=str(exc) or type(exc).__name__,
            )
        self._record_displayed(
            name=scene.name,
            title=scene.title,
            source=request.source,
            reason=request.reason,
            status="played",
            fingerprint=result_fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=device_display_crc32,
        )
        self._record_operation(
            request_id=request_id,
            name=scene.name,
            title=scene.title,
            source=request.source,
            reason=request.reason,
            status="played",
            fingerprint=result_fingerprint,
            operation_id=request.operation_id,
            state=request.state,
            device_display_crc32=device_display_crc32,
        )
        logger.info(
            "[expression] played device_id=%s source=%s requested=%s "
            "resolved=%s state=%s frames=%d reason=%s previous_source=%s "
            "preempt_reason=%s lease=%s fingerprint=%s final=%s",
            self.device_id,
            request.source,
            request.requested,
            scene.name,
            request.state or "",
            delivered,
            request.reason,
            request.previous_source or "",
            request.preempt_reason or "",
            (request.lease_token or "")[:8],
            result_fingerprint["frame_fingerprint"][:12],
            result_fingerprint["final_frame_fingerprint"][:12],
        )
        if BITMAP_IDLE_LOOP_RESEND and scene.assets and request.duration_ms is None and request.kind == "state":
            # 位图表情：固件不循环，播完后按时间线续发同一场景（仍是当前状态/租约才发）。
            # 默认关闭：持续解码 JPEG 会占用设备 CPU（影响 WiFi 收发），先只播满一条 ~9s 的时间线。
            self._schedule_bitmap_loop(request, scene, playback_ms)
        return _result(
            resolved=scene.name,
            status="played",
            frames=len(messages),
            delivered=delivered,
            device_display_crc32=device_display_crc32,
        )

    # ---- 待机卡通脸持久化（固件 ≥0.0.57） ----

    @property
    def persisted_face_tag(self) -> str:
        return self._persisted_face_tag

    def note_face_persisted(self, tag: str) -> None:
        """表情页通过 /api/face_persist 直接存过了：记下标签，运行时不再重复发 face_persist。"""
        self._persisted_face_tag = str(tag or "")

    def _sync_standby_face(self, face_keep: bool, face_tag: str, scene: ExpressionScene) -> None:
        """待机链被设备接收后：位图待机脸安排写盘；矢量待机脸则让设备清掉上一张卡通脸
        （否则断开电脑后设备还会循环旧的卡通脸）。只对 idle 状态推送生效。"""
        if face_keep:
            self._face_clear_checked = False
            self._schedule_face_persist(face_tag)
        elif not scene.assets and not self._face_clear_checked:
            self._face_clear_checked = True
            self._schedule_face_persist("")

    def _schedule_face_persist(self, tag: str, *, delay: float = 1.5) -> None:
        if self._closed or (tag and tag == self._persisted_face_tag):
            return
        previous = self._face_persist_task
        if previous is not None and not previous.done():
            previous.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(delay)
                await self._persist_standby_face(tag)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 落盘失败只记日志，不影响表情主流程
                logger.warning("[expression] standby face persist failed device_id=%s", self.device_id, exc_info=True)

        self._face_persist_task = asyncio.create_task(_run(), name=f"deskbot-face-persist:{self.device_id}")

    async def _persist_standby_face(self, tag: str) -> None:
        """问设备当前待机脸标签；是我们刚推的那张且还没落盘就发 face_persist；``tag`` 为空表示
        待机是矢量脸，设备若还留着卡通脸就 face_clear。旧固件没有回执：静默跳过。"""
        session = self.device_session
        status_request = getattr(session, "face_status_request", None)
        if status_request is None:
            return
        status = await status_request()
        if status is None:
            logger.info("[expression] standby face persist skipped: firmware without face store device_id=%s", self.device_id)
            return
        device_tag = str(status.get("tag") or "")
        if not tag:
            if device_tag:
                clear_request = getattr(session, "face_clear_request", None)
                if clear_request is not None:
                    await clear_request()
                    logger.info("[expression] standby face cleared device_id=%s (idle is vector)", self.device_id)
            self._persisted_face_tag = ""
            return
        if device_tag != tag:
            logger.info(
                "[expression] standby face persist skipped: device tag=%s expected=%s device_id=%s",
                device_tag, tag, self.device_id,
            )
            return
        if status.get("persisted") is True:
            self._persisted_face_tag = tag
            return
        persist_request = getattr(session, "face_persist_request", None)
        if persist_request is None:
            return
        ack = await persist_request()
        if ack is not None and ack.get("ok") is True:
            self._persisted_face_tag = tag
            logger.info(
                "[expression] standby face persisted device_id=%s tag=%s bytes=%s",
                self.device_id, tag, ack.get("bytes"),
            )
        else:
            logger.warning("[expression] standby face persist failed device_id=%s tag=%s ack=%s", self.device_id, tag, ack)

    async def _finish_played_ack(
        self,
        *,
        request_id: str,
        final_idx: int,
        playback_ms: int,
        scene_name: str,
        scene_title: str,
        source: str,
        reason: str,
        fingerprint: dict[str, Any],
        operation_id: str | None,
        state: str | None,
        accepted_generation: int,
        ack_started: bool,
        lease_token: str | None,
    ) -> None:
        from deskbot_server.ws.pb_ack_waiter import (
            pb_ack_gate,
            pb_wait_played_timeout_sec,
        )

        try:
            result = await pb_ack_gate.wait_played_result(
                self.device_id,
                request_id,
                final_idx,
                timeout=pb_wait_played_timeout_sec(playback_ms),
            )
            if not result.ok:
                self._record_operation(
                    request_id=request_id,
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status=result.status,
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                )
                logger.warning(
                    "[expression] background playback terminal=%s "
                    "device_id=%s req=%s expression=%s",
                    result.status,
                    self.device_id,
                    request_id,
                    scene_name,
                )
                if lease_token is not None:
                    await self.release_lease(
                        lease_token,
                        reason=f"background_{result.status}",
                    )
            elif self._accepted_generation == accepted_generation:
                self._record_displayed(
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status="played",
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                    device_display_crc32=result.display_crc32,
                )
            if result.ok:
                self._record_operation(
                    request_id=request_id,
                    name=scene_name,
                    title=scene_title,
                    source=source,
                    reason=reason,
                    status="played",
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    state=state,
                    device_display_crc32=result.display_crc32,
                )
        finally:
            if ack_started:
                await pb_ack_gate.end_req(self.device_id, request_id)

    async def _wait_ack_interruptibly(
        self,
        request: _ExpressionRequest,
        wait_result: Any,
    ) -> Any | None:
        """Wait for one ACK phase unless a newer expression supersedes it."""

        ack_task = (
            wait_result
            if isinstance(wait_result, asyncio.Task)
            else asyncio.create_task(wait_result)
        )
        superseded_task = asyncio.create_task(request.superseded.wait())
        try:
            done, _pending = await asyncio.wait(
                {ack_task, superseded_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if superseded_task in done and self._is_superseded(request):
                if not ack_task.done():
                    ack_task.cancel()
                await asyncio.gather(ack_task, return_exceptions=True)
                return None
            return await ack_task
        finally:
            if not ack_task.done():
                ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)
            if not superseded_task.done():
                superseded_task.cancel()
            await asyncio.gather(superseded_task, return_exceptions=True)

    async def _send_frame_interruptibly(
        self,
        request: _ExpressionRequest,
        wire: str,
        binaries: list[bytes] | None = None,
    ) -> bool:
        """Stop awaiting a stale state/tool frame as soon as it is superseded."""

        send_task = asyncio.create_task(
            _send_json_pb(self.device_session, wire, binaries=binaries)
        )
        superseded_task = asyncio.create_task(request.superseded.wait())
        try:
            done, _pending = await asyncio.wait(
                {send_task, superseded_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if superseded_task in done and self._is_superseded(request):
                if not send_task.done():
                    send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
                return False
            await send_task
            return not self._is_superseded(request)
        finally:
            if not send_task.done():
                send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            if not superseded_task.done():
                superseded_task.cancel()
            await asyncio.gather(superseded_task, return_exceptions=True)


def _message_declares_binary(message: dict[str, Any]) -> bool:
    try:
        from deskbot_server.pb.servo_pcm import pb_expected_binary_lengths

        return bool(pb_expected_binary_lengths(message))
    except (ImportError, TypeError, ValueError):
        audio = message.get("audio")
        return bool(
            isinstance(audio, dict)
            and int(audio.get("next_bin_len") or 0) > 0
        )


def _sender_accepts_binaries(sender: object) -> bool:
    try:
        params = inspect.signature(sender).parameters
    except (TypeError, ValueError):
        return False
    return "binaries" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


async def _send_json_pb(
    session: object, wire: str, binaries: list[bytes] | None = None
) -> None:
    sender = getattr(session, "send_pb_wire", None)
    if not callable(sender):
        sender = getattr(session, "send", None)
    if not callable(sender):
        raise RuntimeError("USB session has no PB sender")
    try:
        _m = json.loads(wire)
    except (TypeError, ValueError):
        _m = {}
    logger.info(
        "[expr TX] type=%s req=%s idx=%s anim_n=%d has_image=%s declares_assets=%s binaries=%d bytes=%d",
        _m.get("type"), _m.get("req"), _m.get("idx"), len(_m.get("anim") or []),
        '"image"' in wire, '"assets"' in wire,
        len(binaries or []), sum(len(b) for b in (binaries or [])),
    )
    blobs = list(binaries or [])
    # 位图（卡通）表情：JSON 已声明 assets[].next_bin_len，JPEG 二进制必须紧跟
    # 声明——串口会话的 send_pb_wire(binaries=) 在同一把 bundle 锁里发完整个
    # 包，否则并发的另一条表情/TTS PB 会插在声明和二进制之间，固件报
    # "BIN fragment overflow" 并判本序列失败（2026-09-07 卡通表情掉回待机的根因）。
    if blobs and _sender_accepts_binaries(sender):
        result = sender(wire, binaries=blobs)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise RuntimeError("USB session rejected PB write")
        return
    result = sender(wire)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise RuntimeError("USB session rejected PB write")
    for blob in blobs:
        bin_sender = getattr(session, "send_pb_binary", None)
        if not callable(bin_sender):
            raise RuntimeError("USB session cannot send PB binaries")
        bin_result = bin_sender(blob)
        if inspect.isawaitable(bin_result):
            bin_result = await bin_result
        if bin_result is False:
            raise RuntimeError("USB session rejected PB binary write")


_rtc_expression_runtimes: dict[str, RtcExpressionRuntime] = {}


def register_rtc_expression_runtime(runtime: RtcExpressionRuntime) -> None:
    if runtime.device_id:
        _rtc_expression_runtimes[runtime.device_id] = runtime


def unregister_rtc_expression_runtime(runtime: RtcExpressionRuntime) -> None:
    if _rtc_expression_runtimes.get(runtime.device_id) is runtime:
        _rtc_expression_runtimes.pop(runtime.device_id, None)


def get_rtc_expression_runtime(device_id: object) -> RtcExpressionRuntime | None:
    return _rtc_expression_runtimes.get(str(device_id or "").strip())


# USB-owned terminology for new callers.  Compatibility aliases above remain
# while older worker/core integration code is migrated incrementally.
register_expression_runtime = register_rtc_expression_runtime
unregister_expression_runtime = unregister_rtc_expression_runtime
get_expression_runtime = get_rtc_expression_runtime


async def play_rtc_expression(
    device_id: object,
    name: object,
    *,
    duration_ms: object = None,
) -> dict[str, Any]:
    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        requested = str(name or "").strip()
        return ExpressionSendResult(
            requested=requested,
            resolved=None,
            status="device_unavailable",
            source="agent",
            reason="tool_play_expression",
            error="RTC expression runtime is not attached to the USB device",
        ).as_tool_result()
    result = await runtime.play_expression(
        name,
        duration_ms=duration_ms,
        reason="tool_play_expression",
        source="agent",
        priority=20,
        lease_ms=duration_ms,
        wait_for_played=False,
    )
    return result.as_tool_result()


async def play_web_expression_frames(
    device_id: object,
    frames: Iterable[dict[str, Any]],
    *,
    assets: Iterable[bytes] = (),
    name: object = None,
    title: object = None,
    hold_ms: object = None,
    reason: str = "web_preview",
    operation_id: object = None,
) -> ExpressionSendResult:
    """Route Web preview frames through the USB expression arbiter."""

    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        return ExpressionSendResult(
            requested="adhoc_web",
            resolved=None,
            status="device_unavailable",
            source="web",
            reason=reason,
            operation_id=str(operation_id or "").strip() or None,
            error="expression runtime is not attached to the USB device",
        )
    return await runtime.play_frames(
        frames,
        assets=assets,
        name=name,
        title=title,
        source="web",
        priority=30,
        reason=reason,
        hold_ms=hold_ms,
        persist_until_preempted=hold_ms is None,
        wait_for_played=True,
        operation_id=operation_id,
    )


async def play_web_expression_scene(
    device_id: object,
    name: object,
    *,
    hold_ms: object = None,
    reason: str = "web_scene",
    operation_id: object = None,
) -> ExpressionSendResult:
    runtime = get_rtc_expression_runtime(device_id)
    if runtime is None:
        return ExpressionSendResult(
            requested=str(name or "").strip(),
            resolved=None,
            status="device_unavailable",
            source="web",
            reason=reason,
            operation_id=str(operation_id or "").strip() or None,
            error="expression runtime is not attached to the USB device",
        )
    return await runtime.play_scene(
        name,
        source="web",
        priority=30,
        reason=reason,
        hold_ms=hold_ms,
        persist_until_preempted=hold_ms is None,
        wait_for_played=True,
        operation_id=operation_id,
    )


__all__ = [
    "CANONICAL_EXPRESSION_STATES",
    "EXPRESSION_STATE_ALIASES",
    "ExpressionCatalog",
    "ExpressionScene",
    "ExpressionSendResult",
    "RtcExpressionRuntime",
    "build_expression_catalog",
    "build_expression_pb_frames",
    "join_expression_pb_chains",
    "expression_tool_catalog",
    "fingerprint_expression_messages",
    "fingerprint_pb_display_pairs",
    "get_expression_runtime",
    "get_rtc_expression_runtime",
    "load_expression_catalog",
    "normalize_expression_state",
    "play_web_expression_frames",
    "play_web_expression_scene",
    "play_rtc_expression",
    "register_expression_runtime",
    "register_rtc_expression_runtime",
    "unregister_expression_runtime",
    "unregister_rtc_expression_runtime",
]
