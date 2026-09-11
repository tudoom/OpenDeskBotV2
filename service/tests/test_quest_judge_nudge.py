"""2026-09-10 晚：小歪开口后一直没标结果的两个根因与修法。

现象（用户日志 20:16）：g_greet 打完招呼，语音 Agent 这次没调 update_task_result，页面一直「等小歪标结果」；
另外 20:15 那次标了结果后 13 s 就「接下一个」，其实小歪还在说话——安静计时没算上它自己。
修法：安静计时算上小歪在说 / 在想（表情运行时显示态）；开口 45 s 还没标且没人在说 → 催语音 Agent
只做判定、只调工具（tool_choice=required），一次开口只催一次。
"""

from __future__ import annotations

import asyncio
import time

from tests.quest_helpers import bind, clear_care_scene, demo_playbook, quest_env  # noqa: F401
from tests.test_quest_proactive import FakeHub, FakeRegistry, FakeRunner


def _dev(device_id: str):
    async def _provider():
        return device_id

    return _provider


def _loop(qp, runner, *, state: dict, activity: dict, monkeypatch):
    loop = qp.QuestProactiveLoop(
        runner, asr_chat_hub=FakeHub(ws=object()), registry=FakeRegistry(), idle_sec=120.0,
        activity_ts_provider=lambda dev: activity["t"], enabled_provider=lambda: True,
        quiet_hours_provider=lambda: False, device_provider=_dev("dev1"),
        runtime_state_provider=lambda dev: state["s"],
    )
    monkeypatch.setattr(loop, "_gate", lambda *a, **k: (True, ""))
    return loop


