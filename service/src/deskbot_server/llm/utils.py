"""LLM 输出解析等纯文本工具，独立于 funasr/torch 等重依赖，
供 ``deskbot_server`` 主服务与 ``web/app.py`` 共享使用。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from deskbot_server.application.quest_service import quest_prompt_appendix
from deskbot_server.constants import SERVO_CFG_FILE
from deskbot_server.device_data import resolve_json_path
from deskbot_server.pb.llm_display import parse_llm_images
from deskbot_server.pb.servo_pcm import parse_pb_volume
from deskbot_server.scene_playbooks_store import scene_catalog_prompt
from deskbot_server.servo_config_store import (
    load_servo_cfg_file,
    servo_model_preset_ids,
    servo_preset_catalog,
)
from deskbot_server.servo_protocol import (
    SERVO_MAX_PLAN_DURATION_MS,
    SERVO_MIN_SEGMENT_DURATION_MS,
    ServoProtocolError,
    validate_servo_steps,
)

_LLM_APPENDIX_CACHE: dict[str, tuple[float, str]] = {}


def _cached_appendix(cache_key: str, mtime_path: str, build_fn) -> str:
    global _LLM_APPENDIX_CACHE
    try:
        mtime = os.path.getmtime(mtime_path)
    except OSError:
        return ""
    cached = _LLM_APPENDIX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = build_fn()
    _LLM_APPENDIX_CACHE[cache_key] = (mtime, text)
    return text


# 与 RTC 的 move_head 一致：五步足够表达点头、张望这类动作，再长就该沉淀成预设。
_MAX_COMPOSED_STEPS = 5


def _servo_envelope() -> dict[str, int]:
    """自由编排要告诉模型的角度包络；读不到配置时给出厂缺省。"""
    try:
        cfg = load_servo_cfg_file() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    envelope = {
        "xMin": int(cfg.get("xMin", 10) or 10),
        "xMax": int(cfg.get("xMax", 170) or 170),
        "yMin": int(cfg.get("yMin", 70) or 70),
        "yMax": int(cfg.get("yMax", 110) or 110),
    }
    envelope["xCenter"] = (envelope["xMin"] + envelope["xMax"]) // 2
    return envelope


def llm_pb_moves_prompt_appendix() -> str:
    """供 system prompt 追加：合法 ``moves`` 预设 id、label 与默认时长。"""
    def _build() -> str:
        lines: list[str] = []
        for preset in servo_preset_catalog(model_visible_only=True):
            if not isinstance(preset, dict):
                continue
            pid = str(preset.get("id") or "").strip()
            label = str(preset.get("label") or "").strip()
            if not pid:
                continue
            default_ms = sum(max(1, int(s.get("ms") or 0)) for s in (preset.get("steps") or []))
            lines.append(f"      - {pid}: {label or pid}（默认 {default_ms} ms）")
        if not lines:
            return ""
        body = "\n".join(lines)
        cfg = _servo_envelope()
        return (
            "  - moves: 数组。两种写法，可混用：\n"
            "    ① 预设：``{\"move\": \"预设动作id\", \"ms\": 执行时长}``。``ms`` 为该动作整体"
            "期望时长（毫秒），服务端按预设各 step 默认时长比例缩放，**ms 越大越慢、越小越快**。\n"
            "    ② 自由编排：``{\"steps\": [{\"x\": 角度, \"y\": 角度, \"ms\": 时长}, ...]}``。"
            f"预设里没有合适的动作时**自己编**——x 左右转（{cfg['xMin']}–{cfg['xMax']}，"
            f"{cfg['xCenter']} 居中），y 上下俯仰（{cfg['yMin']}–{cfg['yMax']}，越大越抬头），"
            f"每步 ms ≥ {SERVO_MIN_SEGMENT_DURATION_MS}，最多 {_MAX_COMPOSED_STEPS} 步。"
            "想让动作重复就把步骤写重复（例如左右各两次即为摇头）。\n"
            "    **left/right 类动作默认按观众视角**（机器人对面的人看到的左/右），"
            "不是机器人自己身体的左右。\n"
            f"    可用预设动作：\n{body}\n"
            "    不需要动作时写 []。**只要你在回复里说了要做某个动作，就必须在 moves 里"
            "真的写出来**：找不到贴切的预设就用自由编排，不要只在嘴上说。\n"
        )

    mtime_path = resolve_json_path(SERVO_CFG_FILE)
    if not os.path.isfile(mtime_path) and os.path.isfile(SERVO_CFG_FILE):
        mtime_path = SERVO_CFG_FILE
    return _cached_appendix("moves", mtime_path, _build)


def llm_pb_anims_prompt_appendix() -> str:
    """The legacy model-authored anim lane is intentionally no longer advertised."""

    return ""


def llm_pb_plan_prompt_appendix() -> str:
    """moves + anims 附录合并（替代旧 ``scenes`` / ``servo`` 直写说明）。"""
    parts = [
        llm_pb_moves_prompt_appendix(),
        llm_pb_anims_prompt_appendix(),
    ]
    return "".join(p for p in parts if p)
def llm_memory_prompt_appendix() -> str:
    # 记忆分区注入 system prompt：经整理的长期核心 + 最近几天。分开写模型
    # 才能分辨长期成立的事和刚发生的事；扁平列表久了会被近期琐事淹没。
    from deskbot_server.memory_store import memory_prompt_text

    return memory_prompt_text()


def llm_device_screen_appendix() -> str:
    """屏幕分辨率与当前音量，注入 system prompt。"""
    from deskbot_server.device_volume_store import get_device_volume
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH

    vol = get_device_volume()
    return (
        "设备屏幕与音量：\n"
        f"  - 逻辑分辨率：**{FACE_LCD_WIDTH}×{FACE_LCD_HEIGHT}** 像素，原点左上角 (0,0)。\n"
        f"  - 当前播放音量：**{vol}**（0–100）。JSON 中写 ``volume`` 会下发到 ESP32 并**持久保存**；"
        "省略则保持当前音量。\n"
        "  - 屏幕仅支持 ``images`` 数组展示图片（``{b64, x?, y?, w?, h?}``，base64 JPEG/PNG），"
        "服务端转为 pb 下发；**不要**使用 ``screen_text`` 等屏幕文字字段。\n"
    )


def llm_tools_prompt_appendix() -> str:
    """LLM 可返回的 tools 数组说明。"""
    return (
        "可用工具（可选 ``tools`` 数组；需要工具时 ``tools`` 非空、``tts`` 可留空，"
        "服务端执行后会再次调用你；最终回复时 ``tools`` 写 [] 并填写 ``tts``）："
        "用户已说话时优先在 ``tts`` 里正常回答；不要只返回 tools 而省略完整 JSON 对象。\n"
        "  - register_face: {\"tool\":\"register_face\",\"name\":\"姓名\",\"face_id\":1}\n"
        "    将当前画面 face_id 的人脸注册/更新到档案（embedding 512 维）；"
        "face_id 见每轮 user 消息「图像识别」；仅一张脸时可省略 face_id；多人须指定 face_id 或先向用户澄清。\n"
        "  - capture_camera: {\"tool\":\"capture_camera\",\"display\":false}\n"
        "    按需获取 ESP32 最近上传的一帧相机 JPEG；服务端会在下一轮以视觉图片块附上，"
        "文本工具结果只含尺寸等元数据，不含 base64。"
        "询问「画面里有什么」等视觉问答必须调用本工具，并保持 display=false；"
        "只有主人明确要求「拍照」「把照片显示出来」时才写 display=true，"
        "让同一帧额外回显到机器人屏幕。不要自行把图片写进 ``images``。"
        "若返回无帧，请提示主人确认相机上行已开启。\n"
        "  - memory_add: {\"tool\":\"memory_add\",\"text\":\"要记住的内容\"}\n"
        "    **对话中一旦出现值得长期记住的事，当轮就调用，不要攒到最后。**\n"
        "    该记的：偏好口味、家人宠物、工作与作息、健康忌口、正在做的事。\n"
        "    不该记的：一次性的闲聊、当天就失效的琐事、你自己刚说过的话。\n"
        "  - memory_delete: {\"tool\":\"memory_delete\",\"id\":\"记忆id\"}\n"
        "  - user_note: {\"tool\":\"user_note\",\"action\":\"append\",\"text\":\"对主人的理解\"}\n"
        "    维护你对主人这个人的整体理解（性格、说话方式、在意什么、当前处境）。\n"
        "    和 memory_add 的区别：memory_add 记**事实**，user_note 记**你对他的判断**。\n"
        "    发现新的理解就当轮 append；read 可先看已有内容；仅在内容重复或过时时才 replace。\n"
        "  - schedule_task: cron 定时任务增删改查（北京时间东八区）。"
        "**用户要求定时/提醒时，必须调用本工具，禁止仅用 tts 口头答应。**\n"
        "    示例：主人说「两分钟后请我喝水」→ 第一轮 JSON：\n"
        "    {\"tools\":[{\"tool\":\"schedule_task\",\"action\":\"create\",\"task\":\"提醒喝水\","
        "\"delay_minutes\":2,\"task_kind\":\"once\"}],\"tts\":\"\"}\n"
        "    工具成功后第二轮：{\"tools\":[],\"tts\":\"好，两分钟后提醒你喝水。\"}\n"
        "    创建时无需填写 session_id（服务端自动绑定当前 session）。\n"
        "    **先判断一次性还是周期性**：\n"
        "    · 一次性 once：如「明天9点提醒」→ task_kind=once + cron \"0 9 13 6 *\"（分 时 日 月 周）\n"
        "    · 周期性 recurring：如「每天8点」→ task_kind=recurring + cron \"0 8 * * *\"\n"
        "    · 相对延迟：delay_minutes 填数字（「两分钟」→ 2）\n"
        "    · 到点先演一段表演：加 scene 字段写表演 name（见可用表演目录），如「生日那天先来段祝福」\n"
        "    · 查询列表：{\"action\":\"list\"}；读取：{\"action\":\"get\",\"id\":\"…\"}\n"
        "    · 修改：{\"action\":\"update\",\"id\":\"…\",\"cron\":\"0 9 * * *\",\"task\":\"…\",\"enabled\":true}\n"
        "    · 删除：{\"action\":\"delete\",\"id\":\"…\"}\n"
        "  - webfetch: {\"tool\":\"webfetch\",\"url\":\"https://…\"} 抓取网页文本\n"
        "  - websearch: {\"tool\":\"websearch\",\"query\":\"搜索词\"} 联网搜索，返回摘要(abstract)与来源(results)。"
        "凡是今天/最近/最新的天气、新闻、股价、赛事、价格等时效性问题，必须先调用它，"
        "拿到结果后再回答；绝不能凭记忆作答，也不能回复“无法获取实时信息”\n"
        "  - read: {\"tool\":\"read\",\"path\":\"notes.txt\"} 读取本机 tmp 目录文件\n"
        "  - write: {\"tool\":\"write\",\"path\":\"notes.txt\",\"content\":\"…\"} 写入本机 tmp 目录\n"
        "    read/write 路径仅限 data/local/tmp/ 下，禁止 .. 与绝对路径。\n"
        "  - session: 查询当前与最近对话 session（服务端按 10 分钟无对话自动开新 session）\n"
        "    · 当前 session：{\"tool\":\"session\",\"action\":\"current\"}\n"
        "    · 最近列表：{\"tool\":\"session\",\"action\":\"list\",\"limit\":10}\n"
        "    · 读取详情：{\"tool\":\"session\",\"action\":\"get\",\"session_id\":\"…\"}（省略 id 则读当前）\n"
        "  - miot: 米家智能家居查看/控制（需已在网站「米家」页绑定）。"
        "**用户要求开关灯/空调/场景等家居操作时必须调用，禁止仅口头答应。**\n"
        "    设备清单见上方「米家智能家居」摘要；优先用 name，重名加 room，或用 did。\n"
        "    · 列表：{\"tool\":\"miot\",\"action\":\"list\",\"online\":true}\n"
        "    · 读属性：{\"tool\":\"miot\",\"action\":\"props\",\"name\":\"台灯\",\"keys\":[\"on\",\"brightness\"]}\n"
        "    · 写属性：{\"tool\":\"miot\",\"action\":\"set\",\"name\":\"台灯\",\"key\":\"on\",\"value\":true}\n"
        "    · 查能力：{\"tool\":\"miot\",\"action\":\"spec\",\"name\":\"台灯\"}\n"
        "    · 调动作：{\"tool\":\"miot\",\"action\":\"action\",\"name\":\"音箱\",\"key\":\"play-text\",\"args\":[\"你好\"]}\n"
        "    · 小爱音箱点歌/播电台/执行口令：一律用 execute-text-directive，把要对小爱说的话原样作为指令，"
        "第二个参数 true 表示静默执行；不要用 play-text（那只会朗读文字）。例：\n"
        "      {\"tool\":\"miot\",\"action\":\"action\",\"name\":\"小爱触屏音箱\",\"key\":\"execute-text-directive\",\"args\":[\"播放周杰伦的晴天\",true]}\n"
        "      暂停/下一首/上一首用 key=pause/next/previous（args 为空）；不必先查 spec。\n"
        "    · 跑场景：{\"tool\":\"miot\",\"action\":\"run_scene\",\"scene_name\":\"回家模式\"}\n"
        "    · 刷新缓存：{\"tool\":\"miot\",\"action\":\"sync\"}；授权状态：{\"tool\":\"miot\",\"action\":\"status\"}\n"
        "    失败时结果含 error/hint/solution，请用口语向用户说明原因与解决办法。\n"
    )


def llm_miot_prompt_appendix() -> str:
    """米家家庭/设备摘要，注入 system prompt。"""
    from deskbot_server.miot_service import llm_miot_prompt_appendix as _miot_ax

    return _miot_ax()


def llm_agent_docs_prompt_appendix() -> str:
    # User.md：Agent 对主人的了解，由它自己维护也可手改。人设不在这里——
    # Agent.md 就是系统提示本身，由 deskbot_agent_system_prompt() 取。
    from deskbot_server.agent_docs import user_prompt_appendix

    try:
        text = user_prompt_appendix()
    except Exception:  # noqa: BLE001 - 文档读不出不能拖垮整轮对话
        return ""
    return f"你对主人的了解：\n{text}" if text else ""


def deskbot_agent_system_prompt() -> str:
    """人设与行为，以 Agent.md 为准。

    页面上编辑 Agent.md 必须真的改变机器人的表现，否则那个编辑器就是摆设。
    """
    from deskbot_server.agent_docs import system_prompt

    try:
        return system_prompt()
    except Exception:  # noqa: BLE001
        from deskbot_server.device_data import load_llm_system_prompt

        return (load_llm_system_prompt() or "").strip()


def llm_static_context_prompt_appendix() -> str:
    """行为偏好 + 长期记忆 + 米家摘要 + 工具说明（传感器/人脸见每轮 user 消息）。"""
    parts = [
        llm_agent_docs_prompt_appendix(),
        llm_memory_prompt_appendix(),
        llm_miot_prompt_appendix(),
        llm_tools_prompt_appendix(),
        # 组合表演目录（无表演时空串）
        scene_catalog_prompt(),
        # 剧本任务：进行中任务 + update_task_result/update_task_strategy 契约（无任务时空串）
        quest_prompt_appendix(),
    ]
    return "\n\n".join(p for p in parts if p)
_LLM_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _parse_need_reply_value(v: Any) -> bool:
    """JSON 里 ``need_reply`` 的宽松解析；缺省由调用方视为需要回复。"""
    if v is False or v == 0:
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "否", "不需要", "不用", "none"):
            return False
        if s in ("true", "1", "yes", "是", "需要"):
            return True
        return bool(s)
    return bool(v)


def _parsed_json_need_reply(parsed: dict) -> bool:
    if "need_reply" not in parsed:
        return True
    return _parse_need_reply_value(parsed.get("need_reply"))


def normalize_pb_servo_dict(obj: Any) -> Optional[dict[str, int]]:
    """校验并归一化单条 pb 舵机指令（``xm``/``ym``/``x``/``y``/``ms``），非法则 ``None``。"""
    if not isinstance(obj, dict):
        return None
    candidate = {
        key: obj.get(key, 0)
        for key in ("xm", "ym", "x", "y", "ms")
    }
    for key in ("x_min", "x_max", "y_min", "y_max"):
        if key in obj:
            candidate[key] = obj[key]
    try:
        checked, _duration = validate_servo_steps(
            [candidate],
            context="PB servo",
        )
    except ServoProtocolError:
        return None
    return dict(checked[0])


def coerce_pb_v2_downlink_payload(payload: Any) -> dict[str, Any]:
    """pb v2 下行：``servo`` 须为数组；兼容误写成单对象的历史调用。"""
    if not isinstance(payload, dict):
        return {}
    servo = payload.get("servo")
    if not isinstance(servo, dict):
        return payload
    norm = normalize_pb_servo_dict(servo)
    out = dict(payload)
    if norm:
        out["servo"] = [norm]
    else:
        out.pop("servo", None)
    return out


def _parse_composed_move_steps(raw: Any) -> Optional[list[dict[str, Any]]]:
    """``{"steps": [...]}`` → 协议层 ``__custom__`` 步骤；非法返回 ``None``。

    只做形状与步数/时长校验；角度由 ``expand_llm_moves`` 按本机限位钳制，
    模型给出界的角度不算错误（和 RTC 的 move_head 行为一致）。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    if len(raw) > _MAX_COMPOSED_STEPS:
        return None
    out: list[dict[str, Any]] = []
    total_ms = 0
    for step in raw:
        if not isinstance(step, dict):
            return None
        raw_ms = step.get("ms", 0)
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, int):
            return None
        if raw_ms < SERVO_MIN_SEGMENT_DURATION_MS:
            return None
        total_ms += raw_ms
        if total_ms > SERVO_MAX_PLAN_DURATION_MS:
            return None
        item: dict[str, Any] = {"move": "__custom__", "ms": raw_ms}
        for key, default in (("x", 90), ("y", 90), ("xm", 0), ("ym", 0)):
            value = step.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            item[key] = value
        out.append(item)
    return out


