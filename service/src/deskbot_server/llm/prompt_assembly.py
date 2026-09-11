"""系统提示的唯一装配点。

此前"人设 + 时间 + 设备屏幕 + PB 计划 + 静态上下文（记忆/米家/工具）"这套
拼装在文字对话（debug_bp）、传统 ChatService（openai_compat）各写一遍，语音
链路（rtc_agent_sdk）又单独维护一段口语人设——结果是语音端直到 2026-09 才
拿到米家设备清单与联网规则。现在渠道只传差异参数，拼装逻辑只在这里。

两种形态：
- ``assemble_text_system_prompt``：结构化 JSON 输出的文字/传统链路。
- ``assemble_rtc_system_prompt``：实时语音，短口语人设 + 行为规则 + 家居清单。
"""

from __future__ import annotations

import os
from typing import Any, Callable

from deskbot_server.application.quest_service import quest_tasks_appendix
from deskbot_server.scene_playbooks_store import scene_catalog_prompt

_RTC_HOME_CONTROL_MAX_DEVICES = 12

_RTC_DEFAULT_PERSONA = (
    "你是“小歪”，一个住在桌面上的活泼、温柔、偶尔俏皮的小型陪伴机器人。"
    "使用自然、简短的中文口语回答，像熟悉的朋友；不要客套开场。"
    "只输出要直接说给用户听的话，不输出 JSON、Markdown、表情符号或动作说明，"
    "通常控制在五十个汉字以内。不确定时坦率说不确定，不要编造。"
)

_RTC_BEHAVIOR_RULES = (
    "涉及当前环境、眼前画面、用户手中物体、颜色、人数等视觉事实时，"
    "必须调用 capture_and_describe 获取当前照片后回答，绝不能凭空猜测，"
    "也不要用只返回元数据的 capture_camera。普通对话不要拍照。"
    "照片仅供当前一轮回答使用，不得声称会记住或长期保存图片。"
    "涉及新闻、天气、股价、赛事、价格等时效性信息时先联网搜索再回答，"
    "只播报结论和关键数字，不要读出网址或来源列表。"
)


def persona_prompt() -> str:
    """人设正文：Agent.md 为准，读不出时退回打包模板。"""
    try:
        from deskbot_server.agent_docs import system_prompt

        text = system_prompt()
    except Exception:  # noqa: BLE001 —— 人设文件损坏不能让对话失败
        text = ""
    if text:
        return text
    from deskbot_server.device_data import load_llm_system_prompt

    return (load_llm_system_prompt() or "").strip()


def assemble_text_system_prompt(
    persona: str | None = None,
    *,
    single_pass: bool = False,
) -> str:
    """文字/传统链路的完整系统提示（结构化 JSON 输出）。

    组成顺序与历史行为一致：人设 → 当前时间 → 设备屏幕说明 → PB 计划说明 →
    静态上下文（行为偏好 + 长期记忆 + 米家摘要 + 工具说明）→（可选）单趟联网附注。
    """
    from deskbot_server.device_preferences import preferred_time_prompt
    from deskbot_server.llm.utils import (
        llm_device_screen_appendix,
        llm_pb_plan_prompt_appendix,
        llm_static_context_prompt_appendix,
    )

    base = f"{persona if persona is not None else persona_prompt()}\n当前时间是: {preferred_time_prompt()}"
    base += "\n" + llm_device_screen_appendix()
    px = llm_pb_plan_prompt_appendix()
    if px:
        base += "\n" + px
    fx = llm_static_context_prompt_appendix()
    if fx:
        base += "\n\n" + fx
    if single_pass:
        from deskbot_server.llm.ark_single_pass import SINGLE_PASS_PROMPT_APPENDIX

        base += "\n\n" + SINGLE_PASS_PROMPT_APPENDIX
    return base


def rtc_home_control_prompt(cache_loader: Callable[[], Any] | None = None) -> str:
    """语音链路的家居控制提示：必须调 miot 工具 + 已绑定设备的简短清单。

    只读本机米家缓存（不访问云端），任何异常都吞掉——家居提示缺失不能让
    语音会话起不来。未绑定时返回空串。
    """
    try:
        if cache_loader is None:
            from deskbot_server.miot_service import load_homes_cache as cache_loader
        cache = cache_loader() or {}
    except Exception:  # noqa: BLE001
        return ""
    devices = cache.get("devices") if isinstance(cache, dict) else None
    if not isinstance(devices, list) or not devices:
        return ""
    names: list[str] = []
    seen: set[str] = set()
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        name = str(dev.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        room = str(dev.get("room_name") or "").strip()
        names.append(f"{name}（{room}）" if room else name)
        if len(names) >= _RTC_HOME_CONTROL_MAX_DEVICES:
            break
    if not names:
        return ""
    return (
        "用户要求开关灯、调空调、让小爱音箱放歌/播电台、跑场景等家居操作时，"
        "必须调用 miot 工具直接执行，不要只口头答应，也不需要再向用户确认；"
        "小爱音箱点歌/口令用 action=action、key=execute-text-directive、"
        "args=[要对小爱说的话, true]。已绑定的米家设备："
        + "、".join(names)
        + "。设备不在清单里时先用 action=list 查一次。"
    )


def rtc_scene_prompt(scene_loader: Callable[[], str] | None = None) -> str:
    """语音链路的组合表演目录段；异常吞掉，无表演 → 空串。"""
    try:
        text = str((scene_loader or scene_catalog_prompt)() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ("\n" + text) if text else ""


def rtc_quest_prompt(quest_loader: Callable[[], str] | None = None) -> str:
    """语音链路的剧情任务段：只列进行中任务（工具契约由原生 schema 承担，不重复）。

    任何异常都吞掉——剧情附录缺失不能让语音会话起不来。未绑定/无任务 → 空串。
    """
    try:
        text = str((quest_loader or quest_tasks_appendix)() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ("\n" + text) if text else ""


def assemble_rtc_system_prompt(
    override: str | None = None,
    *,
    cache_loader: Callable[[], Any] | None = None,
    quest_loader: Callable[[], str] | None = None,
) -> str:
    """实时语音的系统提示：短口语人设 + 行为规则 + 家居清单 + 剧情任务。

    ``override`` 缺省取环境变量 ``DESKBOT_RTC_SYSTEM_PROMPT``（只替换人设段，
    行为规则、家居清单与剧情任务始终附加）。
    """
    if override is None:
        override = str(os.environ.get("DESKBOT_RTC_SYSTEM_PROMPT") or "").strip()
    base = override or _RTC_DEFAULT_PERSONA
    return (
        base
        + _RTC_BEHAVIOR_RULES
        + rtc_home_control_prompt(cache_loader)
        + rtc_scene_prompt()
        + rtc_quest_prompt(quest_loader)
    )


__all__ = [
    "assemble_rtc_system_prompt",
    "assemble_text_system_prompt",
    "persona_prompt",
    "rtc_home_control_prompt",
    "rtc_quest_prompt",
]