def test_quiet_timer_counts_the_robot_talking(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    svc.get_current_tasks()
    calls: list[dict] = []

    class ChainRunner(FakeRunner):
        async def attempt(self, device_id, *, ignore_limits=False, task_id=None, scheduled_hit="", chain=False):
            calls.append({"task_id": task_id, "chain": chain})
            return True

    state = {"s": "speaking"}
    activity = {"t": 0.0}
    loop = _loop(qp, ChainRunner(), state=state, activity=activity, monkeypatch=monkeypatch)
    svc.update_task_result("local", "demo", "g_greet", "success", "回了")
    t0 = 50_000.0
    # 小歪自己还在说：主人没说话也不算安静，接下一个得等
    assert asyncio.run(loop.tick(now=t0)) == "chain_wait"
    assert asyncio.run(loop.tick(now=t0 + 9)) == "chain_wait"
    state["s"] = "listening"  # 倾听脸不算忙：VAD 误报会让它挂很久，主人真说话由 activity_ts 记
    assert asyncio.run(loop.tick(now=t0 + 15)) == "chain_wait"  # 说完 6 s，还没到 10 s
    assert asyncio.run(loop.tick(now=t0 + 20)) == ""  # 说完 11 s → 接下一个
    assert calls == [{"task_id": "g_learn_name", "chain": True}]


def test_loop_nudges_agent_to_mark_once_after_speaking(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application import rtc_instructions as ri

    clear_care_scene()
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    svc.get_current_tasks()
    ri._reset_for_tests()
    ri.configure(attached_provider=lambda dev: True)
    runner = FakeRunner()
    state = {"s": "idle"}
    activity = {"t": 0.0}
    loop = _loop(qp, runner, state=state, activity=activity, monkeypatch=monkeypatch)
    monkeypatch.setattr(loop, "_gate", lambda *a, **k: (False, "reminder_soon"))  # 只看催办，不真的开口
    t0 = time.time() + 5  # 得晚于实例的 started_at（真实钟）：早于它就当作「重开过了」不催
    runner.last_turn = {"task_id": "g_greet", "status": "handed_to_rtc_agent", "at": t0, "voice_ok": True}
    # 开口 20 s：还不催；45 s 后没人说话 → 催一次，只调工具
    asyncio.run(loop.tick(now=t0 + 20))
    assert ri.drain("dev1") == []
    asyncio.run(loop.tick(now=t0 + 46))
    items = ri.drain("dev1")
    assert len(items) == 1 and items[0]["source"] == "quest_judge" and items[0]["tool_choice"] == "required"
    assert items[0]["tools"] == ["update_task_result", "skip_goal"]
    text = items[0]["text"]
    assert "g_greet" in text and "update_task_result" in text and "不要说话" in text and "用户回应了问候" in text
    # 同一次开口不再催第二次；重新计时 / 标了结果之后更不会催
    asyncio.run(loop.tick(now=t0 + 120))
    assert ri.drain("dev1") == []
    runner.last_turn = {"task_id": "g_greet", "status": "handed_to_rtc_agent", "at": t0 + 200, "voice_ok": True}
    svc.update_task_result("local", "demo", "g_greet", "unmet", "没回")
    svc.clear_chain()
    asyncio.run(loop.tick(now=t0 + 300))
    assert ri.drain("dev1") == []  # 已有结果：g_greet 不在进行中，不催
    # 主人还在说话时不催（安静不够 8 s）；g_learn_name 刚重开，开口时间要晚于它的 started_at
    time.sleep(0.01)
    runner.last_turn = {"task_id": "g_learn_name", "status": "handed_to_rtc_agent", "at": t0 + 400, "voice_ok": True}
    activity["t"] = t0 + 447
    asyncio.run(loop.tick(now=t0 + 450))
    assert ri.drain("dev1") == []
    activity["t"] = t0 + 440
    asyncio.run(loop.tick(now=t0 + 450))
    assert [i["source"] for i in ri.drain("dev1")] == ["quest_judge"]
    ri._reset_for_tests()
    ri.configure(attached_provider=None)


def test_worker_passes_tool_choice_through(monkeypatch):
    from deskbot_server import rtc_livekit_plugins as plugins
    from deskbot_server.application import rtc_instructions as ri

    calls: list[dict] = []

    class _Session:
        def generate_reply(self, **kw):
            calls.append(kw)

    ri._reset_for_tests()
    ri.enqueue("dev", "只判定", source="quest_judge", tool_choice="required", tools=["update_task_result", "skip_goal"])
    ri.enqueue("dev", "普通说一句", source="quest_proactive")
    items = ri.drain("dev")
    assert items[0]["tool_choice"] == "required" and "tool_choice" not in items[1] and "tools" not in items[1]
    plugins._deskbot_apply_instructions(_Session(), [items[0]])
    plugins._deskbot_apply_instructions(_Session(), [items[1]])
    assert calls == [
        {"instructions": "只判定", "allow_interruptions": True, "tool_choice": "required", "tools": ["update_task_result", "skip_goal"]},
        {"instructions": "普通说一句", "allow_interruptions": True},
    ]
    ri._reset_for_tests()


def test_page_has_no_advance_line(quest_env):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/quest").get_data(as_text=True)
    assert "<b>进入下一个</b>" not in html and "advanceLabel" not in html


def test_quest_result_tools_do_not_trigger_follow_up_in_instruction_turns(monkeypatch):
    """打招呼说两遍的根因：指令轮里模型说完话又调 update_task_result，LiveKit 拿着同一条指令再生成一轮。
    指令轮（generate_reply(instructions=…) 的 SpeechHandle）里这几个工具返回 None → 不追答；普通对话里照旧。"""
    import asyncio
    from types import SimpleNamespace

    from deskbot_server import rtc_livekit_plugins as plugins
    from deskbot_server import rtc_worker_tools as worker_tools

    class _Session:
        def __init__(self):
            self.calls = []

        def generate_reply(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(id=f"speech-{len(self.calls)}")

    session = _Session()
    assert plugins._deskbot_apply_instructions(session, [{"id": "a", "text": "打个招呼", "source": "quest_proactive"}]) == 1
    assert session._deskbot_instruction_speech_ids == ["speech-1"]

    async def _fake_core(_ctx, tool, raw_arguments):
        return {"tool": tool, "ok": True}

    monkeypatch.setattr(worker_tools, "_call_core_tool", _fake_core)
    tools = {row.id: row for row in worker_tools.build_livekit_deskbot_tools()}

    def _call(name, speech_id):
        tool = tools[name]
        callback = getattr(tool, "_func", None) or getattr(tool, "func", None)
        ctx = SimpleNamespace(session=session, speech_handle=SimpleNamespace(id=speech_id), function_call=SimpleNamespace(call_id="c", id="c"))
        return asyncio.run(callback({"task_id": "g_greet", "status": "success", "result": "ok"}, ctx))

    assert _call("update_task_result", "speech-1") is None  # 指令轮：不追答
    assert _call("skip_goal", "speech-1") is None
    assert _call("update_task_result", "speech-9") == {"tool": "update_task_result", "ok": True}  # 普通对话轮：照旧
    assert _call("memory_add", "speech-1") == {"tool": "memory_add", "ok": True}  # 别的工具不受影响
    # 老的占位 session（generate_reply 返回 None）也不报错
    class _Bare:
        def generate_reply(self, **kw):
            return None

    assert plugins._deskbot_apply_instructions(_Bare(), [{"text": "x"}]) == 1
