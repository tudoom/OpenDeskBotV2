from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from deskbot_server.core.clock import as_utc, utcnow


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        try:
            yield db_path
        finally:
            # Dispose SQLite's pooled handles before TemporaryDirectory tries
            # to remove the database on Windows.
            reset_engine()


def test_normalize_cron_and_next_run():
    from deskbot_server.scheduled_task_service import compute_next_run, normalize_cron_expr

    assert normalize_cron_expr("0 9 * * *") == "0 9 * * *"
    nxt = compute_next_run("0 9 * * *")
    assert nxt.hour == 9
    assert nxt.minute == 0


def test_normalize_cron_repairs_llm_once_datetime():
    from deskbot_server.scheduled_task_service import normalize_cron_expr

    assert normalize_cron_expr("0 49 15 6 12") == "49 15 12 6 *"
    assert normalize_cron_expr("44 15 12 6 *") == "44 15 12 6 *"
    assert normalize_cron_expr("0 44 15 12 6 2026") == "44 15 12 6 *"


def test_create_list_delete_cron_task_is_visible_from_any_hardware(temp_db):
    from deskbot_server.scheduled_task_service import (
        create_scheduled_task,
        delete_scheduled_task,
        execute_schedule_task_tool,
        list_scheduled_tasks,
    )

    row = create_scheduled_task(
        "提醒主人喝水",
        cron="0 9 * * *",
        task_kind="recurring",
    )
    assert "owner_user_id" not in row
    assert row["cron_expr"] == "0 9 * * *"
    assert row["task_kind"] == "recurring"
    assert row["status"] == "active"

    listed = execute_schedule_task_tool({"action": "list"})
    assert listed["ok"] is True
    assert listed["count"] == 1

    tasks = list_scheduled_tasks()
    assert tasks[0]["id"] == row["id"]

    assert delete_scheduled_task(row["id"])
    assert list_scheduled_tasks() == []


def test_schedule_task_crud_via_tool(temp_db):
    from deskbot_server.application.llm_tool_runner import execute_llm_tools
    from deskbot_server.scheduled_task_service import get_scheduled_task

    created = execute_llm_tools(
        [
            {
                "tool": "schedule_task",
                "action": "create",
                "task": "十分钟后提醒开会",
                "delay_minutes": 10,
                "task_kind": "once",
            }
        ],
        device_id="deskbot_a",
    )
    assert created[0]["ok"] is True
    tid = created[0]["id"]
    assert created[0]["task_kind"] == "once"
    # Reminders are intentionally detached from the chat that created them:
    # executing one later must not roll the user's active context backwards.
    assert created[0]["session_id"] is None

    got = execute_llm_tools(
        [{"tool": "schedule_task", "action": "get", "id": tid}],
        device_id="deskbot_b",
    )
    assert got[0]["task"]["id"] == tid

    updated = execute_llm_tools(
        [
            {
                "tool": "schedule_task",
                "action": "update",
                "id": tid,
                "task": "提醒喝水",
            }
        ],
        device_id="deskbot_a",
    )
    assert updated[0]["ok"] is True
    assert updated[0]["description"] == "提醒喝水"

    blocked = execute_llm_tools(
        [{"tool": "schedule_task", "action": "delete", "id": tid}],
        device_id="deskbot_a",
    )
    assert blocked[0]["confirmation_required"] is True
    assert get_scheduled_task(tid) is not None

    deleted = execute_llm_tools(
        [{"tool": "schedule_task", "action": "delete", "id": tid}],
        device_id="deskbot_a",
        user_confirmed=True,
    )
    assert deleted[0]["ok"] is True
    assert get_scheduled_task(tid) is None


