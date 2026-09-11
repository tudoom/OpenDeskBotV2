from __future__ import annotations

import logging
import mimetypes
import time

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from deskbot_server.application.cartoon_gen_limit import (
    CartoonGenLimitExceeded,
    check_and_consume_cartoon_gen,
)
from deskbot_server.ark_face_svg import MAX_IMAGE_BYTES, MAX_IMAGE_UPLOAD_REQUEST_BYTES
from deskbot_server.ark_image_gen import (
    DEFAULT_STYLE,
    MAX_UPLOAD_FRAMES,
    canonicalize_upload_frame,
    generate_cartoon_frames,
    list_styles,
)
from deskbot_server.face_design_store import FaceDesignRevisionConflict
from deskbot_server.face_expr_scenes_store import (
    load_face_expr_scenes_file,
    save_face_expr_scenes_file,
)
from deskbot_server.face_expression_transactions import (
    FaceExpressionPermissionError,
    apply_face_expression_transaction,
    get_face_expression_state,
)
from deskbot_server.face_mouth_config_store import (
    load_face_mouth_cfg_file,
    save_face_mouth_cfg_file,
)
from deskbot_server.hardware_catalog import list_devices
from deskbot_server.image_gen_env_store import image_gen_config_status, save_image_gen_env
from deskbot_server.llm.config_state import (
    get_llm_config_status,
    read_llm_env_values,
)
from deskbot_server.llm.env_store import save_llm_env
from deskbot_server.llm.runtime import (
    DEEPSEEK_OPENAI_BASE_URL,
    DEFAULT_LLM_MODEL,
    ResolvedLlmConfig,
    build_chat_model,
    build_provider_auth_headers,
    chat_completion,
    resolve_llm_config,
    resolve_system_llm_config,
)
from deskbot_server.llm_config_store import (
    SUPPORTED_PROTOCOLS,
)
from deskbot_server.web.blueprints.app_bp import (
    _consume_settings_test_quota,
)
from deskbot_server.web.helpers import camera_view_ws_base
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.websearch_env_store import (
    save_websearch_env,
    test_websearch,
    websearch_config_status,
)

# No url_prefix: consumer pages live directly at the site root.
bp = Blueprint("app2c", __name__)


def _default_robot_face_payload() -> dict:
    """首页 / 实验台 3D 小脸的初始贴图 = 当前 ``idle`` 场景首帧。

    与表情页预览、设备待机屏同源：数据取本机 deskbot-face.json 的
    ``mappings.idle`` 所指场景的第 0 帧；文件缺失或场景不存在时退回
    ``pb.primitive_spec.BUILTIN_FACE_ELEMENTS``（固件内建脸的三端镜像）。
    """
    import copy

    from deskbot_server.face_design_store import (
        _load_face_design_cached,
        find_emotion_expression,
        pick_expression_elements,
    )
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.primitive_spec import BUILTIN_FACE_ELEMENTS, LAYER_ORDER

    elements: dict = {}
    try:
        doc = _load_face_design_cached()
        if isinstance(doc, dict):
            mappings = doc.get("mappings") if isinstance(doc.get("mappings"), dict) else {}
            scene_name = str(mappings.get("idle") or "idle")
            expr = find_emotion_expression(doc, scene_name) or find_emotion_expression(doc, "idle")
            elements = pick_expression_elements(expr, at_ms=0)
    except Exception:  # 首页不能因表情文件损坏而 500；退回内建脸
        elements = {}
    if not any(elements.get(layer) for layer in ("eye_l", "eye_r", "mouth")):
        elements = copy.deepcopy(BUILTIN_FACE_ELEMENTS)
    return {
        "face_lcd_w": FACE_LCD_WIDTH,
        "face_lcd_h": FACE_LCD_HEIGHT,
        "expr_default_anim": {
            "elements": {
                layer: list(elements.get(layer) or []) for layer in LAYER_ORDER
            },
        },
    }


@bp.get("/home")
def home():
    return render_template(
        "app2c/home.html",
        active_nav="home",
        camera_view_ws_base=camera_view_ws_base(),
        **_default_robot_face_payload(),
    )


