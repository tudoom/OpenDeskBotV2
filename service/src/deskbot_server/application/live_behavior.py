"""空闲待机行为：张望（wander）。

没人理它时机器人不再定格待机脸：空闲够久后，按「每分钟几次」在每个自然分钟里随机
挑几个时刻轻轻左右看一眼。全部走舵机专用 PB，level=IDLE，任何对话/提醒/手动操作都能
直接顶掉；机器人一开口（表情运行时离开 idle）本服务立即停手并重新计时。

2026-09-10：去掉人脸相关（注视、「有人在面前不张望」）和「打盹」周期，改成两个参数：
``behavior.wander_per_min``（默认 2，0 = 不张望）、``behavior.wander_idle_sec``（空闲多久后
开始，默认 60 秒）。每一拍都记录「现在为什么没动」（``block_reason``），控制台状态行直接
显示，排障不用再猜。

移植自早期分支的 live_service.py（2026-09），读本机偏好
``behavior.idle_live`` / ``quest.proactive_enabled``（总开关）与我们的表情运行时状态。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from deskbot_server.application.expression_runtime import get_expression_runtime
from deskbot_server.application.interaction_feedback import send_servo_moves_and_wait
from deskbot_server.application.proactive_gate import SOURCE_LIVE, can_be_proactive
from deskbot_server.application.servo_budget import servo_budget
from deskbot_server.device_preferences import load_preferences
from deskbot_server.pb.shapes import PB_LEVEL_IDLE

logger = logging.getLogger("deskbot-server")

TICK_SEC = 1.0                # 主循环节拍
# 张望档位（2026-09-10 用户实测 ±30°@30°/s 仍嫌轻）：轻柔 ±25°/40°/s，正常 ±35°/67°/s（默认），明显 ±40°/100°/s。
# 幅度不能超过 servo_budget.WANDER_MAX_OFFSET_DEG（40），否则会被预算器再夹回去。
# 档位 → (左右幅度°, 每度毫秒)。固件 USB 供电下最快 1°/拍（50°/s），所以 30 ms/deg（33°/s）看起来和没改一样；
# 2026-09-10 晚用户再次反馈：要"缓慢"——正常档 35° 走约 2 s（≈18°/s），轻柔 12°/s，明显 28°/s；停顿另算。
# 预算器对张望只有下限（WANDER_MS_PER_DEG），不会把它再提速；固件按 ms 把速度曲线拉长（profile_init v_fit）。
WANDER_LEVELS = {"gentle": (25, 80), "normal": (35, 55), "bold": (40, 35)}
DEFAULT_IDLE_MOTION = "normal"
DEFAULT_WANDER_PER_MIN = 2    # 每个自然分钟内随机触发的次数；0 = 不张望
WANDER_PER_MIN_MAX = 10
DEFAULT_WANDER_IDLE_SEC = 60  # 空闲多久后才开始张望（2026-09-09：20s→60s，少动即少热）
WANDER_IDLE_SEC_MIN, WANDER_IDLE_SEC_MAX = 10, 600
POST_SPEECH_QUIET_SEC = 30.0  # 说话/表演结束后 30 秒内不做自发动作
PREFS_CACHE_SEC = 5.0
_SERVO_CENTER_X = 90
_SERVO_CENTER_Y = 90


@dataclass
class _DeviceLive:
    task: Optional[asyncio.Task] = None
    idle_since: Optional[float] = None     # monotonic：运行时回到 idle 且过了安静期的时刻
    last_busy_ts: float = 0.0              # 最近一次运行时不空闲（说话/表演/门禁不放行）的时刻
    hold_until: float = 0.0                # 温度严重档：到此之前不动（monotonic）
    slot_minute: int = -1                  # 已经排好随机时刻的那个自然分钟（wall clock // 60）
    slots: list[float] = field(default_factory=list)  # 本分钟内还没触发的时刻（wall clock 秒）
    off_center: bool = False               # 我们自己把头转离中心了（关开关时要回中）
    last_motion_ts: float = 0.0            # 最近一次自发动作（wall clock）
    last_motion_source: str = ""
    block_reason: str = ""                 # 现在为什么没动（见 _tick）
    block_since: float = 0.0               # 该原因从何时开始（wall clock）
    stats: dict[str, int] = field(default_factory=lambda: {"wander": 0, "heat_skips": 0, "thermal_holds": 0})


def gentle_ms(travel_deg: int, ms_per_deg: int) -> int:
    return max(300, int(abs(travel_deg)) * int(ms_per_deg))


# 抬头幅度（逻辑 y 增大 = 抬头；y 上限 110、固件边缘余量 3° → 最高 107）
WANDER_UP_DEG = {"gentle": 10, "normal": 13, "bold": 16}
WANDER_Y_MIN, WANDER_Y_MAX = 84, 107
# 动作库：(名字, 权重, 路径)。路径里每个点是 (x 系数, y 系数, 停顿档)：x 系数乘档位幅度并带随机方向，
# y 系数乘抬头幅度；停顿档 0 = 不停，1 = 短停，2 = 停一会。最后总会回中（回中不在路径里，统一追加）。
# 2026-09-10 晚用户反馈「幅度变小、动作变少」：系数拉回接近满幅，多段的动作权重更高，幅度抖动缩到 90%–100%。
WANDER_GESTURES: tuple[tuple[str, int, tuple[tuple[float, float, int], ...]], ...] = (
    ("sweep", 4, ((1.0, 0.0, 2), (-1.0, 0.0, 2))),           # 左右扫一遍（方向随机）
    ("glance", 2, ((1.0, 0.0, 2),)),                          # 只看一边
    ("up_glance", 2, ((0.9, 1.0, 2),)),                       # 左上 / 右上
    ("up_sweep", 3, ((0.9, 1.0, 2), (-0.9, 0.8, 2))),         # 左上 → 右上（或反过来）
    ("look_up", 1, ((0.0, 1.0, 2),)),                         # 抬头看一眼
    ("peek", 2, ((0.6, 0.0, 1), (1.0, 0.5, 2))),              # 先偏一点，再探得更远、略抬头
    ("double_take", 2, ((0.7, 0.0, 1), (0.0, 0.0, 1), (1.0, 0.3, 2))),  # 看一眼回来再看一次
)


def wander_moves(level: str = DEFAULT_IDLE_MOTION, *, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """随机挑一个张望动作：方向（左/右）、幅度（90%–100%）、停顿时长都不一样；最后回中。

    停顿用保持段（固件零力矩）；行程时长按档位的每度毫秒数算，两轴取行程大的那个。
    ``rng`` 供测试注入固定种子。
    """
    r = rng or random
    key = str(level or "")
    offset, ms_per_deg = WANDER_LEVELS.get(key, WANDER_LEVELS[DEFAULT_IDLE_MOTION])
    up = WANDER_UP_DEG.get(key, WANDER_UP_DEG[DEFAULT_IDLE_MOTION])
    names = [g[0] for g in WANDER_GESTURES]
    weights = [g[1] for g in WANDER_GESTURES]
    name = r.choices(names, weights=weights, k=1)[0]
    path = next(g[2] for g in WANDER_GESTURES if g[0] == name)
    side = r.choice((1, -1))
    scale = r.uniform(0.9, 1.0)
    out: list[dict[str, Any]] = []
    px, py = _SERVO_CENTER_X, _SERVO_CENTER_Y
    waypoints = [(cx, cy, hold) for cx, cy, hold in path] + [(0.0, 0.0, 0)]
    for cx, cy, hold in waypoints:
        x = int(round(_SERVO_CENTER_X + side * cx * offset * scale))
        y = int(round(_SERVO_CENTER_Y + cy * up * scale))
        y = max(WANDER_Y_MIN, min(WANDER_Y_MAX, y))
        travel = max(abs(x - px), abs(y - py))
        if travel == 0 and hold == 0:
            continue
        if travel:
            out.append({"move": "__custom__", "xm": 0, "ym": 0, "x": x, "y": y, "ms": gentle_ms(travel, ms_per_deg)})
            px, py = x, y
        if hold:
            hold_ms = int(r.uniform(400, 800)) if hold == 1 else int(r.uniform(800, 2000))
            out.append({"move": "__custom__", "xm": 2, "ym": 2, "x": _SERVO_CENTER_X, "y": _SERVO_CENTER_Y, "ms": hold_ms})
    if not out or (px, py) != (_SERVO_CENTER_X, _SERVO_CENTER_Y):
        out.append({"move": "__custom__", "xm": 0, "ym": 0, "x": _SERVO_CENTER_X, "y": _SERVO_CENTER_Y,
                    "ms": gentle_ms(max(abs(px - _SERVO_CENTER_X), abs(py - _SERVO_CENTER_Y)) or 1, ms_per_deg)})
    return out


def center_moves() -> list[dict[str, Any]]:
    return [{"move": "__custom__", "xm": 0, "ym": 0, "x": _SERVO_CENTER_X, "y": _SERVO_CENTER_Y, "ms": 900}]


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = int(default)
    return max(lo, min(hi, out))


# 「现在为什么没动」的人话，控制台状态行直接显示
BLOCK_REASON_TEXT = {
    "off": "张望开关关着",
    "no_hub": "服务还没就绪",
    "lane_busy": "设备正在执行别的动作或对话",
    "quiet_hours": "勿扰时段",
    "busy:speaking": "小歪在说话",
    "busy:listening": "在听你说话",
    "busy:thinking": "在思考",
    "busy:no_runtime": "设备表情运行时还没就绪",
    "busy:runtime_error": "读不到设备表情状态",
    "quiet_after_speech": "说完话 30 秒内不动",
    "warming_up": "刚空闲下来，等够时间再动",
    "frequency_zero": "张望频率设为 0",
    "waiting_slot": "等这一分钟里的随机时刻",
    "thermal_hold": "温度过高，暂停待机动作",
    "servo_hot": "舵机发热，这次跳过",
    "wandering": "正在张望",
}


def block_reason_text(reason: str) -> str:
    reason = str(reason or "")
    if reason in BLOCK_REASON_TEXT:
        return BLOCK_REASON_TEXT[reason]
    if reason.startswith("busy:"):
        return f"小歪正忙（{reason[5:]}）"
    return reason or "未知"


class LiveBehaviorService:
    def __init__(self) -> None:
        self._gate = can_be_proactive  # 统一主动门禁（勿扰 / lane 忙）；测试可换
        self._hub: Any = None
        self._devices: dict[str, _DeviceLive] = {}
        self._prefs_cache: tuple[float, dict[str, Any]] = (0.0, {})

    # ── 装配 ──
    def bind(self, hub: Any) -> None:
        self._hub = hub

    def _behavior_prefs(self) -> dict[str, Any]:
        now = time.monotonic()
        cached_at, cached = self._prefs_cache
        if now - cached_at < PREFS_CACHE_SEC and cached:
            return cached
        try:
            prefs = load_preferences()
            behavior = dict(prefs.get("behavior") or {})
            quest = dict(prefs.get("quest") or {})
        except Exception:  # noqa: BLE001 - 偏好读不到按默认
            behavior, quest = {}, {}
        behavior.setdefault("idle_live", True)
        behavior["idle_motion"] = str(behavior.get("idle_motion") or DEFAULT_IDLE_MOTION)
        if behavior["idle_motion"] not in WANDER_LEVELS:
            behavior["idle_motion"] = DEFAULT_IDLE_MOTION
        behavior["wander_per_min"] = _clamp_int(behavior.get("wander_per_min"), DEFAULT_WANDER_PER_MIN, 0, WANDER_PER_MIN_MAX)
        behavior["wander_idle_sec"] = _clamp_int(
            behavior.get("wander_idle_sec"), DEFAULT_WANDER_IDLE_SEC, WANDER_IDLE_SEC_MIN, WANDER_IDLE_SEC_MAX
        )
        # 主动陪伴总开关统管所有自发动作：关了就不张望。
        behavior["master"] = bool(quest.get("proactive_enabled", True))
        self._prefs_cache = (now, behavior)
        return behavior

    def self_motion_enabled(self) -> bool:
        b = self._behavior_prefs()
        return bool(b.get("master", True)) and bool(b.get("idle_live", True))

    def mode(self) -> str:
        b = self._behavior_prefs()
        if not (b.get("master", True) and b.get("idle_live", True)):
            return "off"
        return str(b.get("idle_motion") or DEFAULT_IDLE_MOTION)

    def invalidate_prefs(self) -> None:
        self._prefs_cache = (0.0, {})

    def _dev(self, device_id: str) -> _DeviceLive:
        return self._devices.setdefault(device_id, _DeviceLive())

    def stats(self, device_id: str) -> dict[str, Any]:
        st = self._devices.get(device_id)
        if st is None:
            return {}
        b = self._behavior_prefs()
        wall = time.time()
        next_slot = min(st.slots) - wall if st.slots else None
        return {
            "idle_since": st.idle_since,
            "mode": self.mode(),
            "wander_per_min": int(b.get("wander_per_min", DEFAULT_WANDER_PER_MIN)),
            "wander_idle_sec": int(b.get("wander_idle_sec", DEFAULT_WANDER_IDLE_SEC)),
            "block_reason": st.block_reason,
            "block_text": block_reason_text(st.block_reason),
            "block_since": st.block_since,
            "next_slot_in": max(0.0, next_slot) if next_slot is not None else None,
            "last_motion_ts": st.last_motion_ts,
            "last_motion_source": st.last_motion_source,
            **st.stats,
        }

    def any_stats(self) -> dict[str, Any]:
        """给控制台看的：最近一台有自发动作记录的设备（没有就取任一台；没设备只报模式）。"""
        best = None
        for dev, st in self._devices.items():
            if best is None or st.last_motion_ts > self._devices[best].last_motion_ts:
                best = dev
        out = self.stats(best) if best else {"mode": self.mode()}
        if best:
            out["device_id"] = best
        return out

    # ── 空闲循环 ──
    def start_device(self, device_id: str) -> None:
        dev = str(device_id or "").strip()
        if not dev:
            return
        st = self._dev(dev)
        if st.task is not None and not st.task.done():
            return
        st.idle_since = None
        st.slot_minute = -1
        st.slots = []
        st.task = asyncio.get_running_loop().create_task(self._loop(dev), name=f"live-idle:{dev}")
        logger.info("[live] idle behaviour started device_id=%s", dev)

    def thermal_hold(self, device_id: str, seconds: float) -> None:
        """温度严重档：待机动作停 ``seconds`` 秒。"""
        st = self._dev(str(device_id or "").strip())
        st.hold_until = max(st.hold_until, time.monotonic() + float(seconds))
        st.slots = []
        st.stats["thermal_holds"] = int(st.stats.get("thermal_holds", 0)) + 1
        logger.warning("[live] thermal hold %.0fs device_id=%s", seconds, device_id)

    async def stop_device(self, device_id: str) -> None:
        st = self._devices.pop(str(device_id or "").strip(), None)
        if st is None or st.task is None:
            return
        task = st.task
        st.task = None
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        logger.info("[live] idle behaviour stopped device_id=%s", device_id)

    def _runtime_state(self, device_id: str) -> str:
        """表情运行时当前显示的状态；没绑定 / 读不到都当作忙（设备还没就绪）。"""
        runtime = get_expression_runtime(device_id)
        if runtime is None:
            return "no_runtime"
        try:
            return str(runtime.snapshot().get("displayed_state") or "idle")
        except Exception:  # noqa: BLE001
            return "runtime_error"

    def _note(self, device_id: str, st: _DeviceLive, reason: str) -> None:
        """记录「现在为什么没动」；原因变化时打一条 INFO（张望本身由舵机下发日志记录）。"""
        if reason == st.block_reason:
            return
        previous = st.block_reason
        st.block_reason = reason
        st.block_since = time.time()
        if reason != "wandering" and not (reason == "waiting_slot" and previous == "wandering"):
            logger.info("[live] idle state device_id=%s %s -> %s", device_id, previous or "-", reason)

    async def _send_moves(self, device_id: str, moves: list[dict[str, Any]], *, source: str, summary: str) -> None:
        if self._hub is None:
            return
        st = self._dev(device_id)
        st.last_motion_ts = time.time()
        st.last_motion_source = source
        await send_servo_moves_and_wait(
            self._hub,
            device_id,
            moves,
            source=source,
            summary=summary,
            request_id=f"live-{uuid.uuid4().hex[:10]}",
            level=PB_LEVEL_IDLE,
        )

    async def _loop(self, device_id: str) -> None:
        st = self._dev(device_id)
        while True:
            await asyncio.sleep(TICK_SEC)
            try:
                await self._tick(device_id, st)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 待机行为出错不能影响别的
                self._note(device_id, st, "tick_error")
                logger.warning("[live] tick failed device_id=%s", device_id, exc_info=True)

    async def _tick(self, device_id: str, st: _DeviceLive) -> None:
        now = time.monotonic()
        wall = time.time()
        behavior = self._behavior_prefs()
        if not (behavior.get("master", True) and behavior.get("idle_live", True)) or self._hub is None:
            # 总开关/张望开关关了：正转离中心的话回中一次，然后彻底不动。
            st.idle_since = None
            st.slot_minute = -1
            st.slots = []
            self._note(device_id, st, "off" if self._hub is not None else "no_hub")
            if st.off_center and self._hub is not None:
                st.off_center = False
                await self._send_moves(device_id, center_moves(), source="live_center", summary="回中")
            return
        allowed, why = self._gate(SOURCE_LIVE, device_id)
        state = self._runtime_state(device_id)
        if not allowed or state not in ("idle", "listening"):
            st.last_busy_ts = now
            st.idle_since = None
            st.slots = []
            self._note(device_id, st, (why or "gate") if not allowed else f"busy:{state}")
            return
        if state == "listening":
            # 倾听脸 = VAD 边沿：可能是主人开口，也可能是噪声（办公室里每 30–60 s 一次，2026-09-11 实测）。
            # 这一拍不动，但不清空空闲计时、也不算"刚说完话"：真开口很快会变成 thinking / speaking 再重置，
            # 误报 8 s 内会回到 idle。否则每次误报都要再等 30 s + wander_idle_sec，张望永远轮不到。
            self._note(device_id, st, "busy:listening")
            return
        if now < st.hold_until:
            self._note(device_id, st, "thermal_hold")
            return
        if now - st.last_busy_ts < POST_SPEECH_QUIET_SEC:
            self._note(device_id, st, "quiet_after_speech")
            return
        if st.idle_since is None:
            st.idle_since = now
        if now - st.idle_since < float(behavior["wander_idle_sec"]):
            self._note(device_id, st, "warming_up")
            return
        per_min = int(behavior["wander_per_min"])
        if per_min <= 0:
            self._note(device_id, st, "frequency_zero")
            return
        minute = int(wall // 60)
        if st.slot_minute != minute:
            # 新的一个自然分钟：随机排 per_min 个时刻；这一分钟里已经过去的时刻不补，
            # 免得刚空闲下来就连着动几下。
            st.slot_minute = minute
            base = float(minute * 60)
            st.slots = sorted(t for t in (base + random.uniform(0.0, 60.0) for _ in range(per_min)) if t >= wall)
        if not st.slots or st.slots[0] > wall:
            self._note(device_id, st, "waiting_slot")
            return
        st.slots.pop(0)
        if servo_budget().too_hot(device_id):
            # 舵机热量估计过高：这一次跳过（不补），等它凉下来。
            st.stats["heat_skips"] += 1
            logger.info("[live] wander skipped: servo heat %.0f device_id=%s", servo_budget().heat(device_id), device_id)
            self._note(device_id, st, "servo_hot")
            return
        st.stats["wander"] += 1
        st.off_center = True
        self._note(device_id, st, "wandering")
        await self._send_moves(device_id, wander_moves(behavior.get("idle_motion")), source="live_wander", summary="张望")
        st.off_center = False
        self._note(device_id, st, "waiting_slot")


_service: Optional[LiveBehaviorService] = None


def live_behavior_service() -> LiveBehaviorService:
    global _service
    if _service is None:
        _service = LiveBehaviorService()
    return _service