def test_confirmation_is_bound_to_exact_payload_and_single_operation(temp_db):
    from deskbot_server.application.llm_tool_runner import execute_llm_tools
    from deskbot_server.scheduled_task_service import (
        create_scheduled_task,
        get_scheduled_task,
    )

    first = create_scheduled_task(
        "first",
        delay_seconds=60,
        task_kind="once",
    )
    second = create_scheduled_task(
        "second",
        delay_seconds=60,
        task_kind="once",
    )
    delete_first = {
        "tool": "schedule_task",
        "action": "delete",
        "id": first["id"],
    }
    delete_second = {
        "tool": "schedule_task",
        "action": "delete",
        "id": second["id"],
    }

    pending_first = execute_llm_tools(
        [delete_first],
        device_id="deskbot_confirm",
    )
    assert pending_first[0]["confirmation_required"] is True
    assert pending_first[0]["confirmation_id"]

    mismatched = execute_llm_tools(
        [delete_second],
        device_id="deskbot_confirm",
        user_confirmed=True,
    )
    assert mismatched[0]["confirmation_required"] is True
    assert get_scheduled_task(first["id"]) is not None
    assert get_scheduled_task(second["id"]) is not None

    one_confirmation = execute_llm_tools(
        [delete_second, delete_first],
        device_id="deskbot_confirm",
        user_confirmed=True,
    )
    assert one_confirmation[0]["ok"] is True
    assert one_confirmation[1]["confirmation_required"] is True
    assert get_scheduled_task(second["id"]) is None
    assert get_scheduled_task(first["id"]) is not None


def test_abandoned_tool_operation_becomes_unknown_not_in_progress(temp_db):
    from datetime import datetime, timezone

    from deskbot_server.application.llm_tool_runner import _claim_tool_operation
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation, _new_id

    session = get_session()
    session.add(
        ToolOperation(
            id=_new_id(),
            tool_name="miot",
            operation_id="operation-old",
            status="running",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
    )
    session.commit()
    session.close()

    claimed, result = _claim_tool_operation(
        "miot",
        "operation-old",
    )
    assert claimed is False
    assert result["operation_status"] == "unknown"
    assert "in progress" not in result["error"]


def test_claim_due_tasks_lookback_window(temp_db):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        expire_overdue_active_tasks,
    )

    now = utcnow()
    session = get_session()
    session.add(
        ScheduledTask(
            id=_new_id(),
            description="刚到期",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=now - timedelta(minutes=2),
            status="active",
        )
    )
    session.add(
        ScheduledTask(
            id=_new_id(),
            description="太久以前",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=now - timedelta(minutes=10),
            status="active",
        )
    )
    session.commit()

    expired = expire_overdue_active_tasks(lookback_minutes=5)
    assert expired == 1

    claimed = claim_due_tasks(lookback_minutes=5)
    assert len(claimed) == 1
    assert claimed[0]["description"] == "刚到期"


def test_deliver_when_online_survives_restart_lookback(temp_db, monkeypatch):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        expire_overdue_active_tasks,
    )

    monkeypatch.setattr(
        "deskbot_server.device_preferences.load_preferences",
        lambda: {
            "offline_reminder_policy": "deliver_when_online",
            "offline_reminder_grace_seconds": 300,
        },
    )
    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="恢复在线后补发",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(days=2),
            status="active",
        )
    )
    session.commit()

    assert expire_overdue_active_tasks(lookback_minutes=5) == 0
    claimed = claim_due_tasks(lookback_minutes=5)
    assert [item["id"] for item in claimed] == [tid]


def test_offline_waits_back_off_without_consuming_delivery_attempts(
    temp_db, monkeypatch
):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        defer_offline_scheduled_task,
        get_scheduled_task,
        mark_scheduled_task_delivery_attempt,
        wake_deliver_when_online_tasks,
    )

    monkeypatch.setattr(
        "deskbot_server.device_preferences.load_preferences",
        lambda: {"offline_reminder_policy": "deliver_when_online"},
    )
    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="deliver after reconnect",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(seconds=1),
            status="active",
        )
    )
    session.commit()
    session.close()

    first = claim_due_tasks(lease_seconds=30)[0]
    occurrence_id = first["occurrence_id"]
    assert first["attempt_count"] == 0
    assert defer_offline_scheduled_task(
        tid,
        summary="offline",
        lease_token=first["_lease_token"],
        base_delay_seconds=30,
        max_delay_seconds=120,
        grace_seconds=None,
    )
    session = get_session()
    row = session.get(ScheduledTask, tid)
    first_delay = (as_utc(row.next_run_at) - utcnow()).total_seconds()
    assert 25 <= first_delay <= 35
    row.next_run_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    second = claim_due_tasks(lease_seconds=30)[0]
    assert second["occurrence_id"] == occurrence_id
    assert second["attempt_count"] == 0
    assert defer_offline_scheduled_task(
        tid,
        summary="still offline",
        lease_token=second["_lease_token"],
        base_delay_seconds=30,
        max_delay_seconds=120,
        grace_seconds=None,
    )
    session = get_session()
    row = session.get(ScheduledTask, tid)
    second_delay = (as_utc(row.next_run_at) - utcnow()).total_seconds()
    session.close()
    assert 55 <= second_delay <= 65

    assert wake_deliver_when_online_tasks() == 1
    third = claim_due_tasks(lease_seconds=30)[0]
    assert third["occurrence_id"] == occurrence_id
    assert mark_scheduled_task_delivery_attempt(
        tid,
        lease_token=third["_lease_token"],
    )
    current = get_scheduled_task(tid)
    assert current is not None
    assert current["attempt_count"] == 1
    assert current["offline_wait_count"] == 0


