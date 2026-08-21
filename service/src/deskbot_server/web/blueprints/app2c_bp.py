from __future__ import annotations

import mimetypes
import os

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

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
from deskbot_server.llm.config_state import (
    get_llm_config_status,
    read_llm_env_values,
)
from deskbot_server.llm.env_store import read_llm_env, save_llm_env
from deskbot_server.llm.runtime import (
    DEFAULT_LLM_MODEL,
    MIMO_OPENAI_BASE_URL,
    ResolvedLlmConfig,
    build_chat_model,
    build_provider_auth_headers,
    chat_completion,
    resolve_system_llm_config,
)
from deskbot_server.llm_config_store import (
    SUPPORTED_PROTOCOLS,
)
from deskbot_server.web.blueprints.app_bp import (
    _consume_settings_test_quota,
)
from deskbot_server.web.helpers import camera_view_ws_base, device_pipeline_ws_base
from deskbot_server.web.session_device import get_current_device_id

# No url_prefix: consumer pages live directly at the site root.
bp = Blueprint("app2c", __name__)


def _default_robot_face_payload() -> dict:
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.shapes import _default_mouth_fallback_shape, default_face_circles

    face = default_face_circles()
    return {
        "face_lcd_w": FACE_LCD_WIDTH,
        "face_lcd_h": FACE_LCD_HEIGHT,
        "expr_default_anim": {
            "elements": {
                "nose": face.get("nose") or [],
                "eye_l": face.get("eye_l") or [],
                "eye_r": face.get("eye_r") or [],
                "mouth": (_default_mouth_fallback_shape().get("elements") or []),
                "extra": [],
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
        camera_view_ws_base=camera_view_ws_base(),
        device_pipeline_ws_base=device_pipeline_ws_base(),
        **_default_robot_face_payload(),
    )


@bp.get("/memories")
def memories():
    return render_template("app2c/memories.html", active_nav="memory")


@bp.get("/reminders")
def reminders():
    return render_template("app2c/reminders.html", active_nav="remind")


@bp.get("/sessions")
def sessions():
    return render_template("app2c/sessions.html", active_nav="sessions")


@bp.get("/preferences")
def preferences():
    return render_template("app2c/preferences.html", active_nav="preferences")


@bp.get("/people")
def people():
    return render_template("app2c/people.html", active_nav="people")


@bp.get("/devices")
def devices():
    return render_template("app2c/devices.html", active_nav="device")


@bp.get("/miot")
def miot():
    return render_template("app2c/miot.html", active_nav="miot")


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


# Text chat defaults to Xiaomi MiMo.  Image generation keeps its independent
# Ark configuration and credential path.
DEFAULT_TEXT_MODEL = DEFAULT_LLM_MODEL
DEFAULT_TEXT_PROTOCOL = "openai"
DEFAULT_TEXT_BASE_URL = MIMO_OPENAI_BASE_URL


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


def _ark_api_key_set() -> bool:
    """图片表情独立的 ARK_API_KEY 是否已配置（.env 或进程环境）。"""
    value = str(
        read_llm_env().get("ARK_API_KEY") or os.environ.get("ARK_API_KEY") or ""
    ).strip()
    return _llm_api_key_set(value)


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
            "ark_api_key_set": _ark_api_key_set(),
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

    # 图片表情独立凭证：留空 = 保留现值，与 LLM Key 输入语义一致，不回显。
    if "ark_api_key" in payload:
        ark_api_key = str(payload.get("ark_api_key") or "").strip()
        if ark_api_key and not any(mark in ark_api_key for mark in ("*", "•", "…")):
            updates["ark_api_key"] = ark_api_key

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

    # 仅保存独立 ARK_API_KEY 时不要求 LLM Key 已配置。
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
