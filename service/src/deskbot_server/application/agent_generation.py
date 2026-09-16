"""对话 Agent 也能用的 AI 生成能力（2026-09-14）。

此前控制台五个"AI 生成"按钮——表演页 AI 编排、主动陪伴 AI 生成场景、动作页 AI 生成动作、
表情页卡通套图生成、表情页文生/图生 SVG 表情——实现都只在 Flask blueprint 里，语音/文字对话
的 Agent 只能"执行已有资产"，不能"创作新资产"。这里把生成 + 落库收到应用层：

- ``compose_*``：只生成草稿（网页路由继续用它，行为不变）；
- ``generate_*``：生成并保存到对应的库（表演列表 / 陪伴场景 / 舵机预设 / 表情库），供工具调用；
- ``apply_default_expression_for_device``：表情映射改了之后让设备立刻换上（Core 进程）。

网页与对话共用同一份提示词和白名单规则；对话侧的工具路由见 ``agent_generation_tools``。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from deskbot_server import (
    ark_face_svg,
    ark_image_gen,
    face_expression_transactions,
    scene_playbooks_store,
    servo_config_store,
)
from deskbot_server.application import (
    cartoon_gen_limit,
    expression_catalog,
    expression_runtime,
    persona_generation,
    quest_console,
)
from deskbot_server.application.persona_generation import GenerationError
from deskbot_server.llm import runtime as llm_runtime

logger = logging.getLogger("deskbot-server")

_SNAKE_RE = re.compile(r"[^a-z0-9_]+")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)

CARTOON_STATES: tuple[str, ...] = ("idle", "listening", "thinking", "speaking")
CARTOON_STATE_TITLES = {"idle": "待机", "listening": "倾听", "thinking": "思考", "speaking": "说话"}
# 与表情页 cartoonStateDescription 逐字一致：四个状态必须一眼能分辨，各有一个标志性五官特征。
CARTOON_STATE_PROMPTS = {
    "idle": "放松待机：闭着嘴自然浅笑，眼睛平视前方；第一帧睁眼浅笑，第二帧闭眼眨眼，第三帧眼睛看向右下角、嘴不变",
    "listening": (
        "认真倾听：两只眼睛睁得很大、瞳孔放大，两条眉毛高高扬起，嘴闭成一个小小的圆形 o（专注、好奇地在听）；"
        "第一帧正视，第二帧眼睛微微看向左上方，第三帧回到正前方并睁得更大"
    ),
    "thinking": (
        "思考中：两只眼睛一起看向左上方，一条眉毛皱起压低、另一条挑高，嘴歪到一边抿住；画面右上角有白色小圆点省略号「…」"
        "并逐帧增多——第一帧 1 个点、第二帧 2 个点、第三帧 3 个点，五官保持不变"
    ),
    "speaking": (
        "正在开心地说话：眼睛弯成向下的开心弧线，嘴巴张开的幅度三帧明显不同——第一帧嘴微张、第二帧嘴张得很大露出红色口腔、"
        "第三帧嘴半闭，眼睛和腮红三帧保持不变"
    ),
}
CARTOON_FRAMES_PER_STATE = 3


def _snake(value: object, fallback: str) -> str:
    return _SNAKE_RE.sub("_", str(value or fallback).lower()).strip("_") or fallback


def _unique_name(base: str, taken: set[str]) -> str:
    name, suffix = base, 2
    while name.lower() in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


# ---------------- 表演（scene playbook） ----------------


def compose_scene_playbook(
    description: str,
    *,
    expressions: list[str],
    presets: list[dict[str, str]],
) -> dict[str, Any]:
    """按自然语言描述编一段表演草稿（2-8 步，口播 + 表情 + 动作）。

    表情/动作只能从传入目录里选，模型编造的名称一律清空，绝不下发到设备。失败抛 GenerationError。
    """
    description = str(description or "").strip()
    if not description:
        raise GenerationError("请先描述想要的场景")
    expressions = [str(s).strip() for s in expressions if str(s).strip()][:80]
    clean_presets = [
        {"id": str(row["id"]).strip(), "label": str(row.get("label") or "").strip()}
        for row in presets[:80]
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    ]
    preset_ids = {p["id"] for p in clean_presets}
    expr_set = set(expressions)
    system = (
        "现在你要以自己的身份给自己编一段表演：把主人的描述编成 2-8 个步骤的演出，"
        "每步可含口播文本、一个表情、一个头部动作。口播要像你平时说话，用你对主人的称呼。"
        "只输出 JSON 对象，不要任何解释或代码块标记。格式：\n"
        '{"name":"英文snake_case标识","title":"中文标题","chunks":['
        '{"text":"口播（可空串）","expr":{"scene":"表情名或空串","ms":800},'
        '"servo":{"preset":"动作id或空串","ms":800}}]}\n'
        "表情只能从这里选（不合适就留空串）：" + ("、".join(expressions) or "（无）") + "\n"
        "动作只能从这里选（不合适就留空串）：" + (
            "、".join(f"{p['id']}({p['label']})" if p["label"] else p["id"] for p in clean_presets)
            or "（无）"
        ) + "\n"
        "ms 为该步表情/动作时长（200-10000）。口播要口语化、有性格、简短。"
    )
    data = persona_generation.generate_json(system, description, temperature=0.8)

    def _ms(value: object) -> int:
        try:
            return max(200, min(10000, int(value)))
        except (TypeError, ValueError):
            return 800

    chunks: list[dict[str, Any]] = []
    for raw_chunk in (data.get("chunks") or [])[:12]:
        if not isinstance(raw_chunk, dict):
            continue
        expr = raw_chunk.get("expr") or {}
        servo = raw_chunk.get("servo") or {}
        scene = str(expr.get("scene") or "").strip() if isinstance(expr, dict) else ""
        preset = str(servo.get("preset") or "").strip() if isinstance(servo, dict) else ""
        chunk = {
            "text": str(raw_chunk.get("text") or "").strip(),
            # 白名单过滤：模型编造的名称一律清空。
            "expr": {"scene": scene if scene in expr_set else "", "ms": _ms(expr.get("ms") if isinstance(expr, dict) else None)},
            "servo": {"preset": preset if preset in preset_ids else "", "ms": _ms(servo.get("ms") if isinstance(servo, dict) else None)},
        }
        if chunk["text"] or chunk["expr"]["scene"] or chunk["servo"]["preset"]:
            chunks.append(chunk)
    if not chunks:
        raise GenerationError("生成结果为空，请补充描述再试")
    return {
        "name": _snake(data.get("name"), "ai_scene"),
        "title": str(data.get("title") or "").strip() or "AI 编排",
        "chunks": chunks,
    }


def playbook_outline(playbook: dict[str, Any]) -> list[str]:
    """给模型复述用的一行一步：口播（表情/动作）。"""
    out: list[str] = []
    for chunk in playbook.get("chunks") or []:
        text = str(chunk.get("text") or "").strip()
        expr = str((chunk.get("expr") or {}).get("scene") or "").strip()
        servo = str((chunk.get("servo") or {}).get("preset") or "").strip()
        tags = "/".join(t for t in (expr, servo) if t)
        out.append((text or "（无口播）") + (f"（{tags}）" if tags else ""))
    return out[:12]


def generate_scene_playbook(description: str) -> dict[str, Any]:
    """AI 编排一段表演并存进表演列表（对话工具入口）。目录取本机表情库 + 舵机预设。"""
    try:
        expressions = [scene.name for scene in expression_catalog.load_expression_catalog().scenes]
    except Exception:  # noqa: BLE001 —— 表情库读不出就只编口播/动作
        expressions = []
    presets = [
        {"id": str(p.get("id") or ""), "label": str(p.get("label") or "")}
        for p in servo_config_store.servo_preset_catalog()
        if isinstance(p, dict) and str(p.get("id") or "").strip()
    ]
    draft = compose_scene_playbook(description, expressions=expressions, presets=presets)
    rows = list(scene_playbooks_store.load_scene_playbooks_file() or [])
    taken = {str(r.get("name") or "").strip().lower() for r in rows if isinstance(r, dict)}
    draft["name"] = _unique_name(draft["name"], taken)
    playbook = scene_playbooks_store.normalize_playbook(draft)
    rows.append(playbook)
    scene_playbooks_store.save_scene_playbooks_file(rows)
    logger.info("[agent_generation] scene playbook saved name=%s steps=%d", playbook["name"], len(playbook["chunks"]))
    return {
        "ok": True,
        "name": playbook["name"],
        "title": playbook["title"],
        "steps": len(playbook["chunks"]),
        "outline": playbook_outline(draft),
    }


# ---------------- 头部动作（servo preset） ----------------


def compose_motion_preset(
    description: str,
    *,
    envelope: dict[str, int],
    min_ms: int = 200,
    existing_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """按描述生成一个头部动作（1-6 步 x/y 序列），按包络钳位绝不越界。失败抛 GenerationError。"""
    description = str(description or "").strip()
    if not description:
        raise GenerationError("请先描述想要的动作")

    def _envi(key: str, default: int) -> int:
        try:
            return int(envelope.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    x_min, x_max = _envi("xMin", 0), _envi("xMax", 180)
    y_min, y_max = _envi("yMin", 0), _envi("yMax", 180)
    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min
    x_ctr = max(x_min, min(x_max, _envi("xCenter", 90)))
    y_ctr = max(y_min, min(y_max, _envi("yCenter", 90)))
    min_ms = max(50, int(min_ms or 200))
    max_ms = 3000
    existing = {str(x).strip().lower() for x in existing_ids if str(x).strip()}
    try:
        cfg = llm_runtime.resolve_llm_config()
    except ValueError as exc:
        raise GenerationError(str(exc)) from exc
    system = (
        "你是桌面机器人「小歪」的头部动作设计师。把用户的中文描述编成 1-6 个步骤的头部动作。"
        "只输出一个 JSON 对象，不要任何解释或代码块标记。格式：\n"
        '{"id":"英文snake_case标识","label":"中文短名","steps":[{"x":90,"y":90,"ms":500}]}\n'
        "坐标系：x=左右转头，取值范围 [%d, %d]，居中约 %d；"
        "y=上下俯仰，取值范围 [%d, %d]，居中约 %d；数值都是绝对角度（度）。\n"
        "ms 是该步用时，范围 %d~%d。想要点头/摇头等重复动作就重复相应步骤。"
        "动作自然流畅，最后一步通常回到居中。"
        % (x_min, x_max, x_ctr, y_min, y_max, y_ctr, min_ms, max_ms)
    )
    try:
        raw, _meta = llm_runtime.chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": description}],
            temperature=0.7,
            config=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(f"生成失败：{exc}") from exc
    match = _JSON_OBJECT_RE.search(str(raw or "").strip())
    if not match:
        raise GenerationError("模型未返回有效动作，请换个说法再试")
    try:
        data = json.loads(match.group())
    except ValueError as exc:
        raise GenerationError("模型输出解析失败，请重试") from exc
    if not isinstance(data, dict):
        raise GenerationError("模型输出不是对象，请重试")
    new_id = _unique_name(_snake(data.get("id"), "ai_move"), existing)
    label = (str(data.get("label") or "").strip() or "AI 动作")[:40]

    def _clampi(v: object, lo: int, hi: int, default: int) -> int:
        try:
            return max(lo, min(hi, int(round(float(v)))))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    steps = [
        {
            "x": _clampi(raw_step.get("x"), x_min, x_max, x_ctr),
            "y": _clampi(raw_step.get("y"), y_min, y_max, y_ctr),
            "xm": 0,
            "ym": 0,
            "ms": _clampi(raw_step.get("ms"), min_ms, max_ms, 500),
        }
        for raw_step in (data.get("steps") or [])[:8]
        if isinstance(raw_step, dict)
    ]
    if not steps:
        raise GenerationError("生成结果为空，请补充描述再试")
    return {"id": new_id, "label": label, "steps": steps}


def generate_motion_preset(description: str, *, label: str = "") -> dict[str, Any]:
    """AI 生成一个头部动作并存为模型可见的预设（对话工具入口）。包络取本机 servo.json 限位。"""
    cfg = copy.deepcopy(servo_config_store.load_servo_cfg_file() or {})
    limits = servo_config_store.servo_limits()
    envelope = {
        "xMin": limits["xMin"], "xMax": limits["xMax"], "yMin": limits["yMin"], "yMax": limits["yMax"],
        "xCenter": 90, "yCenter": 90,
    }
    presets = [p for p in (cfg.get("presets") or []) if isinstance(p, dict)]
    preset = compose_motion_preset(
        description, envelope=envelope, existing_ids=[str(p.get("id") or "") for p in presets]
    )
    clean_label = str(label or "").strip()[:40]
    if clean_label:
        preset["label"] = clean_label
    preset["exposeToModel"] = True
    presets.append(preset)
    cfg["presets"] = presets
    servo_config_store.save_servo_cfg_file(cfg)
    logger.info("[agent_generation] motion preset saved id=%s steps=%d", preset["id"], len(preset["steps"]))
    return {"ok": True, "id": preset["id"], "label": preset["label"], "steps": preset["steps"]}


# ---------------- 主动陪伴场景（quest） ----------------


def generate_quest_scene(description: str, *, title: str = "") -> dict[str, Any]:
    """一句话生成一个陪伴场景并启用：主线小目标排成一条线，反复做的进「日常关心」（与网页同一入口）。"""
    out = quest_console.generate_playbook(str(description or ""), title=str(title or ""), select=True)
    pb = out.get("playbook") or {}
    return {
        "ok": True,
        "playbook_name": str(pb.get("name") or "") or None,
        "playbook_title": str(pb.get("title") or pb.get("name") or "") or None,
        "story_count": int(out.get("story_count") or 0),
        "care_count": int(out.get("care_count") or 0),
        "care_titles": list(out.get("care_titles") or []),
        "dropped": list(out.get("dropped") or []),
    }


# ---------------- 表情库（SVG / 卡通） ----------------


def save_expression_scenes(scenes: list[dict[str, Any]], *, mapping: dict[str, int] | None = None) -> dict[str, Any]:
    """把若干场景作为「我的表情」存进表情库（原子事务，名字由库分配）；``mapping`` = {状态: 第几个新场景}。"""
    state = face_expression_transactions.get_face_expression_state()
    payload: dict[str, Any] = {"expected_revision": state["revision"], "scenes": {"create": scenes}}
    if mapping:
        payload["map_create_refs"] = dict(mapping)
    result = face_expression_transactions.apply_face_expression_transaction(payload)
    created = [
        {"name": str(row.get("name") or ""), "title": str(row.get("title") or "")}
        for row in (result.get("created") or [])
        if isinstance(row, dict)
    ]
    return {"created": created, "revision": result.get("revision")}


def generate_svg_expression(
    description: str,
    *,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    set_default: bool = False,
) -> dict[str, Any]:
    """文生 / 图生一个矢量表情，存进「我的表情」；``set_default`` 时映射为待机表情。"""
    prompt = str(description or "").strip()
    if not prompt and not image_bytes:
        raise GenerationError("请先描述想要的表情")
    try:
        if image_bytes:
            out = ark_face_svg.generate_face_svg_from_image(image_bytes, image_mime, prompt=prompt)
        else:
            out = ark_face_svg.generate_face_svg_from_text(prompt)
    except (ValueError, RuntimeError) as exc:
        raise GenerationError(str(exc)) from exc
    scene = copy.deepcopy(out.get("scene") or {})
    scene.pop("name", None)  # 名字由表情库分配（用户表情）
    scene["title"] = str(out.get("title") or scene.get("title") or prompt[:20] or "AI 表情").strip()[:80]
    saved = save_expression_scenes([scene], mapping={"idle": 0} if set_default else None)
    created = (saved.get("created") or [{}])[0]
    return {
        "ok": True,
        "name": created.get("name"),
        "title": created.get("title") or scene["title"],
        "frames": len(scene.get("frames") or []),
        "set_default": bool(set_default),
        "source": "camera" if image_bytes else "text",
    }


def generate_cartoon_set(
    description: str,
    *,
    style: str = "",
    progress: Callable[[str], None] | None = None,
    frames_per_state: int = CARTOON_FRAMES_PER_STATE,
) -> dict[str, list[str]]:
    """生成整套卡通表情（待机/倾听/思考/说话各 3 帧 JPEG base64）。

    与表情页向导同一套做法：先画待机定下角色，再以它的第一帧为参考图并行画另外三个状态，
    整套才是同一个角色、同一画风。返回 {状态: [b64...]}。
    """
    style_key = str(style or "").strip().lower()
    if style_key not in ark_image_gen.CARTOON_STYLES:
        style_key = ark_image_gen.DEFAULT_STYLE
    frames_per_state = max(1, min(CARTOON_FRAMES_PER_STATE, int(frames_per_state or CARTOON_FRAMES_PER_STATE)))
    try:
        cartoon_gen_limit.check_and_consume_cartoon_gen(images=frames_per_state * len(CARTOON_STATES))
    except cartoon_gen_limit.CartoonGenLimitExceeded as exc:
        raise GenerationError(str(exc)) from exc
    tell = progress or (lambda _text: None)
    base = f"角色特点：{str(description or '').strip()}。" if str(description or "").strip() else ""

    def one(state: str, reference: str | None = None) -> list[str]:
        try:
            result = ark_image_gen.generate_cartoon_frames(
                base + CARTOON_STATE_PROMPTS[state],
                style=style_key,
                frames=frames_per_state,
                reference_b64=reference,
                state=state,
            )
        except ValueError as exc:
            raise GenerationError(f"{CARTOON_STATE_TITLES[state]}：{exc}") from exc
        frames = [str(f) for f in (result.get("frames") or []) if str(f)][:frames_per_state]
        if not frames:
            raise GenerationError(f"{CARTOON_STATE_TITLES[state]}：模型没有返回图片")
        return frames

    started = time.monotonic()
    tell("先画待机状态定下角色（约 100 秒）…")
    frames = {"idle": one("idle")}
    reference = frames["idle"][0]
    tell(f"待机画好了（{int(time.monotonic() - started)}s），再并行画倾听 / 思考 / 说话（约 100 秒）…")
    rest = [s for s in CARTOON_STATES if s != "idle"]
    with ThreadPoolExecutor(max_workers=len(rest), thread_name_prefix="deskbot-cartoon") as pool:
        futures = {state: pool.submit(one, state, reference) for state in rest}
        for state, future in futures.items():
            frames[state] = future.result()
    tell(f"四个状态都画好了（{int(time.monotonic() - started)}s）")
    return frames


def save_cartoon_set(frames: dict[str, list[str]], *, set_tag: str, apply: bool) -> dict[str, Any]:
    """整套存进「我的表情」（四个状态各一条）；``apply`` 时同时把待机/倾听/思考/说话映射到它们。"""
    scenes: list[dict[str, Any]] = []
    mapping: dict[str, int] = {}
    for index, state in enumerate(CARTOON_STATES):
        assets = list(frames.get(state) or [])
        if not assets:
            raise GenerationError(f"缺少{CARTOON_STATE_TITLES[state]}状态的图片")
        scene = ark_image_gen.frames_to_scene(assets, name=f"cartoon_{state}", title=f"卡通·{CARTOON_STATE_TITLES[state]}（{set_tag}）")
        scene.pop("name", None)
        scenes.append(scene)
        mapping[state] = index
    saved = save_expression_scenes(scenes, mapping=mapping if apply else None)
    return {"created": saved["created"], "applied": bool(apply)}


async def apply_default_expression_for_device(device_id: str) -> dict[str, Any]:
    """表情映射改了之后让设备立刻换上（Core 进程）：按当前生命周期状态重推一次，映射变了就会重发。"""
    runtime = expression_runtime.get_rtc_expression_runtime(device_id)
    if runtime is None:
        return {"ok": False, "error": "设备没有连接，表情已保存，下次连上就会用"}
    result = await runtime.transition(runtime.desired_state or "idle", force=False, reason="agent_generation_apply")
    out = result.as_tool_result()
    out["ok"] = bool(out.get("ok", True)) or str(out.get("status") or "") in ("accepted", "played", "unchanged", "coalesced", "deferred")
    return out


__all__ = [
    "CARTOON_STATES",
    "CARTOON_STATE_PROMPTS",
    "CARTOON_STATE_TITLES",
    "apply_default_expression_for_device",
    "compose_motion_preset",
    "compose_scene_playbook",
    "generate_cartoon_set",
    "generate_motion_preset",
    "generate_quest_scene",
    "generate_scene_playbook",
    "generate_svg_expression",
    "playbook_outline",
    "save_cartoon_set",
    "save_expression_scenes",
]
