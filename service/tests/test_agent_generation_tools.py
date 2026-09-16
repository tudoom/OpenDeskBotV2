"""2026-09-14：控制台五个「AI 生成」功能全部接到对话 Agent（语音 / 文字 / Core 三条链路），
move_head / play_expression 也补到文字链路。"""

from __future__ import annotations

import asyncio
import json

import pytest

from deskbot_server.application import generation_jobs
from deskbot_server.application.agent_generation_tools import ChannelOps, execute_generation_tool

GEN_TOOLS = ("generate_scene", "generate_quest_scene", "generate_motion", "generate_cartoon_faces", "generate_expression", "generation_status")


def _ops(**overrides) -> ChannelOps:
    calls: list = []
    base = dict(
        device_id="dev-1",
        perform_scene=lambda name: (calls.append(("perform", name)), {"ok": True, "name": name})[1],
        move_head=lambda args: (calls.append(("move", dict(args))), {"ok": True, "status": "moved"})[1],
        play_expression=lambda args: (calls.append(("play", dict(args))), {"ok": True})[1],
        apply_default_expression=lambda: (calls.append(("apply", None)), {"ok": True, "status": "accepted"})[1],
        capture_frame=lambda: b"\xff\xd8\xff-jpeg",
        announce=lambda text: calls.append(("announce", text)),
    )
    base.update(overrides)
    ops = ChannelOps(**base)
    ops.calls = calls  # type: ignore[attr-defined]
    return ops


# ---------------- 能力矩阵 / schema / 提示词 ----------------


def test_generation_and_device_tools_registered_on_every_channel():
    from deskbot_server.application import tool_executor as te
    from deskbot_server.application.rtc_tool_service import rtc_tool_names
    from deskbot_server.rtc_worker_tools import _TOOL_HTTP_TIMEOUT_SECONDS, build_rtc_tool_schemas

    for channel in te.ToolChannel:
        allowed = te.tools_for_channel(channel)
        for name in (*GEN_TOOLS, "move_head", "play_expression"):
            assert name in allowed, (channel, name)
    assert te.is_generation_tool("generate_scene") and te.is_device_tool("move_head") and not te.is_device_tool("miot")
    assert set(GEN_TOOLS) <= rtc_tool_names()
    schemas = {s["name"]: s for s in build_rtc_tool_schemas()}
    for name in GEN_TOOLS:
        assert name in schemas, name
    assert schemas["generate_scene"]["parameters"]["required"] == ["description"]
    assert schemas["generate_quest_scene"]["parameters"]["required"] == ["description"]
    assert schemas["generate_motion"]["parameters"]["required"] == ["description"]
    assert set(schemas["generate_cartoon_faces"]["parameters"]["properties"]["style"]["enum"]) >= {"kawaii", "lineart", "doodle", "flat"}
    assert schemas["generate_expression"]["parameters"]["properties"]["source"]["enum"] == ["text", "camera"]
    # 生成要等大模型：桥的 HTTP 超时单独放宽
    assert _TOOL_HTTP_TIMEOUT_SECONDS["generate_scene"] >= 90 and _TOOL_HTTP_TIMEOUT_SECONDS["generate_expression"] >= 90


def test_text_prompt_documents_generation_and_device_tools(monkeypatch):
    from deskbot_server.llm import prompt_assembly as pa
    from deskbot_server.llm.utils import (
        llm_device_catalog_prompt_appendix,
        llm_tools_prompt_appendix,
    )

    text = llm_tools_prompt_appendix()
    for name in (*GEN_TOOLS, "play_expression", "move_head"):
        assert f'"tool":"{name}"' in text or f"- {name}:" in text, name
    monkeypatch.setattr("deskbot_server.application.expression_catalog.expression_tool_catalog", lambda: {"values": ["happy", "sad"]})
    monkeypatch.setattr("deskbot_server.servo_config_store.servo_preset_catalog", lambda **kw: [{"id": "nod", "label": "点头"}])
    catalog = llm_device_catalog_prompt_appendix()
    assert "可用表情" in catalog and "happy" in catalog and "nod(点头)" in catalog
    monkeypatch.delenv("DESKBOT_RTC_SYSTEM_PROMPT", raising=False)
    rtc = pa.assemble_rtc_system_prompt(cache_loader=lambda: {"devices": []}, quest_loader=lambda: "")
    assert "generate_cartoon_faces" in rtc and "generate_scene" in rtc


