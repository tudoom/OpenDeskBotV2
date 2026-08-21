"""定时任务调度：到期后调用 LLM 并将结果下发到设备。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from deskbot_server.application.chat_flow import (
    _voice_was_played,
    publish_chat_turn,
    run_chat_turn,
)
from deskbot_server.application.turn_arbiter import (
    PRIORITY_REMINDER,
    TurnInterrupted,
    device_turn_arbiter,
)
from deskbot_server.device_preferences import quiet_hours_resume_at
from deskbot_server.infrastructure.ws.downlink_adapter import (
    WsDownlinkAdapter,
    WsPipelineEventsAdapter,
)
from deskbot_server.log_privacy import safe_log_content
from deskbot_server.playback_receipts import (
    has_any_playback_receipt,
    record_playback_receipts,
)
from deskbot_server.scheduled_task_service import (
    claim_due_tasks,
    defer_offline_scheduled_task,
    defer_quiet_hours_scheduled_task,
    expire_overdue_active_tasks,
    finish_scheduled_task,
    mark_scheduled_task_delivery_attempt,
    recover_expired_running_tasks,
    renew_scheduled_task_lease,
    retry_scheduled_task,
    wake_deliver_when_online_tasks,
)

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.asr_chat_hub import AsrChatHub
    from deskbot_server.ws.device_pipeline import DevicePipelineBroker
    from deskbot_server.ws.registry import DeviceRegistry

logger = logging.getLogger("deskbot-server")


class ScheduledTaskScheduler:
    def __init__(
        self,
        *,
        chat: "ChatService",
        asr_chat_hub: "AsrChatHub",
        registry: "DeviceRegistry",
        dp_broker: "DevicePipelineBroker",
        poll_interval_sec: float = 2.0,
        lookback_minutes: float = 5.0,
        lease_seconds: float = 120.0,
    ) -> None:
        self._chat = chat
        self._hub = asr_chat_hub
        self._registry = registry
        self._broker = dp_broker
        self._poll_interval = max(1.0, float(poll_interval_sec))
        self._lookback_minutes = max(1.0, float(lookback_minutes))
        self._lease_seconds = max(30.0, float(lease_seconds))
        self._lease_heartbeat_interval = min(
            30.0,
            max(5.0, self._lease_seconds / 3.0),
        )
        self._task: Optional[asyncio.Task] = None
        self._workers: set[asyncio.Task] = set()
        self._running_ids: set[str] = set()
        self._stopping = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        add_listener = getattr(self._hub, "add_online_listener", None)
        if callable(add_listener):
            add_listener(self._on_device_online)
        self._task = asyncio.create_task(self._loop(), name="scheduled_task_scheduler")
        logger.info(
            "[scheduler] 定时任务调度已启动 poll_interval=%.1fs lookback=%.1fmin",
            self._poll_interval,
            self._lookback_minutes,
        )

    async def stop(self) -> None:
        """Stop polling and wait until every scheduler-owned task has exited."""

        self._stopping = True
        remove_listener = getattr(self._hub, "remove_online_listener", None)
        if callable(remove_listener):
            try:
                remove_listener(self._on_device_online)
            except Exception:
                logger.exception("[scheduler] failed to remove online listener")
        poller = self._task
        self._task = None
        if poller is not None and not poller.done():
            poller.cancel()
        if poller is not None:
            await asyncio.gather(poller, return_exceptions=True)

        # The poller is fully stopped before taking this snapshot, so no new
        # workers can be added while shutdown is draining the current set.
        workers = tuple(self._workers)
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.difference_update(workers)
        self._running_ids.clear()
        logger.info("[scheduler] stopped")

    async def _on_device_online(self, device_id: str) -> None:
        if self._stopping:
            return
        try:
            woken = await asyncio.to_thread(
                wake_deliver_when_online_tasks,
            )
        except Exception:
            logger.exception(
                "[scheduler] failed to wake online reminders device_id=%s",
                device_id,
            )
            return
        if woken:
            logger.info(
                "[scheduler] device online; reminders made due device_id=%s count=%d",
                device_id,
                woken,
            )

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[scheduler] tick 异常")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        # Every scheduled-task DB helper commits through the shared SQLite
        # store (busy_timeout=30s).  Running them inline would let a Web
        # process write lock freeze audio/PB paths on this loop for the whole
        # busy timeout, so each call is dispatched to a worker thread.
        recovered = await asyncio.to_thread(recover_expired_running_tasks)
        if recovered:
            logger.warning("[scheduler] recovered expired leases count=%d", recovered)
        expired = await asyncio.to_thread(
            expire_overdue_active_tasks,
            lookback_minutes=self._lookback_minutes,
        )
        if expired:
            logger.info("[scheduler] 已标记超时未执行任务数=%d", expired)
        due = await asyncio.to_thread(
            claim_due_tasks,
            limit=10,
            lookback_minutes=self._lookback_minutes,
            lease_seconds=self._lease_seconds,
        )
        for item in due:
            tid = str(item.get("id") or "")
            if self._stopping or not tid or tid in self._running_ids:
                continue
            self._running_ids.add(tid)
            worker = asyncio.create_task(
                self._run_one(item),
                name=f"scheduled_task_{tid[:8]}",
            )
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _run_one(self, item: dict) -> None:
        tid = str(item.get("id") or "")
        lease_token = str(item.get("_lease_token") or "").strip()
        # Tasks belong to the PC-local profile. Resolve physical hardware only
        # when delivery starts so swapping robots remains plug-and-play.
        device_id = str(await self._hub.first_connected_device_id() or "").strip()
        description = str(item.get("description") or "").strip()
        heartbeat_task: asyncio.Task | None = None
        try:
            if not lease_token:
                logger.error(
                    "[scheduler] claimed task missing lease token task_id=%s",
                    tid,
                )
                return
            owner_task = asyncio.current_task()
            if owner_task is not None:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_lease(tid, lease_token, owner_task),
                    name=f"scheduled_task_lease_{tid[:8]}",
                )
            occurrence_id = str(item.get("occurrence_id") or "").strip()
            if occurrence_id and await asyncio.to_thread(
                has_any_playback_receipt,
                occurrence_id,
            ):
                finished = await asyncio.to_thread(
                    finish_scheduled_task,
                    tid,
                    ok=True,
                    lease_token=lease_token,
                    summary="设备已确认播放（根据持久回执恢复）",
                )
                logger.info(
                    "[scheduler] durable played receipt recovered "
                    "task_id=%s device_id=%s occurrence_id=%s finished=%s",
                    tid,
                    device_id,
                    occurrence_id,
                    finished,
                )
                # Even when ``finished`` is false, this worker has lost its
                # fencing token and must not invoke LLM/TTS.
                return
            # Quiet hours suppress unsolicited speech: a reminder that comes
            # due inside the window is deferred to the window's end instead of
            # being played or dropped.  User-initiated conversation is not
            # routed through this scheduler and stays unaffected.  The pure
            # file read still goes through to_thread to keep the realtime
            # loop free of disk I/O.
            quiet_resume_at = await asyncio.to_thread(quiet_hours_resume_at)
            if quiet_resume_at is not None:
                now_local = datetime.now(tz=quiet_resume_at.tzinfo)
                # +5s lands the retry just after the boundary minute so the
                # next claim does not re-enter the quiet window.
                delay_seconds = (
                    max(1.0, (quiet_resume_at - now_local).total_seconds())
                    + 5.0
                )
                requeued = await asyncio.to_thread(
                    defer_quiet_hours_scheduled_task,
                    tid,
                    lease_token=lease_token,
                    summary="勿扰时段内暂缓提醒，将在勿扰结束后播报",
                    delay_seconds=delay_seconds,
                )
                logger.info(
                    "[scheduler] quiet hours defer task_id=%s device_id=%s "
                    "resume_at=%s requeued=%s",
                    tid,
                    device_id,
                    quiet_resume_at.isoformat(),
                    requeued,
                )
                return
            ws = await self._hub.first_ws(device_id)
            if ws is None:
                from deskbot_server.device_preferences import load_preferences

                preferences = await asyncio.to_thread(load_preferences)
                policy = preferences.get(
                    "offline_reminder_policy", "retry_within_grace"
                )
                if policy == "expire":
                    await asyncio.to_thread(
                        finish_scheduled_task,
                        tid,
                        ok=False,
                        lease_token=lease_token,
                        summary="设备到点时离线，按用户策略明确过期",
                    )
                    requeued = False
                else:
                    if policy == "deliver_when_online":
                        grace_seconds = None
                        base_delay_seconds = 30.0
                        max_delay_seconds = 15 * 60.0
                    else:
                        # 0 is a legal value (expire immediately); only a
                        # missing key falls back to the 300s default.
                        grace_seconds = float(
                            preferences.get("offline_reminder_grace_seconds", 300)
                        )
                        base_delay_seconds = max(5.0, self._poll_interval)
                        max_delay_seconds = 60.0
                    requeued = await asyncio.to_thread(
                        defer_offline_scheduled_task,
                        tid,
                        lease_token=lease_token,
                        summary="设备 USB 会话未连接，等待恢复在线",
                        base_delay_seconds=base_delay_seconds,
                        max_delay_seconds=max_delay_seconds,
                        grace_seconds=grace_seconds,
                    )
                logger.warning(
                    "[scheduler] device offline task_id=%s device_id=%s policy=%s requeued=%s",
                    tid,
                    device_id,
                    policy,
                    requeued,
                )
                return

            if not await asyncio.to_thread(
                mark_scheduled_task_delivery_attempt,
                tid,
                lease_token=lease_token,
            ):
                logger.warning(
                    "[scheduler] delivery lease lost before provider call task_id=%s",
                    tid,
                )
                return
            # Stable across retries and service restarts. Firmware can
            # acknowledge this occurrence without replaying it if the server
            # crashes after the played ACK but before the final DB transition.
            req_id = str(occurrence_id or uuid.uuid4().hex)[:36]
            user_text = (
                f"[系统定时任务] 请向主人朗声提醒并执行以下任务：{description}\n"
                "要求：need_reply 必须为 true，tts 写直接说给主人听的提醒语，禁止写「已发送」等汇报语。"
            )
            logger.info(
                "[scheduler] 开始执行 task_id=%s device_id=%s session_id=%s content=%s req=%s",
                tid,
                device_id,
                item.get("session_id"),
                safe_log_content(description),
                req_id,
            )
            downlink = WsDownlinkAdapter(
                ws,
                settings=self._chat.settings,
                device_id=device_id,
                dp_broker=self._broker,
            )
            events = WsPipelineEventsAdapter(self._broker, self._registry)
            t0 = asyncio.get_event_loop().time()
            async def _run_reminder_turn():
                return await run_chat_turn(
                    downlink,
                    self._chat,
                    user_text,
                    request_id=req_id,
                    device_id=device_id,
                    registry=self._registry,
                    t_asr_text=t0,
                    force_voice=True,
                    make_session_current=False,
                    playback_receipt_id=occurrence_id or None,
                )

            try:
                turn = await device_turn_arbiter.run(
                    device_id,
                    _run_reminder_turn,
                    source="scheduled_task",
                    priority=PRIORITY_REMINDER,
                    preemptible=True,
                )
            except TurnInterrupted as exc:
                from deskbot_server.device_preferences import load_preferences

                preferences = await asyncio.to_thread(load_preferences)
                requeued = await asyncio.to_thread(
                    retry_scheduled_task,
                    tid,
                    summary=f"reminder deferred: {exc.reason}",
                    lease_token=lease_token,
                    retry_delay_seconds=max(5.0, self._poll_interval),
                    # Deliberate key reuse: the *offline* grace preference also
                    # bounds how long an *online but interrupted* reminder may
                    # keep retrying. There is no separate online-failure grace
                    # knob; 0 is legal and means "do not retry".
                    grace_seconds=float(
                        preferences.get("offline_reminder_grace_seconds", 300)
                    ),
                )
                logger.info(
                    "[scheduler] reminder interrupted task_id=%s device_id=%s "
                    "reason=%s requeued=%s",
                    tid,
                    device_id,
                    exc.reason,
                    requeued,
                )
                return
            await publish_chat_turn(
                events,
                device_id,
                source="scheduled_task",
                asr_text=user_text,
                t_asr_start=t0,
                t_asr_text=t0,
                turn=turn,
                request_id=req_id,
            )
            voice_ok = _voice_was_played(turn)
            ok = (turn.status or "ok") == "ok" and not turn.error and voice_ok
            if (turn.status or "ok") == "ok" and not turn.error and not voice_ok:
                summary = "LLM 已响应但未下发语音提醒"
                logger.warning(
                    "[scheduler] 任务未播报 task_id=%s device_id=%s need_reply=%s content=%s",
                    tid,
                    device_id,
                    turn.need_reply,
                    safe_log_content(turn.llm_text),
                )
            else:
                summary = (turn.llm_text or "")[:500] if ok else (turn.error or "执行失败")
            if ok:
                # This commit must precede the scheduled-task transition. If
                # the process dies between the two commits, a later lease
                # holder finishes from the receipt without replaying speech.
                await asyncio.to_thread(
                    record_playback_receipts,
                    device_id,
                    [req_id, *turn.playback_request_ids],
                    source="scheduled_task",
                )
                await asyncio.to_thread(
                    finish_scheduled_task,
                    tid,
                    ok=True,
                    summary=summary,
                    lease_token=lease_token,
                )
            else:
                from deskbot_server.device_preferences import load_preferences

                preferences = await asyncio.to_thread(load_preferences)
                # Deliberate key reuse: the *offline* grace preference also
                # bounds retries after an *online* delivery failure (no
                # separate knob). 0 is a legal value meaning "fail now";
                # only a missing key falls back to the 300s default.
                grace_seconds = float(
                    preferences.get("offline_reminder_grace_seconds", 300)
                )
                requeued = await asyncio.to_thread(
                    retry_scheduled_task,
                    tid,
                    summary=summary or "reminder delivery failed",
                    lease_token=lease_token,
                    retry_delay_seconds=max(5.0, self._poll_interval),
                    grace_seconds=grace_seconds,
                )
                logger.info(
                    "[scheduler] delivery retry decision task_id=%s requeued=%s",
                    tid,
                    requeued,
                )
            logger.info(
                "[scheduler] 任务完成 task_id=%s device_id=%s ok=%s voice_ok=%s content=%s",
                tid,
                device_id,
                ok,
                voice_ok,
                safe_log_content(summary),
            )
        except asyncio.CancelledError:
            # A lost lease cancels a stale worker and must not mutate the row.
            # During an orderly service shutdown, however, this worker still
            # owns its fencing token; release it immediately so a restarted
            # process doesn't have to wait for the lease timeout.
            if self._stopping and tid and lease_token:
                try:
                    requeued = await asyncio.to_thread(
                        retry_scheduled_task,
                        tid,
                        summary="service shutdown interrupted reminder; retrying",
                        lease_token=lease_token,
                        retry_delay_seconds=1.0,
                        grace_seconds=None,
                    )
                    logger.info(
                        "[scheduler] shutdown released execution lease "
                        "task_id=%s requeued=%s",
                        tid,
                        requeued,
                    )
                except Exception:
                    # The bounded lease still guarantees eventual recovery if
                    # the database is unavailable during shutdown.
                    logger.exception(
                        "[scheduler] shutdown failed to release execution "
                        "lease task_id=%s",
                        tid,
                    )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                retry_scheduled_task,
                tid,
                summary=str(exc),
                lease_token=lease_token,
                retry_delay_seconds=max(5.0, self._poll_interval),
                grace_seconds=300.0,
            )
            logger.exception(
                "[scheduler] 任务异常 task_id=%s device_id=%s",
                tid,
                device_id,
            )
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            self._running_ids.discard(tid)

    async def _heartbeat_lease(
        self,
        task_id: str,
        lease_token: str,
        owner_task: asyncio.Task,
    ) -> None:
        while True:
            await asyncio.sleep(self._lease_heartbeat_interval)
            try:
                renewed = await asyncio.to_thread(
                    renew_scheduled_task_lease,
                    task_id,
                    lease_token=lease_token,
                    lease_seconds=self._lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[scheduler] lease renewal failed; cancelling worker "
                    "task_id=%s",
                    task_id,
                )
                owner_task.cancel()
                return
            if renewed:
                continue
            logger.error(
                "[scheduler] execution lease lost; cancelling stale worker task_id=%s",
                task_id,
            )
            owner_task.cancel()
            return
