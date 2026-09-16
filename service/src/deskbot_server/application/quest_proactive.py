"""主动陪伴（剧本冷场开口）：设备冷场 ≥ 偏好 ``quest.idle_sec``（默认 2 分钟）且主人在面前时，
发起一轮系统主动对话。

触发条件（``QuestProactiveLoop.tick`` 每 5 s 检查一次）：
1. 偏好 ``quest.proactive_enabled`` 开（主线场景可选可不选；日常关心场景一直生效）
2. 有已连接的机器人（``asr_chat_hub.first_connected_device_id``）
3. 距最后一轮对话 ≥ 冷场时长（默认取 registry 的 ``interaction_state_ts``：
   文字轮 THINKING/SPEAKING/IDLE、语音 VAD 边沿 LISTENING 都会刷新；设备刚连上无打点时
   以本循环首次看见它的时间起算）
4. 人在不在面前不再判断（2026-09-10 去掉人脸相关功能）。
5. 不在勿扰时段；设备对话通道空闲（``device_turn_arbiter.submit_if_idle``）

主线的推进（2026-09-10 用户定稿）：
- 小歪就一个小目标开过口后，这一轮不再重复提它；达成 / 未达成 / 跳过只由小歪（按达成条件和主人的回复）
  或主人在控制台明确标；
- 标了结果 → 状态文件里留一个「接下一个」标记（``quest_service.note_chain``）；本循环看到标记后，等大家
  安静 ``next_delay_sec``（默认 10 s）再触发顺序里的下一个还没达成的小目标；
- 每隔场景的 ``check_interval_hours``（默认 1 小时）做一次「定时检测」（``quest_service.begin_pass``）：
  进行中的重新计时、没有进行中的就按顺序重开第一个未达成的，之后仍按冷场规则开口。

节流：无 running 任务 / 设备离线 / 达到每日上限（``quest.daily_limit``）/ 进行中任务这一轮都提过
→ ``QUEST_NO_TASK_COOLDOWN_SEC`` 空转冷却；同一设备 attempt 进行中不重入。
控制台可通过 ``request_speak_now`` 跳过节流立刻试一句。

投递复用定时提醒的路径：``run_chat_turn(force_voice=True)`` + ``publish_chat_turn``；
user_text 以 ``_QUEST_PROACTIVE_PREFIX`` 开头，chat_flow 据此强制 need_reply 并把
「已发送/已汇报」类 meta 文案兜底成面向主人的口播语。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from deskbot_server.application import quest_service, rtc_instructions, user_activity
from deskbot_server.application.chat_flow import (
    _QUEST_PROACTIVE_PREFIX,
    _voice_was_played,
    publish_chat_turn,
    run_chat_turn,
    run_device_playbook,
)
from deskbot_server.application.expression_runtime import get_expression_runtime
from deskbot_server.application.proactive_gate import SOURCE_QUEST, can_be_proactive
from deskbot_server.application.turn_arbiter import (
    PRIORITY_AUTOMATION,
    device_turn_arbiter,
)
from deskbot_server.device_preferences import (
    preferred_timezone_name,
    quiet_hours_active,
    quiet_hours_resume_at,
)
from deskbot_server.infrastructure.ws.downlink_adapter import (
    WsDownlinkAdapter,
    WsPipelineEventsAdapter,
)
from deskbot_server.log_privacy import safe_log_content
from deskbot_server.quest_playbooks_store import (
    PERFORM_EXPRESSION,
    PERFORM_SCENE,
    ensure_default_playbook,
)
from deskbot_server.scene_playbooks_store import find_playbook_by_name, load_scene_playbooks_file

# 协作对象只按鸭子类型使用（first_ws / snapshot / settings），不引入 ws 层类型
# ——避免为类型注解增加函数内延迟 import（test_layering 棘轮）。

logger = logging.getLogger("deskbot-server")

QUEST_ATTEMPT_IDLE_SEC = 120.0  # 距最后一轮对话超过此时长才尝试推进剧情（偏好 quest.idle_sec 可改）
QUEST_CHECK_SEC = 5.0  # 检查节流
QUEST_NO_TASK_COOLDOWN_SEC = 60.0  # 无 running 任务 / 设备离线 / 达到每日上限时空转冷却
QUEST_TASK_RETRY_SEC = 120.0  # 日常关心（看情况的）两次之间至少隔这么久；主线不用（一轮只提一次，结果由小歪标）
SCHEDULE_GRACE_SEC = 7200.0  # 定时日常关心：到点后这么久内都算“该提了”（勿扰/离线过后补提），超过就算今天错过

QUEST_PROACTIVE_SOURCE = "quest_proactive"
QUEST_JUDGE_SOURCE = "quest_judge"
# 小歪就一个主线小目标开口后，这么久还没标结果 → 催语音 Agent 只做判定、只调工具（每次开口最多催一次）
QUEST_JUDGE_NUDGE_SEC = 45.0
QUEST_JUDGE_QUIET_SEC = 8.0  # 催的时候得没人在说话

# 进程内唯一的主动推进循环（start() 时登记），供控制台读状态 / 立即试一句。
_active_loop: "QuestProactiveLoop | None" = None
_active_asyncio_loop: asyncio.AbstractEventLoop | None = None


def _idle_label(idle_sec: float) -> str:
    sec = max(1, int(round(float(idle_sec or 0))))
    if sec % 60 == 0:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


def _opener(reason: str, idle_sec: float, *, voice: bool) -> str:
    """开口的由头：冷场 / 主人点了触发一次 / 定时到点。不再判断人在不在面前（人脸相关已去掉）。"""
    idle = _idle_label(idle_sec)
    if reason == "scheduled":
        return "到了主人定的时间，现在请你主动提这件事：" if voice else "到了主人定的时间，现在要主动提这件事："
    if reason == "manual":
        return (
            "主人在控制台点了「触发一次」，现在请你就这个小目标开口："
            if voice else "主人在控制台点了「触发一次」，现在就和主人聊这个小目标："
        )
    if reason == "chain":
        return (
            "上一个小目标刚有了结果，现在请你接着聊下一个小目标（顺着刚才的话，不要重新打招呼）："
            if voice else "上一个小目标刚有了结果，现在接着推进下一个小目标（顺着刚才的话，不要重新打招呼）："
        )
    return (
        f"主人约 {idle}没有和你说话，不确定他在不在旁边。现在请你轻轻地主动开口，推进这个小目标（没人回应就别追问）："
        if voice else f"主人约 {idle}没有和你对话，不确定他在不在旁边；轻轻地主动开口推进剧情任务（没人回应就别追问）："
    )


def build_user_text(
    task: dict[str, Any],
    *,
    idle_sec: float = QUEST_ATTEMPT_IDLE_SEC,
    performed: str = "",
    reason: str = "idle",
) -> str:
    """构造剧情推进指令（以系统前缀开头，chat_flow 强制本轮开口）。"""
    parts = [
        f"{_QUEST_PROACTIVE_PREFIX} {_opener(reason, idle_sec, voice=False)}"
        f"[{task.get('task_id')}] {task.get('title') or 'notitle'}",
    ]
    for key, label in (
        ("goal", "目标"),
        ("strategy", "策略"),
        ("trigger_hint", "额外触发时机"),
        ("success_condition", "达成条件"),
    ):
        val = str(task.get(key) or "").strip()
        if val:
            parts.append(f"  {label}：{val}")
    attempts = int(task.get("attempt_count") or 0)
    if attempts:
        parts.append(f"  这个目标你已经主动提过 {attempts} 次，这次换个角度、更轻一点。")
    if task.get("kind") != "care":
        parts.append(
            "  聊完这一步必须调用 update_task_result 标结果：按达成条件和主人的回复判断，达成 success、没达成 unmet；"
            "主人明确说不想要就 skip_goal。不标结果就不会进入下一步。"
        )
    if task.get("next_title"):
        parts.append(
            f"  标了结果、大家安静 {int(task.get('next_delay_sec') or 0)} 秒后会进入下一步「{task['next_title']}」，不要反复纠缠。"
        )
    if performed:
        parts.append(f"  你刚刚已经{performed}，接着自然地开口即可，不要再重复打招呼。")
    if task.get("kind") == "care":
        parts.append("  这是一件日常关心的小事：一句话提一下就好，主人说不用就调用 skip_goal（会暂停，之后不再提）。")
    if task.get("scheduled_hit"):
        parts.append(f"  现在到了主人定的时间（{task['scheduled_hit']}），这次一定要提，不用再判断条件。")
    parts.append(
        "要求：need_reply 必须为 true，tts 写直接说给主人听的一两句话（提问 / 请求配合 / 接着上次的话题），"
        "口语、简短、不要生硬地念任务；禁止写「已发送」「已汇报」等汇报语；"
        "若依据已掌握的信息能明确判断成功条件或失败条件满足，直接调用 update_task_result 判定终态并简短口播结论；"
        "主人表示不想聊这个、别再问了，就先调用 update_task_strategy 记下「主人不想聊这个」，之后不再主动提；"
        "若任务暂无法推进，就自然地把话题引向下一个进行中任务或闲聊，不要让对话冷场。"
    )
    return "\n".join(parts)


def build_rtc_instruction(
    task: dict[str, Any],
    *,
    idle_sec: float = QUEST_ATTEMPT_IDLE_SEC,
    performed: str = "",
    reason: str = "idle",
) -> str:
    """给语音 Agent 的「现在请你说」指令：口语、直接说，不要 JSON 格式。"""
    parts = [
        f"{_opener(reason, idle_sec, voice=True)}"
        f"[{task.get('task_id')}] {task.get('title') or 'notitle'}。"
    ]
    for key, label in (
        ("goal", "目标"),
        ("strategy", "怎么做"),
        ("trigger_hint", "额外触发时机"),
        ("success_condition", "达成条件"),
    ):
        val = str(task.get(key) or "").strip()
        if val:
            parts.append(f"{label}：{val}。")
    attempts = int(task.get("attempt_count") or 0)
    if attempts:
        parts.append(f"这个目标你已经主动提过 {attempts} 次，这次换个角度、更轻一点。")
    if task.get("next_title"):
        parts.append(
            f"你标了结果、大家安静 {int(task.get('next_delay_sec') or 0)} 秒后会进入下一步「{task['next_title']}」，不用反复纠缠。"
        )
    if performed:
        parts.append(f"你刚刚已经{performed}，接着自然地开口即可，不要再重复打招呼。")
    if task.get("kind") == "care":
        parts.append("这是一件日常关心的小事：一句话提一下就好，主人说不用就调用 skip_goal，之后不再提。")
    if task.get("scheduled_hit"):
        parts.append(f"现在到了主人定的时间（{task['scheduled_hit']}），这次一定要提，不用再判断条件。")
    parts.append(
        "只说一两句、口语、像顺口聊起，不要生硬念任务；"
        "主人回答后按达成条件判断，达成就调用 update_task_result 标 success、没达成就标 unmet——聊完一定要标，不标不会进入下一步；"
        "主人说不想聊就调用 skip_goal，之后不再提。"
    )
    return " ".join(parts)


def build_judge_instruction(task: dict[str, Any]) -> str:
    """催语音 Agent 给一个主线小目标标结果：只判定、只调工具、不说话。"""
    cond = str(task.get("success_condition") or "").strip() or "主人回应了就算达成"
    return (
        f"刚才你就小目标「{task.get('title') or task.get('task_id')}」主动开了口（目标：{task.get('goal') or ''}）。"
        f"现在请你只做判定、不要出声：按达成条件「{cond}」和主人在那之后的回应来判断——没有任何回应就按没达成——"
        f"调用 update_task_result（task_id={task.get('task_id')}，达成填 success、没达成填 unmet，result 写一句原因）；"
        "主人明确说过不要这个就改调 skip_goal。只调用工具，不要说话，不要再问主人。"
    )


def _displayed_state(device_id: str) -> str:
    """小歪现在的状态（表情运行时显示态：idle / listening / thinking / speaking）；读不到当 idle。"""
    runtime = get_expression_runtime(device_id)
    if runtime is None:
        return "idle"
    try:
        return str(runtime.snapshot().get("displayed_state") or "idle")
    except Exception:  # noqa: BLE001
        return "idle"


def registry_activity_ts(registry: Any, device_id: str) -> float:
    """设备最后一次交互状态变化的时间戳（秒），无记录 → 0。"""
    try:
        rows = registry.snapshot()
    except Exception:  # noqa: BLE001
        return 0.0
    for row in rows:
        if str(row.get("device_id") or "") == device_id:
            if str(row.get("interaction_state") or "").upper() == "LISTENING":
                # 设备 VAD 边沿翻出来的 LISTENING 只说明"麦克风听到了动静"（噪声也算），不当作主人在说话；
                # 真对话紧接着就是 THINKING / SPEAKING（文字链路）或语音 Agent 的忙态 / 主人回话记录。
                return 0.0
            try:
                return float(row.get("interaction_state_ts") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


class QuestProactiveRunner:
    """对设备发起一轮「剧情推进」对话。

    ``attempt(device_id) -> bool`` 返回是否已发起尝试（False = 无可推进任务 /
    设备离线 / 功能关闭，由调度层做空转冷却）；内部异常兜底记日志，不向上抛。
    """

    def __init__(
        self,
        *,
        chat: Any,
        asr_chat_hub: Any,
        registry: Any,
        dp_broker: Any,
        daily_limit_provider: Optional[Callable[[], int]] = None,
        idle_sec_provider: Optional[Callable[[], float]] = None,
        task_retry_sec: float | None = None,
        care_daily_limit_provider: Optional[Callable[[], int]] = None,
        activity_ts_provider: Optional[Callable[[str], float]] = None,
    ) -> None:
        self._chat = chat
        # 主人最近一次真的在说话（只记主人侧）：上次主动开口后主人回应过，那次就不算「没回应」
        self._user_activity_ts = activity_ts_provider or user_activity.last_ts
        self._hub = asr_chat_hub
        self._registry = registry
        self._broker = dp_broker
        self._daily_limit = daily_limit_provider or quest_service.proactive_daily_limit
        self._care_daily_limit = care_daily_limit_provider or quest_service.care_daily_limit
        self._idle_sec = idle_sec_provider or quest_service.proactive_idle_sec
        self._care_today_count: int = 0
        # 同一小目标再提间隔：构造时给定则固定（测试用），否则每次现读偏好 quest.task_retry_sec
        self._task_retry_fixed = None if task_retry_sec is None else max(0.0, float(task_retry_sec))
        self.last_turn: dict[str, Any] | None = None
        self.last_skip_reason: str = ""
        self._task_last_attempt: dict[str, float] = {}
        self._today: str = ""
        self._today_count: int = 0
        self.last_spoke_at: float = 0.0

    # ── 节流状态 ──────────────────────────────────────────────

    def _roll_day(self, now: float | None = None) -> None:
        day = local_datetime(now).strftime("%Y-%m-%d")  # 按偏好时区翻天，和定时日常关心一个钟
        if day != self._today:
            self._today = day
            self._today_count = 0
            self._care_today_count = 0

    def restore_state(self) -> None:
        """Core 重启后接着数：今天开口次数 / 各小目标上次提的时间（跨天的记数不要）。"""
        try:
            state = dict(quest_service.load_proactive_state().get("runner") or {})
        except Exception:  # noqa: BLE001
            return
        last = state.get("task_last_attempt")
        if isinstance(last, dict):
            self._task_last_attempt = {
                str(k): float(v) for k, v in last.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        try:
            self.last_spoke_at = float(state.get("last_spoke_at") or 0.0)
        except (TypeError, ValueError):
            self.last_spoke_at = 0.0
        self._roll_day()
        if str(state.get("day") or "") == self._today:
            self._today_count = int(state.get("today_count") or 0)
            self._care_today_count = int(state.get("care_today_count") or 0)

    def _persist_state(self) -> None:
        try:
            quest_service.save_proactive_state(
                runner={
                    "day": self._today,
                    "today_count": self._today_count,
                    "care_today_count": self._care_today_count,
                    "task_last_attempt": dict(self._task_last_attempt),
                    "last_spoke_at": self.last_spoke_at,
                }
            )
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] persist runner state failed", exc_info=True)

    def today_count(self) -> int:
        self._roll_day()
        return self._today_count

    def stats(self) -> dict[str, Any]:
        return {
            "today_count": self.today_count(),
            "care_today_count": self._care_today_count,
            "task_last_attempt": dict(self._task_last_attempt),
            "task_retry_sec": self._retry_sec(),
            "last_spoke_at": self.last_spoke_at or None,
            "last_skip_reason": self.last_skip_reason,
            "last_task_id": (self.last_turn or {}).get("task_id"),
        }

    def _retry_sec(self) -> float:
        if self._task_retry_fixed is not None:
            return self._task_retry_fixed
        try:
            return max(0.0, float(quest_service.task_retry_sec()))
        except Exception:  # noqa: BLE001
            return QUEST_TASK_RETRY_SEC

    def story_task_due(self, task: dict[str, Any]) -> bool:
        """主线小目标这一轮还没提过：从没提过，或上次提是在这轮开始（重开 / 定时检测重新计时）之前。"""
        tid = str(task.get("task_id") or "")
        last = float(self._task_last_attempt.get(tid, 0.0) or 0.0)
        if last <= 0:
            return True
        started = float(task.get("started_at_ts") or 0.0)
        return started > 0 and last < started

    def pick_task(self, tasks: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any] | None:
        """挑一个进行中任务：主线优先（这一轮还没提过的；提过就等小歪标结果 / 等定时检测），
        主线没得提才轮到日常关心，且日常关心受每日名额限制、两次之间至少隔它自己的频率。"""
        now = time.time() if now is None else now
        story = [t for t in tasks if t.get("kind") != "care"]
        care = [t for t in tasks if t.get("kind") == "care"]
        retry = self._retry_sec()
        for task in story:
            if self.story_task_due(task):
                return task
        try:
            care_limit = int(self._care_daily_limit())
        except Exception:  # noqa: BLE001
            care_limit = 2
        self._roll_day(now)
        if care_limit > 0 and self._care_today_count >= care_limit:
            return None
        for task in care:
            if task.get("schedule_time"):
                continue  # 定时的只在到点时由循环按钟表叫，不在冷场时顺带提
            tid = str(task.get("task_id") or "")
            last = self._task_last_attempt.get(tid, 0.0)
            gap = max(float(task.get("repeat_interval_sec") or 0), retry)
            if last <= 0 or (now - last) >= gap:
                return task
        return None

    async def attempt(
        self, device_id: str, *, ignore_limits: bool = False, task_id: str | None = None, scheduled_hit: str = "",
        chain: bool = False,
    ) -> bool:
        """发起一轮主动开口。``chain``：上一个小目标刚有了结果、接着提下一个（由头不同，仍占每日名额）。"""
        dev = str(device_id or "").strip()
        if not dev:
            return False
        want = str(task_id or "").strip()
        try:
            enabled = await asyncio.to_thread(quest_service.proactive_enabled)
            if not enabled and not ignore_limits:
                self.last_skip_reason = "disabled"
                return False
            tasks = await asyncio.to_thread(quest_service.get_current_tasks, dev)
            if not tasks:
                self.last_skip_reason = "no_task"
                return False
            if not ignore_limits:
                try:
                    limit = int(await asyncio.to_thread(self._daily_limit))
                except Exception:  # noqa: BLE001
                    limit = 0
                if limit > 0 and self.today_count() >= limit:
                    self.last_skip_reason = "daily_limit"
                    return False
            if want:
                task = next((t for t in tasks if str(t.get("task_id")) == want), None)
                if task is None:
                    self.last_skip_reason = "task_not_running"
                    return False
                if scheduled_hit:
                    task = {**task, "scheduled_hit": scheduled_hit}
            else:
                task = tasks[0] if ignore_limits else self.pick_task(tasks)
            if task is None:
                self.last_skip_reason = "task_cooldown"
                return False
            ws = await self._hub.first_ws(dev)
            if ws is None:
                logger.info("[quest_proactive] 设备离线，跳过 device_id=%s", dev)
                self.last_skip_reason = "offline"
                return False
            try:
                idle_sec = float(await asyncio.to_thread(self._idle_sec))
            except Exception:  # noqa: BLE001
                idle_sec = QUEST_ATTEMPT_IDLE_SEC
            reason = "scheduled" if scheduled_hit else ("manual" if ignore_limits else ("chain" if chain else "idle"))
            prev_attempt_at = float(self._task_last_attempt.get(str(task.get("task_id") or ""), 0.0))
            req_id = uuid.uuid4().hex[:16]
            downlink = WsDownlinkAdapter(
                ws,
                settings=self._chat.settings,
                device_id=dev,
                dp_broker=self._broker,
            )
            events = WsPipelineEventsAdapter(self._broker, self._registry)
            t0 = asyncio.get_event_loop().time()
            perform = dict(task.get("perform") or {})
            user_text = build_user_text(task, idle_sec=idle_sec, reason=reason)

            if rtc_instructions.agent_attached(dev):
                # 语音 Agent 在线：先在设备 lane 上做表情 / 演表演，再把「说什么」交给它，
                # 这样这句话进它的对话历史，主人回话能接上。
                async def _perform_then_hand_over():
                    performed = await self._perform(downlink, dev, perform, req_id)
                    text = build_rtc_instruction(task, idle_sec=idle_sec, performed=performed, reason=reason)
                    rtc_instructions.enqueue(dev, text, source=QUEST_PROACTIVE_SOURCE)
                    return performed

                submitted = device_turn_arbiter.submit_if_idle(
                    dev,
                    _perform_then_hand_over,
                    source=QUEST_PROACTIVE_SOURCE,
                    priority=PRIORITY_AUTOMATION,
                    preemptible=True,
                )
                if submitted is None:
                    logger.info("[quest_proactive] 对话通道忙，跳过 device_id=%s", dev)
                    self.last_skip_reason = "busy"
                    return True
                self._count_attempt(task, ignore_limits)
                _entry, hand_task = submitted
                try:
                    await hand_task
                except asyncio.CancelledError:
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
                    return True
                await self._after_attempt(task, dev, prev_attempt_at, count=reason != "manual")
                self.last_turn = {
                    "task_id": task.get("task_id"),
                    "request_id": req_id,
                    "voice_ok": True,
                    "status": "handed_to_rtc_agent",
                    "error": None,
                    "at": time.time(),
                }
                logger.info(
                    "[quest_proactive] task_id=%s device_id=%s req=%s 已交给语音 Agent 开口",
                    task.get("task_id"), dev, req_id,
                )
                return True

            async def _run_turn():
                # 执行方式：先做表情 / 先演一段表演，再让模型开口（同一条 lane 内串行）
                performed = await self._perform(downlink, dev, perform, req_id)
                text = build_user_text(task, idle_sec=idle_sec, performed=performed, reason=reason) if performed else user_text
                return await run_chat_turn(
                    downlink,
                    self._chat,
                    text,
                    request_id=req_id,
                    device_id=dev,
                    registry=self._registry,
                    t_asr_text=t0,
                    force_voice=True,
                    make_session_current=False,
                )

            submitted = device_turn_arbiter.submit_if_idle(
                dev,
                _run_turn,
                source=QUEST_PROACTIVE_SOURCE,
                priority=PRIORITY_AUTOMATION,
                preemptible=True,
            )
            if submitted is None:
                logger.info("[quest_proactive] 对话通道忙，跳过 device_id=%s", dev)
                self.last_skip_reason = "busy"
                return True
            self._count_attempt(task, ignore_limits)
            _entry, turn_task = submitted
            try:
                turn = await turn_task
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
                logger.info("[quest_proactive] 主动轮被用户抢占 device_id=%s", dev)
                return True
            await publish_chat_turn(
                events,
                dev,
                source=QUEST_PROACTIVE_SOURCE,
                asr_text=user_text,
                t_asr_start=t0,
                t_asr_text=t0,
                turn=turn,
                request_id=req_id,
            )
            await self._after_attempt(task, dev, prev_attempt_at, count=reason != "manual")
            voice_ok = _voice_was_played(turn)
            self.last_turn = {
                "task_id": task.get("task_id"),
                "request_id": req_id,
                "voice_ok": voice_ok,
                "status": turn.status,
                "error": turn.error,
                "at": time.time(),
            }
            log = logger.warning if not voice_ok else logger.info
            log(
                "[quest_proactive] task_id=%s device_id=%s req=%s voice_ok=%s status=%s summary=%s",
                task.get("task_id"),
                dev,
                req_id,
                voice_ok,
                turn.status,
                safe_log_content((turn.llm_text or turn.error or "")[:120]),
            )
            return True
        except Exception:
            logger.exception("[quest_proactive] 主动轮异常 device_id=%s", device_id)
            return True  # 异常视为已尝试，避免调度层立即重试


    def _count_attempt(self, task: dict[str, Any], ignore_limits: bool) -> None:
        """记一次开口：定时到点 / 主人点「触发一次」不占每日名额（ignore_limits），只记时间。"""
        self._roll_day()
        if not ignore_limits:
            self._today_count += 1
            if task.get("kind") == "care":
                self._care_today_count += 1
        self.last_spoke_at = time.time()
        self._task_last_attempt[str(task.get("task_id") or "")] = self.last_spoke_at
        self.last_skip_reason = ""
        self._persist_state()

    async def _perform(self, downlink: Any, dev: str, perform: dict[str, Any], req_id: str) -> str:
        """按执行方式先做表情 / 演表演；失败只记日志不阻塞开口。返回给模型的一句说明。"""
        mode = str(perform.get("mode") or "")
        try:
            if mode == PERFORM_EXPRESSION and perform.get("expression"):
                runtime = get_expression_runtime(dev)
                if runtime is None:
                    return ""
                res = await runtime.play_scene(
                    perform["expression"],
                    source=QUEST_PROACTIVE_SOURCE,
                    priority=25,
                    reason=f"quest:{req_id}",
                    wait_for_played=True,
                )
                return f"做了一个「{perform['expression']}」的表情" if getattr(res, "ok", False) else ""
            if mode == PERFORM_SCENE and perform.get("scene"):
                rows = await asyncio.to_thread(load_scene_playbooks_file) or []
                pb = find_playbook_by_name(rows, str(perform["scene"]))
                if pb is None:
                    logger.warning("[quest_proactive] 表演不存在 scene=%s", perform["scene"])
                    return ""
                res = await run_device_playbook(downlink, self._chat, pb, request_id=f"{req_id}_pf", device_id=dev)
                ok = (getattr(res, "status", "ok") or "ok") == "ok" and not getattr(res, "error", None)
                return f"演了一段「{pb.get('title') or pb.get('name')}」" if ok else ""
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[quest_proactive] perform 失败 mode=%s", mode, exc_info=True)
        return ""

    async def _after_attempt(
        self, task: dict[str, Any], device_id: str = "", prev_attempt_at: float = 0.0, *, count: bool = True
    ) -> None:
        """日常关心：记一次「没回应」，连续到了上限 → 歇一天。
        上次主动开口之后主人说过话 → 之前那些不算「没回应」，先清零再记这一次。
        主人在控制台点的「触发一次」不算（count=False）：测两下不该把日常关心测暂停。
        主线小目标不数次数：达成 / 未达成 / 跳过只由小歪或主人明确标。"""
        playbook = str(task.get("playbook") or "")
        task_id = str(task.get("task_id") or "")
        if not playbook or not task_id or not count or task.get("kind") != "care":
            return
        try:
            if prev_attempt_at > 0 and device_id:
                try:
                    replied_at = float(self._user_activity_ts(device_id) or 0.0)
                except Exception:  # noqa: BLE001
                    replied_at = 0.0
                if replied_at > prev_attempt_at:
                    await asyncio.to_thread(
                        quest_service.reset_attempts, quest_service.LOCAL_PROFILE_DEVICE, playbook, task_id
                    )
                    logger.info("[quest_proactive] task %s 上次开口后主人回应过，之前的次数不算没回应", task_id)
            await asyncio.to_thread(
                quest_service.record_attempt, quest_service.LOCAL_PROFILE_DEVICE, playbook, task_id
            )
            out = await asyncio.to_thread(
                quest_service.pause_care_if_missed, quest_service.LOCAL_PROFILE_DEVICE, playbook, task_id
            )
            if out:
                logger.info("[quest_proactive] care %s 连续没回应到上限，先歇一天", task_id)
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] after_attempt failed", exc_info=True)


class QuestProactiveLoop:
    """冷场检查循环：5 s 一查，满足条件则交给 runner 发起一轮。"""

    def __init__(
        self,
        runner: Any,
        *,
        asr_chat_hub: Any,
        registry: Any,
        check_interval_sec: float = QUEST_CHECK_SEC,
        idle_sec: float = QUEST_ATTEMPT_IDLE_SEC,
        cooldown_sec: float = QUEST_NO_TASK_COOLDOWN_SEC,
        activity_ts_provider: Optional[Callable[[str], float]] = None,
        enabled_provider: Optional[Callable[[], bool]] = None,
        quiet_hours_provider: Optional[Callable[[], bool]] = None,
        device_provider: Optional[Callable[[], Awaitable[str | None]]] = None,
        idle_sec_provider: Optional[Callable[[], float]] = None,
        gate: Optional[Callable[[str, str], tuple[bool, str]]] = None,
        runtime_state_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._runner = runner
        # 三个主动源的统一门禁（提醒快到点时让路）；测试可注入
        self._gate = gate or can_be_proactive
        self._hub = asr_chat_hub
        self._registry = registry
        self._check_interval = max(0.5, float(check_interval_sec))
        self._idle_sec = max(1.0, float(idle_sec))
        # 偏好里的冷场时长优先（每次 tick 现读，改了立即生效）；测试可传固定值
        self._idle_provider = idle_sec_provider
        self._cooldown_sec = max(1.0, float(cooldown_sec))
        self._activity_ts = activity_ts_provider or (
            lambda dev: max(
                registry_activity_ts(registry, dev),
                device_turn_arbiter.last_activity_ts(dev, exclude=(QUEST_PROACTIVE_SOURCE,)),
                user_activity.last_ts(dev),  # 语音 Agent 回传的主人真话（VAD 误报不会记到这里）
            )
        )
        self._enabled = enabled_provider or _default_enabled
        self._quiet_hours = quiet_hours_provider or quiet_hours_active
        # 小歪自己在说 / 在想也算「有人在说话」：表情运行时的显示状态（idle 以外都算忙）；测试可注入
        self._runtime_state = runtime_state_provider or _displayed_state
        self._busy_ts: dict[str, float] = {}
        # 催标结果：记住已经催过哪一次开口（runner.last_turn["at"]），一次开口只催一次
        self._judged_at: float = 0.0
        # hub 按鸭子类型在 tick 时才取用（启动装配阶段允许传占位对象）
        self._device = device_provider
        self._task: asyncio.Task | None = None
        # 定时检测：每个主线场景上次检测的时间（wall clock），落在状态文件里跨重启接着算
        self._pass_at: dict[str, float] = {}
        self._passes_loaded = False
        self._inflight: set[str] = set()
        self._next_ok: dict[str, float] = {}
        self._recovered: set[str] = set()  # 每台设备启动后只做一次「重启前提过但没标结果」的恢复
        self._first_seen: dict[str, float] = {}
        self._last_attempt: dict[str, float] = {}
        # 定时日常关心：task_id → 今天已触发的日期（YYYY-MM-DD），一天只按时叫一次
        self._scheduled_fired: dict[str, str] = {}
        self._workers: set[asyncio.Task] = set()
        self.last_skip_reason: str = ""

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self) -> None:
        global _active_loop, _active_asyncio_loop
        if self._task is not None and not self._task.done():
            return
        try:
            if ensure_default_playbook():
                logger.info("[quest_proactive] 已写入随包默认场景")
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] ensure_default_playbook failed", exc_info=True)
        try:
            self._scheduled_fired.update(quest_service.load_scheduled_fired())  # 重启后今天叫过的不再叫
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] load_scheduled_fired failed", exc_info=True)
        restore = getattr(self._runner, "restore_state", None)
        if callable(restore):
            try:
                restore()  # 今天的开口次数 / 再提间隔也接着算
            except Exception:  # noqa: BLE001
                logger.debug("[quest_proactive] restore runner state failed", exc_info=True)
        self._task = asyncio.create_task(self._loop(), name="quest_proactive_loop")
        _active_loop = self
        _active_asyncio_loop = asyncio.get_running_loop()
        logger.info(
            "[quest_proactive] 冷场推进循环已启动 check=%.1fs idle>=%.0fs cooldown=%.0fs",
            self._check_interval,
            self._idle_sec,
            self._cooldown_sec,
        )

    async def stop(self) -> None:
        global _active_loop, _active_asyncio_loop
        if _active_loop is self:
            _active_loop = None
            _active_asyncio_loop = None
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        workers = tuple(self._workers)
        for w in workers:
            if not w.done():
                w.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.difference_update(workers)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[quest_proactive] tick 异常")

    # ── 判定 ──────────────────────────────────────────────────

    # ── 接下一个 / 定时检测 ──

    def _load_passes(self) -> None:
        if self._passes_loaded:
            return
        self._passes_loaded = True
        try:
            raw = quest_service.load_proactive_state().get("passes")
        except Exception:  # noqa: BLE001
            raw = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self._pass_at[str(k)] = float(v)

    def _persist_passes(self) -> None:
        try:
            quest_service.save_proactive_state(passes=dict(self._pass_at))
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] save passes failed", exc_info=True)

    def next_pass_at(self, playbook: str) -> float | None:
        """下次定时检测的时间（wall clock）；没登记过 → None。"""
        self._load_passes()
        last = self._pass_at.get(playbook)
        if last is None:
            return None
        try:
            hours = int(quest_service.check_interval_hours(playbook))
        except Exception:  # noqa: BLE001
            hours = 1
        return last + hours * 3600.0

    async def _maybe_begin_pass(self, device_id: str, now: float) -> str:
        """到了目标检测间隔 → 检测目标完成情况：进行中的重新计时、没有进行中的按顺序重开第一个未达成的。
        返回本次做了什么（"" = 没到点）。"""
        self._load_passes()
        try:
            playbook = str(await asyncio.to_thread(quest_service.bound_playbook) or "")
        except Exception:  # noqa: BLE001
            return ""
        if not playbook:
            return ""
        last = self._pass_at.get(playbook)
        if last is None:
            # 启动 / 刚换的场景：从现在起算，第一轮靠冷场规则和「接下一个」自己走
            self._pass_at[playbook] = now
            self._persist_passes()
            return ""
        try:
            hours = int(await asyncio.to_thread(quest_service.check_interval_hours, playbook))
        except Exception:  # noqa: BLE001
            hours = 1
        if now - last < hours * 3600.0:
            return ""
        self._pass_at[playbook] = now
        self._persist_passes()
        try:
            out = await asyncio.to_thread(quest_service.begin_pass, quest_service.LOCAL_PROFILE_DEVICE, playbook)
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] begin_pass failed", exc_info=True)
            return "pass_error"
        what = "reopened" if out.get("reopened") else ("touched" if out.get("touched") else "nothing")
        logger.info("[quest_proactive] 定时检测 playbook=%s %s=%s", playbook, what, out.get("reopened") or out.get("touched") or "-")
        return what

    async def _recover_unjudged_after_restart(self, device_id: str) -> None:
        """Core 重启前小歪已就某个主线小目标开口、但还没标结果（催判定那一轮随进程一起没了，
        语音 Agent 的对话上下文也没了，没法再判）：这轮当作没提过，到下一个冷场窗口重新问一遍。
        否则要等下一次定时检测（默认 1 小时）才会再提——2026-09-14 目标 5 就这样卡了近一小时。"""
        attempts = getattr(self._runner, "_task_last_attempt", None)
        if not isinstance(attempts, dict):
            return
        try:
            tasks = await asyncio.to_thread(quest_service.get_current_tasks, device_id)
        except Exception:  # noqa: BLE001
            return
        changed = False
        for task in tasks:  # get_current_tasks 只返回进行中的任务
            if task.get("kind") == "care":
                continue
            tid = str(task.get("task_id") or "")
            last = float(attempts.get(tid, 0.0) or 0.0)
            started = float(task.get("started_at_ts") or 0.0)
            if tid and last > 0 and started > 0 and last >= started:
                attempts[tid] = 0.0
                changed = True
                logger.info("[quest_proactive] 重启前已提过但没标结果，这轮重新问 task_id=%s", tid)
        if changed:
            persist = getattr(self._runner, "_persist_state", None)
            if callable(persist):
                persist()

    async def _maybe_nudge_judge(self, device_id: str, now: float) -> bool:
        """小歪就主线小目标开口后 QUEST_JUDGE_NUDGE_SEC 还没标结果（也没人在说话）→ 催语音 Agent 只做判定、只调工具。
        一次开口只催一次；催了还不标就等定时检测再提。返回是否催了。"""
        last = getattr(self._runner, "last_turn", None) or {}
        at = float(last.get("at") or 0.0)
        task_id = str(last.get("task_id") or "")
        if not task_id or at <= 0 or at == self._judged_at or last.get("status") != "handed_to_rtc_agent":
            return False
        if now - at < QUEST_JUDGE_NUDGE_SEC or self.quiet_seconds(device_id, now=now) < QUEST_JUDGE_QUIET_SEC:
            return False
        if not rtc_instructions.agent_attached(device_id):
            return False
        try:
            tasks = await asyncio.to_thread(quest_service.get_current_tasks, device_id)
        except Exception:  # noqa: BLE001
            return False
        task = next((t for t in tasks if str(t.get("task_id")) == task_id and t.get("kind") != "care"), None)
        self._judged_at = at  # 不论催不催，这次开口只看一回
        if task is None or float(task.get("started_at_ts") or 0.0) > at:
            return False  # 已经标了结果 / 重开过了：不用催
        text = build_judge_instruction(task)
        try:
            rtc_instructions.enqueue(
                device_id, text, source=QUEST_JUDGE_SOURCE, ttl_sec=120.0,
                tool_choice="required", tools=["update_task_result", "skip_goal"],
            )
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] judge nudge enqueue failed", exc_info=True)
            return False
        logger.info("[quest_proactive] 开口 %.0fs 还没标结果，催语音 Agent 判定 task_id=%s", now - at, task_id)
        return True

    async def _check_chain(self, device_id: str, now: float) -> str | None:
        """「接下一个」标记：大家安静 delay_sec 后触发顺序里的下一个。
        返回 None = 没有标记，走常规流程；"" = 已发起；其它 = 跳过原因（本拍不走常规流程）。"""
        try:
            chain = await asyncio.to_thread(quest_service.pending_chain)
        except Exception:  # noqa: BLE001
            return None
        if chain is None:
            return None
        try:
            bound = str(await asyncio.to_thread(quest_service.bound_playbook) or "")
        except Exception:  # noqa: BLE001
            bound = ""
        if chain["playbook"] != bound:
            await asyncio.to_thread(quest_service.clear_chain)  # 场景换了：标记作废
            return None
        if self.quiet_seconds(device_id, now=now) < float(chain["delay_sec"]):
            return "chain_wait"
        try:
            quiet = await asyncio.to_thread(self._quiet_hours)
        except Exception:  # noqa: BLE001
            quiet = False
        if quiet:
            return "quiet_hours"
        try:
            allowed, why = await asyncio.to_thread(self._gate, SOURCE_QUEST, device_id)
        except Exception:  # noqa: BLE001
            allowed, why = True, ""
        if not allowed:
            return why or "gated"
        try:
            next_id = await asyncio.to_thread(
                quest_service.chain_next_task, quest_service.LOCAL_PROFILE_DEVICE, chain["playbook"], chain["after"]
            )
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] chain_next_task failed", exc_info=True)
            next_id = None
        await asyncio.to_thread(quest_service.clear_chain)
        if not next_id:
            logger.info("[quest_proactive] 「%s」之后没有要接的小目标，这一轮到头了", chain["after"])
            return "chain_end"
        self._inflight.add(device_id)
        self._last_attempt[device_id] = now
        worker = asyncio.create_task(
            self._run_attempt(device_id, started_at=now, chain_task_id=next_id),
            name=f"quest_chain_{device_id[:8]}",
        )
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        logger.info("[quest_proactive] 接下一个 task_id=%s（安静 %ss 后）", next_id, chain["delay_sec"])
        return ""

    # 只有小歪自己在说 / 在想才算忙；「倾听」是 VAD 边沿翻出来的脸，噪声也能触发且可能挂很久（2026-09-11 12:47），
    # 主人真的在说话由 activity_ts（registry / arbiter / user_activity）负责。
    _BUSY_STATES = frozenset({"speaking", "thinking"})

    def _note_busy(self, device_id: str, now: float) -> str:
        """每拍看一眼小歪自己的状态：在说 / 在想记为「此刻有人在说话」。返回状态。"""
        try:
            state = str(self._runtime_state(device_id) or "idle")
        except Exception:  # noqa: BLE001
            state = "idle"
        if state in self._BUSY_STATES:
            self._busy_ts[device_id] = now
        return state

    def quiet_seconds(self, device_id: str, *, now: float | None = None) -> float:
        """距最后一轮对话（主人说话、小歪说话 / 思考、本循环首次看见设备、上次主动轮）的秒数。"""
        now = time.time() if now is None else now
        last = max(
            float(self._activity_ts(device_id) or 0.0),
            float(self._last_attempt.get(device_id, 0.0)),
            float(self._busy_ts.get(device_id, 0.0)),
        )
        if last <= 0:
            first = self._first_seen.setdefault(device_id, now)
            last = first
        return max(0.0, now - last)

    async def tick(self, *, now: float | None = None) -> str:
        """执行一次检查；返回跳过原因（"" = 已发起尝试），便于测试与状态面板。"""
        now = time.time() if now is None else now
        reason = await self._check(now)
        self.last_skip_reason = reason
        return reason

    async def _check(self, now: float) -> str:
        try:
            enabled = await asyncio.to_thread(self._enabled)
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] enabled check failed", exc_info=True)
            return "prefs_error"
        if not enabled:
            return "disabled"
        device_id = str(await self._connected_device() or "").strip()
        if not device_id:
            return "offline"
        if device_id not in self._recovered:
            self._recovered.add(device_id)
            await self._recover_unjudged_after_restart(device_id)
        self._note_busy(device_id, now)
        mono = time.monotonic()
        if mono < self._next_ok.get(device_id, 0.0):
            return "cooldown"
        if device_id in self._inflight:
            return "inflight"
        await self._maybe_nudge_judge(device_id, now)
        # 定时的日常关心：到点就提，不等冷场、不看人脸、不占每日名额；勿扰时段照旧让路
        due = await self._scheduled_due(device_id, now)
        if due is not None:
            try:
                quiet = await asyncio.to_thread(self._quiet_hours)
            except Exception:  # noqa: BLE001
                quiet = False
            if quiet:
                return "quiet_hours"
            try:
                allowed, why = await asyncio.to_thread(self._gate, SOURCE_QUEST, device_id)
            except Exception:  # noqa: BLE001
                allowed, why = True, ""
            if not allowed:
                return why or "gated"  # 提醒快到点：定时的也让路，宽限期内下个 tick 再提
            self._inflight.add(device_id)
            self._last_attempt[device_id] = now
            self._scheduled_fired[due["task_id"]] = due["date"]
            self._persist_scheduled_fired()
            worker = asyncio.create_task(
                self._run_attempt(
                    device_id, started_at=now, task_id=due["task_id"], scheduled_hit=due["schedule_time"],
                    playbook=str(due.get("playbook") or ""), one_off=bool(due.get("schedule_date")),
                ),
                name=f"quest_scheduled_{device_id[:8]}",
            )
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)
            return ""
        chained = await self._check_chain(device_id, now)
        if chained is not None:
            return chained
        await self._maybe_begin_pass(device_id, now)
        idle = self.quiet_seconds(device_id, now=now)
        if idle < self.effective_idle_sec():
            return "recent_conversation"
        try:
            quiet = await asyncio.to_thread(self._quiet_hours)
        except Exception:  # noqa: BLE001
            quiet = False
        if quiet:
            return "quiet_hours"
        try:
            allowed, why = await asyncio.to_thread(self._gate, SOURCE_QUEST, device_id)
        except Exception:  # noqa: BLE001
            allowed, why = True, ""
        if not allowed:
            return why or "gated"
        self._inflight.add(device_id)
        self._last_attempt[device_id] = now
        worker = asyncio.create_task(
            self._run_attempt(device_id, started_at=now), name=f"quest_attempt_{device_id[:8]}"
        )
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        return ""

    def effective_idle_sec(self) -> float:
        if self._idle_provider is None:
            return self._idle_sec
        try:
            return max(1.0, float(self._idle_provider()))
        except Exception:  # noqa: BLE001
            return self._idle_sec

    async def trigger_now(self, device_id: str | None = None, *, task_id: str | None = None) -> bool:
        """控制台「触发一次」：跳过冷场/人脸/每日上限，直接就某个小目标发起一轮（通道忙则跳过）。"""
        dev = str(device_id or await self._connected_device() or "").strip()
        if not dev:
            return False
        if dev in self._inflight:
            return False
        self._inflight.add(dev)
        try:
            attempt = getattr(self._runner, "attempt")
            try:
                started = await attempt(dev, ignore_limits=True, task_id=task_id)
            except TypeError:
                started = await attempt(dev)
            return bool(started)
        finally:
            self._inflight.discard(dev)
            self._last_attempt[dev] = time.time()

    async def _connected_device(self) -> str | None:
        if self._device is not None:
            return await self._device()
        return await self._hub.first_connected_device_id()

    async def _scheduled_due(self, device_id: str, now: float) -> dict[str, Any] | None:
        """今天到点、还没叫过、且没被暂停的定时日常关心（本地时区，宽限 SCHEDULE_GRACE_SEC）。"""
        try:
            candidates = await asyncio.to_thread(quest_service.scheduled_care_candidates, device_id)
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] scheduled candidates failed", exc_info=True)
            return None
        if not candidates:
            return None
        local_now = local_datetime(now)

        def _quiet_resume_for(at: datetime) -> datetime | None:
            # 到点那一刻若在勿扰窗口里，给出那次窗口的结束时刻：勿扰过后宽限内补提
            try:
                return quiet_hours_resume_at(now=at.replace(tzinfo=None))
            except Exception:  # noqa: BLE001
                return None

        return pick_scheduled_due(candidates, local_now, self._scheduled_fired, quiet_resume_for=_quiet_resume_for)

    async def _run_attempt(
        self, device_id: str, *, started_at: float, task_id: str | None = None, scheduled_hit: str = "", playbook: str = "",
        chain_task_id: str | None = None, one_off: bool = False,
    ) -> None:
        mono0 = time.monotonic()
        try:
            if chain_task_id:
                # 接下一个：占每日名额、不看冷场时长；没说成（离线 / 通道忙 / 名额用完）就交给冷场规则
                started = await self._runner.attempt(device_id, task_id=chain_task_id, chain=True)
                if not started:
                    why = str(getattr(self._runner, "last_skip_reason", "") or "")
                    logger.info("[quest_proactive] 接下一个没说成 task_id=%s reason=%s", chain_task_id, why or "-")
                    self._next_ok[device_id] = time.monotonic() + min(self._cooldown_sec, 15.0)
                return
            if task_id:
                # 定时到点：冷却中的先重开；暂停中的不提（并把今天的标记撤掉，明天再看）
                # 定时的日常关心住在「日常关心」场景里，按候选自带的场景名找
                scene = playbook or quest_service.care_playbook() or quest_service.bound_playbook() or ""
                ready = await asyncio.to_thread(quest_service.prepare_scheduled_task, device_id, scene, task_id)
                if not ready:
                    self._scheduled_fired.pop(task_id, None)
                    self._persist_scheduled_fired()
                    self._next_ok[device_id] = time.monotonic() + self._cooldown_sec
                    return
                started = await self._runner.attempt(device_id, ignore_limits=True, task_id=task_id, scheduled_hit=scheduled_hit)
                if not started or getattr(self._runner, "last_skip_reason", "") == "busy":
                    # 没说成（离线 / 通道忙）：撤掉今天的标记，下个 tick 再试（宽限期内）
                    self._scheduled_fired.pop(task_id, None)
                    self._persist_scheduled_fired()
                    self._next_ok[device_id] = time.monotonic() + min(self._cooldown_sec, 15.0)
                else:
                    logger.info("[quest_proactive] 定时提醒已触发 task_id=%s at=%s one_off=%s", task_id, scheduled_hit, one_off)
                    if one_off:
                        # 一次性提醒：提过就标记，明天不会再来
                        try:
                            await asyncio.to_thread(quest_service.mark_scheduled_done, scene, task_id)
                        except Exception:  # noqa: BLE001
                            logger.warning("[quest_proactive] mark one-off reminder done failed task_id=%s", task_id, exc_info=True)
                return
            started = await self._runner.attempt(device_id)
            if not started:
                self._next_ok[device_id] = time.monotonic() + self._cooldown_sec
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[quest_proactive] attempt 异常 device_id=%s", device_id)
        finally:
            self._inflight.discard(device_id)
            # 以"主动轮结束"为冷场起点（一轮可能持续十几秒）；时间基与 tick 的 now 一致
            self._last_attempt[device_id] = started_at + (time.monotonic() - mono0)

    def _persist_scheduled_fired(self) -> None:
        try:
            quest_service.save_scheduled_fired(dict(self._scheduled_fired))
        except Exception:  # noqa: BLE001
            logger.debug("[quest_proactive] save_scheduled_fired failed", exc_info=True)

    def last_runner_skip_reason(self) -> str:
        return str(getattr(self._runner, "last_skip_reason", "") or self.last_skip_reason or "")

    def snapshot(self) -> dict[str, Any]:
        stats_fn = getattr(self._runner, "stats", None)
        stats = stats_fn() if callable(stats_fn) else {}
        return {
            "running": self._task is not None and not self._task.done(),
            "inflight": sorted(self._inflight),
            "last_skip_reason": self.last_skip_reason or str(stats.get("last_skip_reason") or ""),
            "last_turn": getattr(self._runner, "last_turn", None),
            "idle_sec": self.effective_idle_sec(),
            "today_count": int(stats.get("today_count") or 0),
            "care_today_count": int(stats.get("care_today_count") or 0),
            "task_last_attempt": dict(stats.get("task_last_attempt") or {}),
            "task_retry_sec": float(stats.get("task_retry_sec") or QUEST_TASK_RETRY_SEC),
            "last_spoke_at": stats.get("last_spoke_at"),
            "pass_at": dict(self._pass_at),
        }


def local_datetime(ts: float | None = None) -> datetime:
    """按偏好时区（默认 Asia/Shanghai）换算的本地时间。"""
    try:
        tz = ZoneInfo(preferred_timezone_name())
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(time.time() if ts is None else float(ts), tz)


def pick_scheduled_due(
    candidates: list[dict[str, Any]], local_now: datetime, fired: dict[str, str], *, grace_sec: float = SCHEDULE_GRACE_SEC,
    quiet_resume_for: Callable[[datetime], datetime | None] | None = None,
) -> dict[str, Any] | None:
    """纯函数：从带定时的定时提醒里挑今天该提的一条。

    规则：今天是它设定的星期几（空 = 每天）/ 一次性的只在 schedule_date 那天；本地时间已过 HH:MM
    且不超过宽限；今天还没叫过；没有无限期暂停 / 歇一天中。到点时正处在勿扰窗口的，勿扰结束后
    宽限内补提（``quiet_resume_for(at)`` 给出那次勿扰的结束时刻）。多条同时到点按时间早的先。"""
    due: list[tuple[datetime, dict[str, Any]]] = []
    for c in candidates:
        st = str(c.get("schedule_time") or "")
        if not st or c.get("done_at"):
            continue
        try:
            hh, mm = (int(x) for x in st.split(":"))
        except ValueError:
            continue
        date = str(c.get("schedule_date") or "")
        days = list(c.get("schedule_days") or [])
        task_id = str(c.get("task_id"))
        # 今天这个钟点没到就看昨天那次：晚上 23:00 的提醒赶上勿扰，要在次日勿扰结束后补
        at_today = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        picked: tuple[datetime, str] | None = None
        for at in (at_today, at_today - timedelta(days=1)):
            occ = at.strftime("%Y-%m-%d")
            if fired.get(task_id) == occ:
                continue
            if date:
                if date != occ:
                    continue  # 一次性：只在那一天
            elif days and at.weekday() not in days:
                continue
            delta = (local_now - at).total_seconds()
            if delta < 0:
                continue
            if delta > grace_sec:
                resume = quiet_resume_for(at) if quiet_resume_for is not None else None
                if resume is None:
                    continue
                if resume.tzinfo is None and local_now.tzinfo is not None:
                    resume = resume.replace(tzinfo=local_now.tzinfo)
                elif resume.tzinfo is not None and local_now.tzinfo is None:
                    resume = resume.replace(tzinfo=None)
                after_quiet = (local_now - resume).total_seconds()
                if after_quiet < 0 or after_quiet > grace_sec:
                    continue
            picked = (at, occ)
            break
        if picked is None:
            continue
        at, today = picked
        status = str(c.get("status") or "")
        if status == "failed":
            until = c.get("paused_until")
            if not until:
                continue  # 主人说不用了
            try:
                until_dt = datetime.fromisoformat(str(until))
            except ValueError:
                continue
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=ZoneInfo("UTC"))
            if local_now < until_dt:
                continue  # 歇一天中
        due.append((at, {**c, "date": today}))
    if not due:
        return None
    due.sort(key=lambda x: x[0])
    return due[0][1]


def _default_enabled() -> bool:
    """总开关开着就跑：没选主线场景时日常关心照样生效（它随主动陪伴一直在）。"""
    return quest_service.proactive_enabled() and bool(quest_service.active_playbooks())


# ── 控制台接口（Flask 线程调用）──────────────────────────────


def active_loop() -> "QuestProactiveLoop | None":
    """当前进程里正在跑的主动推进循环（Core 进程内有效；控制台进程恒为 None）。"""
    return _active_loop


def status_snapshot() -> dict[str, Any] | None:
    """当前进程里的主动推进循环状态；Core 未启动（测试 / 纯控制台）→ None。
    控制台进程拿不到（另一个进程），要走 Core 路由 ``/api/quest_proactive``。"""
    lp = _active_loop
    if lp is None:
        return None
    try:
        return lp.snapshot()
    except Exception:  # noqa: BLE001
        logger.debug("[quest_proactive] snapshot failed", exc_info=True)
        return None


def request_speak_now(timeout_sec: float = 3.0) -> dict[str, Any]:
    """让机器人现在就按剧本说一句（线程安全；只等提交，不等整轮说完）。"""
    lp, aio = _active_loop, _active_asyncio_loop
    if lp is None or aio is None or aio.is_closed():
        return {"ok": False, "error": "服务还没启动主动陪伴，稍后再试"}
    fut = asyncio.run_coroutine_threadsafe(lp.trigger_now(), aio)
    try:
        started = fut.result(timeout=timeout_sec)
    except TimeoutError:
        # 一轮对话可能持续十几秒：提交成功但还没说完，按已发起处理
        return {"ok": True, "started": True, "pending": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not started:
        reason = lp.last_runner_skip_reason()
        return {"ok": False, "error": _skip_text(reason), "reason": reason}
    return {"ok": True, "started": True}


_SKIP_TEXT = {
    "disabled": "主动陪伴已关闭",
    "offline": "小歪没有连接",
    "no_task": "当前场景没有进行中的小目标",
    "daily_limit": "今天的主动开口次数已用完",
    "task_cooldown": "进行中的小目标这一轮已经提过，等小歪标结果或下次检测",
    "chain_wait": "上一个小目标刚有了结果，等大家安静几秒就接着下一个",
    "chain_end": "上一个小目标有了结果，后面没有要接的了",
    "task_not_running": "这个小目标不在进行中，先点「现在开始」",
    "busy": "小歪正在说话，稍后再试",
    "quiet_hours": "现在是勿扰时段",
    "recent_conversation": "你们刚聊过，还没到冷场时间",
    "cooldown": "刚试过一次，稍等",
    "inflight": "正在说话",
    "prefs_error": "读取设置失败",
}


def _skip_text(reason: str) -> str:
    return _SKIP_TEXT.get(str(reason or ""), "现在还不能开口")


def skip_reason_text(reason: str) -> str:
    return _skip_text(reason)


__all__ = [
    "QUEST_ATTEMPT_IDLE_SEC",
    "QUEST_CHECK_SEC",
    "QUEST_NO_TASK_COOLDOWN_SEC",
    "QUEST_JUDGE_NUDGE_SEC",
    "QUEST_JUDGE_SOURCE",
    "QUEST_PROACTIVE_SOURCE",
    "QUEST_TASK_RETRY_SEC",
    "build_judge_instruction",
    "SCHEDULE_GRACE_SEC",
    "QuestProactiveLoop",
    "QuestProactiveRunner",
    "active_loop",
    "build_rtc_instruction",
    "build_user_text",
    "local_datetime",
    "pick_scheduled_due",
    "registry_activity_ts",
    "request_speak_now",
    "skip_reason_text",
    "status_snapshot",
]