@bp.get("/voice")
def voice():
    return render_template("app2c/voice.html", active_nav="voice")


@bp.get("/expr")
def expr():
    return render_template("app2c/expr.html", active_nav="expr")


@bp.get("/lab")
def lab():
    return render_template(
        "app2c/lab.html",
        active_nav="lab",
        **_default_robot_face_payload(),
    )


@bp.get("/playbooks")
def playbooks():
    return render_template("app2c/playbooks.html", active_nav="playbooks")


@bp.post("/api/playbooks/ai_generate")
def playbooks_ai_generate():
    """按自然语言描述生成一个场景编排草稿。

    可用表情/动作目录由前端随请求带入（它已经加载过），服务端把目录写进
    提示词并对模型输出做白名单过滤，未知名称一律清空，绝不下发到设备。
    """
    import re as _re

    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    if not description:
        return jsonify({"ok": False, "error": "请先描述想要的场景"}), 400
    expressions = [
        str(s).strip()
        for s in (payload.get("expressions") or [])
        if str(s).strip()
    ][:80]
    presets_in = payload.get("presets") or []
    presets = []
    for row in presets_in[:80]:
        if isinstance(row, dict) and str(row.get("id") or "").strip():
            presets.append(
                {
                    "id": str(row["id"]).strip(),
                    "label": str(row.get("label") or "").strip(),
                }
            )
    preset_ids = {p["id"] for p in presets}
    expr_set = set(expressions)

    _quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err

    from deskbot_server.application.persona_generation import GenerationError, generate_json

    system = (
        "现在你要以自己的身份给自己编一段表演：把主人的描述编成 2-8 个步骤的演出，"
        "每步可含口播文本、一个表情、一个头部动作。口播要像你平时说话，用你对主人的称呼。"
        "只输出 JSON 对象，不要任何解释或代码块标记。格式：\n"
        '{"name":"英文snake_case标识","title":"中文标题","chunks":['
        '{"text":"口播（可空串）","expr":{"scene":"表情名或空串","ms":800},'
        '"servo":{"preset":"动作id或空串","ms":800}}]}\n'
        "表情只能从这里选（不合适就留空串）：" + ("、".join(expressions) or "（无）") + "\n"
        "动作只能从这里选（不合适就留空串）：" + (
            "、".join(f"{p['id']}({p['label']})" if p["label"] else p["id"] for p in presets)
            or "（无）"
        ) + "\n"
        "ms 为该步表情/动作时长（200-10000）。口播要口语化、有性格、简短。"
    )
    try:
        data = generate_json(system, description, temperature=0.8)
    except GenerationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    name = _re.sub(r"[^a-z0-9_]+", "_", str(data.get("name") or "ai_scene").lower()).strip("_") or "ai_scene"
    chunks = []
    for raw_chunk in (data.get("chunks") or [])[:12]:
        if not isinstance(raw_chunk, dict):
            continue
        expr = raw_chunk.get("expr") or {}
        servo = raw_chunk.get("servo") or {}

        def _ms(value: object) -> int:
            try:
                return max(200, min(10000, int(value)))
            except (TypeError, ValueError):
                return 800

        scene = str(expr.get("scene") or "").strip()
        preset = str(servo.get("preset") or "").strip()
        chunk = {
            "text": str(raw_chunk.get("text") or "").strip(),
            # 白名单过滤：模型编造的名称一律清空。
            "expr": {"scene": scene if scene in expr_set else "", "ms": _ms(expr.get("ms"))},
            "servo": {"preset": preset if preset in preset_ids else "", "ms": _ms(servo.get("ms"))},
        }
        if chunk["text"] or chunk["expr"]["scene"] or chunk["servo"]["preset"]:
            chunks.append(chunk)
    if not chunks:
        return jsonify({"ok": False, "error": "生成结果为空，请补充描述再试"}), 502
    return jsonify(
        {
            "ok": True,
            "playbook": {
                "name": name,
                "title": str(data.get("title") or "").strip() or "AI 编排",
                "chunks": chunks,
            },
        }
    )


