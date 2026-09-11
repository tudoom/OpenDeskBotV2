"""冷场主动推进：QuestProactiveLoop 节流/判定分支 与 QuestProactiveRunner 投递路径。

循环用可注入的 provider 隔离（不查库、不连设备）；runner 用 fake hub/chat 与打桩的
run_chat_turn / publish_chat_turn。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from tests.quest_helpers import bind, clear_care_scene, demo_playbook, quest_env  # noqa: F401


class FakeRunner:
    def __init__(self, result: bool = True, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls: list[str] = []

    async def attempt(self, device_id: str) -> bool:
        self.calls.append(device_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class FakeHub:
    def __init__(self, device: str | None = "dev1", ws: object | None = None) -> None:
        self.device = device
        self.ws = ws
        self.listeners = []

    async def first_connected_device_id(self):
        return self.device

    async def first_ws(self, device_id):
        return self.ws if device_id == self.device else None


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def snapshot(self):
        return list(self.rows)


def _loop(runner, **overrides):
    from deskbot_server.application.quest_proactive import QuestProactiveLoop

    kw = dict(
        asr_chat_hub=FakeHub(),
        registry=FakeRegistry(),
        idle_sec=60.0,
        cooldown_sec=60.0,
        activity_ts_provider=lambda dev: 0.0,
        enabled_provider=lambda: True,
        quiet_hours_provider=lambda: False,
    )
    kw.update(overrides)
    return QuestProactiveLoop(runner, **kw)


async def _settle(loop_obj):
    await asyncio.sleep(0.02)
    if loop_obj._workers:
        await asyncio.gather(*loop_obj._workers, return_exceptions=True)


# ── 循环判定分支 ──────────────────────────────────────────────


def test_disabled_and_offline_never_attempt():
    runner = FakeRunner()

    async def _go():
        lp = _loop(runner, enabled_provider=lambda: False)
        assert await lp.tick(now=1_000_000.0) == "disabled"
        lp = _loop(runner, asr_chat_hub=FakeHub(device=None))
        assert await lp.tick(now=1_000_000.0) == "offline"
        await _settle(lp)

    asyncio.run(_go())
    assert runner.calls == []


def test_recent_conversation_blocks_and_cold_idle_triggers():
    runner = FakeRunner()
    activity = {"ts": 0.0}

    async def _go():
        lp = _loop(runner, activity_ts_provider=lambda dev: activity["ts"])
        now = 1_000_000.0
        # 刚对话过 → 不触发
        activity["ts"] = now - 10
        assert await lp.tick(now=now) == "recent_conversation"
        # 冷场 61s → 触发一次
        activity["ts"] = now - 61
        assert await lp.tick(now=now) == ""
        await _settle(lp)
        assert runner.calls == ["dev1"]
        # 主动轮刚发过 → 以上次主动轮时间起算，60s 内不再打扰
        assert await lp.tick(now=now + 30) == "recent_conversation"
        assert await lp.tick(now=now + 61) == ""
        await _settle(lp)
        assert runner.calls == ["dev1", "dev1"]

    asyncio.run(_go())


def test_first_seen_fallback_when_no_activity_record():
    """设备刚连上没有交互打点：以循环首次看见它的时间起算，不会立刻开口。"""
    runner = FakeRunner()

    async def _go():
        lp = _loop(runner, activity_ts_provider=lambda dev: 0.0)
        now = 1_000_000.0
        assert await lp.tick(now=now) == "recent_conversation"
        assert await lp.tick(now=now + 59) == "recent_conversation"
        assert await lp.tick(now=now + 60) == ""
        await _settle(lp)

    asyncio.run(_go())
    assert runner.calls == ["dev1"]


def test_quiet_hours_and_inflight_block():
    runner = FakeRunner(delay=0.05)

    async def _go():
        lp = _loop(runner, quiet_hours_provider=lambda: True)
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == "quiet_hours"
        lp = _loop(runner)
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == ""
        # attempt 进行中不重入
        lp._last_attempt.pop("dev1", None)
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == "inflight"
        await _settle(lp)
        assert runner.calls == ["dev1"]

    asyncio.run(_go())


def test_no_task_cooldown_after_false_attempt():
    runner = FakeRunner(result=False)

    async def _go():
        lp = _loop(runner, cooldown_sec=60.0)
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == ""
        await _settle(lp)
        assert runner.calls == ["dev1"]
        assert lp._next_ok["dev1"] > time.monotonic()
        # 冷却期间不重复触发；解除后允许
        lp._last_attempt.clear()
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 200) == "cooldown"
        lp._next_ok["dev1"] = 0.0
        assert await lp.tick(now=1_000_000.0 + 200) == ""
        await _settle(lp)
        assert runner.calls == ["dev1", "dev1"]

    asyncio.run(_go())


def test_loop_start_stop_and_snapshot():
    runner = FakeRunner()

    async def _go():
        lp = _loop(runner, check_interval_sec=0.5)
        lp.start()
        assert lp.snapshot()["running"] is True
        await lp.stop()
        assert lp.snapshot()["running"] is False

    asyncio.run(_go())


def test_registry_activity_ts_reads_interaction_state():
    from deskbot_server.application.quest_proactive import registry_activity_ts

    reg = FakeRegistry()
    reg.rows = [{"device_id": "dev1", "interaction_state_ts": 123.5, "last_seen_ts": 999.0}]
    assert registry_activity_ts(reg, "dev1") == 123.5
    assert registry_activity_ts(reg, "dev2") == 0.0
    # 设备 VAD 边沿翻出来的 LISTENING（噪声也会）不算主人在说话
    reg.rows = [{"device_id": "dev1", "interaction_state": "LISTENING", "interaction_state_ts": 200.0}]
    assert registry_activity_ts(reg, "dev1") == 0.0
    reg.rows = [{"device_id": "dev1", "interaction_state": "THINKING", "interaction_state_ts": 201.0}]
    assert registry_activity_ts(reg, "dev1") == 201.0


# ── Runner ────────────────────────────────────────────────────


def test_runner_returns_false_when_disabled_no_task_or_offline(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.quest_proactive import QuestProactiveRunner

    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    runner = QuestProactiveRunner(chat=SimpleNamespace(settings=None), asr_chat_hub=FakeHub(ws=None), registry=FakeRegistry(), dp_broker=None)
    assert asyncio.run(runner.attempt("dev1")) is False  # 在线但无 ws → 离线
    monkeypatch.setattr(svc, "proactive_enabled", lambda: False)
    assert asyncio.run(runner.attempt("dev1")) is False
    monkeypatch.setattr(svc, "proactive_enabled", lambda: True)
    bind("")
    assert asyncio.run(runner.attempt("dev1")) is False  # 无任务
    assert asyncio.run(runner.attempt("")) is False


def test_runner_full_path_uses_reminder_delivery(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    captured: dict = {}

    async def _fake_run_chat_turn(downlink, chat, user_text, **kw):
        captured["user_text"] = user_text
        captured["kw"] = kw
        return SimpleNamespace(
            status="ok", error=None, llm_text="主人，要不要聊聊？", need_reply=True,
            voice_auto_reply_off=False, playback_status="played",
        )

    async def _fake_publish(events, device_id, **kw):
        captured["publish"] = (device_id, kw.get("source"), kw.get("asr_text"))

    monkeypatch.setattr(qp, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(qp, "publish_chat_turn", _fake_publish)
    monkeypatch.setattr(qp, "WsDownlinkAdapter", lambda ws, **kw: SimpleNamespace(ws=ws, **kw))
    monkeypatch.setattr(qp, "WsPipelineEventsAdapter", lambda broker, registry: SimpleNamespace())

    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()),
        asr_chat_hub=FakeHub(ws=object()),
        registry=FakeRegistry(),
        dp_broker=None,
    )
    assert asyncio.run(runner.attempt("dev1")) is True
    assert captured["user_text"].startswith("[系统剧情推进]")
    assert "[g_greet] 初次问候" in captured["user_text"]
    assert captured["kw"]["force_voice"] is True
    assert captured["kw"]["make_session_current"] is False
    assert captured["publish"] == ("dev1", "quest_proactive", captured["user_text"])
    assert runner.last_turn["voice_ok"] is True and runner.last_turn["task_id"] == "g_greet"


def test_runner_skips_when_device_lane_busy(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    calls: list[str] = []

    async def _fake_run_chat_turn(*a, **kw):
        calls.append("run")
        return SimpleNamespace(status="ok", error=None, llm_text="", voice_auto_reply_off=False, playback_status="played")

    monkeypatch.setattr(qp, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(qp, "WsDownlinkAdapter", lambda ws, **kw: SimpleNamespace())
    monkeypatch.setattr(qp, "WsPipelineEventsAdapter", lambda broker, registry: SimpleNamespace())
    monkeypatch.setattr(qp.device_turn_arbiter, "submit_if_idle", lambda *a, **kw: None)
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=None), asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), dp_broker=None
    )
    assert asyncio.run(runner.attempt("dev1")) is True  # 视为已尝试（不进入空转冷却）
    assert calls == []


# ── 节流：每日上限 / 同一小目标冷却 / 现在试一句 ──────────────


def _stub_delivery(monkeypatch, qp, calls: list[str]):
    async def _fake_run_chat_turn(downlink, chat, user_text, **kw):
        calls.append(user_text)
        return SimpleNamespace(
            status="ok", error=None, llm_text="嗯", need_reply=True,
            voice_auto_reply_off=False, playback_status="played",
        )

    async def _fake_publish(events, device_id, **kw):
        return None

    monkeypatch.setattr(qp, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(qp, "publish_chat_turn", _fake_publish)
    monkeypatch.setattr(qp, "WsDownlinkAdapter", lambda ws, **kw: SimpleNamespace(ws=ws, **kw))
    monkeypatch.setattr(qp, "WsPipelineEventsAdapter", lambda broker, registry: SimpleNamespace())


def test_runner_daily_limit_and_task_cooldown(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    pb = demo_playbook()
    pb["tasks"][0]["max_attempts"] = 0  # 这里只验证节流，不让尝试上限把起点判掉
    svc.save_playbook("demo", pb)
    bind("demo")
    clear_care_scene()  # 只看主线的节流
    calls: list[str] = []
    _stub_delivery(monkeypatch, qp, calls)
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()),
        asr_chat_hub=FakeHub(ws=object()),
        registry=FakeRegistry(),
        dp_broker=None,
        daily_limit_provider=lambda: 2,
        idle_sec_provider=lambda: 300.0,
        task_retry_sec=1800.0,
    )
    # 第一次：说了；提示词里的冷场时长取偏好（5 分钟）
    assert asyncio.run(runner.attempt("dev1")) is True
    assert "约 5 分钟没有和你对话" in calls[0]
    assert runner.stats()["today_count"] == 1 and runner.stats()["last_spoke_at"]
    # 唯一的进行中任务刚提过 → 冷却，不再说
    assert asyncio.run(runner.attempt("dev1")) is False
    assert runner.last_skip_reason == "task_cooldown"
    # 冷却过期 → 可以再说；达到每日上限后不再说
    runner._task_last_attempt["g_greet"] -= 1801
    assert asyncio.run(runner.attempt("dev1")) is True
    runner._task_last_attempt["g_greet"] -= 1801
    assert asyncio.run(runner.attempt("dev1")) is False
    assert runner.last_skip_reason == "daily_limit"
    # 「触发一次」跳过上限与冷却；指定的小目标不在进行中 → 不开口
    assert asyncio.run(runner.attempt("dev1", ignore_limits=True)) is True
    assert len(calls) == 3
    assert asyncio.run(runner.attempt("dev1", ignore_limits=True, task_id="g_learn_name")) is False
    assert runner.last_skip_reason == "task_not_running"
    assert asyncio.run(runner.attempt("dev1", ignore_limits=True, task_id="g_greet")) is True
    assert len(calls) == 4


def test_request_speak_now_without_loop_and_with_loop(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp

    monkeypatch.setattr(qp, "_active_loop", None)
    monkeypatch.setattr(qp, "_active_asyncio_loop", None)
    assert qp.status_snapshot() is None
    out = qp.request_speak_now()
    assert out["ok"] is False and "稍后再试" in out["error"]

    runner = FakeRunner(result=True)

    async def _go():
        lp = _loop(runner, check_interval_sec=0.5)
        lp.start()
        try:
            assert qp.status_snapshot()["running"] is True
            assert await lp.trigger_now() is True
            assert runner.calls == ["dev1"]
            # 试过一句后，以此为冷场起点
            assert lp.quiet_seconds("dev1") < 5
        finally:
            await lp.stop()
        assert qp.status_snapshot() is None

    asyncio.run(_go())
    assert qp.skip_reason_text("daily_limit") == "今天的主动开口次数已用完"
    assert qp.skip_reason_text("whatever") == "现在还不能开口"


def test_loop_idle_sec_provider_overrides_fixed_value():
    runner = FakeRunner()
    idle = {"sec": 300.0}

    async def _go():
        lp = _loop(runner, idle_sec=60.0, idle_sec_provider=lambda: idle["sec"])
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == "recent_conversation"
        idle["sec"] = 30.0
        assert await lp.tick(now=1_000_000.0 + 61) == ""
        await _settle(lp)
        assert lp.snapshot()["idle_sec"] == 30.0

    asyncio.run(_go())
    assert runner.calls == ["dev1"]


def test_runner_performs_expression_or_scene_before_speaking(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    pb = demo_playbook()
    pb["tasks"][0]["perform"] = {"mode": "expression", "expression": "happy_smile"}
    svc.save_playbook("demo", pb)
    bind("demo")
    calls: list[str] = []
    _stub_delivery(monkeypatch, qp, calls)
    played: list[str] = []

    class _Runtime:
        async def play_scene(self, name, **kw):
            played.append(name)
            return SimpleNamespace(ok=True)

    monkeypatch.setattr(qp, "get_expression_runtime", lambda dev: _Runtime())
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()),
        registry=FakeRegistry(), dp_broker=None, daily_limit_provider=lambda: 0,
    )
    assert asyncio.run(runner.attempt("dev1")) is True
    assert played == ["happy_smile"] and "做了一个「happy_smile」的表情" in calls[0]
    # 表演模式：找不到表演 → 只说话，不报错
    pb["tasks"][0]["perform"] = {"mode": "scene", "scene": "ghost_scene"}
    svc.save_playbook("demo", pb)
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True
    assert "演了" not in calls[1]
    # 表演存在 → 走 run_device_playbook
    ran: list[str] = []

    async def _fake_run_playbook(downlink, chat, playbook, **kw):
        ran.append(playbook["name"])
        return SimpleNamespace(status="ok", error=None)

    monkeypatch.setattr(qp, "run_device_playbook", _fake_run_playbook)
    monkeypatch.setattr(qp, "load_scene_playbooks_file", lambda: [{"name": "ghost_scene", "title": "打招呼", "chunks": []}])
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True
    assert ran == ["ghost_scene"] and "演了一段「打招呼」" in calls[2]
    # 主线不数次数、不会自动按未达成处理：提了三次还是进行中，等小歪标结果
    rows = {r["task_id"]: r for r in svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo")}
    assert rows["g_greet"]["status"] == "running" and rows["g_greet"]["attempt_count"] == 0
    assert "主动提过" not in calls[2] and "update_task_result" in calls[2]


def test_loop_idle_counts_device_lane_activity(monkeypatch):
    """控制台做的表情 / 动作 / 表演也算交互：arbiter 的 lane 活动会重置空闲计时。"""
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application.turn_arbiter import DeviceTurnArbiter

    arb = DeviceTurnArbiter()
    monkeypatch.setattr(qp, "device_turn_arbiter", arb)
    runner = FakeRunner()

    async def _go():
        from deskbot_server.application.quest_proactive import QuestProactiveLoop

        lp = QuestProactiveLoop(
            runner, asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
            enabled_provider=lambda: True,
            quiet_hours_provider=lambda: False,
        )
        now = time.time()
        lp._first_seen["dev1"] = now - 120
        # 刚跑完一个手动表演 → 算交互
        async def _noop():
            return None

        await arb.run("dev1", _noop, source="http_scene_playbook", priority=60)
        assert arb.last_activity_ts("dev1") > 0
        assert await lp.tick(now=now) == "recent_conversation"
        # 主动陪伴自己的轮不算
        assert arb.last_activity_ts("dev1", exclude=("http_scene_playbook",)) == 0.0
        await _settle(lp)

    asyncio.run(_go())
    assert runner.calls == []
