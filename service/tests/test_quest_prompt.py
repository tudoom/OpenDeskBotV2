"""剧情任务注入 system prompt：附录内容 / 空值分支 / 装配点接入 / chat_flow 系统轮前缀。"""

from __future__ import annotations

from tests.quest_helpers import bind, clear_care_scene, demo_playbook, quest_env  # noqa: F401


def _setup_bound():
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    return svc


def test_appendix_empty_when_unbound_or_no_running(quest_env):
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("")
    assert "当前剧情任务" not in svc.quest_prompt_appendix() and "可以顺带关心的事" in svc.quest_prompt_appendix()  # 日常关心随主动陪伴生效
    clear_care_scene()
    assert svc.quest_prompt_appendix() == ""
    bind("demo")
    assert "当前剧情任务" in svc.quest_prompt_appendix()
    # 全部置终态后 → 无进行中任务 → 空串（不广告不可用工具，避免编造 task_id）
    svc.update_task_result(svc.LOCAL_PROFILE_DEVICE, "demo", "g_greet", "success", "回应了")
    svc.update_task_result(svc.LOCAL_PROFILE_DEVICE, "demo", "g_learn_name", "success", "说了名字")
    svc.update_task_result(svc.LOCAL_PROFILE_DEVICE, "demo", "g_learn_name_soften", "success", "聊起来了")
    assert svc.quest_prompt_appendix() == ""


def test_appendix_lists_tasks_and_tool_contract(quest_env):
    svc = _setup_bound()
    ax = svc.quest_prompt_appendix()
    assert "当前剧情任务" in ax
    assert "[g_greet] 初次问候" in ax and "主动向用户问好" in ax
    assert "达成条件：用户回应了问候" in ax and "失败条件" not in ax
    assert "update_task_result(unmet)" in ax and '"status":"success|unmet"' in ax
    assert "update_task_result" in ax and "update_task_strategy" in ax
    assert '"tool":"update_task_result"' in ax  # 本项目 tools JSON 数组约定
    assert "可用任务 id：g_greet" in ax
    assert "进度" not in ax and "0/100" not in ax  # 不展示分数


def test_appendix_caps_at_three_tasks(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc

    fake = [
        {
            "task_id": f"t{i}",
            "title": f"任务{i}",
            "goal": f"目标{i}",
            "strategy": "",
            "success_condition": f"成功{i}",
            "failure_condition": f"失败{i}",
            "current_score": i,
            "activation_score": 10,
        }
        for i in range(1, 5)
    ]
    monkeypatch.setattr(svc, "get_current_tasks", lambda device_id=None: fake)
    ax = svc.quest_prompt_appendix()
    assert ax.count("  - [") == 3
    assert "[t4]" not in ax
    assert "可用任务 id：t1, t2, t3, t4" in ax  # 工具仍可操作全部 running 任务


def test_text_system_prompt_injects_quest_appendix(quest_env, monkeypatch):
    from deskbot_server.llm import prompt_assembly as pa

    _setup_bound()
    monkeypatch.setattr("deskbot_server.device_preferences.preferred_time_prompt", lambda: "2026-09-07 21:00")
    text = pa.assemble_text_system_prompt("我是小歪")
    assert text.startswith("我是小歪")
    assert "当前剧情任务" in text and "update_task_result" in text
    assert text.index("可用工具") < text.index("当前剧情任务")  # 附在工具说明之后
    bind("")
    text2 = pa.assemble_text_system_prompt("我是小歪")
    assert "当前剧情任务" not in text2


def test_rtc_prompt_lists_tasks_without_duplicating_tool_contract(quest_env):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.llm import prompt_assembly as pa

    _setup_bound()
    ax = svc.quest_tasks_appendix()
    assert "[g_greet] 初次问候" in ax and "update_task_result" in ax
    assert '"tool":' not in ax  # 原生工具模式不重复 JSON 契约
    text = pa.assemble_rtc_system_prompt("你是阿福", cache_loader=lambda: {})
    assert text.startswith("你是阿福") and "当前剧情任务" in text
    assert pa.assemble_rtc_system_prompt("你是阿福", cache_loader=lambda: {}, quest_loader=lambda: "") == pa.assemble_rtc_system_prompt("你是阿福", cache_loader=lambda: {}, quest_loader=lambda: "   ")
    assert "当前剧情任务" not in pa.assemble_rtc_system_prompt("x", cache_loader=lambda: {}, quest_loader=lambda: "")
    bind("")
    assert "当前剧情任务" not in pa.assemble_rtc_system_prompt("x", cache_loader=lambda: {})


def test_appendix_failure_never_breaks_prompt(quest_env, monkeypatch):
    from deskbot_server.application import quest_service as svc

    def _boom(device_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "get_current_tasks", _boom)
    assert svc.quest_prompt_appendix() == ""
    assert svc.quest_tasks_appendix() == ""
    from deskbot_server.llm import prompt_assembly as pa

    assert pa.rtc_quest_prompt(_boom) == ""


def test_chat_flow_treats_quest_prefix_as_system_turn():
    from deskbot_server.application import chat_flow as cf
    from deskbot_server.application.quest_proactive import build_user_text

    text = build_user_text(
        {"task_id": "g_greet", "title": "初次问候", "goal": "主动问好", "strategy": "轻松", "success_condition": "回应"}
    )
    assert text.startswith(cf._QUEST_PROACTIVE_PREFIX)
    assert cf._is_quest_proactive_user_text(text)
    assert not cf._is_scheduled_task_user_text(text)
    assert "need_reply 必须为 true" in text and "禁止写「已发送」" in text
    assert "update_task_result" in text
    # 兜底口播：按任务标题起话头；汇报语被判为 meta
    assert cf._quest_proactive_fallback_tts(text) == "主人，我们聊聊初次问候吧？"
    assert cf._quest_proactive_fallback_tts("[系统剧情推进] x：[t1] notitle") == "主人，我们聊聊吧？"
    assert cf._scheduled_tts_looks_like_meta_report("已发送提醒")
    assert not cf._scheduled_tts_looks_like_meta_report("主人，今天心情怎么样？")