@bp.post("/api/servo/ai_generate")
def servo_ai_generate():
    """按自然语言描述生成一个新的头部舵机动作（步骤序列），供实验台测试/保存。

    与 /api/playbooks/ai_generate 不同：那个从已有预设里挑，这个直接生成 x/y 步骤。
    生成结果按前端传入的舵机包络（硬件限位）钳位，绝不越界；不落盘，保存由前端
    走 /api/servo_config。
    """
    import json as _json
    import re as _re

    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    if not description:
        return jsonify({"ok": False, "error": "请先描述想要的动作"}), 400

    env = payload.get("envelope") or {}
    def _envi(key, default):
        try:
            return int(env.get(key))
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
    try:
        min_ms = max(50, int(payload.get("minMs") or 200))
    except (TypeError, ValueError):
        min_ms = 200
    max_ms = 3000
    existing = {str(x).strip().lower() for x in (payload.get("existingIds") or []) if str(x).strip()}

    _quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err

    try:
        cfg = resolve_llm_config()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

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
        raw, _meta = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": description},
            ],
            temperature=0.7,
            config=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"生成失败：{exc}"}), 502

    text = str(raw or "").strip()
    match = _re.search(r"\{.*\}", text, _re.S)
    if not match:
        return jsonify({"ok": False, "error": "模型未返回有效动作，请换个说法再试"}), 502
    try:
        data = _json.loads(match.group())
    except ValueError:
        return jsonify({"ok": False, "error": "模型输出解析失败，请重试"}), 502

    base_id = _re.sub(r"[^a-z0-9_]+", "_", str(data.get("id") or "ai_move").lower()).strip("_") or "ai_move"
    new_id = base_id
    suffix = 2
    while new_id.lower() in existing:
        new_id = f"{base_id}_{suffix}"
        suffix += 1
    label = str(data.get("label") or "").strip() or "AI 动作"
    if len(label) > 40:
        label = label[:40]

    steps = []
    for raw_step in (data.get("steps") or [])[:8]:
        if not isinstance(raw_step, dict):
            continue
        def _clampi(v, lo, hi, default):
            try:
                return max(lo, min(hi, int(round(float(v)))))
            except (TypeError, ValueError):
                return default
        steps.append({
            "x": _clampi(raw_step.get("x"), x_min, x_max, x_ctr),
            "y": _clampi(raw_step.get("y"), y_min, y_max, y_ctr),
            "xm": 0,
            "ym": 0,
            "ms": _clampi(raw_step.get("ms"), min_ms, max_ms, 500),
        })
    if not steps:
        return jsonify({"ok": False, "error": "生成结果为空，请补充描述再试"}), 502

    return jsonify({
        "ok": True,
        "preset": {"id": new_id, "label": label, "steps": steps},
    })


@bp.get("/api/cartoon_face/styles")
def cartoon_face_styles():
    return jsonify({"ok": True, "styles": list_styles(), "default": DEFAULT_STYLE})


@bp.get("/api/cartoon_face/config")
def cartoon_face_config_get():
    """表情页「生图 API 配置」：Key 来源 / 接口地址 / 模型（不回传 Key）。"""
    return jsonify({"ok": True, **image_gen_config_status()})


@bp.patch("/api/cartoon_face/config")
def cartoon_face_config_patch():
    payload = request.get_json(silent=True) or {}
    try:
        status = save_image_gen_env(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **status})