def _parse_llm_move_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    allowed = {preset_id.casefold(): preset_id for preset_id in servo_model_preset_ids()}
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            return []
        raw_steps = item.get("steps")
        if raw_steps is not None:
            # 自由编排：展开成协议层的 __custom__ 步骤（expand_llm_moves 会按
            # 限位与包络钳制），与 RTC move_head 的 steps 同一套语义。
            expanded = _parse_composed_move_steps(raw_steps)
            if expanded is None:
                return []
            out.extend(expanded)
            continue
        move_id = str(item.get("move") or "").strip()
        raw_ms = item.get("ms", 0)
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, int):
            return []
        ms = raw_ms
        canonical_id = allowed.get(move_id.casefold())
        if (
            canonical_id is None
            or ms < SERVO_MIN_SEGMENT_DURATION_MS
            or ms > SERVO_MAX_PLAN_DURATION_MS
        ):
            return []
        out.append({"move": canonical_id, "ms": ms})
    return out


def _parse_llm_anim_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        anim_name = str(item.get("anim") or "").strip()
        try:
            ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            continue
        if not anim_name or ms <= 0:
            continue
        out.append({"anim": anim_name, "ms": ms})
    return out


def _parse_llm_tool_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if not tool:
            continue
        row = dict(item)
        row["tool"] = tool
        out.append(row)
    return out


