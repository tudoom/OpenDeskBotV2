"""空闲待机张望：每个自然分钟内随机几次；开关 / 门禁 / 热量 / 说话后安静期；「为什么没动」可见。"""

from __future__ import annotations

import asyncio
import time

import pytest

from deskbot_server.application import live_behavior as lb


class _Runtime:
    def __init__(self, state: str = "idle") -> None:
        self.state = state

    def snapshot(self):
        return {"displayed_state": self.state}


@pytest.fixture
def svc(monkeypatch):
    service = lb.LiveBehaviorService()
    sent: list[tuple[str, str, list]] = []

    async def _fake_send(hub, device_id, moves, *, source, summary, request_id=None, level=None, **_kw):
        sent.append((device_id, source, moves))

    monkeypatch.setattr(lb, "send_servo_moves_and_wait", _fake_send)
    runtime = _Runtime("idle")
    monkeypatch.setattr(lb, "get_expression_runtime", lambda dev: runtime)
    prefs = {"behavior": {"idle_live": True, "wander_per_min": 2, "wander_idle_sec": 10}}
    monkeypatch.setattr(lb, "load_preferences", lambda: prefs)
    clock = {"wall": 999_965.0}  # 自然分钟 999_960 开始后 5 秒
    monkeypatch.setattr(lb.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(lb.random, "uniform", lambda a, b: 0.0)  # 随机时刻都落在分钟开始 → 一进新分钟就到点
    service.bind(object())
    return service, sent, runtime, prefs, clock


def _tick(service, st):
    asyncio.run(service._tick("dev", st))  # noqa: SLF001


def _idle_long_enough(st):
    st.last_busy_ts = 0.0
    st.idle_since = time.monotonic() - 999.0


def test_wanders_per_natural_minute_at_random_slots(svc):
    service, sent, _runtime, _prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    _tick(service, st)
    assert st.block_reason in ("quiet_after_speech", "warming_up") and sent == []
    _idle_long_enough(st)
    # 进入这一分钟时已经过去的时刻不补（随机点在分钟开始，现在是 +5s）
    _tick(service, st)
    assert st.block_reason == "waiting_slot" and sent == []
    # 下一个自然分钟（1_000_020 = 16667×60）：2 个随机时刻都到点 → 一拍一次，共两次，然后等下一分钟
    clock["wall"] = 1_000_020.0
    _tick(service, st)
    _tick(service, st)
    _tick(service, st)
    assert [s[1] for s in sent] == ["live_wander", "live_wander"]
    assert st.block_reason == "waiting_slot" and service.stats("dev")["wander"] == 2
    clock["wall"] = 1_000_080.0
    _tick(service, st)
    assert len(sent) == 3 and service.stats("dev")["next_slot_in"] == 0.0


def test_frequency_zero_and_switch_off_never_move(svc):
    service, sent, _runtime, prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    _idle_long_enough(st)
    prefs["behavior"]["wander_per_min"] = 0
    service.invalidate_prefs()
    clock["wall"] = 1_000_020.0
    for _ in range(3):
        _tick(service, st)
    assert sent == [] and st.block_reason == "frequency_zero"
    prefs["behavior"]["wander_per_min"] = 2
    prefs["behavior"]["idle_live"] = False
    service.invalidate_prefs()
    _tick(service, st)
    assert sent == [] and st.block_reason == "off" and service.mode() == "off"


def test_master_switch_off_stops_all_self_motion_and_recenters_once(svc):
    """主动陪伴总开关关了：不张望；正转离中心时回中一次。"""
    service, sent, _runtime, prefs, _clock = svc
    prefs["quest"] = {"proactive_enabled": False}
    service.invalidate_prefs()
    st = service._dev("dev")  # noqa: SLF001
    st.off_center = True
    _tick(service, st)
    assert [s[1] for s in sent] == ["live_center"]
    _tick(service, st)
    _tick(service, st)
    assert len(sent) == 1 and service.mode() == "off"
    prefs["quest"] = {"proactive_enabled": True}
    service.invalidate_prefs()
    assert service.mode() == "normal"


def test_busy_runtime_gate_and_post_speech_quiet_block_wander(svc, monkeypatch):
    service, sent, runtime, _prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    clock["wall"] = 1_000_020.0
    runtime.state = "speaking"
    _tick(service, st)
    assert st.block_reason == "busy:speaking" and st.idle_since is None
    runtime.state = "idle"
    for _ in range(3):
        _tick(service, st)
    assert sent == [] and st.block_reason == "quiet_after_speech"  # 说完 30 秒内不动
    monkeypatch.setattr(lb, "POST_SPEECH_QUIET_SEC", 0.0)
    _tick(service, st)
    assert st.block_reason == "warming_up"  # 空闲计时刚开始
    _idle_long_enough(st)
    clock["wall"] = 1_000_080.0
    _tick(service, st)
    assert [s[1] for s in sent] == ["live_wander"]
    # 门禁（lane 忙 / 勿扰）也算忙，原因原样透出
    service._gate = lambda source, dev: (False, "lane_busy")  # noqa: SLF001
    _tick(service, st)
    assert st.block_reason == "lane_busy" and service.stats("dev")["block_text"] == "设备正在执行别的动作或对话"


def test_thermal_hold_and_servo_heat_skip(svc, monkeypatch):
    service, sent, _runtime, _prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    _idle_long_enough(st)
    service.thermal_hold("dev", 600.0)
    clock["wall"] = 1_000_020.0
    _tick(service, st)
    assert sent == [] and st.block_reason == "thermal_hold" and service.stats("dev")["thermal_holds"] == 1
    st.hold_until = 0.0
    monkeypatch.setattr(lb.servo_budget(), "too_hot", lambda dev: True)
    _tick(service, st)
    assert sent == [] and st.block_reason == "servo_hot" and service.stats("dev")["heat_skips"] == 1


def _travel_steps(moves):
    return [m for m in moves if m.get("xm") == 0]


def test_wander_levels_bound_amplitude_and_speed():
    import random

    for level, (offset, ms_per_deg) in lb.WANDER_LEVELS.items():
        up = lb.WANDER_UP_DEG[level]
        seen_x, seen_y = set(), set()
        for seed in range(60):
            moves = lb.wander_moves(level, rng=random.Random(seed))
            travel = _travel_steps(moves)
            assert travel and travel[-1]["x"] == 90 and travel[-1]["y"] == 90  # 最后总回中
            px, py = 90, 90
            for m in travel:
                assert 90 - offset <= m["x"] <= 90 + offset
                assert lb.WANDER_Y_MIN <= m["y"] <= min(lb.WANDER_Y_MAX, 90 + up)
                # 时长按行程与档位速度算，不会比档位允许的更快
                assert m["ms"] >= max(abs(m["x"] - px), abs(m["y"] - py)) * ms_per_deg
                px, py = m["x"], m["y"]
                seen_x.add(m["x"])
                seen_y.add(m["y"])
        # 幅度有随机抖动（90%–100%），两个方向都会出现，也会抬头；单步时长按档位算得出真慢（正常档 35° ≥ 1.9 s）
        assert min(seen_x) <= 90 - offset * 0.9 and max(seen_x) >= 90 + offset * 0.9
        assert offset * ms_per_deg >= 1400
        assert max(seen_y) > 90
    assert lb.DEFAULT_IDLE_MOTION == "normal"


def test_wander_is_varied_not_always_left_then_right():
    import random

    shapes = set()
    first_dirs = set()
    for seed in range(80):
        moves = lb.wander_moves("normal", rng=random.Random(seed))
        travel = _travel_steps(moves)
        shapes.add(tuple((1 if m["x"] > 90 else -1 if m["x"] < 90 else 0, 1 if m["y"] > 90 else 0) for m in travel))
        first_dirs.add(1 if travel[0]["x"] > 90 else -1 if travel[0]["x"] < 90 else 0)
    assert len(shapes) >= 6, shapes            # 至少六种不同路线
    assert {1, -1} <= first_dirs                 # 第一步有时向左有时向右
    assert any(any(dy for _dx, dy in shape) for shape in shapes)  # 有抬头（左上/右上/仰头）
    holds = [m for m in lb.wander_moves("normal", rng=random.Random(1)) if m.get("xm") == 2]
    assert holds and all(400 <= h["ms"] <= 2000 for h in holds)


def test_wander_level_selectable(svc):
    service, sent, _runtime, prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    _idle_long_enough(st)
    clock["wall"] = 1_000_020.0
    _tick(service, st)
    wander = [s for s in sent if s[1] == "live_wander"]
    assert wander and all(90 - 35 <= m["x"] <= 90 + 35 for m in wander[0][2] if m.get("xm") == 0)  # 未设档位 → 默认正常
    prefs["behavior"]["idle_motion"] = "bold"
    service.invalidate_prefs()
    assert service.mode() == "bold"


def test_prefs_are_clamped_and_status_carries_reason_text():
    service = lb.LiveBehaviorService()
    lb_prefs = {"behavior": {"idle_live": True, "wander_per_min": 99, "wander_idle_sec": 1}}
    service._prefs_cache = (0.0, {})  # noqa: SLF001
    lb.load_preferences = lambda: lb_prefs  # 本测试内替换（无 monkeypatch 夹具）
    try:
        b = service._behavior_prefs()  # noqa: SLF001
        assert b["wander_per_min"] == lb.WANDER_PER_MIN_MAX and b["wander_idle_sec"] == lb.WANDER_IDLE_SEC_MIN
    finally:
        del lb.load_preferences
        from deskbot_server.device_preferences import load_preferences

        lb.load_preferences = load_preferences
    assert lb.block_reason_text("busy:custom") == "小歪正忙（custom）"
    assert lb.block_reason_text("waiting_slot") == "等这一分钟里的随机时刻"
    assert not hasattr(lb, "on_face_analysis") and not hasattr(lb, "gaze_target_from_analysis")


def test_listening_blip_pauses_but_does_not_reset_idle(svc):
    """2026-09-11：VAD 误报翻出的倾听脸（≤8 s）不算说话——这一拍不动，但不清空空闲计时、不进 30 s 安静期；
    真开口会紧接着变成 thinking / speaking，那才重置。否则办公室里每 30–60 s 一次误报，张望永远轮不到。"""
    service, sent, runtime, _prefs, clock = svc
    st = service._dev("dev")  # noqa: SLF001
    _idle_long_enough(st)
    idle_since = st.idle_since
    clock["wall"] = 1_000_020.0
    runtime.state = "listening"
    _tick(service, st)
    assert st.block_reason == "busy:listening" and sent == []
    assert st.idle_since == idle_since and st.last_busy_ts == 0.0  # 没有当成"忙过"
    runtime.state = "idle"
    _tick(service, st)
    assert [s[1] for s in sent] == ["live_wander"]  # 误报结束立刻接着张望，不用再等安静期 + 预热
    runtime.state = "thinking"
    _tick(service, st)
    assert st.block_reason == "busy:thinking" and st.idle_since is None  # 真对话才重置
