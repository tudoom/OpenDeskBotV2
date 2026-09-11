"""渠道 × 工具能力矩阵：三条链路都必须能拍照、写米家、联网搜索。

2026-09 之前"执行工具"有四份实现，各自决定可用集合，于是文字对话没有拍照、
语音端米家只读。这个测试把矩阵钉死：新增渠道或工具漏登记会直接红。
"""

from __future__ import annotations

import asyncio

import pytest

from deskbot_server.application import tool_executor as te


@pytest.mark.parametrize("channel", list(te.ToolChannel))
def test_every_channel_can_capture_control_home_and_search(channel):
    allowed = te.tools_for_channel(channel)
    for tool in ("capture_camera", "capture_and_describe", "miot", "websearch", "memory_add"):
        assert tool in allowed, f"{channel.value} 缺少 {tool}"


def test_device_only_tools_stay_on_rtc():
    assert "move_head" in te.tools_for_channel(te.ToolChannel.RTC)
    assert "move_head" not in te.tools_for_channel(te.ToolChannel.WEB)
    assert "play_expression" not in te.tools_for_channel(te.ToolChannel.CORE)


def test_rtc_allowlist_is_derived_from_the_matrix():
    from deskbot_server.application import rtc_tool_service as rts

    assert rts._RTC_TOOL_NAMES == te.tools_for_channel(te.ToolChannel.RTC)
    assert "miot" in rts._RTC_TOOL_NAMES and "move_head" in rts._RTC_TOOL_NAMES


def test_web_channel_routes_capture_to_core_and_rest_to_runner(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def _fake_runner(tools, *, device_id=None, user_confirmed=False):
        calls.append(("runner", user_confirmed))
        return [{"tool": t["tool"], "ok": True} for t in tools]

    def _fake_capture(device_id, *, fetch=None):
        calls.append(("capture", device_id == "dev-1"))
        return {"ok": True, "jpeg_bytes": 3}

    monkeypatch.setattr("deskbot_server.application.llm_tool_runner.execute_llm_tools", _fake_runner)
    monkeypatch.setattr(
        "deskbot_server.application.web_chat_capture.fetch_core_camera_capture", _fake_capture
    )
    out = te.execute_tools_sync(
        [{"tool": "miot", "action": "set"}, {"tool": "capture_camera"}, {"tool": "websearch", "query": "q"}],
        channel=te.ToolChannel.WEB,
        device_id="dev-1",
        user_confirmed=True,
    )
    assert [r["tool"] for r in out] == ["miot", "capture_camera", "websearch"]
    assert ("capture", True) in calls and ("runner", True) in calls


def test_core_channel_routes_capture_to_frame_store(monkeypatch):
    seen: list[str] = []

    async def _fake_capture_async(device_id, *, hub=None, display=False):
        seen.append("frame_store")
        return {"ok": True, "display_requested": display}

    def _fake_runner(tools, *, device_id=None, user_confirmed=False):
        seen.append("runner")
        return [{"tool": t["tool"], "ok": True} for t in tools]

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store.capture_camera_for_device_async",
        _fake_capture_async,
    )
    monkeypatch.setattr("deskbot_server.application.llm_tool_runner.execute_llm_tools", _fake_runner)
    out = asyncio.run(
        te.execute_tools_async(
            [{"tool": "capture_and_describe", "display": True}, {"tool": "miot", "action": "list"}],
            channel=te.ToolChannel.CORE,
            device_id="dev-1",
            user_confirmed=False,
            allow_camera_display=True,
        )
    )
    assert [r["tool"] for r in out] == ["capture_and_describe", "miot"]
    assert out[0]["display_requested"] is True
    assert seen == ["frame_store", "runner"]
