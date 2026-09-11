"""Flow control for downlink PB sequences using device ``pb_ack`` events."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("deskbot-server")


def pb_wait_ack_timeout_sec() -> float:
    return max(0.5, float(os.environ.get("PB_WAIT_ACK_TIMEOUT_SEC", "8.0")))


def pb_wait_played_timeout_sec(audio_ms: int) -> float:
    """Playback deadline: media duration plus configurable transport margin."""
    margin = max(
        1.0, float(os.environ.get("PB_WAIT_PLAYED_MARGIN_SEC", "5.0"))
    )
    return max(pb_wait_ack_timeout_sec(), max(0, int(audio_ms)) / 1000.0 + margin)


@dataclass
class _ReqAckState:
    accepted_idx: int = -1
    played_idx: int = -1
    terminal_phase: str | None = None
    display_crc32: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cond: asyncio.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.cond = asyncio.Condition(self.lock)


@dataclass(frozen=True)
class PbAckWaitResult:
    """Detailed ACK result for callers that need the terminal reason."""

    ok: bool
    status: str
    accepted_idx: int = -1
    played_idx: int = -1
    display_crc32: str | None = None


class PbAckGate:
    """Wait for PB ACK progress keyed by ``(device_id, req)``."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _ReqAckState] = {}
        self._meta_lock = asyncio.Lock()

    async def _state(self, device_id: str, req: str) -> _ReqAckState | None:
        key = (device_id, req)
        async with self._meta_lock:
            return self._states.get(key)

    async def begin_req(
        self,
        device_id: str,
        req: str,
        *,
        owner_cleanup: bool = True,
    ) -> None:
        """Register a new request without replacing an active waiter."""
        if not device_id or not req:
            return
        key = (device_id, req)
        state = _ReqAckState()
        async with self._meta_lock:
            if key in self._states:
                raise RuntimeError(
                    "PB ACK request is already active: "
                    f"device_id={device_id!r} req={req!r}"
                )
            self._states[key] = state
        owner = asyncio.current_task()
        if owner_cleanup and owner is not None:

            def _cleanup_abandoned(_done: asyncio.Task) -> None:
                try:
                    asyncio.get_running_loop().create_task(
                        self._end_state_if_current(key, state)
                    )
                except RuntimeError:
                    pass

            owner.add_done_callback(_cleanup_abandoned)

    async def _end_state_if_current(
        self,
        key: tuple[str, str],
        state: _ReqAckState,
    ) -> None:
        async with self._meta_lock:
            if self._states.get(key) is not state:
                return
            self._states.pop(key, None)
        await self._terminate_state(state, "ended")

    @staticmethod
    async def _terminate_state(state: _ReqAckState, phase: str) -> None:
        async with state.cond:
            if state.terminal_phase is None:
                state.terminal_phase = phase
            state.cond.notify_all()

    async def end_req(self, device_id: str, req: str) -> None:
        """Release per-request state and wake any outstanding waiter."""
        if not device_id or not req:
            return
        async with self._meta_lock:
            state = self._states.pop((device_id, req), None)
        if state is not None:
            await self._terminate_state(state, "ended")

    async def cancel_device(self, device_id: str) -> None:
        """Wake and release all ACK state owned by a disconnected device."""
        if not device_id:
            return
        async with self._meta_lock:
            keys = [key for key in self._states if key[0] == device_id]
            states = [self._states.pop(key) for key in keys]
        for state in states:
            await self._terminate_state(state, "disconnected")

    async def state_count(self) -> int:
        async with self._meta_lock:
            return len(self._states)

    async def notify(self, device_id: str, ack: dict[str, Any]) -> None:
        if not device_id:
            return
        req = ack.get("req")
        if not isinstance(req, str) or not req:
            return
        try:
            idx = int(ack.get("idx", -1))
        except (TypeError, ValueError):
            idx = -1
        phase = str(ack.get("phase") or "").strip().lower()
        if phase not in ("accepted", "played", "failed", "cancelled"):
            phase = "accepted"
        state = await self._state(device_id, req)
        if state is None:
            logger.debug(
                "[pb_ack] ignored late or unknown ACK device_id=%s req=%s",
                device_id,
                req,
            )
            return
        async with state.cond:
            if state.terminal_phase is not None:
                logger.debug(
                    "[pb_ack] ignored ACK after terminal phase "
                    "device_id=%s req=%s phase=%s terminal=%s",
                    device_id,
                    req,
                    phase,
                    state.terminal_phase,
                )
                return
            if phase in ("accepted", "played") and idx > state.accepted_idx:
                state.accepted_idx = idx
            if phase == "played":
                if idx > state.played_idx:
                    state.played_idx = idx
                state.terminal_phase = "played"
                crc = str(ack.get("display_crc32") or "").strip().lower()
                state.display_crc32 = crc or None
            elif phase in ("failed", "cancelled"):
                state.terminal_phase = phase
            state.cond.notify_all()

    async def wait_idx(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        timeout: Optional[float] = None,
    ) -> bool:
        return await self.wait_accepted(
            device_id,
            req,
            idx,
            timeout=timeout,
        )

    async def wait_accepted(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        timeout: Optional[float] = None,
    ) -> bool:
        result = await self.wait_accepted_result(
            device_id,
            req,
            idx,
            timeout=timeout,
        )
        return result.ok

    async def wait_played(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        timeout: Optional[float] = None,
    ) -> bool:
        result = await self.wait_played_result(
            device_id,
            req,
            idx,
            timeout=timeout,
        )
        return result.ok

    async def wait_accepted_result(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        timeout: Optional[float] = None,
    ) -> PbAckWaitResult:
        return await self._wait_watermark_result(
            device_id,
            req,
            idx,
            phase="accepted",
            timeout=timeout,
        )

    async def wait_played_result(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        timeout: Optional[float] = None,
    ) -> PbAckWaitResult:
        return await self._wait_watermark_result(
            device_id,
            req,
            idx,
            phase="played",
            timeout=timeout,
        )

    async def _wait_watermark(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        phase: str,
        timeout: Optional[float],
    ) -> bool:
        result = await self._wait_watermark_result(
            device_id,
            req,
            idx,
            phase=phase,
            timeout=timeout,
        )
        return result.ok

    async def _wait_watermark_result(
        self,
        device_id: str,
        req: str,
        idx: int,
        *,
        phase: str,
        timeout: Optional[float],
    ) -> PbAckWaitResult:
        if not device_id or not req:
            return PbAckWaitResult(True, phase)
        if timeout is None:
            timeout = pb_wait_ack_timeout_sec()
        state = await self._state(device_id, req)
        if state is None:
            logger.warning(
                "[pb_ack] wait requested without active request device_id=%s req=%s",
                device_id,
                req,
            )
            return PbAckWaitResult(False, "not_started")
        deadline = time.monotonic() + timeout
        async with state.cond:
            while True:
                watermark = (
                    state.played_idx
                    if phase == "played"
                    else state.accepted_idx
                )
                if state.terminal_phase in {
                    "failed",
                    "cancelled",
                    "disconnected",
                    "ended",
                }:
                    result = PbAckWaitResult(
                        False,
                        state.terminal_phase,
                        accepted_idx=state.accepted_idx,
                        played_idx=state.played_idx,
                        display_crc32=state.display_crc32,
                    )
                    break
                if watermark >= idx:
                    result = PbAckWaitResult(
                        True,
                        phase,
                        accepted_idx=state.accepted_idx,
                        played_idx=state.played_idx,
                        display_crc32=state.display_crc32,
                    )
                    break
                if state.terminal_phase == "played":
                    result = PbAckWaitResult(
                        False,
                        "incomplete",
                        accepted_idx=state.accepted_idx,
                        played_idx=state.played_idx,
                        display_crc32=state.display_crc32,
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return PbAckWaitResult(
                        False,
                        "timeout",
                        accepted_idx=state.accepted_idx,
                        played_idx=state.played_idx,
                        display_crc32=state.display_crc32,
                    )
                try:
                    await asyncio.wait_for(state.cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return PbAckWaitResult(
                        False,
                        "timeout",
                        accepted_idx=state.accepted_idx,
                        played_idx=state.played_idx,
                        display_crc32=state.display_crc32,
                    )
        if not result.ok:
            logger.warning(
                "[pb_ack] terminal wait failure phase=%s status=%s "
                "device_id=%s req=%s need_idx>=%s accepted_idx=%s played_idx=%s",
                phase,
                result.status,
                device_id,
                req,
                idx,
                result.accepted_idx,
                result.played_idx,
            )
            return result
        logger.info(
            "[pb_ack] confirmed phase=%s device_id=%s req=%s need_idx=%s "
            "accepted_idx=%s played_idx=%s",
            phase,
            device_id,
            req,
            idx,
            result.accepted_idx,
            result.played_idx,
        )
        return result


pb_ack_gate = PbAckGate()
