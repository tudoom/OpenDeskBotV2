"""系统提示单一装配点：文字/传统链路与语音链路各只有一个入口。"""

from __future__ import annotations

from deskbot_server.llm import prompt_assembly as pa


def test_text_prompt_layers_persona_time_screen_and_static_context(monkeypatch):
    monkeypatch.setattr("deskbot_server.device_preferences.preferred_time_prompt", lambda: "2026-09-02 21:00")
    monkeypatch.setattr("deskbot_server.llm.utils.llm_device_screen_appendix", lambda: "[屏幕]")
    monkeypatch.setattr("deskbot_server.llm.utils.llm_pb_plan_prompt_appendix", lambda: "[PB]")
    monkeypatch.setattr("deskbot_server.llm.utils.llm_static_context_prompt_appendix", lambda: "[静态]")
    text = pa.assemble_text_system_prompt("我是小歪")
    assert text.startswith("我是小歪\n当前时间是: 2026-09-02 21:00")
    assert text.index("[屏幕]") < text.index("[PB]") < text.index("[静态]")
    assert "联网说明" not in text
    single = pa.assemble_text_system_prompt("我是小歪", single_pass=True)
    assert "联网说明" in single and single.startswith(text)


def test_rtc_prompt_has_persona_rules_and_home_devices(monkeypatch):
    monkeypatch.delenv("DESKBOT_RTC_SYSTEM_PROMPT", raising=False)
    cache = {"devices": [{"name": "小爱触屏音箱", "room_name": "客厅"}, {"name": "台灯", "room_name": ""}]}
    text = pa.assemble_rtc_system_prompt(cache_loader=lambda: cache)
    assert "小歪" in text
    assert "capture_and_describe" in text and "联网搜索" in text
    assert "小爱触屏音箱（客厅）、台灯" in text
    # 覆盖人设只替换人设段，规则与家居清单始终附加
    custom = pa.assemble_rtc_system_prompt("你是阿福", cache_loader=lambda: cache)
    assert custom.startswith("你是阿福") and "台灯" in custom


def test_legacy_entry_points_delegate_to_the_assembler(monkeypatch):
    """旧名字仍可用，但内容必须来自同一个装配器。"""
    from deskbot_server import rtc_agent_sdk

    assert rtc_agent_sdk.rtc_home_control_prompt is pa.rtc_home_control_prompt
