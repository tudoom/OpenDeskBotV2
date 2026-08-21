from __future__ import annotations

import asyncio


def test_interactive_turn_preempts_active_reminder_without_overlap():
    from deskbot_server.application.turn_arbiter import (
        PRIORITY_INTERACTIVE,
        PRIORITY_REMINDER,
        DeviceTurnArbiter,
        TurnInterrupted,
    )

    async def _run() -> None:
        arbiter = DeviceTurnArbiter()
        reminder_started = asyncio.Event()
        user_finished = asyncio.Event()
        events: list[str] = []
        active = 0
        max_active = 0

        async def reminder_job() -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            events.append("reminder-start")
            reminder_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("reminder-stop")
                active -= 1

        async def run_reminder() -> str:
            try:
                await arbiter.run(
                    "deskbot-priority",
                    reminder_job,
                    source="scheduled_task",
                    priority=PRIORITY_REMINDER,
                    preemptible=True,
                )
            except TurnInterrupted as exc:
                return exc.reason
            raise AssertionError("reminder should be preempted")

        reminder = asyncio.create_task(run_reminder())
        await asyncio.wait_for(reminder_started.wait(), timeout=1)

        async def user_job() -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            events.append("user")
            active -= 1
            user_finished.set()

        await arbiter.run(
            "deskbot-priority",
            user_job,
            source="text",
            priority=PRIORITY_INTERACTIVE,
            preempt_lower=True,
            replace_group="interactive",
        )
        reason = await asyncio.wait_for(reminder, timeout=1)

        assert reason == "preempted_by_text"
        assert user_finished.is_set()
        assert events == ["reminder-start", "reminder-stop", "user"]
        assert max_active == 1

    asyncio.run(_run())


def test_waiting_interactive_turn_runs_before_deferred_reminder():
    from deskbot_server.application.turn_arbiter import (
        PRIORITY_INTERACTIVE,
        PRIORITY_REMINDER,
        DeviceTurnArbiter,
    )

    async def _run() -> None:
        arbiter = DeviceTurnArbiter()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        events: list[str] = []

        async def first_user() -> None:
            events.append("user-1")
            first_started.set()
            await release_first.wait()

        first = asyncio.create_task(
            arbiter.run(
                "deskbot-order",
                first_user,
                source="asr",
                priority=PRIORITY_INTERACTIVE,
                replace_group="interactive-active",
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)

        async def reminder() -> None:
            events.append("reminder")

        deferred = asyncio.create_task(
            arbiter.run(
                "deskbot-order",
                reminder,
                source="scheduled_task",
                priority=PRIORITY_REMINDER,
                preemptible=True,
            )
        )
        await asyncio.sleep(0)

        async def second_user() -> None:
            events.append("user-2")

        newest = asyncio.create_task(
            arbiter.run(
                "deskbot-order",
                second_user,
                source="text",
                priority=PRIORITY_INTERACTIVE,
                replace_group="interactive-pending",
            )
        )
        await asyncio.sleep(0)
        assert events == ["user-1"]

        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(first, newest, deferred),
            timeout=1,
        )
        assert events == ["user-1", "user-2", "reminder"]

    asyncio.run(_run())


def test_best_effort_automation_is_dropped_while_lane_is_busy():
    from deskbot_server.application.turn_arbiter import (
        PRIORITY_AUTOMATION,
        PRIORITY_INTERACTIVE,
        DeviceTurnArbiter,
    )

    async def _run() -> None:
        arbiter = DeviceTurnArbiter()
        started = asyncio.Event()
        release = asyncio.Event()
        automation_ran = False

        async def interactive() -> None:
            started.set()
            await release.wait()

        active = asyncio.create_task(
            arbiter.run(
                "deskbot-camera-drop",
                interactive,
                source="asr",
                priority=PRIORITY_INTERACTIVE,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        async def automation() -> None:
            nonlocal automation_ran
            automation_ran = True

        assert (
            arbiter.submit_if_idle(
                "deskbot-camera-drop",
                automation,
                source="scheduled_automation",
                priority=PRIORITY_AUTOMATION,
                preemptible=True,
            )
            is None
        )
        assert automation_ran is False

        release.set()
        await asyncio.wait_for(active, timeout=1)
        submitted = arbiter.submit_if_idle(
            "deskbot-camera-drop",
            automation,
            source="scheduled_automation",
            priority=PRIORITY_AUTOMATION,
            preemptible=True,
        )
        assert submitted is not None
        _entry, task = submitted
        await asyncio.wait_for(task, timeout=1)
        assert automation_ran is True

    asyncio.run(_run())