def _coerce_llm_reply_object(obj: Any) -> Optional[dict[str, Any]]:
    """把 LLM 误输出的「仅 tools 数组 / 单条 tool 对象」规范为完整 JSON 对象。"""
    if isinstance(obj, list):
        tools = _parse_llm_tool_items(obj)
        if tools:
            return {"tools": tools, "tts": "", "need_reply": True}
        return None
    if not isinstance(obj, dict):
        return None
    if (obj.get("tool") or obj.get("name")) and "tools" not in obj:
        tools = _parse_llm_tool_items([obj])
        if tools:
            out: dict[str, Any] = {"tools": tools}
            for key in (
                "need_reply",
                "tts",
                "reply",
                "moves",
                "anims",
                "volume",
                "images",
                "scenes",
            ):
                if key in obj:
                    out[key] = obj[key]
            out.setdefault("tts", "")
            return out
    return obj


def parse_llm_reply(raw: str) -> dict:
    """把 LLM 输出尝试解析为约定 JSON。

    格式 ``{"need_reply", "tts", "volume?", "moves", "anims", "tools": [...]}``；
    仍兼容旧版 ``scenes`` 与 ``reply`` 字段；原始 ``servo`` 坐标不再接受。

    失败时把整段文本当作 ``reply`` 返回，**不抛异常**。
    """
    text = (raw or "").strip()
    parsed: Optional[dict] = None

    candidates = []
    if text:
        candidates.append(text)
        m = _LLM_JSON_FENCE_RE.search(text)
        if m:
            candidates.append(m.group(1))
        try:
            i = text.index("{")
            j = text.rindex("}")
            if j > i:
                candidates.append(text[i : j + 1])
        except ValueError:
            pass

        try:
            i = text.index("[")
            j = text.rindex("]")
            if j > i:
                candidates.append(text[i : j + 1])
        except ValueError:
            pass

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        coerced = _coerce_llm_reply_object(obj)
        if isinstance(coerced, dict):
            parsed = coerced
            break

    moves_out: list[dict[str, Any]] = []
    anims_out: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        moves_out = _parse_llm_move_items(parsed.get("moves"))
        # Never execute legacy model-authored display plans. Explicit visible
        # expressions use the validated play_expression tool and the common
        # USB expression arbiter.
        anims_out = []
        tools_out = _parse_llm_tool_items(parsed.get("tools"))
        reply_tts = parsed.get("tts")
        reply_legacy = parsed.get("reply")
        reply: str
        if isinstance(reply_tts, str) and reply_tts.strip():
            reply = reply_tts.strip()
        elif isinstance(reply_legacy, str) and reply_legacy.strip():
            reply = reply_legacy.strip()
        else:
            # 合法 JSON 但 tts/reply 均为空：勿把整段 JSON 当朗读文本
            reply = ""
        scenes_out: list[str] = []
        # ``scenes`` was the older post-TTS display lane; accept the JSON
        # shape for compatibility but deliberately discard its contents.
        vol = parse_pb_volume(parsed.get("volume"))
        images_out = parse_llm_images(parsed.get("images"))
        need_reply = _parsed_json_need_reply(parsed)
        has_deliverable = bool(
            reply
            or tools_out
            or moves_out
            or anims_out
            or scenes_out
            or images_out
        )
        contract_ok = not need_reply or has_deliverable
        return {
            "reply": reply,
            "moves": moves_out,
            "anims": anims_out,
            "tools": tools_out,
            "scenes": scenes_out,
            "volume": vol,
            "images": images_out,
            "need_reply": need_reply,
            "json_ok": True,
            "contract_ok": contract_ok,
            "contract_error": (
                None
                if contract_ok
                else "need_reply=true requires non-empty tts or an executable action"
            ),
            "raw": text,
        }

    return {
        "reply": text,
        "moves": [],
        "anims": [],
        "tools": [],
        "scenes": [],
        "volume": None,
        "images": [],
        "need_reply": True,
        "json_ok": False,
        "contract_ok": bool(text),
        "contract_error": None if text else "empty model response",
        "raw": text,
    }


__all__ = [
    "llm_device_screen_appendix",
    "llm_agent_docs_prompt_appendix",
    "llm_memory_prompt_appendix",
    "llm_pb_anims_prompt_appendix",
    "llm_pb_moves_prompt_appendix",
    "llm_pb_plan_prompt_appendix",
    "llm_static_context_prompt_appendix",
    "llm_tools_prompt_appendix",
    "coerce_pb_v2_downlink_payload",
    "normalize_pb_servo_dict",
    "parse_llm_reply",
]