def test_asr_chat_hub_notifies_first_online_transition():
    from deskbot_server.ws.asr_chat_hub import AsrChatHub

    class FakeWs:
        pass

    async def _run():
        hub = AsrChatHub()
        observed: list[str] = []

        async def listener(device_id: str):
            observed.append(device_id)

        hub.add_online_listener(listener)
        await hub.attach("deskbot-online-event", FakeWs())
        hub.remove_online_listener(listener)
        await hub.attach("deskbot-other-event", FakeWs())
        return observed

    assert asyncio.run(_run()) == ["deskbot-online-event"]


def test_scheduler_online_wake_uses_pc_local_scope(monkeypatch):
    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )

    calls = 0

    def _wake():
        nonlocal calls
        calls += 1
        return 1

    monkeypatch.setattr(
        scheduler_module,
        "wake_deliver_when_online_tasks",
        _wake,
    )
    scheduler = ScheduledTaskScheduler(
        chat=None,  # type: ignore[arg-type]
        asr_chat_hub=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        dp_broker=None,  # type: ignore[arg-type]
    )

    asyncio.run(scheduler._on_device_online("deskbot-online-event"))

    assert calls == 1


def test_running_lease_uses_private_fencing_token_and_cas_finish(temp_db):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        finish_scheduled_task,
        get_scheduled_task,
        renew_scheduled_task_lease,
    )

    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="fenced reminder",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(seconds=1),
            status="active",
        )
    )
    session.commit()
    session.close()

    claimed = claim_due_tasks(lease_seconds=30)
    assert [item["id"] for item in claimed] == [tid]
    token = claimed[0]["_lease_token"]
    assert token
    assert "_lease_token" not in get_scheduled_task(tid)

    assert not renew_scheduled_task_lease(
        tid, lease_token="wrong-token", lease_seconds=30
    )
    assert not finish_scheduled_task(
        tid,
        ok=True,
        summary="stale worker",
        lease_token="wrong-token",
    )
    assert get_scheduled_task(tid)["status"] == "running"

    assert renew_scheduled_task_lease(tid, lease_token=token, lease_seconds=30)
    assert finish_scheduled_task(
        tid,
        ok=True,
        summary="played",
        lease_token=token,
    )
    assert get_scheduled_task(tid)["status"] == "completed"


def test_recovered_lease_rejects_old_worker_completion(temp_db):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        finish_scheduled_task,
        get_scheduled_task,
        recover_expired_running_tasks,
    )

    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="recover reminder",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(seconds=1),
            status="active",
        )
    )
    session.commit()
    session.close()

    old_claim = claim_due_tasks(lease_seconds=30)[0]
    session = get_session()
    row = session.get(ScheduledTask, tid)
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    assert recover_expired_running_tasks() == 1
    new_claim = claim_due_tasks(lease_seconds=30)[0]
    assert new_claim["_lease_token"] != old_claim["_lease_token"]
    assert not finish_scheduled_task(
        tid,
        ok=True,
        summary="late old worker",
        lease_token=old_claim["_lease_token"],
    )
    assert get_scheduled_task(tid)["status"] == "running"
    assert finish_scheduled_task(
        tid,
        ok=True,
        summary="new worker",
        lease_token=new_claim["_lease_token"],
    )


