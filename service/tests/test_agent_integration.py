"""Agent 串联：perform_scene 工具（三条链路）、提醒带表演、skip_goal / propose after、
User.md 回写、带人设的生成入口、主动源门禁、Agent 页能力总览。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.db_helpers import temp_db  # noqa: F401
from tests.quest_helpers import bind, demo_playbook, quest_env  # noqa: F401

# ── perform_scene：能力矩阵 + 目录提示词 ──────────────────────


def test_perform_scene_and_skip_goal_registered_everywhere():
    from deskbot_server.application import tool_executor as te
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    for channel in te.ToolChannel:
        allowed = te.tools_for_channel(channel)
        assert "perform_scene" in allowed and "skip_goal" in allowed
    assert te.is_scene_tool("perform_scene") and not te.is_scene_tool("miot")
    names = {s["name"]: s for s in build_rtc_tool_schemas()}
    assert names["perform_scene"]["parameters"]["required"] == ["name"]
    assert names["skip_goal"]["parameters"]["required"] == ["task_id"]
    assert "scene" in names["schedule_task"]["parameters"]["properties"]
    assert "after" in names["propose_goal"]["parameters"]["properties"]


def test_scene_catalog_prompt_lists_and_caps(monkeypatch):
    from deskbot_server import scene_playbooks_store as store

    rows = [{"name": f"s{i}", "title": f"表演{i}", "chunks": []} for i in range(25)]
    monkeypatch.setattr(store, "load_scene_playbooks_file", lambda **kw: rows)
    cat = store.scene_catalog()
    assert len(cat) == 20 and cat[0] == {"name": "s0", "title": "表演0"}
    text = store.scene_catalog_prompt()
    assert "表演0(s0)" in text and "表演19(s19)" in text and "表演20(s20)" not in text
    assert "perform_scene" in text
    monkeypatch.setattr(store, "load_scene_playbooks_file", lambda **kw: [])
    assert store.scene_catalog_prompt() == ""


def test_scene_catalog_reaches_text_and_rtc_prompts(monkeypatch):
    from deskbot_server.llm import prompt_assembly as pa

    monkeypatch.setattr("deskbot_server.llm.utils.scene_catalog_prompt", lambda: "可用的组合表演：挥手(wave)")
    monkeypatch.setattr("deskbot_server.llm.prompt_assembly.scene_catalog_prompt", lambda: "可用的组合表演：挥手(wave)")
    monkeypatch.setattr("deskbot_server.device_preferences.preferred_time_prompt", lambda: "2026-09-08 14:00")
    text = pa.assemble_text_system_prompt("我是小歪")
    assert "挥手(wave)" in text and text.index("可用工具") < text.index("挥手(wave)")
    monkeypatch.delenv("DESKBOT_RTC_SYSTEM_PROMPT", raising=False)
    rtc = pa.assemble_rtc_system_prompt(cache_loader=lambda: {"devices": []}, quest_loader=lambda: "")
    assert "挥手(wave)" in rtc


# ── perform_scene：三条链路的落地 ─────────────────────────────


def test_core_channel_runs_scene_on_device_lane(monkeypatch):
    from deskbot_server.application import scene_perform as sp
    from deskbot_server.application import tool_executor as te

    # 未配置运行时 → 可读错误
    monkeypatch.setattr(sp, "_state", {"chat": None, "hub": None, "broker": None})
    out = asyncio.run(sp.perform_scene_for_device("dev-1", "wave"))
    assert out["ok"] is False and "还没就绪" in out["error"]

    class _Hub:
        async def first_ws(self, dev):
            return object() if dev == "dev-1" else None

    ran: list[tuple[str, str]] = []

    async def _fake_run_playbook(downlink, chat, pb, *, request_id=None, device_id=None, **kw):
        ran.append((device_id, pb["name"]))
        return SimpleNamespace(status="ok", error=None)

    rows = [{"name": "wave", "title": "挥手问候", "chunks": [{"id": "c1", "text": "你好", "expr": {"scene": "", "ms": 500}, "servo": {"preset": "", "ms": 500}}]}]
    monkeypatch.setattr(sp, "load_scene_playbooks_file", lambda **kw: rows)
    monkeypatch.setattr("deskbot_server.scene_playbooks_store.load_scene_playbooks_file", lambda **kw: rows)
    monkeypatch.setattr(sp, "run_device_playbook", _fake_run_playbook)
    monkeypatch.setattr(sp, "WsDownlinkAdapter", lambda ws, **kw: SimpleNamespace(ws=ws))
    sp.configure(chat=SimpleNamespace(settings=None), asr_chat_hub=_Hub(), dp_broker=None)
    # 按标题点名也行
    out = asyncio.run(sp.perform_scene_for_device("dev-1", "挥手问候"))
    assert out["ok"] is True and out["title"] == "挥手问候" and ran == [("dev-1", "wave")]
    out = asyncio.run(sp.perform_scene_for_device("dev-1", "ghost"))
    assert out["ok"] is False and "wave" in out["error"]
    out = asyncio.run(sp.perform_scene_for_device("dev-2", "wave"))
    assert out["ok"] is False and "没有连接" in out["error"]
    # 执行器 CORE 通道把 perform_scene 路由到这里，其它工具照走 runner
    calls: list[str] = []

    def _fake_runner(tools, *, device_id=None, user_confirmed=False):
        calls.extend(t["tool"] for t in tools)
        return [{"tool": t["tool"], "ok": True} for t in tools]

    monkeypatch.setattr("deskbot_server.application.llm_tool_runner.execute_llm_tools", _fake_runner)
    res = asyncio.run(
        te.execute_tools_async(
            [{"tool": "miot", "action": "list"}, {"tool": "perform_scene", "name": "wave"}],
            channel=te.ToolChannel.CORE, device_id="dev-1", user_confirmed=False, asr_chat_hub=None,
        )
    )
    assert [r["tool"] for r in res] == ["miot", "perform_scene"] and res[1]["ok"] is True
    assert calls == ["miot"] and len(ran) == 2


def test_web_channel_forwards_scene_to_core_http(monkeypatch):
    from deskbot_server.application import tool_executor as te
    from deskbot_server.application.web_chat_capture import run_core_scene

    posted: list[tuple[str, dict]] = []

    def _post(url, body):
        posted.append((url, json.loads(body)))
        if b"ghost" in body:
            return 404, "application/json", b'{"ok": false, "error": "playbook_not_found"}'
        return 200, "application/json", b'{"ok": true}'

    out = run_core_scene("dev-1", "wave", base_url="http://core", post=_post)
    assert out == {"tool": "perform_scene", "ok": True, "name": "wave", "status": "played"}
    assert posted[0] == ("http://core/api/scene_playbook/run", {"device_id": "dev-1", "name": "wave"})
    assert "没有叫" in run_core_scene("dev-1", "ghost", base_url="http://core", post=_post)["error"]
    assert "选择设备" in run_core_scene("", "wave", base_url="http://core", post=_post)["error"]
    monkeypatch.setattr("deskbot_server.application.web_chat_capture.run_core_scene",
                        lambda dev, name, fetch=None: {"tool": "perform_scene", "ok": True, "name": name})
    monkeypatch.setattr("deskbot_server.application.llm_tool_runner.execute_llm_tools",
                        lambda tools, **kw: [{"tool": t["tool"], "ok": True} for t in tools])
    res = te.execute_tools_sync(
        [{"tool": "memory_add", "text": "x"}, {"tool": "perform_scene", "name": "wave"}, {"tool": "miot", "action": "list"}],
        channel=te.ToolChannel.WEB, device_id="dev-1",
    )
    assert [r["tool"] for r in res] == ["memory_add", "perform_scene", "miot"]


# ── skip_goal / propose after / User.md 回写 ─────────────────


def test_skip_goal_and_propose_after_and_user_doc_writeback(quest_env, monkeypatch):
    from deskbot_server import agent_docs
    from deskbot_server.application import quest_console as qc
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    docs: dict[str, str] = {"User.md": "# 主人\n"}
    monkeypatch.setattr(agent_docs, "read_doc", lambda name: docs.get(agent_docs.resolve(name) or name, ""))
    monkeypatch.setattr(agent_docs, "write_doc", lambda name, text: docs.__setitem__(agent_docs.resolve(name) or name, text) or {"name": name, "bytes": len(text)})
    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    # 达成 → User.md 追加一行
    out = execute_llm_tools([{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": "主人回了个你好"}], device_id="dev-1")
    assert out[0]["ok"] is True
    assert "陪伴小目标「初次问候」达成：主人回了个你好" in docs["User.md"] and docs["User.md"].startswith("# 主人")
    # skip_goal → 跳过 + 原话 + 沿失败边推进
    out = execute_llm_tools([{"tool": "skip_goal", "task_id": "g_learn_name", "reason": "别问我名字了"}], device_id="dev-1")
    assert out[0]["ok"] is True and out[0]["task"]["status"] == "skipped"
    assert "主人说不用了：别问我名字了" in out[0]["task"]["result"]
    assert out[0]["activated"][0]["task_id"] == "g_learn_name_soften"
    assert "达成" not in docs["User.md"].split("\n")[-2] or docs["User.md"].count("达成") == 1  # 未达成不回写
    # propose 带 after → 选主线同意时插在它后面（默认同意是日常关心，不进链）
    out = execute_llm_tools([{"tool": "propose_goal", "title": "关心作息", "goal": "别熬夜", "after": "g_learn_name_soften"}], device_id="dev-1")
    assert out[0]["ok"] is True
    view = qc.overview()
    assert view["proposals"][0]["proposed_after"] == "g_learn_name_soften" and view["proposals"][0]["kind"] == "care"
    task = qc.approve_proposal("care", "p1", kind="story")  # 提议挂在日常关心；选主线 → 搬进 demo，after=None → 用它自己提的
    assert task["kind"] == "story" and task["order"] == 4  # 插在 g_learn_name_soften（第 3 步）之后
    # after 指向不存在的任务 → 忽略
    out = execute_llm_tools([{"tool": "propose_goal", "title": "x", "goal": "y", "after": "ghost"}], device_id="dev-1")
    assert out[0]["ok"] is True and qc.overview()["proposals"][0]["proposed_after"] == ""


# ── 带人设的生成入口 ────────────────────────────────────────


def test_generate_json_prefixes_persona_and_parses(monkeypatch):
    from deskbot_server.application import persona_generation as pg

    captured: dict = {}

    def _fake_chat(messages, *, temperature, config, **kw):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return 'noise {"title": "作息", "tasks": [{"goal": "g"}]} tail', {}

    monkeypatch.setattr(pg, "chat_completion", _fake_chat)
    monkeypatch.setattr(pg, "resolve_llm_config", lambda: object())
    monkeypatch.setattr(pg.agent_docs, "system_prompt", lambda: "你是小歪，叫主人老板")
    monkeypatch.setattr(pg.agent_docs, "user_prompt_appendix", lambda: "主人是程序员\n- 2026-09-10 陪伴小目标「知道怎么称呼主人」达成：叫小朋友")
    data = pg.generate_json("只输出 JSON", "关心我的作息")
    assert data["title"] == "作息"
    assert captured["system"].startswith("你的人设") and "叫主人老板" in captured["system"]
    assert "主人是程序员" in captured["system"] and captured["system"].endswith("只输出 JSON")
    # 人设只当背景：明说回复格式这次不适用；User.md 里「陪伴小目标 … 达成」的记录不给模型看（它会照抄成新目标）
    assert "这次一律不适用" in captured["system"] and "陪伴小目标" not in captured["system"]
    # 模型把短句当成聊天（回 need_reply/tts）→ 自动重试一次，还是聊天格式就报可读错误
    answers = iter(['{"need_reply": true, "tts": "好呀几点？"}', '{"title": "健身", "tasks": [{"goal": "提醒健身"}]}'])
    seen_systems: list[str] = []

    def _chat_twice(messages, *, temperature, config, **kw):
        seen_systems.append(messages[0]["content"])
        return next(answers), {}

    monkeypatch.setattr(pg, "chat_completion", _chat_twice)
    assert pg.generate_json("只输出 JSON", "每天晚上提醒我健身")["title"] == "健身"
    assert len(seen_systems) == 2 and "聊天回复格式" in seen_systems[1]
    monkeypatch.setattr(pg, "chat_completion", lambda *a, **kw: ('{"need_reply": true, "tts": "嗯"}', {}))
    with pytest.raises(pg.GenerationError, match="当成了聊天"):
        pg.generate_json("只输出 JSON", "每天晚上提醒我健身")
    monkeypatch.setattr(pg, "chat_completion", lambda *a, **kw: ("no json here", {}))
    with pytest.raises(pg.GenerationError):
        pg.generate_json("x", "y")
    with pytest.raises(pg.GenerationError):
        pg.generate_json("x", "")


def test_quest_generate_and_ai_arrange_use_persona(quest_env, monkeypatch):
    from deskbot_server.application import persona_generation as pg
    from deskbot_server.web.app import create_app
    from deskbot_server.web.blueprints import quest_bp

    monkeypatch.setattr(quest_bp, "_core_call", lambda *a, **kw: (0, {}))
    seen: dict = {}

    def _fake_generate(instruction, user_text, *, temperature=0.7):
        seen["instruction"] = instruction
        seen["user"] = user_text
        if not str(user_text or "").strip():
            raise pg.GenerationError("先描述一下想要什么")
        if "陪伴场景" in instruction:
            assert "care" in instruction and "米家" in instruction  # 生成说明：主线 / 日常关心怎么分，小歪能做什么
            assert "已有的小目标（不要再列" in instruction and "第一次打招呼" in instruction  # 已有的小目标名一并告诉模型
            return {"title": "作息", "tasks": [
                {"title": "几点起", "goal": "知道主人几点起床", "strategy": "顺口问", "success_condition": "说了", "failure_condition": "不说"},
                {"title": "第一次打招呼", "goal": "再打个招呼"},  # 抄了已有的小目标 → 丢掉
                {"title": "别熬夜", "goal": "提醒主人别熬夜", "repeatable": True},
                {"kind": "care", "title": "冷笑话", "goal": "偶尔讲个冷笑话", "trigger_hint": "主人闲下来", "repeat_interval_sec": 14400},
                {"kind": "care", "title": "放首歌", "goal": "到点用音箱放首歌", "schedule_time": "18:30", "repeat_interval_sec": 999},
                {"goal": ""},
            ]}
        return {"name": "morning", "title": "早安", "chunks": [{"text": "早呀老板", "expr": {"scene": "happy", "ms": 800}, "servo": {"preset": "nod", "ms": 500}}]}

    monkeypatch.setattr("deskbot_server.application.quest_console.generate_json", _fake_generate)
    monkeypatch.setattr("deskbot_server.application.persona_generation.generate_json", _fake_generate)
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    ov = client.post("/api/quest/playbooks/generate", json={"description": "关心我的作息"}).get_json()
    assert ov["ok"] is True and ov["playbook"]["title"] == "作息"
    tasks = ov["tasks"]
    assert [t["title"] for t in tasks] == ["几点起", "别熬夜"]
    assert tasks[0]["status"] == "running" and tasks[1]["order"] == 2 and tasks[1]["status"] == "not_started"
    assert tasks[1]["repeatable"] is False and seen["user"] == "关心我的作息"  # 主线不回头
    # 反复做的（kind=care）进「日常关心」：看情况的带频率，定时的带钟点；非法频率回默认
    assert ov["generated"] == {"story_count": 2, "care_count": 2, "care_titles": ["冷笑话", "放首歌"], "dropped": ["第一次打招呼"],
                               "playbook_name": ov["playbook"]["name"], "playbook_title": "作息"}
    assert "playbook" not in {k for k in ov if k == "playbook" and ov[k] is None}  # playbook 键仍是总览的当前场景
    care = {t["title"]: t for t in ov["care_tasks"]}
    assert care["冷笑话"]["kind"] == "care" and care["冷笑话"]["repeat_interval_sec"] == 14400 and care["冷笑话"]["trigger_hint"] == "主人闲下来"
    assert care["放首歌"]["schedule_time"] == "18:30" and care["放首歌"]["repeat_interval_sec"] == 86400
    assert "冷笑话" not in [t["title"] for t in tasks]
    assert client.post("/api/quest/playbooks/generate", json={"description": ""}).status_code == 502
    # 组合表演的 AI 编排也走同一入口（人设由 generate_json 前置）
    r = client.post("/api/playbooks/ai_generate", json={"description": "早安", "expressions": ["happy"], "presets": [{"id": "nod", "label": "点头"}]})
    assert r.status_code == 200, r.get_json()
    pb = r.get_json()["playbook"]
    assert pb["chunks"][0]["text"] == "早呀老板" and pb["chunks"][0]["expr"]["scene"] == "happy"
    assert "编一段表演" in seen["instruction"]


# ── 主动源门禁 ───────────────────────────────────────────────


def test_proactive_gate_rules(monkeypatch):
    """2026-09-14：定时任务并进定时提醒后，门禁只剩勿扰与 lane 忙。"""
    from deskbot_server.application import proactive_gate as gate
    from deskbot_server.application.turn_arbiter import DeviceTurnArbiter

    monkeypatch.setattr(gate, "quiet_hours_active", lambda: False)
    assert gate.can_be_proactive(gate.SOURCE_QUEST) == (True, "")
    assert gate.can_be_proactive(gate.SOURCE_LIVE, "dev") == (True, "")
    assert not hasattr(gate, "seconds_until_next_reminder")
    monkeypatch.setattr(gate, "quiet_hours_active", lambda: True)
    assert gate.can_be_proactive(gate.SOURCE_LIVE, "dev") == (False, "quiet_hours")

    arb = DeviceTurnArbiter()
    monkeypatch.setattr(gate, "device_turn_arbiter", arb)
    monkeypatch.setattr(gate, "quiet_hours_active", lambda: False)

    async def _go():
        started = asyncio.Event()
        release = asyncio.Event()

        async def _busy():
            started.set()
            await release.wait()

        task = asyncio.ensure_future(arb.run("dev", _busy, source="scheduled_task", priority=20))
        await started.wait()
        assert gate.can_be_proactive(gate.SOURCE_LIVE, "dev") == (False, "lane_busy")
        release.set()
        await task
        assert gate.can_be_proactive(gate.SOURCE_LIVE, "dev") == (True, "")

    asyncio.run(_go())


def test_quest_loop_and_live_behavior_respect_gate(monkeypatch):
    from deskbot_server.application import live_behavior as lb
    from deskbot_server.application.quest_proactive import QuestProactiveLoop
    from tests.test_quest_proactive import FakeHub, FakeRegistry, FakeRunner, _settle

    runner = FakeRunner()

    async def _go():
        lp = QuestProactiveLoop(
            runner, asr_chat_hub=FakeHub(), registry=FakeRegistry(), idle_sec=60.0,
            activity_ts_provider=lambda dev: 0.0, enabled_provider=lambda: True, quiet_hours_provider=lambda: False, gate=lambda src, dev: (False, "reminder_soon"),
        )
        lp._first_seen["dev1"] = 1_000_000.0
        assert await lp.tick(now=1_000_000.0 + 61) == "reminder_soon"
        await _settle(lp)

    asyncio.run(_go())
    assert runner.calls == []

    service = lb.LiveBehaviorService()
    sent: list = []

    async def _fake_send(hub, device_id, moves, **kw):
        sent.append(moves)

    monkeypatch.setattr(lb, "send_servo_moves_and_wait", _fake_send)
    monkeypatch.setattr(lb, "get_expression_runtime", lambda dev: SimpleNamespace(snapshot=lambda: {"displayed_state": "idle"}))
    monkeypatch.setattr(lb, "load_preferences", lambda: {"behavior": {"idle_live": True, "wander_idle_sec": 10}})
    service._hub = object()
    service._gate = lambda src, dev: (False, "quiet_hours")

    async def _live():
        st = service._dev("dev")
        for _ in range(3):
            await service._tick("dev", st)
        return st

    st = asyncio.run(_live())
    assert sent == [] and st.block_reason == "quiet_hours"  # 门禁原因原样透出到状态里


# ── Agent 页能力总览 ────────────────────────────────────────


def test_agent_page_lists_abilities(quest_env, monkeypatch):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    html = app.test_client().get("/agent").get_data(as_text=True)
    assert "它会做的事" in html and "loadAbilities" in html
    for path in ("/api/quest/overview", "/proxy/deskbot/api/scene_playbooks", "/app/api/miot/status"):
        assert path in html
    assert "定时提醒" in html and "/app/api/scheduled-tasks" not in html