# ---------------- 表演编排：网页与工具共用一份实现 ----------------


def test_compose_scene_playbook_whitelists_names(monkeypatch):
    from deskbot_server.application import persona_generation as pg
    from deskbot_server.application.agent_generation import compose_scene_playbook

    seen: dict = {}

    def _fake(instruction, user_text, *, temperature=0.7):
        seen["instruction"] = instruction
        return {"name": "Morning Show!", "title": "早安", "chunks": [
            {"text": "早呀", "expr": {"scene": "happy", "ms": 500}, "servo": {"preset": "nod", "ms": 20000}},
            {"text": "", "expr": {"scene": "made_up", "ms": 500}, "servo": {"preset": "ghost", "ms": 500}},
            {"text": "拜拜", "expr": {"scene": "", "ms": 500}, "servo": {"preset": "", "ms": 500}},
        ]}

    monkeypatch.setattr(pg, "generate_json", _fake)
    pb = compose_scene_playbook("早安", expressions=["happy"], presets=[{"id": "nod", "label": "点头"}])
    assert pb["name"] == "morning_show" and pb["title"] == "早安"
    assert [c["text"] for c in pb["chunks"]] == ["早呀", "拜拜"]  # 全是编造名称的空步骤被丢掉
    assert pb["chunks"][0]["servo"] == {"preset": "nod", "ms": 10000}  # ms 钳位
    assert "happy" in seen["instruction"] and "nod(点头)" in seen["instruction"]
    with pytest.raises(pg.GenerationError):
        compose_scene_playbook("", expressions=[], presets=[])


def test_generate_scene_playbook_saves_unique_name_then_tool_performs(monkeypatch):
    from deskbot_server.application import agent_generation as ag

    saved: dict = {}
    rows = [{"name": "ai_scene", "title": "旧", "chunks": [{"text": "x"}]}]
    monkeypatch.setattr("deskbot_server.scene_playbooks_store.load_scene_playbooks_file", lambda **kw: list(rows))
    monkeypatch.setattr("deskbot_server.scene_playbooks_store.save_scene_playbooks_file", lambda new_rows: saved.setdefault("rows", new_rows))
    monkeypatch.setattr("deskbot_server.servo_config_store.servo_preset_catalog", lambda **kw: [{"id": "nod", "label": "点头"}])

    class _Scene:
        name = "happy"

    class _Catalog:
        scenes = [_Scene()]

    monkeypatch.setattr("deskbot_server.application.expression_catalog.load_expression_catalog", lambda: _Catalog())
    monkeypatch.setattr(ag, "compose_scene_playbook", lambda d, **kw: {"name": "ai_scene", "title": "新", "chunks": [{"text": "嗨", "expr": {"scene": "happy", "ms": 500}, "servo": {"preset": "nod", "ms": 500}}]})
    out = ag.generate_scene_playbook("打个招呼")
    assert out["ok"] and out["name"] == "ai_scene_2" and out["steps"] == 1 and out["outline"] == ["嗨（happy/nod）"]
    assert [r["name"] for r in saved["rows"]] == ["ai_scene", "ai_scene_2"]

    monkeypatch.setattr(ag, "generate_scene_playbook", lambda d: {"ok": True, "name": "ai_scene_2", "title": "新", "steps": 1, "outline": ["嗨"]})
    ops = _ops()
    res = execute_generation_tool({"tool": "generate_scene", "description": "打个招呼"}, ops=ops)
    assert res["ok"] and res["performed"] is True and ops.calls == [("perform", "ai_scene_2")]
    res = execute_generation_tool({"tool": "generate_scene", "description": "打个招呼", "perform": False}, ops=_ops())
    assert res["performed"] is False
    assert execute_generation_tool({"tool": "generate_scene"}, ops=_ops())["ok"] is False


# ---------------- 动作：生成 + 存预设 + 立刻做 ----------------