def test_played_then_finish_crash_does_not_replay_after_device_switch(
    temp_db, monkeypatch
):
    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )
    from deskbot_server.core.types import ChatTurnResult
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import (
        get_session,
        init_engine,
        reset_engine,
    )
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.playback_receipts import has_playback_receipt
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        get_scheduled_task,
        recover_expired_running_tasks,
    )

    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="只播一次",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(seconds=1),
            status="active",
        )
    )
    session.commit()
    session.close()

    request_ids: list[str] = []
    ws_lookups: list[str] = []

    async def _run_chat_turn(*_args, request_id=None, **_kwargs):
        request_ids.append(str(request_id))
        return ChatTurnResult(
            llm_text="提醒已播放",
            status="ok",
            playback_status="played",
        )

    async def _publish(*_args, **_kwargs):
        return None

    class _Hub:
        device_id = "deskbot-played-crash"

        async def first_connected_device_id(self):
            return self.device_id

        async def first_ws(self, _device_id):
            ws_lookups.append(str(_device_id))
            return object()

    class _Chat:
        settings = object()

    monkeypatch.setattr(scheduler_module, "run_chat_turn", _run_chat_turn)
    monkeypatch.setattr(scheduler_module, "publish_chat_turn", _publish)
    # Pin quiet hours off so the test is independent of the wall clock.
    monkeypatch.setattr(scheduler_module, "quiet_hours_resume_at", lambda: None)
    real_finish = scheduler_module.finish_scheduled_task

    def _crash_after_played(*_args, **_kwargs):
        raise SystemExit("simulated process crash before DB finish")

    monkeypatch.setattr(
        scheduler_module,
        "finish_scheduled_task",
        _crash_after_played,
    )
    hub = _Hub()
    scheduler = ScheduledTaskScheduler(
        chat=_Chat(),  # type: ignore[arg-type]
        asr_chat_hub=hub,  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        dp_broker=object(),  # type: ignore[arg-type]
        lease_seconds=30,
    )

    first = claim_due_tasks(lease_seconds=30)[0]
    occurrence_id = first["occurrence_id"]
    with pytest.raises(SystemExit, match="simulated process crash"):
        asyncio.run(scheduler._run_one(first))
    assert request_ids == [occurrence_id]
    assert ws_lookups == ["deskbot-played-crash"]
    assert has_playback_receipt("deskbot-played-crash", occurrence_id)
    assert get_scheduled_task(tid)["status"] == "running"

    session = get_session()
    row = session.get(ScheduledTask, tid)
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    # Simulate a fresh service process using the same SQLite file. The
    # receipt must not depend on in-memory ACK-gate state.
    reset_engine()
    init_engine(temp_db)
    init_database()
    assert recover_expired_running_tasks() == 1

    monkeypatch.setattr(
        scheduler_module,
        "finish_scheduled_task",
        real_finish,
    )
    hub.device_id = "deskbot-replacement"
    second = claim_due_tasks(lease_seconds=30)[0]
    assert second["occurrence_id"] == occurrence_id
    asyncio.run(scheduler._run_one(second))

    assert request_ids == [occurrence_id]
    assert ws_lookups == ["deskbot-played-crash"]
    assert get_scheduled_task(tid)["status"] == "completed"


@pytest.mark.parametrize("renew_result", [False, RuntimeError("database unavailable")])
def test_lease_heartbeat_cancels_worker_when_renewal_is_not_safe(
    monkeypatch,
    renew_result,
):
    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )

    def _renew(*_args, **_kwargs):
        if isinstance(renew_result, Exception):
            raise renew_result
        return renew_result

    monkeypatch.setattr(scheduler_module, "renew_scheduled_task_lease", _renew)

    async def _run():
        scheduler = ScheduledTaskScheduler(
            chat=None,  # type: ignore[arg-type]
            asr_chat_hub=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            dp_broker=None,  # type: ignore[arg-type]
        )
        scheduler._lease_heartbeat_interval = 0.001
        owner = asyncio.current_task()
        assert owner is not None
        heartbeat = asyncio.create_task(
            scheduler._heartbeat_lease("task-heartbeat", "token", owner)
        )
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await heartbeat
            return True
        finally:
            if not heartbeat.done():
                heartbeat.cancel()
        return False

    assert asyncio.run(_run()) is True


