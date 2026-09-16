"""对话 Agent 的"AI 生成"工具：三条链路共用的分发（2026-09-14）。

工具（登记在 tool_executor.TOOL_CAPABILITIES，schema 在 rtc_worker_tools，文字说明在 llm/utils）：

- generate_scene        编一段表演并存进表演列表，默认立刻表演
- agent_generation.generate_quest_scene  一句话生成一个陪伴场景（主线小目标 + 日常关心）并启用
- generate_motion       设计一个头部动作并存为预设，默认立刻做一遍
- generate_cartoon_faces 后台生成整套卡通表情（约 3～4 分钟），存库并（默认）换到设备
- generate_expression   文生 / 照着眼前画面画一个矢量表情，存库，可设为待机表情
- generation_status     查后台生成的进度 / 结果

渠道差异只在 ``ChannelOps``：怎么表演、怎么动头、怎么让设备换表情、怎么拍一帧、做完了怎么开口。
Core / RTC 由 tool_executor 用协程包起来，Flask 文字对话走 Core HTTP。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from deskbot_server.application import agent_generation, generation_jobs
from deskbot_server.application.persona_generation import GenerationError
from deskbot_server.ark_image_gen import CARTOON_STYLES, DEFAULT_STYLE

logger = logging.getLogger("deskbot-server")

GENERATION_TOOLS = frozenset(
    {
        "generate_scene",
        "generate_quest_scene",
        "generate_motion",
        "generate_cartoon_faces",
        "generate_expression",
        "generation_status",
    }
)
DEVICE_TOOLS = frozenset({"move_head", "play_expression"})
CARTOON_ETA_SECONDS = 240
CARTOON_SOURCE = "agent_generation"


@dataclass
class ChannelOps:
    device_id: str
    perform_scene: Callable[[str], dict[str, Any]]
    move_head: Callable[[dict[str, Any]], dict[str, Any]]
    play_expression: Callable[[dict[str, Any]], dict[str, Any]]
    apply_default_expression: Callable[[], dict[str, Any]]
    capture_frame: Callable[[], bytes | None]
    announce: Callable[[str], None]


def is_generation_tool(name: str) -> bool:
    return str(name or "").strip().lower() in GENERATION_TOOLS


def is_device_tool(name: str) -> bool:
    return str(name or "").strip().lower() in DEVICE_TOOLS


def _flag(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
    return default


def _job_view(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {"ok": False, "error": "没有找到这个生成任务"}
    view = {
        "ok": job.get("status") != "failed",
        "job_id": job.get("job_id"),
        "kind": job.get("kind"),
        "summary": job.get("summary"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "elapsed_seconds": job.get("elapsed_seconds"),
    }
    if job.get("status") == "done":
        view["result"] = job.get("result")
    elif job.get("status") == "failed":
        view["error"] = job.get("error")
    return view


def _generate_scene(raw: dict[str, Any], ops: ChannelOps) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    if not description:
        raise GenerationError("generate_scene 需要 description：想要什么样的表演")
    out = agent_generation.generate_scene_playbook(description)
    if _flag(raw, "perform", True):
        if not ops.device_id:
            out["performed"] = False
            out["perform_error"] = "没有连接的设备，表演已保存"
        else:
            played = ops.perform_scene(str(out["name"]))
            out["performed"] = bool(played.get("ok"))
            if not played.get("ok"):
                out["perform_error"] = str(played.get("error") or played.get("status") or "表演失败")[:200]
    else:
        out["performed"] = False
    out["hint"] = "以后主人再要看这段，用 perform_scene 按 name 表演即可。"
    return out


def _generate_quest_scene(raw: dict[str, Any]) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    if not description:
        raise GenerationError("generate_quest_scene 需要 description：想让小歪主动关心什么")
    out = agent_generation.generate_quest_scene(description, title=str(raw.get("title") or ""))
    out["hint"] = "场景已启用：主线小目标会在主人闲下来时按顺序推进，日常关心按频率/钟点触发。"
    return out


def _generate_motion(raw: dict[str, Any], ops: ChannelOps) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    if not description:
        raise GenerationError("generate_motion 需要 description：想要什么样的动作")
    out = agent_generation.generate_motion_preset(description, label=str(raw.get("label") or ""))
    if _flag(raw, "execute", True):
        if not ops.device_id:
            out["executed"] = False
            out["execute_error"] = "没有连接的设备，动作已保存"
        else:
            moved = ops.move_head({"move": out["id"]})
            out["executed"] = bool(moved.get("ok"))
            if not moved.get("ok"):
                out["execute_error"] = str(moved.get("error") or moved.get("status") or "动作失败")[:200]
    else:
        out["executed"] = False
    out["hint"] = "以后主人再要这个动作，用 move_head 的 move 填这个 id。"
    return out


def _generate_cartoon_faces(raw: dict[str, Any], ops: ChannelOps) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()[:200]
    style = str(raw.get("style") or "").strip().lower()
    if style not in CARTOON_STYLES:
        style = DEFAULT_STYLE
    apply = _flag(raw, "apply", True)
    style_title = str((CARTOON_STYLES.get(style) or {}).get("title") or style)
    stamp = time.strftime("%m-%d %H:%M")
    set_tag = f"{style_title} · {description[:12] or stamp}"
    device_id = ops.device_id

    def _work(progress: Callable[[str], None]) -> dict[str, Any]:
        frames = agent_generation.generate_cartoon_set(description, style=style, progress=progress)
        progress("画好了，正在存进表情库…")
        saved = agent_generation.save_cartoon_set(frames, set_tag=set_tag, apply=apply)
        result: dict[str, Any] = {
            "created": saved["created"],
            "applied": bool(apply),
            "style": style,
            "set_tag": set_tag,
        }
        if apply and device_id:
            progress("正在换到设备上…")
            result["device"] = ops.apply_default_expression()
        return result

    def _done(job: dict[str, Any]) -> None:
        if job.get("status") == "done":
            where = "并且已经换到你脸上了" if apply else "存在表情库里了，主人在表情页可以设为默认"
            ops.announce(
                f"（系统提示：你之前答应主人做的新卡通表情套图「{set_tag}」已经生成完成，{where}。"
                "用一句话主动告诉主人，可以顺便问问喜不喜欢；不要念标签里的时间。）"
            )
        else:
            ops.announce(
                f"（系统提示：你之前答应主人做的卡通表情套图失败了，原因：{str(job.get('error') or '未知')[:80]}。"
                "用一句话告诉主人，并说可以再试一次。）"
            )

    job = generation_jobs.start_job(
        "cartoon_faces", _work, summary=f"卡通表情套图：{set_tag}", device_id=device_id, on_done=_done
    )
    return {
        "ok": True,
        "status": "running",
        "job_id": job["job_id"],
        "style": style,
        "apply": apply,
        "eta_seconds": CARTOON_ETA_SECONDS,
        "note": (
            "已在后台生成，大约 3～4 分钟。现在只需告诉主人在做了、做好会主动说；"
            "不要等待、不要重复调用。主人追问进度时用 generation_status。"
        ),
    }


def _generate_expression(raw: dict[str, Any], ops: ChannelOps) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    source = str(raw.get("source") or "text").strip().lower()
    set_default = _flag(raw, "set_default", False)
    image: bytes | None = None
    if source == "camera":
        if not ops.device_id:
            raise GenerationError("没有连接的设备，拍不了画面；可以改成按描述画")
        image = ops.capture_frame()
        if not image:
            raise GenerationError("没拿到相机画面，请确认相机上行已开启，或改成按描述画")
    elif not description:
        raise GenerationError("generate_expression 需要 description：想要什么样的表情")
    out = agent_generation.generate_svg_expression(description, image_bytes=image, set_default=set_default)
    if set_default and ops.device_id:
        out["device"] = ops.apply_default_expression()
    elif not set_default:
        out["hint"] = "已存进表情库；主人想看就用 play_expression 按 name 播，想当待机脸就再调一次并 set_default=true。"
    return out


def _generation_status(raw: dict[str, Any]) -> dict[str, Any]:
    job_id = str(raw.get("job_id") or "").strip()
    job = generation_jobs.get_job(job_id) if job_id else generation_jobs.latest_job()
    if job is None and not job_id:
        return {"ok": True, "status": "none", "note": "最近没有后台生成任务"}
    return _job_view(job)


def execute_generation_tool(raw: dict[str, Any], *, ops: ChannelOps) -> dict[str, Any]:
    """执行一个生成类工具，返回给模型的结果（永远带 tool/ok，异常转成可读 error）。"""
    tool = str(raw.get("tool") or raw.get("name") or "").strip()
    try:
        if tool == "generate_scene":
            out = _generate_scene(raw, ops)
        elif tool == "generate_quest_scene":
            out = _generate_quest_scene(raw)
        elif tool == "generate_motion":
            out = _generate_motion(raw, ops)
        elif tool == "generate_cartoon_faces":
            out = _generate_cartoon_faces(raw, ops)
        elif tool == "generate_expression":
            out = _generate_expression(raw, ops)
        elif tool == "generation_status":
            out = _generation_status(raw)
        else:
            return {"tool": tool, "ok": False, "error": f"未知生成工具: {tool}"}
    except GenerationError as exc:
        return {"tool": tool, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 —— 生成失败要给模型可读原因，不能炸掉整轮
        logger.warning("[agent_generation] %s failed", tool, exc_info=True)
        return {"tool": tool, "ok": False, "error": (str(exc) or type(exc).__name__)[:200]}
    return {"tool": tool, **out}


__all__ = [
    "CARTOON_ETA_SECONDS",
    "DEVICE_TOOLS",
    "GENERATION_TOOLS",
    "ChannelOps",
    "execute_generation_tool",
    "is_device_tool",
    "is_generation_tool",
]