def test_compose_motion_preset_clamps_to_envelope(monkeypatch):
    from deskbot_server.application.agent_generation import compose_motion_preset

    monkeypatch.setattr("deskbot_server.llm.runtime.resolve_llm_config", lambda: object())
    monkeypatch.setattr(
        "deskbot_server.llm.runtime.chat_completion",
        lambda messages, **kw: ('```json {"id":"Look Around","label":"张望一下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下下","steps":[{"x":999,"y":-5,"ms":9},{"x":90,"y":90,"ms":800}]} ```', {}),
    )
    preset = compose_motion_preset("左右张望", envelope={"xMin": 30, "xMax": 150, "yMin": 78, "yMax": 110}, existing_ids=["look_around"])
    assert preset["id"] == "look_around_2" and len(preset["label"]) == 40
    assert preset["steps"][0] == {"x": 150, "y": 78, "xm": 0, "ym": 0, "ms": 200}
    assert preset["steps"][1]["ms"] == 800


def test_generate_motion_preset_saves_model_visible_preset_and_tool_executes(monkeypatch):
    from deskbot_server.application import agent_generation as ag

    cfg = {"xMin": 30, "xMax": 150, "yMin": 78, "yMax": 110, "presets": [{"id": "nod", "label": "点头", "steps": [{"x": 90, "y": 100, "ms": 300}]}]}
    saved: dict = {}
    monkeypatch.setattr("deskbot_server.servo_config_store.load_servo_cfg_file", lambda: json.loads(json.dumps(cfg)))
    monkeypatch.setattr("deskbot_server.servo_config_store.save_servo_cfg_file", lambda doc: saved.setdefault("cfg", doc))
    monkeypatch.setattr(ag, "compose_motion_preset", lambda d, **kw: {"id": "wiggle", "label": "扭一扭", "steps": [{"x": 60, "y": 90, "xm": 0, "ym": 0, "ms": 300}]})
    out = ag.generate_motion_preset("扭一扭", label="小扭")
    assert out["ok"] and out["id"] == "wiggle" and out["label"] == "小扭"
    ids = [p["id"] for p in saved["cfg"]["presets"]]
    assert ids == ["nod", "wiggle"] and saved["cfg"]["presets"][1]["exposeToModel"] is True

    monkeypatch.setattr(ag, "generate_motion_preset", lambda d, **kw: {"ok": True, "id": "wiggle", "label": "扭一扭", "steps": []})
    ops = _ops()
    res = execute_generation_tool({"tool": "generate_motion", "description": "扭一扭"}, ops=ops)
    assert res["ok"] and res["executed"] is True and ops.calls == [("move", {"move": "wiggle"})]


# ---------------- 陪伴场景 ----------------


def test_generate_quest_scene_wraps_console_generation(monkeypatch):
    from deskbot_server.application import quest_console

    seen: dict = {}

    def _fake(description, *, title="", select=True):
        seen.update(description=description, title=title, select=select)
        return {"playbook": {"name": "q1", "title": "作息"}, "story_count": 2, "care_count": 1, "care_titles": ["喝水"], "dropped": []}

    monkeypatch.setattr(quest_console, "generate_playbook", _fake)
    res = execute_generation_tool({"tool": "generate_quest_scene", "description": "关心我的作息", "title": "作息"}, ops=_ops())
    assert res["ok"] and res["playbook_name"] == "q1" and res["story_count"] == 2 and res["care_titles"] == ["喝水"]
    assert seen == {"description": "关心我的作息", "title": "作息", "select": True}


# ---------------- 卡通套图：后台任务 + 完成后开口 ----------------