def test_scheduler_stop_cancels_and_awaits_owned_workers(monkeypatch):
    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )

    claimed = [
        {
            "id": "shutdown-task",
            "_lease_token": "lease",
            "description": "test",
        }
    ]
    monkeypatch.setattr(
        scheduler_module,
        "recover_expired_running_tasks",
        lambda: 0,
    )
    monkeypatch.setattr(
        scheduler_module,
        "expire_overdue_active_tasks",
        lambda **_kwargs: 0,
    )

    def _claim_due(**_kwargs):
        return [claimed.pop(0)] if claimed else []

    monkeypatch.setattr(
        scheduler_module,
        "claim_due_tasks",
        _claim_due,
    )

    class _Hub:
        def __init__(self):
            self.listeners = set()

        def add_online_listener(self, listener):
            self.listeners.add(listener)

        def remove_online_listener(self, listener):
            self.listeners.discard(listener)

    async def _run():
        hub = _Hub()
        scheduler = ScheduledTaskScheduler(
            chat=None,  # type: ignore[arg-type]
            asr_chat_hub=hub,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            dp_broker=None,  # type: ignore[arg-type]
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _blocking_worker(_item):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        scheduler._run_one = _blocking_worker  # type: ignore[method-assign]
        scheduler.start()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert scheduler._workers
        assert hub.listeners

        await scheduler.stop()
        assert cancelled.is_set()
        assert scheduler._task is None
        assert not scheduler._workers
        assert not scheduler._running_ids
        assert not hub.listeners

        # Shutdown is idempotent and must not resurrect listeners or tasks.
        await scheduler.stop()
        assert not hub.listeners

    asyncio.run(_run())


def test_scheduler_shutdown_releases_owned_execution_lease(monkeypatch):
    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )

    monkeypatch.setattr(
        scheduler_module,
        "has_any_playback_receipt",
        lambda *_args, **_kwargs: False,
    )
    released: list[tuple[str, dict]] = []

    def _retry(task_id, **kwargs):
        released.append((task_id, kwargs))
        return True

    monkeypatch.setattr(scheduler_module, "retry_scheduled_task", _retry)
    # Pin quiet hours off so the test is independent of the wall clock.
    monkeypatch.setattr(scheduler_module, "quiet_hours_resume_at", lambda: None)

    async def _run():
        entered = asyncio.Event()

        class _Hub:
            async def first_connected_device_id(self):
                return "deskbot_shutdown"

            async def first_ws(self, _device_id):
                entered.set()
                await asyncio.Future()

        scheduler = ScheduledTaskScheduler(
            chat=None,  # type: ignore[arg-type]
            asr_chat_hub=_Hub(),  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            dp_broker=None,  # type: ignore[arg-type]
        )
        scheduler._stopping = True
        worker = asyncio.create_task(
            scheduler._run_one(
                {
                    "id": "leased-task",
                    "_lease_token": "lease-token",
                    "description": "test",
                }
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(_run())
    assert released == [
        (
            "leased-task",
            {
                "summary": "service shutdown interrupted reminder; retrying",
                "lease_token": "lease-token",
                "retry_delay_seconds": 1.0,
                "grace_seconds": None,
            },
        )
    ]


def test_transient_retry_uses_grace_not_small_attempt_cap(temp_db):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        get_scheduled_task,
        retry_scheduled_task,
    )

    tid = _new_id()
    now = utcnow()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="宽限期内补发",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=now,
            first_due_at=now - timedelta(seconds=120),
            attempt_count=50,
            status="running",
        )
    )
    session.commit()

    assert retry_scheduled_task(
        tid,
        summary="offline",
        retry_delay_seconds=5,
        grace_seconds=300,
    )
    row = get_scheduled_task(tid)
    assert row is not None
    assert row["status"] == "active"
    assert row["enabled"] is True


