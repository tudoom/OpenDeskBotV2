from __future__ import annotations

from deskbot_server.llm.utils import llm_tools_prompt_appendix
from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas


def test_tools_prompt_tells_model_how_to_drive_xiaoai_speaker():
    """点歌/播电台必须走 execute-text-directive，而不是只会朗读文字的 play-text。"""
    prompt = llm_tools_prompt_appendix()
    assert "execute-text-directive" in prompt
    assert '"key":"execute-text-directive","args":["播放周杰伦的晴天",true]' in prompt
    assert "不要用 play-text" in prompt
    assert "pause/next/previous" in prompt


def test_rtc_miot_tool_exposes_writes_and_speaker_guidance():
    """语音链路的 miot 工具要能写（set/action/run_scene），并带同样的点歌指引。"""
    schema = next(s for s in build_rtc_tool_schemas() if s["name"] == "miot")
    props = schema["parameters"]["properties"]
    assert {"set", "action", "run_scene"} <= set(props["action"]["enum"])
    assert {"key", "value", "args", "scene_name"} <= set(props)
    assert "execute-text-directive" in schema["description"]
    assert "play-text" in schema["description"]


def test_miot_writes_execute_without_user_confirmation(monkeypatch):
    """产品决定：开关灯/点歌/跑场景直接执行，不再要求用户先说"我确认"。"""
    import deskbot_server.application.llm_tool_runner as runner

    calls: list[dict] = []

    def _fake_execute_miot_tool(raw):
        calls.append(dict(raw))
        return {"tool": "miot", "ok": True, "action": raw.get("action")}

    monkeypatch.setattr(runner, "execute_miot_tool", _fake_execute_miot_tool)
    tools = [
        {"tool": "miot", "action": "set", "name": "台灯", "key": "on", "value": True},
        {
            "tool": "miot",
            "action": "action",
            "name": "小爱触屏音箱",
            "key": "execute-text-directive",
            "args": ["播放周杰伦的晴天", True],
        },
        {"tool": "miot", "action": "run_scene", "scene_name": "回家模式"},
    ]
    results = runner.execute_llm_tools(tools, device_id="dev-1", user_confirmed=False)
    assert [r.get("ok") for r in results] == [True, True, True]
    assert not any(r.get("confirmation_required") for r in results)
    assert [c["action"] for c in calls] == ["set", "action", "run_scene"]


def test_action_args_bools_become_ints_for_mi_cloud():
    """米家云端动作入参不认 JSON 布尔（-704220043），认 1/0；其它类型原样。"""
    from deskbot_server.iotctl.miot_ctl.util import coerce_action_args

    assert coerce_action_args(["播放周杰伦的晴天", True]) == ["播放周杰伦的晴天", 1]
    assert coerce_action_args([False, 3, "x", 2.5]) == [0, 3, "x", 2.5]
    assert coerce_action_args([]) == []


def test_rtc_prompt_lists_bound_devices_and_home_control_rule():
    """语音提示词要带设备清单和"必须调 miot、免确认"的规则；未绑定/异常时为空。"""
    from deskbot_server.rtc_agent_sdk import rtc_home_control_prompt

    cache = {
        "devices": [
            {"name": "小爱触屏音箱", "room_name": "默认家庭"},
            {"name": "台灯", "room_name": ""},
            {"name": "台灯", "room_name": "书房"},  # 重名只列一次
        ]
    }
    text = rtc_home_control_prompt(cache_loader=lambda: cache)
    assert "小爱触屏音箱（默认家庭）、台灯" in text
    assert "execute-text-directive" in text
    assert "不需要再向用户确认" in text
    assert rtc_home_control_prompt(cache_loader=lambda: {}) == ""

    def _boom():
        raise RuntimeError("no cache")

    assert rtc_home_control_prompt(cache_loader=_boom) == ""