@bp.post("/api/cartoon_face/generate")
def cartoon_face_generate():
    """按风格预设 + 情绪描述，用 Seedream 组图生成 3 帧 240×240 卡通表情。"""
    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    style = str(payload.get("style") or "").strip().lower()
    try:
        frames = int(payload.get("frames") or 3)
    except (TypeError, ValueError):
        frames = 3
    # 生图单独记账（一轮套图 7 张），不占设置页"测试"的 50 次/天。
    try:
        check_and_consume_cartoon_gen(images=max(1, min(3, frames)))
    except CartoonGenLimitExceeded as exc:
        return jsonify({"ok": False, "error": str(exc), "daily_limit": exc.limit}), 429
    reference = payload.get("reference")
    reference = str(reference).strip() if isinstance(reference, str) else ""
    state = str(payload.get("state") or "").strip().lower() or None
    try:
        variant = int(payload["variant"]) if payload.get("variant") is not None else None
    except (TypeError, ValueError):
        variant = None
    started = time.perf_counter()
    try:
        result = generate_cartoon_frames(
            description, style=style, frames=frames, reference_b64=reference or None,
            state=state, variant=variant,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - 生成失败要给用户可读原因
        _cartoon_logger.warning(
            "[cartoon_face] generate failed style=%s frames=%d %.1fs: %s",
            style, frames, time.perf_counter() - started, exc,
        )
        return jsonify({"ok": False, "error": f"生成失败：{exc}"}), 502
    # 一次组图（3 张 2048²）实测 ~100s，这里留时长方便回答"为什么这么慢"。
    _cartoon_logger.info(
        "[cartoon_face] generate ok style=%s frames=%d model=%s %.1fs",
        style, len(result["frames"]), result["model"], time.perf_counter() - started,
    )
    return jsonify({"ok": True, "frames": result["frames"], "model": result["model"]})


_cartoon_logger = logging.getLogger("deskbot-server")


@bp.post("/api/cartoon_face/upload")
def cartoon_face_upload():
    """上传 1~3 张图片 → 240×240 JPEG base64 帧（不落盘，前端拼成场景后保存到库）。"""
    request.max_content_length = MAX_IMAGE_UPLOAD_REQUEST_BYTES * MAX_UPLOAD_FRAMES
    try:
        uploads = request.files.getlist("images") or request.files.getlist("image")
    except RequestEntityTooLarge:
        return jsonify({"ok": False, "error": "上传请求过大"}), 413
    uploads = [u for u in uploads if u is not None and u.filename][:MAX_UPLOAD_FRAMES]
    if not uploads:
        return jsonify({"ok": False, "error": "请先选择图片"}), 400
    frames: list[str] = []
    for upload in uploads:
        image_bytes = upload.stream.read(MAX_IMAGE_BYTES + 1)
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            return jsonify({"ok": False, "error": f"{upload.filename} 为空或超过 6MB"}), 400
        declared = _image_mime_from_upload(
            upload.filename, upload.mimetype or upload.content_type or "", image_bytes
        )
        try:
            frames.append(canonicalize_upload_frame(image_bytes, declared_mime=declared))
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"{upload.filename}: {exc}"}), 400
    return jsonify({"ok": True, "frames": frames})


@bp.get("/agent")
def agent():
    return render_template("app2c/agent.html", active_nav="agent")


@bp.get("/memories")
def memories():
    return render_template("app2c/memories.html", active_nav="agent")


@bp.get("/reminders")
def reminders():
    return render_template("app2c/reminders.html", active_nav="agent")


@bp.get("/sessions")
def sessions():
    return render_template("app2c/sessions.html", active_nav="agent")


@bp.get("/preferences")
def preferences():
    return render_template("app2c/preferences.html", active_nav="agent")


@bp.get("/people")
def people():
    # 「认识的人」收进 Agent 对话页右上角「更多功能」，侧栏高亮跟随 Agent。
    return render_template("app2c/people.html", active_nav="agent")


@bp.get("/devices")
def devices():
    # USB 设备与 WiFi 配网合并为「设备连接」单页。
    return render_template("app2c/connection.html", active_nav="device")


@bp.get("/wifi")
def wifi():
    return redirect(url_for("app2c.devices"))


@bp.get("/miot")
def miot():
    return render_template("app2c/miot.html", active_nav="miot")


@bp.get("/params")
def params():
    """参数设置：设备端的静音 / 麦克风上行 / 温度断电（原在动作页）。"""
    return render_template("app2c/params.html", active_nav="params")


@bp.get("/advanced")
def advanced():
    from deskbot_server.device_data import local_data_dir

    return render_template(
        "app2c/advanced.html",
        active_nav="advanced",
        local_data_dir=str(local_data_dir()),
    )


@bp.get("/onboarding")
def onboarding():
    return redirect(url_for("app2c.advanced", tab="llm"))