def test_zero_grace_is_legal_and_fails_immediately(temp_db):
    # grace_seconds=0 must mean "no retry window" (fail now), never be
    # silently coerced to the 300s default.
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        get_scheduled_task,
        retry_scheduled_task,
    )

    tid = _new_id()
    now = utcnow()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="零宽限立即失败",
            cron_expr="0 9 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=now,
            first_due_at=now - timedelta(seconds=1),
            attempt_count=0,
            status="running",
        )
    )
    session.commit()

    assert not retry_scheduled_task(
        tid,
        summary="delivery failed",
        retry_delay_seconds=5,
        grace_seconds=0.0,
    )
    row = get_scheduled_task(tid)
    assert row is not None
    assert row["status"] == "failed"
    assert row["enabled"] is False


def test_migrate_legacy_run_at_column(temp_db, monkeypatch):
    from sqlalchemy import inspect, text

    from deskbot_server.db.engine import init_engine
    from deskbot_server.db.init_db import _migrate_scheduled_tasks_drop_legacy_run_at
    from deskbot_server.scheduled_task_service import create_scheduled_task

    engine = init_engine(temp_db)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS scheduled_tasks"))
        conn.execute(
            text(
                """
                CREATE TABLE scheduled_tasks (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    device_id VARCHAR(128) NOT NULL,
                    description TEXT NOT NULL,
                    run_at DATETIME NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    result_summary TEXT,
                    created_at DATETIME NOT NULL,
                    executed_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO scheduled_tasks (
                    id, device_id, description, run_at, status, created_at
                ) VALUES (
                    'legacy1', 'deskbot_a', '旧任务', '2026-06-12 09:00:00', 'active', '2026-06-12 08:00:00'
                )
                """
            )
        )

    _migrate_scheduled_tasks_drop_legacy_run_at(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("scheduled_tasks")}
    assert "run_at" not in cols
    assert "device_id" not in cols
    assert "next_run_at" in cols
    assert "lease_token" in cols
    assert "offline_wait_count" in cols
    assert "occurrence_id" in cols

    row = create_scheduled_task("提醒喝水", cron="44 15 12 6 *", task_kind="once")
    assert row["description"] == "提醒喝水"


def test_scheduled_reminder_tts_helpers():
    from deskbot_server.application.chat_flow import (
        _scheduled_reminder_tts,
        _scheduled_task_description,
        _scheduled_tts_looks_like_meta_report,
    )

    desc = _scheduled_task_description(
        "[系统定时任务] 请向主人朗声提醒并执行以下任务：提醒喝水"
    )
    assert desc == "提醒喝水"
    assert _scheduled_reminder_tts("提醒喝水") == "主人，该喝水啦。"
    assert _scheduled_tts_looks_like_meta_report("提醒已发送，小明记得喝水哦。")
    assert not _scheduled_tts_looks_like_meta_report("该喝水啦")


def test_finish_recurring_reschedules(temp_db):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        finish_scheduled_task,
        get_scheduled_task,
    )

    tid = _new_id()
    now = utcnow()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="每日提醒",
            cron_expr="0 9 * * *",
            task_kind="recurring",
            enabled=True,
            next_run_at=now,
            status="running",
        )
    )
    session.commit()
    finish_scheduled_task(tid, ok=True, summary="完成")
    row = get_scheduled_task(tid)
    assert row is not None
    assert row["status"] == "active"
    assert row["enabled"] is True


def test_created_at_serializes_utc_storage_to_beijing_time(temp_db):
    from datetime import datetime, timedelta, timezone

    from deskbot_server.scheduled_task_service import (
        create_scheduled_task,
        format_cst,
        list_scheduled_tasks,
    )

    row = create_scheduled_task("测试时区", cron="0 9 * * *", task_kind="recurring")
    listed = next(t for t in list_scheduled_tasks() if t["id"] == row["id"])

    # created_at 以 UTC 墙钟落库；序列化边界转成北京时间后应≈当前本地时刻，
    # 不再恒差 8 小时。
    now_cst = datetime.now(timezone(timedelta(hours=8)))
    rendered = datetime.strptime(listed["created_at"], "%Y-%m-%d %H:%M:%S")
    delta = abs((now_cst.replace(tzinfo=None) - rendered).total_seconds())
    assert delta < 120

    # 落库值（naive=UTC 墙钟）与带 tz 的值都渲染一致。
    naive_utc = datetime(2026, 1, 2, 3, 4, 5)
    aware_utc = naive_utc.replace(tzinfo=timezone.utc)
    assert format_cst(naive_utc) == "2026-01-02 11:04:05"
    assert format_cst(aware_utc) == "2026-01-02 11:04:05"
    assert format_cst(None) is None