def test_generate_cartoon_faces_runs_as_background_job_and_announces(monkeypatch):
    from deskbot_server.application import agent_generation as ag

    generation_jobs._reset_for_tests()
    seen: dict = {}

    def _fake_set(description, *, style, progress=None, frames_per_state=3):
        seen["style"] = style
        progress and progress("画中")
        return {s: ["a", "b", "c"] for s in ag.CARTOON_STATES}

    def _fake_save(frames, *, set_tag, apply):
        seen["apply"] = apply
        return {"created": [{"name": f"user_{s}", "title": s} for s in ag.CARTOON_STATES], "applied": apply}

    monkeypatch.setattr(ag, "generate_cartoon_set", _fake_set)
    monkeypatch.setattr(ag, "save_cartoon_set", _fake_save)
    # 测试里同步跑后台任务（thread=False），结果与开口都能立刻断言
    real_start = generation_jobs.start_job
    monkeypatch.setattr(generation_jobs, "start_job", lambda kind, fn, **kw: real_start(kind, fn, **{**kw, "thread": False}))

    ops = _ops()
    res = execute_generation_tool({"tool": "generate_cartoon_faces", "description": "戴眼镜的小猫", "style": "lineart"}, ops=ops)
    assert res["ok"] and res["status"] == "running" and res["job_id"] and res["eta_seconds"] >= 180
    assert seen == {"style": "lineart", "apply": True}
    assert ("apply", None) in ops.calls
    announced = [c[1] for c in ops.calls if c[0] == "announce"]
    assert len(announced) == 1 and "已经换到你脸上了" in announced[0]
    status = execute_generation_tool({"tool": "generation_status", "job_id": res["job_id"]}, ops=_ops())
    assert status["ok"] and status["status"] == "done" and [c["name"] for c in status["result"]["created"]] == ["user_idle", "user_listening", "user_thinking", "user_speaking"]
    latest = execute_generation_tool({"tool": "generation_status"}, ops=_ops())
    assert latest["job_id"] == res["job_id"]
    # 失败也要开口告诉主人
    monkeypatch.setattr(ag, "generate_cartoon_set", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("配额用完")))
    ops2 = _ops()
    res2 = execute_generation_tool({"tool": "generate_cartoon_faces", "apply": False}, ops=ops2)
    failed = execute_generation_tool({"tool": "generation_status", "job_id": res2["job_id"]}, ops=_ops())
    assert failed["ok"] is False and failed["status"] == "failed" and "配额用完" in failed["error"]
    assert any(c[0] == "announce" and "失败" in c[1] for c in ops2.calls)
    generation_jobs._reset_for_tests()


def test_generation_jobs_registry_tracks_progress_and_caps():
    generation_jobs._reset_for_tests()
    for i in range(generation_jobs.MAX_JOBS + 3):
        generation_jobs.start_job("t", lambda progress, i=i: (progress(f"step {i}"), {"i": i})[1], summary=f"j{i}", thread=False)
    jobs = generation_jobs.list_jobs()
    assert len(jobs) == generation_jobs.MAX_JOBS and jobs[-1]["status"] == "done" and jobs[-1]["result"] == {"i": generation_jobs.MAX_JOBS + 2}
    assert generation_jobs.latest_job("t")["job_id"] == jobs[-1]["job_id"] and generation_jobs.latest_job("other") is None
    assert generation_jobs.get_job("nope") is None
    failed = generation_jobs.start_job("t", lambda progress: (_ for _ in ()).throw(ValueError("boom")), summary="bad", thread=False)
    assert failed["status"] == "failed" and failed["error"] == "boom"
    generation_jobs._reset_for_tests()


# ---------------- 画表情（SVG）：文生 / 照着画面画 ----------------


def test_generate_expression_saves_scene_and_can_set_default(monkeypatch):
    from deskbot_server.application import agent_generation as ag

    seen: dict = {}
    monkeypatch.setattr("deskbot_server.ark_face_svg.generate_face_svg_from_text", lambda prompt, **kw: {"scene": {"name": "x", "title": "眨眼", "frames": [{"ms": 500, "elements": {}}]}, "title": "眨眼"})
    monkeypatch.setattr("deskbot_server.ark_face_svg.generate_face_svg_from_image", lambda image, mime, *, prompt="": seen.setdefault("image", (len(image), mime, prompt)) and {"scene": {"title": "照着画", "frames": [{"ms": 500, "elements": {}}]}, "title": "照着画"})

    def _fake_save(scenes, *, mapping=None):
        seen["mapping"] = mapping
        assert "name" not in scenes[0] and scenes[0]["title"]
        return {"created": [{"name": "user_a1", "title": scenes[0]["title"]}], "revision": 3}

    monkeypatch.setattr(ag, "save_expression_scenes", _fake_save)
    ops = _ops()
    res = execute_generation_tool({"tool": "generate_expression", "description": "眨眼", "set_default": True}, ops=ops)
    assert res["ok"] and res["name"] == "user_a1" and res["set_default"] is True and seen["mapping"] == {"idle": 0}
    assert ops.calls == [("apply", None)] and res["device"]["ok"]
    res = execute_generation_tool({"tool": "generate_expression", "source": "camera", "description": "夸张一点"}, ops=_ops())
    assert res["ok"] and res["source"] == "camera" and seen["image"] == (len(b"\xff\xd8\xff-jpeg"), "image/jpeg", "夸张一点") and seen["mapping"] is None
    no_cam = execute_generation_tool({"tool": "generate_expression", "source": "camera"}, ops=_ops(capture_frame=lambda: None))
    assert no_cam["ok"] is False and "相机" in no_cam["error"]