# Text chat defaults to DeepSeek (deepseek-flash).  Image generation keeps its
# independent Ark configuration and credential path.
DEFAULT_TEXT_MODEL = DEFAULT_LLM_MODEL
DEFAULT_TEXT_PROTOCOL = "openai"
DEFAULT_TEXT_BASE_URL = DEEPSEEK_OPENAI_BASE_URL


def _editable_system_llm_fields(sys: ResolvedLlmConfig) -> dict[str, str]:
    """Return values suitable for editing, without expanding explicit blanks."""
    raw = read_llm_env_values()
    return {
        "model_name": (
            str(raw.get("LLM_MODEL") or "").strip()
            or str(sys.model or "").strip()
            or DEFAULT_TEXT_MODEL
        ),
        "protocol": (
            str(raw.get("LLM_PROTOCOL") or "").strip()
            or str(sys.protocol or "").strip()
            or DEFAULT_TEXT_PROTOCOL
        ),
        "base_url": raw.get(
            "LLM_BASE_URL",
            sys.api_base or DEFAULT_TEXT_BASE_URL,
        ),
    }


def _system_llm_payload() -> dict:
    sys = resolve_system_llm_config(require_model=False)
    api_key_set = _llm_api_key_set(sys.api_key)
    fields = _editable_system_llm_fields(sys)
    model_configured = bool(fields["model_name"].strip())
    configured = api_key_set and model_configured
    status = get_llm_config_status()
    return {
        "config": {
            "display_name": sys.display_name,
            **fields,
            "api_key_set": api_key_set,
            "model_configured": model_configured,
            "configured": configured,
            **status,
        },
        "default_model": DEFAULT_TEXT_MODEL,
        "protocols": list(SUPPORTED_PROTOCOLS),
        "needs_config": not configured,
        "configured": configured,
        "configuration_status": "configured" if configured else "unconfigured",
        **status,
    }


@bp.get("/api/setup/llm")
def setup_llm_get():
    return jsonify(
        {
            "ok": True,
            "scope": "global",
            **_system_llm_payload(),
        }
    )


@bp.post("/api/setup/llm")
@bp.patch("/api/setup/llm")
def setup_llm_post():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400

    current = resolve_system_llm_config(require_model=False)
    editable = _editable_system_llm_fields(current)
    updates: dict[str, str] = {}

    if "api_key" in payload:
        api_key = str(payload.get("api_key") or "").strip()
        if api_key and not any(mark in api_key for mark in ("*", "•", "…")):
            updates["api_key"] = api_key

    if "model_name" in payload:
        model_name = str(payload.get("model_name") or "").strip()
        if not model_name:
            return jsonify({"ok": False, "error": "请填写模型名称"}), 400
        if model_name != editable["model_name"]:
            updates["model_name"] = model_name
    elif request.method == "POST" and editable["model_name"] != DEFAULT_TEXT_MODEL:
        # Legacy onboarding clients only sent api_key.  Keep their historical
        # default model while PATCH remains strictly partial.
        updates["model_name"] = DEFAULT_TEXT_MODEL

    if "protocol" in payload:
        protocol = str(payload.get("protocol") or "").strip().lower()
        if protocol not in SUPPORTED_PROTOCOLS:
            return jsonify({"ok": False, "error": f"不支持的协议: {protocol}"}), 400
        if protocol != editable["protocol"]:
            updates["protocol"] = protocol
    elif request.method == "POST" and editable["protocol"] != DEFAULT_TEXT_PROTOCOL:
        updates["protocol"] = DEFAULT_TEXT_PROTOCOL

    if "base_url" in payload:
        base_url = str(payload.get("base_url") or "").strip()
        if base_url != editable["base_url"]:
            updates["base_url"] = base_url

    touches_llm = request.method == "POST" or any(
        field in payload for field in ("api_key", "model_name", "protocol", "base_url")
    )
    supplied_key = str(updates.get("api_key") or "").strip()
    if touches_llm and not supplied_key and not _llm_api_key_set(current.api_key):
        return jsonify({"ok": False, "error": "请填写 LLM 对话 API Key"}), 400

    try:
        save_llm_env(updates)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    result = _system_llm_payload()
    if touches_llm and not result["config"]["api_key_set"]:
        return jsonify({"ok": False, "error": "API Key 未生效，请确认已填写 LLM_API_KEY"}), 400
    return jsonify({"ok": True, **result})