def test_quiet_hours_defer_reminder_until_window_end_then_deliver(
    temp_db, monkeypatch
):
    """勿扰窗口内的提醒被推迟到窗口结束；窗口外恢复正常交付。"""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    import deskbot_server.application.scheduled_task_scheduler as scheduler_module
    from deskbot_server.application.scheduled_task_scheduler import (
        ScheduledTaskScheduler,
    )
    from deskbot_server.core.types import ChatTurnResult
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ScheduledTask, _new_id
    from deskbot_server.scheduled_task_service import (
        claim_due_tasks,
        get_scheduled_task,
    )

    tid = _new_id()
    session = get_session()
    session.add(
        ScheduledTask(
            id=tid,
            description="夜间提醒",
            cron_expr="0 23 * * *",
            task_kind="once",
            enabled=True,
            next_run_at=utcnow() - timedelta(seconds=1),
            # 模拟到点已久：勿扰 defer 必须重置 first_due_at，否则窗口结束后
            # 一旦设备离线，宽限期会按夜里的原始到期时刻立即判过期。
            first_due_at=utcnow() - timedelta(hours=3),
            status="active",
        )
    )
    session.commit()
    session.close()

    turns: list[str] = []
    ws_lookups: list[str] = []

    async def _run_chat_turn(*_args, request_id=None, **_kwargs):
        turns.append(str(request_id))
        return ChatTurnResult(
            llm_text="提醒已播放",
            status="ok",
            playback_status="played",
        )

    async def _publish(*_args, **_kwargs):
        return None

    class _Hub:
        async def first_connected_device_id(self):
            return "deskbot-quiet"

        async def first_ws(self, _device_id):
            ws_lookups.append(str(_device_id))
            return object()

    class _Chat:
        settings = object()

    monkeypatch.setattr(scheduler_module, "run_chat_turn", _run_chat_turn)
    monkeypatch.setattr(scheduler_module, "publish_chat_turn", _publish)
    resume_at = datetime.now(dt_timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(
        scheduler_module, "quiet_hours_resume_at", lambda: resume_at
    )

    scheduler = ScheduledTaskScheduler(
        chat=_Chat(),  # type: ignore[arg-type]
        asr_chat_hub=_Hub(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        dp_broker=object(),  # type: ignore[arg-type]
        lease_seconds=30,
    )

    claimed = claim_due_tasks(lease_seconds=30)[0]
    asyncio.run(scheduler._run_one(claimed))

    # 勿扰中：没有 LLM/TTS，也没有查找设备连接；任务回到 active 并推迟。
    assert turns == []
    assert ws_lookups == []
    row = get_scheduled_task(tid)
    assert row["status"] == "active"
    assert row["first_due_at"] is None
    assert row["attempt_count"] == 0
    session = get_session()
    db_row = session.get(ScheduledTask, tid)
    assert db_row.lease_token is None
    deferred_next_run = as_utc(db_row.next_run_at)
    session.close()
    # next_run_at ≈ 勿扰结束时刻（+5s 缓冲）；容差 5 分钟避免脆弱。
    delta = abs((deferred_next_run - as_utc(resume_at)).total_seconds())
    assert delta < 300

    # 窗口结束：勿扰判定返回 None，任务到期后正常交付并完成。
    monkeypatch.setattr(scheduler_module, "quiet_hours_resume_at", lambda: None)
    session = get_session()
    db_row = session.get(ScheduledTask, tid)
    db_row.next_run_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()

    second = claim_due_tasks(lease_seconds=30)[0]
    asyncio.run(scheduler._run_one(second))

    assert len(turns) == 1
    assert ws_lookups == ["deskbot-quiet"]
    assert get_scheduled_task(tid)["status"] == "completed"


def test_scheduled_tasks_single_clock_convention(temp_db):
    """同表口径一致：created_at 与 next_run_at 都按 naive-UTC 落库。"""
    from datetime import datetime

    from sqlalchemy import text as sql_text

    from deskbot_server.db.engine import get_session
    from deskbot_server.scheduled_task_service import create_scheduled_task

    row = create_scheduled_task("口径一致", delay_seconds=30.0)
    session = get_session()
    try:
        raw = session.execute(
            sql_text(
                "SELECT created_at, next_run_at FROM scheduled_tasks WHERE id = :id"
            ),
            {"id": row["id"]},
        ).one()
    finally:
        session.close()
    created = datetime.strptime(str(raw[0])[:19], "%Y-%m-%d %H:%M:%S")
    next_run = datetime.strptime(str(raw[1])[:19], "%Y-%m-%d %H:%M:%S")
    # 旧实现两列相差约 8 小时（UTC vs CST 墙钟）；统一后仅差 delay（30s）。
    assert abs((next_run - created).total_seconds() - 30.0) < 120


def test_versioned_migration_shifts_legacy_cst_rows(temp_db):
    """旧数据迁移正确性：naive-CST 旧行 -8h 转 UTC 后，渲染时刻不变。"""
    import uuid as _uuid

    from sqlalchemy import text as sql_text

    from deskbot_server.db import init_database
    from deskbot_server.db.engine import get_session
    from deskbot_server.scheduled_task_service import get_scheduled_task

    task_id = str(_uuid.uuid4())
    session = get_session()
    try:
        session.execute(
            sql_text(
                "INSERT INTO scheduled_tasks ("
                "id, description, cron_expr, task_kind, enabled, next_run_at,"
                " status, created_at, executed_at, attempt_count,"
                " offline_wait_count, occurrence_id"
                ") VALUES ("
                ":id, :desc, :cron, 'once', 1, :next_run_at,"
                " 'completed', :created_at, :executed_at, 0, 0, :occ)"
            ),
            {
                "id": task_id,
                "desc": "legacy naive-CST row",
                "cron": "0 9 2 1 *",
                # 旧口径：next_run_at 为 CST 墙钟（naive）。
                "next_run_at": "2026-01-02 09:00:00.000000",
                # created_at 一直是 UTC 墙钟（同一时刻）。
                "created_at": "2026-01-02 01:00:00.000000",
                # 旧 _utc_text 兜底写法：带显式 +00:00 后缀。
                "executed_at": "2026-01-02 01:00:00.000000+00:00",
                "occ": str(_uuid.uuid4()),
            },
        )
        session.execute(sql_text("PRAGMA user_version = 0"))
        session.commit()
    finally:
        session.close()

    init_database()  # 重新触发带版本号的一次性迁移

    session = get_session()
    try:
        raw = session.execute(
            sql_text(
                "SELECT next_run_at, created_at, executed_at, "
                "(SELECT * FROM pragma_user_version()) "
                "FROM scheduled_tasks WHERE id = :id"
            ),
            {"id": task_id},
        ).one()
    finally:
        session.close()
    assert str(raw[0]).startswith("2026-01-02 01:00:00")  # -8h 转 UTC
    assert str(raw[1]).startswith("2026-01-02 01:00:00")  # UTC 保持不变
    assert str(raw[2]) == "2026-01-02 01:00:00"  # 偏移后缀被规范化
    assert int(raw[3]) >= 1

    rendered = get_scheduled_task(task_id)
    assert rendered is not None
    # 渲染时刻不变：仍是北京时间 2026-01-02 09:00。
    assert rendered["next_run_at"] == "2026-01-02 09:00:00"
    assert rendered["created_at"] == "2026-01-02 09:00:00"
    assert rendered["executed_at"] == "2026-01-02 09:00:00"

    # 幂等：版本已提升，重复启动不再二次偏移。
    init_database()
    check = get_scheduled_task(task_id)
    assert check is not None and check["next_run_at"] == "2026-01-02 09:00:00"
