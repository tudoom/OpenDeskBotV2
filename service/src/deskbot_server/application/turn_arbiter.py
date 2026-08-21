from __future__ import annotations

import asyncio
import itertools
import weakref
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")

PRIORITY_AUTOMATION = 10
PRIORITY_REMINDER = 20
PRIORITY_MANUAL = 60
PRIORITY_INTERACTIVE = 100


class TurnInterrupted(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"device turn interrupted: {reason}")


@dataclass
class _Entry(Generic[T]):
    device_id: str
    source: str
    priority: int
    sequence: int
    factory: Callable[[], Awaitable[T]]
    preemptible: bool
    replace_group: str | None
    ready: asyncio.Future[None]
    task: asyncio.Task[T] | None = None
    cancel_reason: str | None = None


@dataclass
class _DeviceState:
    active: _Entry | None = None
    pending: list[_Entry] = field(default_factory=list)


class DeviceTurnArbiter:
    """One authoritative interaction lane per device and event loop.

    Interactive speech has priority over deferred reminders.  A newer
    interactive utterance replaces only an older *pending* utterance; it never
    cancels another user turn that is already executing.  Reminders remain
    queued unless an active reminder is explicitly preempted by user input.
    """

    def __init__(self) -> None:
        self._states_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, _DeviceState]
        ] = weakref.WeakKeyDictionary()
        self._sequence = itertools.count()

    def _states(self) -> dict[str, _DeviceState]:
        loop = asyncio.get_running_loop()
        return self._states_by_loop.setdefault(loop, {})

    def submit(
        self,
        device_id: str,
        factory: Callable[[], Awaitable[T]],
        *,
        source: str,
        priority: int,
        preempt_lower: bool = False,
        preemptible: bool = False,
        replace_group: str | None = None,
    ) -> tuple[_Entry[T], asyncio.Task[T]]:
        device = str(device_id or "").strip()
        if not device:
            device = f"__anonymous__:{next(self._sequence)}"
        loop = asyncio.get_running_loop()
        states = self._states()
        state = states.setdefault(device, _DeviceState())
        entry: _Entry[T] = _Entry(
            device_id=device,
            source=str(source or "unknown"),
            priority=int(priority),
            sequence=next(self._sequence),
            factory=factory,
            preemptible=bool(preemptible),
            replace_group=str(replace_group) if replace_group else None,
            ready=loop.create_future(),
        )

        async def _runner() -> T:
            try:
                await entry.ready
                return await entry.factory()
            finally:
                self._finish(entry)

        task = asyncio.create_task(
            _runner(),
            name=f"device-turn:{device}:{entry.source}:{entry.sequence}",
        )
        entry.task = task

        if state.active is None:
            state.active = entry
            entry.ready.set_result(None)
            return entry, task

        if entry.replace_group:
            kept: list[_Entry] = []
            for pending in state.pending:
                if pending.replace_group == entry.replace_group:
                    pending.cancel_reason = "superseded"
                    if pending.task is not None:
                        pending.task.cancel()
                else:
                    kept.append(pending)
            state.pending = kept

        state.pending.append(entry)
        active = state.active
        if (
            preempt_lower
            and active is not None
            and active.preemptible
            and entry.priority > active.priority
        ):
            active.cancel_reason = f"preempted_by_{entry.source}"
            if active.task is not None:
                active.task.cancel()
        return entry, task

    def submit_if_idle(
        self,
        device_id: str,
        factory: Callable[[], Awaitable[T]],
        *,
        source: str,
        priority: int,
        preemptible: bool = False,
        replace_group: str | None = None,
    ) -> tuple[_Entry[T], asyncio.Task[T]] | None:
        """Start a best-effort action only when the device lane is idle.

        This is intended for high-frequency automation such as camera
        following.  A stale automation sample must be dropped instead of
        waiting behind speech, reminders, or manual controls.
        """

        device = str(device_id or "").strip()
        if not device:
            return None
        state = self._states().get(device)
        if state is not None and (state.active is not None or state.pending):
            return None
        return self.submit(
            device,
            factory,
            source=source,
            priority=priority,
            preemptible=preemptible,
            replace_group=replace_group,
        )

    def _finish(self, entry: _Entry) -> None:
        states = self._states()
        state = states.get(entry.device_id)
        if state is None:
            return
        if state.active is not entry:
            try:
                state.pending.remove(entry)
            except ValueError:
                pass
            if state.active is None and not state.pending:
                states.pop(entry.device_id, None)
            return

        state.active = None
        live = [
            pending
            for pending in state.pending
            if pending.task is not None and not pending.task.done()
        ]
        state.pending = live
        if not live:
            states.pop(entry.device_id, None)
            return
        # Higher priority first; FIFO within the same priority.  Interactive
        # replacement ensures there is at most one pending utterance per
        # connection/device replace group.
        next_entry = min(live, key=lambda row: (-row.priority, row.sequence))
        state.pending.remove(next_entry)
        state.active = next_entry
        if not next_entry.ready.done():
            next_entry.ready.set_result(None)

    async def run(
        self,
        device_id: str,
        factory: Callable[[], Awaitable[T]],
        *,
        source: str,
        priority: int,
        preempt_lower: bool = False,
        preemptible: bool = False,
        replace_group: str | None = None,
    ) -> T:
        entry, task = self.submit(
            device_id,
            factory,
            source=source,
            priority=priority,
            preempt_lower=preempt_lower,
            preemptible=preemptible,
            replace_group=replace_group,
        )
        try:
            return await task
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if entry.cancel_reason:
                raise TurnInterrupted(entry.cancel_reason) from exc
            raise

    async def cancel_device(self, device_id: str, *, reason: str = "cancelled") -> int:
        device = str(device_id or "").strip()
        if not device:
            return 0
        state = self._states().get(device)
        if state is None:
            return 0
        entries = ([state.active] if state.active is not None else []) + list(
            state.pending
        )
        tasks: list[asyncio.Task] = []
        for entry in entries:
            entry.cancel_reason = reason
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
                tasks.append(entry.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def snapshot(self, device_id: str) -> dict[str, object]:
        state = self._states().get(str(device_id or "").strip())
        if state is None:
            return {"active": None, "pending": []}
        return {
            "active": state.active.source if state.active is not None else None,
            "pending": [entry.source for entry in state.pending],
        }


device_turn_arbiter = DeviceTurnArbiter()