@bp.get("/api/setup/websearch")
def setup_websearch_get():
    """模型配置页「联网检索」：Key 来源（单独 / 复用大模型 / 无）与检索模型，不回传 Key。"""
    return jsonify({"ok": True, **websearch_config_status()})


@bp.patch("/api/setup/websearch")
def setup_websearch_patch():
    payload = request.get_json(silent=True) or {}
    try:
        status = save_websearch_env(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **status})


@bp.post("/api/setup/websearch/test")
def setup_websearch_test():
    """真的搜一次（占设置页测试配额），证明 Key 可用且走的是火山方舟。"""
    quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err
    payload = request.get_json(silent=True) or {}
    result = test_websearch(str(payload.get("query") or ""))
    if not result.get("ok"):
        code = 400 if "还没有可用" in str(result.get("error") or "") else 502
        return jsonify(result), code
    return jsonify({**result, "quota": quota})


@bp.post("/api/setup/llm/test")
def setup_llm_test():
    payload = request.get_json(silent=True) or {}
    current = resolve_system_llm_config(require_model=False)
    editable = _editable_system_llm_fields(current)
    model_name = str(
        payload.get("model_name") or editable["model_name"] or DEFAULT_TEXT_MODEL
    ).strip()
    protocol = (
        str(payload.get("protocol") or editable["protocol"] or DEFAULT_TEXT_PROTOCOL)
        .strip()
        .lower()
    )
    # 显式留空时交给协议默认解析；字段缺失时测试当前保存的 Base URL。
    base_url = str(
        payload.get("base_url") if "base_url" in payload else editable["base_url"]
    ).strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key or "*" in api_key or "•" in api_key:
        api_key = str(current.api_key or "").strip()
    prompt = str(payload.get("prompt") or "你好，请用一句话介绍你自己。").strip()

    if not model_name:
        return jsonify({"ok": False, "error": "请填写模型名称"}), 400
    if protocol not in SUPPORTED_PROTOCOLS:
        return jsonify({"ok": False, "error": f"不支持的协议: {protocol}"}), 400
    if not _llm_api_key_set(api_key):
        return jsonify({"ok": False, "error": "请填写 LLM 对话 API Key"}), 400

    quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err

    try:
        config = ResolvedLlmConfig(
            model=build_chat_model(protocol, model_name),
            api_key=api_key,
            api_base=base_url or None,
            protocol=protocol,
            source="test",
            display_name=model_name,
        )
        reply, meta = chat_completion(
            [{"role": "user", "content": prompt}],
            config=config,
            json_mode=False,
            temperature=0.7,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surface provider error to the user
        return jsonify({"ok": False, "error": str(exc)}), 502

    return jsonify(
        {
            "ok": True,
            "reply": reply,
            "meta": {"model": meta.get("model"), "display_name": meta.get("display_name")},
            "quota": quota,
        }
    )


def _list_provider_models(api_key: str, base_url: str | None = None) -> list[dict]:
    """用 OpenAI-compatible API Key 拉取模型清单。

    返回 [{id, name, status}]，默认访问 Xiaomi MiMo；显式 Base URL 仍支持
    其它兼容供应商。
    """
    import json as _json
    import urllib.request

    from deskbot_server.safe_fetch import safe_provider_urlopen

    url = (str(base_url or "").strip() or DEFAULT_TEXT_BASE_URL).rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers=build_provider_auth_headers(url, api_key),
    )
    with safe_provider_urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    # 只保留能做对话/多模态理解的模型，过滤 embedding / 语音 / 图像视频生成等非对话族
    non_chat = (
        "embedding",
        "-tts",
        "-asr",
        "seedream",
        "seedance",
        "seededit",
        "-voice",
        "music",
        "podcast",
    )
    out: list[dict] = []
    for m in data.get("data", []) or []:
        if m.get("status") == "Shutdown":
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or any(k in mid for k in non_chat):
            continue
        out.append({"id": mid, "name": m.get("name") or mid, "status": m.get("status") or "active"})
    return out