def test_save_expression_scenes_uses_atomic_transaction(monkeypatch):
    from deskbot_server.application.agent_generation import save_expression_scenes

    seen: dict = {}
    monkeypatch.setattr("deskbot_server.face_expression_transactions.get_face_expression_state", lambda: {"revision": 7})

    def _apply(payload):
        seen["payload"] = payload
        return {"created": [{"name": "user_x", "title": "t"}], "revision": 8}

    monkeypatch.setattr("deskbot_server.face_expression_transactions.apply_face_expression_transaction", _apply)
    out = save_expression_scenes([{"title": "t", "frames": []}], mapping={"idle": 0})
    assert out == {"created": [{"name": "user_x", "title": "t"}], "revision": 8}
    assert seen["payload"]["expected_revision"] == 7 and seen["payload"]["map_create_refs"] == {"idle": 0}


# ---------------- 文字链路：move_head / play_expression 转 Core HTTP ----------------


def test_web_channel_device_tools_go_through_core_http():
    from deskbot_server.application.web_chat_capture import (
        run_core_apply_default_expression,
        run_core_move_head,
        run_core_play_expression,
    )

    posts: list = []

    def _post(url, body):
        posts.append((url, json.loads(body or b"{}")))
        if "device_face_play" in url and "name=ghost" in url:
            return 400, "application/json", json.dumps({"ok": False, "error": "unknown emotion", "valid_names": ["happy"]}).encode()
        return 200, "application/json", b'{"ok": true, "operation_id": "op1"}'

    base = "http://127.0.0.1:1"
    assert run_core_move_head("d1", {"move": "nod", "duration_ms": 99999}, base_url=base, post=_post)["ok"]
    assert posts[-1] == (f"{base}/api/device_servo", {"device_id": "d1", "preset": "nod", "duration_ms": 8000})
    composed = run_core_move_head("d1", {"steps": [{"x": 60, "y": 90, "ms": 300}]}, base_url=base, post=_post)
    assert composed["ok"] and composed["move"] == "composed" and posts[-1][1]["steps"] == [{"x": 60, "y": 90, "xm": 0, "ym": 0, "ms": 300}]
    assert run_core_move_head("d1", {}, base_url=base, post=_post)["ok"] is False
    assert run_core_play_expression("d1", {"name": "happy"}, base_url=base, post=_post)["ok"]
    assert "kind=emotion" in posts[-1][0] and "name=happy" in posts[-1][0]
    bad = run_core_play_expression("d1", {"name": "ghost"}, base_url=base, post=_post)
    assert bad["ok"] is False and "happy" in bad["error"]
    assert run_core_apply_default_expression("d1", base_url=base, post=_post)["ok"] and posts[-1][0].endswith("/api/expression_apply_default")


