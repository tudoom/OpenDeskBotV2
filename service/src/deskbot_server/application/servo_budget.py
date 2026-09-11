"""舵机运动预算器：所有来源的转头动作在这里限频。

2026-09-09 转头舵机崩齿：24 秒内 18 次全幅急停反向。固件已限速限加速度，
这里管"次数"：令牌桶限制大幅动作与总段数，超出时按来源处理——待机张望、
表情这类装饰性动作直接丢弃；用户点击、语音、剧情动作不丢，改为拉长时长
（更慢更柔），并记一条遥测。没有电流采样，"频率"是唯一能在 PC 侧控制的量。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from deskbot_server.telemetry import emit as _telemetry_emit

logger = logging.getLogger("deskbot-server")

BIG_TRAVEL_DEG = 90
BIG_MOVES_PER_WINDOW = 3
BIG_WINDOW_SEC = 10.0
SEGMENTS_PER_MINUTE = 20
STRETCH_FACTOR = 2
DROP_SOURCES = ("live_wander", "live_center", "expression", "expression_runtime")

# PC 侧热量估计：常数与固件 head.cpp 的热预算一致（每度 +1、换向 +5、每秒 -25），
# 用来在下发前就决定"这一轮待机动作跳过"，不必等设备回报。
HEAT_PER_DEG = 1.0
HEAT_PER_REVERSAL = 5.0
HEAT_COOL_PER_SEC = 25.0
HEAT_SKIP_THRESHOLD = 300.0

# 自发动作（待机、语音工具）的"轻柔档"：绝对目标限定在中心 ±30°，每度至少 33 ms（≈30°/s）。
SELF_MOTION_SOURCES = ("live_wander", "live_center", "rtc_tool_move", "rtc_tool_move_composed")
GENTLE_MAX_OFFSET_DEG = 30
GENTLE_MS_PER_DEG = 33
# 待机张望（live_wander / live_center）：2026-09-10 用户反馈 ±30°@33ms/deg 看不出来，放宽到 ±40°、每度 10 ms（100°/s）。
# ±40° 的折返是 80°，仍低于 BIG_TRAVEL_DEG（90），所以张望永远不占"大幅动作"名额；热量由固件热预算和 too_hot 兜底。
WANDER_MAX_OFFSET_DEG = 40
WANDER_MS_PER_DEG = 10
WANDER_SOURCES = ("live_wander", "live_center")
# 张望一次 = 3 段行程 + 2 段保持；保持段不计入分钟段数预算，且张望的段数上限放宽（10 次/分钟 × 3 段 = 30）。
WANDER_SEGMENTS_PER_MINUTE = 40
GENTLE_MIN_MS = 300
_CENTER = 90


@dataclass(frozen=True)
class BudgetDecision:
    allow: bool
    stretch: int
    reason: str
    big_moves: int
    segments: int

    @property
    def throttled(self) -> bool:
        return self.stretch > 1 or not self.allow


def _step_travel(step: Mapping[str, Any], prev_x: int | None) -> tuple[int, int | None]:
    """粗略行程：绝对目标按与上一目标（缺省 90）之差，相对按绝对值。"""
    xm = int(step.get("xm", 0) or 0)
    x = int(step.get("x", 0) or 0)
    if xm == 2:
        return 0, prev_x
    if xm == 1:
        return abs(x), (prev_x if prev_x is None else prev_x + x)
    base = 90 if prev_x is None else prev_x
    return abs(x - base), x


def soften_steps(steps: Iterable[Mapping[str, Any]], *, max_offset: int = GENTLE_MAX_OFFSET_DEG,
                 ms_per_deg: int = GENTLE_MS_PER_DEG) -> list[dict[str, Any]]:
    """把一串协议步收进轻柔档：绝对目标夹到中心 ±max_offset，相对量夹到 ±max_offset，时长按行程拉长。"""
    out: list[dict[str, Any]] = []
    prev = {"x": _CENTER, "y": _CENTER}
    for raw in steps:
        st = dict(raw)
        travel = 0
        for axis, mode_key in (("x", "xm"), ("y", "ym")):
            mode = int(st.get(mode_key, 0) or 0)
            val = int(st.get(axis, 0) or 0)
            if mode == 0:
                lo, hi = _CENTER - max_offset, _CENTER + max_offset
                lo = max(lo, int(st.get(f"{axis}_min", lo) or lo))
                hi = min(hi, int(st.get(f"{axis}_max", hi) or hi))
                target = max(lo, min(hi, val))
                st[axis] = target
                travel = max(travel, abs(target - prev[axis]))
                prev[axis] = target
            elif mode == 1:
                d = max(-max_offset, min(max_offset, val))
                st[axis] = d
                travel = max(travel, abs(d))
                prev[axis] = max(_CENTER - max_offset, min(_CENTER + max_offset, prev[axis] + d))
        st["ms"] = max(int(st.get("ms", 0) or 0), GENTLE_MIN_MS if travel else 0, travel * ms_per_deg)
        out.append(st)
    return out


class ServoBudget:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._big: dict[str, list[float]] = {}
        self._segs: dict[str, list[float]] = {}
        self._heat: dict[str, tuple[float, float, int]] = {}  # dev -> (heat, updated_at, last_dir_x)
        self.throttled_count = 0

    # ── 热量估计 ──
    def heat(self, device_id: str) -> float:
        dev = str(device_id or "")
        now = self._clock()
        with self._lock:
            heat, at, d = self._heat.get(dev, (0.0, now, 0))
            heat = max(0.0, heat - HEAT_COOL_PER_SEC * max(0.0, now - at))
            self._heat[dev] = (heat, now, d)
            return heat

    def note_motion(self, device_id: str, steps: Iterable[Mapping[str, Any]]) -> float:
        """按刚下发的步累加热量（行程 + 换向），返回当前估计值。"""
        dev = str(device_id or "")
        heat = self.heat(dev)
        with self._lock:
            _h, at, last_dir = self._heat.get(dev, (0.0, self._clock(), 0))
            prev_x: int | None = None
            for st in steps:
                travel, nxt = _step_travel(st, prev_x)
                if travel:
                    heat += HEAT_PER_DEG * travel
                    direction = 0
                    if nxt is not None and prev_x is not None:
                        direction = 1 if nxt > prev_x else (-1 if nxt < prev_x else 0)
                    elif nxt is not None:
                        direction = 1 if nxt > _CENTER else (-1 if nxt < _CENTER else 0)
                    if direction and last_dir and direction != last_dir:
                        heat += HEAT_PER_REVERSAL
                    if direction:
                        last_dir = direction
                prev_x = nxt if nxt is not None else prev_x
            self._heat[dev] = (heat, self._clock(), last_dir)
        return heat

    def too_hot(self, device_id: str) -> bool:
        return self.heat(device_id) >= HEAT_SKIP_THRESHOLD

    def _prune(self, dev: str, now: float) -> None:
        self._big[dev] = [t for t in self._big.get(dev, []) if now - t < BIG_WINDOW_SEC]
        self._segs[dev] = [t for t in self._segs.get(dev, []) if now - t < 60.0]

    def decide(self, device_id: str, steps: Iterable[Mapping[str, Any]], *, source: str) -> BudgetDecision:
        dev = str(device_id or "")
        steps = list(steps)
        big = 0
        prev: int | None = None
        for st in steps:
            travel, prev = _step_travel(st, prev)
            if travel >= BIG_TRAVEL_DEG:
                big += 1
        # 纯保持段（xm=ym=2）不出力，不算一段
        moving = [
            st for st in steps
            if not (int(st.get("xm", 0) or 0) == 2 and int(st.get("ym", 0) or 0) == 2)
        ]
        seg_limit = WANDER_SEGMENTS_PER_MINUTE if source in WANDER_SOURCES else SEGMENTS_PER_MINUTE
        now = self._clock()
        with self._lock:
            self._prune(dev, now)
            big_used = len(self._big[dev])
            seg_used = len(self._segs[dev])
            over_big = big > 0 and big_used + big > BIG_MOVES_PER_WINDOW
            over_seg = seg_used + len(moving) > seg_limit
            if over_big or over_seg:
                self.throttled_count += 1
                reason = "big_moves" if over_big else "segments"
                if source in DROP_SOURCES:
                    return BudgetDecision(False, 1, reason, big_used, seg_used)
                # 不丢：记账并拉长
                self._big[dev].extend([now] * big)
                self._segs[dev].extend([now] * len(moving))
                return BudgetDecision(True, STRETCH_FACTOR, reason, big_used, seg_used)
            self._big[dev].extend([now] * big)
            self._segs[dev].extend([now] * len(moving))
            return BudgetDecision(True, 1, "ok", big_used, seg_used)

    def apply(self, device_id: str, steps: list[dict[str, Any]], *, source: str) -> tuple[list[dict[str, Any]], BudgetDecision]:
        """返回（可能拉长/放柔后的）steps 与决策；不允许时返回空列表。"""
        if source in WANDER_SOURCES:
            steps = soften_steps(steps, max_offset=WANDER_MAX_OFFSET_DEG, ms_per_deg=WANDER_MS_PER_DEG)
        elif source in SELF_MOTION_SOURCES:
            steps = soften_steps(steps)
        decision = self.decide(device_id, steps, source=source)
        if not decision.allow:
            logger.info("[servo_budget] dropped device_id=%s source=%s why=%s", device_id, source, decision.reason)
            _emit("servo.budget.dropped", device_id=device_id, source=source, reason=decision.reason)
            return [], decision
        if decision.stretch > 1:
            out = []
            for st in steps:
                d = dict(st)
                d["ms"] = int(d.get("ms", 0) or 0) * decision.stretch
                out.append(d)
            logger.info("[servo_budget] stretched x%d device_id=%s source=%s why=%s", decision.stretch, device_id, source, decision.reason)
            _emit("servo.budget.stretched", device_id=device_id, source=source, reason=decision.reason, factor=decision.stretch)
            self.note_motion(device_id, out)
            return out, decision
        self.note_motion(device_id, steps)
        return steps, decision


def _emit(event: str, **fields: Any) -> None:
    try:
        _telemetry_emit(event, **fields)
    except Exception:  # noqa: BLE001 - 遥测不影响动作
        pass


_budget: ServoBudget | None = None


def servo_budget() -> ServoBudget:
    global _budget
    if _budget is None:
        _budget = ServoBudget()
    return _budget


__all__ = ["BudgetDecision", "ServoBudget", "servo_budget", "soften_steps", "HEAT_SKIP_THRESHOLD", "SELF_MOTION_SOURCES", "WANDER_MAX_OFFSET_DEG", "WANDER_MS_PER_DEG", "WANDER_SOURCES", "GENTLE_MAX_OFFSET_DEG", "GENTLE_MS_PER_DEG", "BIG_TRAVEL_DEG", "BIG_MOVES_PER_WINDOW", "SEGMENTS_PER_MINUTE", "STRETCH_FACTOR"]