@bp.post("/api/setup/llm/models")
def setup_llm_models():
    payload = request.get_json(silent=True) or {}
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key or "*" in api_key or "•" in api_key:
        api_key = str(resolve_system_llm_config(require_model=False).api_key or "").strip()
    if not _llm_api_key_set(api_key):
        return jsonify({"ok": False, "error": "请先填写 LLM 对话 API Key"}), 400
    try:
        models = _list_provider_models(api_key, payload.get("base_url"))
    except Exception as exc:  # noqa: BLE001 - surface fetch error to the user
        return jsonify({"ok": False, "error": f"获取模型清单失败：{exc}"}), 502
    return jsonify({"ok": True, "models": models, "default_model": DEFAULT_TEXT_MODEL})


def _llm_api_key_set(api_key: str | None) -> bool:
    key = str(api_key or "").strip()
    return bool(key) and "请替换" not in key


def _llm_config_message(
    *,
    api_key_set: bool,
    model_configured: bool = True,
) -> str:
    if api_key_set and model_configured:
        return ""
    if api_key_set:
        return "需要完成大模型配置：API Key 已填写，但模型 ID 为空。"
    return "需要完成大模型配置：请填写模型 ID 与 LLM_API_KEY。"


@bp.get("/api/advanced")
def advanced_summary_get():
    current_device_id = get_current_device_id()
    system_default = resolve_system_llm_config(require_model=False)
    api_key_set = _llm_api_key_set(system_default.api_key)
    model_configured = bool(system_default.model.strip())
    configured = api_key_set and model_configured
    system_payload = {
        "display_name": system_default.display_name,
        "model": system_default.model,
        "api_base": system_default.api_base or "",
        "api_key_set": api_key_set,
        "model_configured": model_configured,
        "configured": configured,
        "needs_config": not configured,
        "configuration_status": "configured" if configured else "unconfigured",
    }
    return jsonify(
        {
            "ok": True,
            "current_device_id": current_device_id,
            "devices": [
                {
                    "device_id": device.device_id,
                    "display_name": device.display_name or device.device_id,
                    "is_current": device.device_id == current_device_id,
                }
                for device in list_devices()
            ],
            "llm": {
                "protocols": list(SUPPORTED_PROTOCOLS),
                "models": [],
                "active_model_id": None,
                "active": system_payload,
                "system_default": system_payload,
                "error": "",
                "needs_config": not configured,
                "config_message": _llm_config_message(
                    api_key_set=api_key_set,
                    model_configured=model_configured,
                ),
            },
        }
    )


def _image_mime_from_upload(filename: str, content_type: str, image_bytes: bytes) -> str:
    mime_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if mime_type.startswith("image/"):
        return mime_type
    guessed = mimetypes.guess_type(filename or "")[0] or ""
    guessed = guessed.split(";", 1)[0].strip().lower()
    if guessed.startswith("image/"):
        return guessed
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return mime_type


@bp.get("/api/face_expression_transaction")
def face_expression_transaction_get():
    """Load scenes, mappings and revision from one consistent snapshot."""

    try:
        state = get_face_expression_state()
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "scope": "local", **state})


@bp.post("/api/face_expression_transaction")
def face_expression_transaction_post():
    """Apply one optimistic, atomic expression-library mutation."""

    payload = request.get_json(silent=True)
    try:
        result = apply_face_expression_transaction(payload)
    except FaceDesignRevisionConflict as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "revision_conflict",
                    "expected_revision": exc.expected,
                    "revision": exc.actual,
                }
            ),
            409,
        )
    except FaceExpressionPermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "scope": "local", **result})


@bp.get("/api/face_expr_scenes")
def face_expr_scenes_get():
    try:
        rows = load_face_expr_scenes_file(seed_if_missing=True) or []
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "scope": "local", "config": rows})