def test_sync_executor_routes_device_and_generation_tools(monkeypatch):
    from deskbot_server.application import tool_executor as te

    calls: list = []
    monkeypatch.setattr("deskbot_server.application.web_chat_capture.run_core_move_head", lambda dev, raw: (calls.append(("move", dev, raw)), {"tool": "move_head", "ok": True})[1])
    monkeypatch.setattr("deskbot_server.application.web_chat_capture.run_core_play_expression", lambda dev, raw: (calls.append(("play", dev, raw)), {"tool": "play_expression", "ok": True})[1])
    monkeypatch.setattr("deskbot_server.application.agent_generation_tools.execute_generation_tool", lambda raw, *, ops: (calls.append(("gen", ops.device_id, raw["tool"])), {"tool": raw["tool"], "ok": True})[1])
    monkeypatch.setattr("deskbot_server.application.llm_tool_runner.execute_llm_tools", lambda tools, **kw: [{"tool": t["tool"], "ok": True, "plain": True} for t in tools])
    results = te.execute_tools_sync(
        [{"tool": "websearch", "query": "x"}, {"tool": "move_head", "move": "nod"}, {"tool": "generation_status"}, {"tool": "play_expression", "name": "happy"}],
        device_id="d1",
    )
    assert [r["tool"] for r in results] == ["websearch", "move_head", "generation_status", "play_expression"]
    assert calls == [("move", "d1", {"tool": "move_head", "move": "nod"}), ("gen", "d1", "generation_status"), ("play", "d1", {"tool": "play_expression", "name": "happy"})]


def test_async_executor_routes_device_and_generation_tools_in_core(monkeypatch):
    from deskbot_server.application import tool_executor as te

    calls: list = []

    async def _play(device_id, name, *, duration_ms=None):
        calls.append(("play", device_id, name, duration_ms))
        return {"tool": "play_expression", "ok": True}

    async def _move(*, device_id, arguments, asr_chat_hub, call_id=""):
        calls.append(("move", device_id, arguments, asr_chat_hub))
        return {"tool": "move_head", "ok": True}

    monkeypatch.setattr("deskbot_server.application.expression_runtime.play_rtc_expression", _play)
    monkeypatch.setattr("deskbot_server.application.rtc_tool_service._move_head", _move)
    monkeypatch.setattr("deskbot_server.application.agent_generation_tools.execute_generation_tool", lambda raw, *, ops: (calls.append(("gen", ops.device_id, raw["tool"])), {"tool": raw["tool"], "ok": True})[1])

    async def _run():
        return await te.execute_tools_async(
            [{"tool": "play_expression", "name": "happy", "duration_ms": 800}, {"tool": "move_head", "steps": [{"ms": 300}]}, {"tool": "generation_status"}],
            channel=te.ToolChannel.CORE, device_id="d1", user_confirmed=False, asr_chat_hub="hub",
        )

    results = asyncio.run(_run())
    assert [r["ok"] for r in results] == [True, True, True]
    assert calls[0] == ("play", "d1", "happy", 800)
    assert calls[1] == ("move", "d1", {"steps": [{"ms": 300}]}, "hub")
    assert calls[2] == ("gen", "d1", "generation_status")


def test_core_apply_default_route_registered_and_reapplies_current_state(monkeypatch):
    from deskbot_server.application.agent_generation import apply_default_expression_for_device
    from deskbot_server.ws.routes import ROUTES_API_KEY

    assert "/api/expression_apply_default" in ROUTES_API_KEY

    class _Result:
        def as_tool_result(self):
            return {"ok": True, "status": "accepted"}

    class _Runtime:
        desired_state = "idle"
        seen: list = []

        async def transition(self, state, *, force=False, reason=""):
            self.seen.append((state, force, reason))
            return _Result()

    rt = _Runtime()
    monkeypatch.setattr("deskbot_server.application.expression_runtime.get_rtc_expression_runtime", lambda dev: rt if dev == "d1" else None)
    out = asyncio.run(apply_default_expression_for_device("d1"))
    assert out["ok"] and rt.seen == [("idle", False, "agent_generation_apply")]
    assert asyncio.run(apply_default_expression_for_device("nope"))["ok"] is False


def test_web_ai_routes_still_work_and_share_the_implementation(monkeypatch):
    from deskbot_server.web.blueprints import app2c_bp

    src = open(app2c_bp.__file__, encoding="utf-8").read()
    assert "compose_scene_playbook(" in src and "compose_motion_preset(" in src
    assert '"你是桌面机器人「小歪」的头部动作设计师' not in src  # 提示词只在应用层一份