@bp.post("/api/face_expr_scenes")
def face_expr_scenes_post():
    payload = request.get_json(silent=True) or {}
    scenes = payload.get("scenes")
    if scenes is None:
        scenes = payload.get("config")
    if not isinstance(scenes, list):
        return jsonify({"ok": False, "error": "scenes 必须是数组"}), 400
    try:
        saved = save_face_expr_scenes_file(scenes)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "scope": "local", "config": saved})


@bp.get("/api/face_mouth_by_phoneme")
def face_mouth_by_phoneme_get():
    try:
        groups = load_face_mouth_cfg_file(seed_if_missing=True) or []
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {"ok": True, "scope": "local", "mouth_by_phoneme_groups": groups}
    )


@bp.post("/api/face_mouth_by_phoneme")
def face_mouth_by_phoneme_post():
    payload = request.get_json(silent=True) or {}
    groups = payload.get("mouth_by_phoneme_groups")
    if not isinstance(groups, list):
        return jsonify({"ok": False, "error": "mouth_by_phoneme_groups 必须是数组"}), 400
    try:
        save_face_mouth_cfg_file(groups)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {"ok": True, "scope": "local", "mouth_by_phoneme_groups": groups}
    )


@bp.post("/api/scene_playbook/export_plan")
def scene_playbook_export_plan_post():
    payload = request.get_json(silent=True) or {}
    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        return jsonify({"ok": False, "error": "missing playbook"}), 400
    try:
        from deskbot_server.scene_playbook_runner import playbook_debug_snapshot
        from deskbot_server.scene_playbooks_store import normalize_playbook

        pb = normalize_playbook(playbook)
        snap = playbook_debug_snapshot(pb)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "scope": "local", **snap})


@bp.post("/api/face_design/generate-from-text")
def face_design_generate_from_text_post():
    """文生表情：与图生共用 Ark 流水线，产物同为可保存的表情 scene。"""
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "请先输入一句描述"}), 400
    quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err
    try:
        from deskbot_server.ark_face_svg import generate_face_svg_from_text

        result = generate_face_svg_from_text(prompt)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify({"scope": "local", "quota": quota, **result})


@bp.post("/api/face_design/generate-from-image")
def face_design_generate_from_image_post():
    from deskbot_server.ark_face_svg import (
        ARK_IMAGE_PROCESSING_NOTICE,
        MAX_IMAGE_BYTES,
        MAX_IMAGE_UPLOAD_REQUEST_BYTES,
        validate_image_upload,
    )

    # Enforce the multipart request limit before Werkzeug parses/spools the
    # complete upload. The small allowance covers boundaries and prompt fields.
    request.max_content_length = MAX_IMAGE_UPLOAD_REQUEST_BYTES
    if (
        request.content_length is not None
        and request.content_length > MAX_IMAGE_UPLOAD_REQUEST_BYTES
    ):
        return jsonify({"ok": False, "error": "上传请求过大，图片不能超过 6MB"}), 413
    try:
        upload = request.files.get("image") or request.files.get("file")
    except RequestEntityTooLarge:
        return jsonify({"ok": False, "error": "上传请求过大，图片不能超过 6MB"}), 413
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "请先上传图片"}), 400
    # Never perform an unbounded ``read()`` even when Content-Length is absent
    # or supplied by an untrusted client.
    image_bytes = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if not image_bytes:
        return jsonify({"ok": False, "error": "上传图片为空"}), 400
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return jsonify({"ok": False, "error": "图片不能超过 6MB"}), 413
    prompt = str(request.form.get("prompt") or "").strip()
    declared_mime = _image_mime_from_upload(
        upload.filename, upload.mimetype or upload.content_type or "", image_bytes
    )
    try:
        mime_type = validate_image_upload(image_bytes, declared_mime)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err
    try:
        from deskbot_server.ark_face_svg import generate_face_svg_from_image

        result = generate_face_svg_from_image(image_bytes, mime_type, prompt=prompt)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    result.setdefault(
        "external_processor",
        {
            "provider": "火山引擎 Ark",
            "purpose": "图片转机器人表情",
            "notice": ARK_IMAGE_PROCESSING_NOTICE,
        },
    )
    return jsonify({"scope": "local", "quota": quota, **result})
