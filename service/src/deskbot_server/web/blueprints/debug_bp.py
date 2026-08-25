from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import secrets
import time

from flask import Blueprint, jsonify, render_template, request

from deskbot_server.application.face_registration import register_face_for_device
from deskbot_server.auth.debug_ws_token import issue_debug_ws_token
from deskbot_server.camera_face_config_store import (
    load_camera_face_cfg_file,
    normalize_camera_face_document,
    save_camera_face_cfg_file,
)
from deskbot_server.camera_face_tune import apply_camera_face_tune
from deskbot_server.constants import (
    CAMERA_FACE_CFG_FILE,
    FACE_PROFILES_FILE,
    SCENE_PLAYBOOKS_FILE,
)
from deskbot_server.device_data import (
    load_llm_system_prompt,
    resolve_json_path,
    save_llm_system_prompt,
)
from deskbot_server.device_preferences import preferred_time_prompt
from deskbot_server.face_expr_scenes_store import load_face_expr_scenes_file
from deskbot_server.face_profiles_store import load_face_profiles
from deskbot_server.hardware_catalog import list_devices
from deskbot_server.llm.utils import llm_pb_plan_prompt_appendix, parse_llm_reply
from deskbot_server.scene_playbooks_store import (
    collect_missing_servo_presets,
    load_scene_playbooks_file,
    normalize_playbook,
    normalize_scene_playbooks,
    save_scene_playbooks_file,
)
from deskbot_server.util import pcm_to_wav_bytes
from deskbot_server.web.helpers import (
    ALLOWED_LLM_ROLES,
    camera_view_ws_base,
    deskbot_http_base,
    device_pipeline_ws_base,
    load_config,
    tcp_alive,
)
from deskbot_server.web.session_device import get_current_device_id

bp = Blueprint("debug", __name__)
logger = logging.getLogger("deskbot-server")


def _consume_consumer_test_quota():
    """Share the PC-local cloud test budget with the settings APIs."""
    from deskbot_server.web.blueprints.app_bp import _consume_settings_test_quota

    return _consume_settings_test_quota()


def _page_pinned_device_id(devices) -> str | None:
    """Choose a physical device target for the debug page."""

    device_ids = {row.device_id for row in devices}
    if "device_id" in request.args:
        requested = str(request.args.get("device_id") or "").strip()
        return requested if requested in device_ids else None
    current = str(get_current_device_id() or "").strip()
    if current in device_ids:
        return current
    return devices[0].device_id if current and devices else None


@bp.get("/health")
def health_ok():
    return ("ok", 200)


@bp.get("/api/debug/ws_token")
def api_debug_ws_token():
    """为本机控制台签发短期调试 WebSocket bearer 令牌。"""
    info = issue_debug_ws_token()
    return jsonify(
        {
            "ok": True,
            "token": info.token,
            "expires_in": info.expires_in,
        }
    )


@bp.get("/debug/devices")
def debug_devices():
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.shapes import _default_mouth_fallback_shape, default_face_circles

    fc = default_face_circles()
    mouth_fb = _default_mouth_fallback_shape().get("elements") or []
    expr_default_anim = {
        "elements": {
            "nose": fc.get("nose") or [],
            "eye_l": fc.get("eye_l") or [],
            "eye_r": fc.get("eye_r") or [],
            "mouth": mouth_fb,
            "extra": [],
        },
    }
    devices = list_devices()
    device_rows = [
        {"device_id": d.device_id, "display_name": d.display_name or d.device_id}
        for d in devices
    ]
    current = _page_pinned_device_id(devices)

    return render_template(
        "debug_devices.html",
        active_nav="devices",
        device_pipeline_ws_base=device_pipeline_ws_base(),
        camera_view_ws_base=camera_view_ws_base(),
        deskbot_http_base=deskbot_http_base(),
        face_lcd_w=FACE_LCD_WIDTH,
        face_lcd_h=FACE_LCD_HEIGHT,
        expr_default_anim=expr_default_anim,
        current_device_id=current or "",
        device_rows=device_rows,
    )


@bp.get("/debug/tts")
def debug_tts():
    from deskbot_server.tts.doubao import load_doubao_tts_config
    from deskbot_server.tts.speakers import list_doubao_tts_speaker_presets

    cfg = load_doubao_tts_config()
    initial = cfg.masked()
    return render_template(
        "debug_tts.html",
        active_nav="tts",
        initial_config=initial,
        speaker_presets=list_doubao_tts_speaker_presets(),
    )


@bp.get("/api/doubao_tts/speakers")
def api_doubao_tts_speakers():
    from deskbot_server.tts.speakers import (
        list_doubao_tts_consumer_speaker_presets,
        list_doubao_tts_speaker_presets,
    )

    if request.args.get("scope") == "consumer":
        speakers = list_doubao_tts_consumer_speaker_presets()
    else:
        speakers = list_doubao_tts_speaker_presets()
    return jsonify({"ok": True, "speakers": speakers, "t": time.time()})


@bp.get("/api/doubao_tts/config")
def api_doubao_tts_config_get():
    from deskbot_server.tts.doubao import load_doubao_tts_config

    cfg = load_doubao_tts_config()
    return jsonify(
        {
            "ok": True,
            "scope": "global",
            "config": cfg.masked(),
            "t": time.time(),
        }
    )


@bp.post("/api/doubao_tts/config")
def api_doubao_tts_config_post():
    from deskbot_server.tts.doubao import load_doubao_tts_config, resolve_optional_secret
    from deskbot_server.tts.env_store import save_doubao_tts_env

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400

    base = load_doubao_tts_config()
    api_key = resolve_optional_secret(payload.get("api_key"), base.api_key)
    if not api_key:
        return jsonify({"ok": False, "error": "DOUBAO_TTS_API_KEY 不能为空"}), 400
    try:
        save_doubao_tts_env(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    cfg = load_doubao_tts_config()
    return jsonify({"ok": True, "config": cfg.masked(), "t": time.time()})


def _doubao_cfg_from_payload(payload: dict):
    from deskbot_server.tts.doubao import (
        DoubaoTtsConfig,
        load_doubao_tts_config,
        resolve_optional_secret,
    )

    base = load_doubao_tts_config()
    api_key = resolve_optional_secret(payload.get("api_key"), base.api_key)
    sample_rate_raw = payload.get("sample_rate", base.sample_rate)
    try:
        sample_rate = int(sample_rate_raw)
    except (TypeError, ValueError):
        sample_rate = base.sample_rate
    return DoubaoTtsConfig(
        api_key=api_key,
        speaker=str(payload.get("speaker") or base.speaker).strip(),
        resource_id=str(payload.get("resource_id") or base.resource_id).strip(),
        model=str(payload.get("model") or base.model).strip(),
        # Provider endpoints are platform configuration, never per-request
        # input.  This prevents an authenticated browser from turning the
        # synthesis API into a credential-bearing WebSocket proxy.
        ws_url=base.ws_url,
        sample_rate=sample_rate,
        audio_format=str(payload.get("audio_format") or base.audio_format).strip(),
        enable_timestamp=base.enable_timestamp,
    )


def _doubao_voice_clone_cfg_from_payload(payload: dict):
    from deskbot_server.tts.doubao import load_doubao_tts_config, resolve_optional_secret
    from deskbot_server.tts.voice_clone import (
        DEFAULT_VOICE_CLONE_RESOURCE_ID,
        DoubaoVoiceCloneConfig,
    )

    base = load_doubao_tts_config()
    api_key = resolve_optional_secret(payload.get("api_key"), base.api_key)
    app_key = resolve_optional_secret(payload.get("app_id"), base.app_id)
    access_key = resolve_optional_secret(payload.get("access_token"), base.access_token)
    resource_id = str(
        payload.get("resource_id")
        or payload.get("voice_clone_resource_id")
        or base.voice_clone_resource_id
        or DEFAULT_VOICE_CLONE_RESOURCE_ID
    ).strip()
    clone_url = base.voice_clone_url
    status_url = base.voice_status_url
    return DoubaoVoiceCloneConfig(
        api_key=api_key,
        app_key=app_key,
        access_key=access_key,
        resource_id=resource_id,
        clone_url=clone_url,
        status_url=status_url,
    )


def _audio_format_from_upload(filename: str, content_type: str) -> str:
    guessed = (mimetypes.guess_type(filename or "")[0] or content_type or "").lower()
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").strip().lower()
    if ext in {"wav", "mp3", "ogg", "m4a", "aac", "pcm", "flac", "opus"}:
        return "ogg" if ext == "opus" else ext
    if "wav" in guessed:
        return "wav"
    if "mpeg" in guessed or "mp3" in guessed:
        return "mp3"
    if "ogg" in guessed or "opus" in guessed:
        return "ogg"
    if "mp4" in guessed or "m4a" in guessed:
        return "m4a"
    if "aac" in guessed:
        return "aac"
    if "flac" in guessed:
        return "flac"
    return ext


def _voice_clone_payload(result) -> dict:
    payload = result.as_payload()
    return {"ok": True, **payload, "t": time.time()}


@bp.post("/api/doubao_tts/synthesize")
def api_doubao_tts_synthesize():
    from deskbot_server.tts.doubao import synthesize_doubao_tts

    payload = request.get_json(force=True, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "空文本"}), 400
    cfg = _doubao_cfg_from_payload(payload)
    if not cfg.api_key:
        return jsonify({"ok": False, "error": "豆包 TTS 未配置 DOUBAO_TTS_API_KEY"}), 400
    if not cfg.speaker:
        return jsonify({"ok": False, "error": "未设置 speaker（音色）"}), 400
    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err
    t0 = time.monotonic()
    try:
        result = asyncio.run(synthesize_doubao_tts(text, cfg))
    except ValueError as exc:
        logger.warning("豆包 TTS 参数错误: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.exception(
            "豆包 TTS 合成失败 text_len=%d speaker=%r resource_id=%s elapsed_ms=%d",
            len(text),
            cfg.speaker,
            cfg.resource_id,
            elapsed_ms,
        )
        return jsonify({"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms}), 502
    wav = pcm_to_wav_bytes(result.pcm, result.sample_rate)
    return jsonify(
        {
            "ok": True,
            "sample_rate": result.sample_rate,
            "pcm_total_bytes": len(result.pcm),
            "elapsed_ms": result.elapsed_ms,
            "events": result.events,
            "speaker": cfg.speaker,
            "ws_url": cfg.ws_url,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "quota": quota,
        }
    )


@bp.post("/api/doubao_tts/voice-clone")
def api_doubao_tts_voice_clone():
    from deskbot_server.tts.voice_clone import clone_doubao_voice, custom_speaker_id_from_name
    from deskbot_server.tts.voice_clone_jobs import (
        create_voice_clone_job,
        mark_voice_clone_job_failed,
        update_voice_clone_job_result,
        voice_clone_job_payload,
    )

    upload = request.files.get("audio") or request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "请先上传训练音频"}), 400
    audio_bytes = upload.read()
    if not audio_bytes:
        return jsonify({"ok": False, "error": "训练音频为空"}), 400
    if len(audio_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "训练音频不能超过 10MB"}), 400

    form = request.form
    voice_name = str(form.get("voice_name") or form.get("display_name") or "").strip()
    if not voice_name:
        return jsonify({"ok": False, "error": "请填写音色名称"}), 400
    local_prefix = "local-" + secrets.token_hex(4)
    # Never accept a provider speaker id chosen by the browser.  The opaque
    # random suffix prevents a request from guessing an existing identifier.
    custom_speaker_id = custom_speaker_id_from_name(
        voice_name,
        namespace=local_prefix + secrets.token_hex(2),
    )
    audio_format = str(form.get("audio_format") or "").strip().lower() or _audio_format_from_upload(
        upload.filename or "",
        upload.content_type or "",
    )
    if not audio_format:
        return jsonify({"ok": False, "error": "无法识别音频格式，请上传 wav/mp3/ogg/m4a/aac/pcm"}), 400
    try:
        language = int(form.get("language") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "language 必须是数字枚举"}), 400
    cfg = _doubao_voice_clone_cfg_from_payload(dict(form))
    try:
        cfg.headers()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err
    job = create_voice_clone_job(
        speaker_id=custom_speaker_id,
        display_name=voice_name,
    )
    try:
        result = clone_doubao_voice(
            cfg,
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            language=language,
            display_name=voice_name,
            custom_speaker_id=custom_speaker_id,
            prompt_text=str(form.get("prompt_text") or "").strip(),
        )
    except ValueError as exc:
        mark_voice_clone_job_failed(job.id, exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surface provider error to the user
        mark_voice_clone_job_failed(job.id, exc)
        logger.exception("火山声音复刻训练提交失败 custom_speaker_id=%r", custom_speaker_id)
        return jsonify({"ok": False, "error": str(exc)}), 502
    job = update_voice_clone_job_result(job.id, result)
    response = _voice_clone_payload(result)
    response.update(voice_clone_job_payload(job))
    response["quota"] = quota
    return jsonify(response)


@bp.post("/api/doubao_tts/voice-clone/status")
def api_doubao_tts_voice_clone_status():
    from deskbot_server.tts.voice_clone import get_doubao_voice_clone_status
    from deskbot_server.tts.voice_clone_jobs import (
        claim_voice_clone_status_poll,
        get_voice_clone_job,
        update_voice_clone_job_result,
        voice_clone_job_payload,
    )

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id required"}), 400
    job = get_voice_clone_job(job_id)

    if job is None:
        return jsonify({"ok": False, "error": "voice clone job not found"}), 404
    if job.ready or job.state == "failed":
        response = voice_clone_job_payload(job)
        response.update({"ok": True, "cached": True})
        return jsonify(response)
    retry_after = claim_voice_clone_status_poll(job_id=job.id)
    if retry_after:
        response = jsonify(
            {
                "ok": False,
                "error": f"status polling too frequent; retry in {retry_after}s",
                "retry_after": retry_after,
                "job": voice_clone_job_payload(job),
            }
        )
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err
    speaker_id = job.speaker_id
    cfg = _doubao_voice_clone_cfg_from_payload(payload)
    try:
        result = get_doubao_voice_clone_status(cfg, speaker_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surface provider error to the user
        logger.exception("火山声音复刻状态查询失败 speaker=%r", speaker_id)
        return jsonify({"ok": False, "error": str(exc)}), 502
    job = update_voice_clone_job_result(job.id, result)
    response = _voice_clone_payload(result)
    response.update(voice_clone_job_payload(job))
    response["quota"] = quota
    return jsonify(response)


@bp.get("/api/doubao_tts/voice-clone/jobs")
def api_doubao_tts_voice_clone_jobs():
    from deskbot_server.tts.voice_clone import get_doubao_voice_clone_status
    from deskbot_server.tts.voice_clone_jobs import (
        claim_voice_clone_status_poll,
        list_voice_clone_jobs,
        update_voice_clone_job_result,
        voice_clone_job_payload,
    )

    rows = list_voice_clone_jobs()

    # 行里存的是上一次查询的结果，训练是异步完成的，没人再查它就永远停在旧
    # 状态上——已经训练好的音色因此显示为「未知」且试听/设为当前一直不可点。
    # 这里对未就绪的行补一次实时查询，频控沿用 status 接口那套 claim。
    cfg = None
    for index, row in enumerate(rows):
        if row.ready or row.state == "failed" or not row.speaker_id:
            continue
        if claim_voice_clone_status_poll(job_id=row.id):
            continue
        if cfg is None:
            cfg = _doubao_voice_clone_cfg_from_payload({})
        try:
            result = get_doubao_voice_clone_status(cfg, row.speaker_id)
        except Exception:  # noqa: BLE001 - 单行刷新失败不能拖垮整个列表
            logger.exception("刷新声音复刻状态失败 speaker=%r", row.speaker_id)
            continue
        refreshed = update_voice_clone_job_result(row.id, result)
        if refreshed is not None:
            rows[index] = refreshed

    return jsonify(
        {
            "ok": True,
            "jobs": [voice_clone_job_payload(row) for row in rows],
        }
    )


@bp.get("/debug/llm")
def debug_llm():
    from deskbot_server.llm.utils import llm_pb_plan_prompt_appendix

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    devices = list_devices()
    device_rows = [
        {"device_id": d.device_id, "display_name": d.display_name or d.device_id}
        for d in devices
    ]
    current = _page_pinned_device_id(devices)
    initial_prompt = load_llm_system_prompt()

    return render_template(
        "debug_llm.html",
        active_nav="llm",
        llm_model=llm_cfg.get("model_name", ""),
        llm_base_url=llm_cfg.get("base_url", ""),
        llm_system_prompt=initial_prompt,
        llm_plan_appendix=llm_pb_plan_prompt_appendix(),
        current_device_id=current or "",
        device_rows=device_rows,
    )


@bp.get("/debug/simulation")
def debug_simulation():
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.shapes import default_face_circles

    cfg = load_config()
    tts = cfg.get("tts") or {}
    return render_template(
        "debug_simulation.html",
        active_nav="sim",
        default_spk=int(tts.get("spk_id", 0)),
        sample_rate=int(tts.get("sample_rate", 16000)),
        face_lcd_w=FACE_LCD_WIDTH,
        face_lcd_h=FACE_LCD_HEIGHT,
        default_face_circles=default_face_circles(),
    )


@bp.post("/api/tts/phoneme_tts")
def api_tts_phoneme_tts():
    """豆包 TTS + 音素分片，供仿真调试等页面使用。"""
    from deskbot_server.core.settings import AppSettings
    from deskbot_server.infrastructure.tts.factory import build_tts_adapter

    payload = request.get_json(force=True, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "空文本"}), 400
    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err
    cfg = load_config()
    sr_cfg = int((cfg.get("tts") or {}).get("sample_rate", 16000))
    try:
        settings = AppSettings.from_config(cfg)
        adapter = build_tts_adapter(settings)
        sr, segs = asyncio.run(adapter.synthesize_phoneme_segments(text))
    except Exception as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
    pcm = bytearray()
    display: list[dict] = []
    for seg in segs:
        chunk = bytes(seg.pcm or b"")
        pcm.extend(chunk)
        display.append(
            {
                "phoneme_id": seg.phoneme_id,
                "phoneme": seg.phoneme,
                "ms": seg.ms,
                "pcm_bytes": len(chunk),
            }
        )
    sr = int(sr or sr_cfg or 16000)
    wav = pcm_to_wav_bytes(bytes(pcm), sr)
    return jsonify(
        {
            "ok": True,
            "provider": "doubao",
            "sample_rate": sr,
            "segments": display,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "pcm_total_bytes": len(pcm),
            "quota": quota,
        }
    )


@bp.get("/api/llm/system_prompt")
def api_llm_system_prompt_get():
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "system_prompt": load_llm_system_prompt(),
            "file": "llm_system.txt",
            "t": time.time(),
        }
    )


@bp.post("/api/llm/system_prompt")
def api_llm_system_prompt_post():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    content = str(payload.get("system_prompt") or payload.get("content") or "")
    try:
        path = save_llm_system_prompt(content)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "system_prompt": load_llm_system_prompt(),
            "file": os.path.basename(path),
            "t": time.time(),
        }
    )


@bp.post("/api/llm/chat")
def llm_chat():
    payload = request.get_json(force=True, silent=True) or {}
    user_text = str(payload.get("text") or "").strip()
    raw_history = payload.get("history") or []

    if not user_text:
        return jsonify({"ok": False, "error": "空文本"}), 400

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    debug_device_id = str(payload.get("device_id") or "").strip()

    default_system_prompt = load_llm_system_prompt() or llm_cfg.get(
        "system_prompt", "你是中文助手，请简洁回答。每次回答不超过50字"
    )
    raw_sys = payload.get("system_prompt")
    if isinstance(raw_sys, str) and raw_sys.strip():
        system_prompt = raw_sys
    else:
        system_prompt = default_system_prompt

    from deskbot_server.llm.runtime import resolve_llm_config

    try:
        llm_runtime_cfg = resolve_llm_config()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not llm_runtime_cfg.api_key or "请替换" in llm_runtime_cfg.api_key:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "LLM API Key 未配置（设备 LLM 管理或环境变量 ARK_API_KEY / VOLCENGINE_API_KEY / DOUBAO_API_KEY / LLM_API_KEY）",
                }
            ),
            400,
        )

    sys_content = f"{system_prompt}\n当前时间是: {preferred_time_prompt()}"
    from deskbot_server.llm.user_message import build_llm_user_message
    from deskbot_server.llm.utils import (
        llm_device_screen_appendix,
        llm_static_context_prompt_appendix,
    )

    sys_content += "\n" + llm_device_screen_appendix()
    px = llm_pb_plan_prompt_appendix()
    if px:
        sys_content += "\n" + px
    fx = llm_static_context_prompt_appendix()
    if fx:
        sys_content += "\n\n" + fx

    raw_ctx = payload.get("device_context")
    device_context: str | None = None
    if isinstance(raw_ctx, dict):
        device_context = json.dumps(raw_ctx, ensure_ascii=False)
    elif isinstance(raw_ctx, str) and raw_ctx.strip():
        device_context = raw_ctx.strip()

    messages = [
        {
            "role": "system",
            "content": sys_content,
        }
    ]
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "")
        if role not in ALLOWED_LLM_ROLES or not content:
            continue
        if role == "system":
            continue
        messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": build_llm_user_message(
                user_text,
                device_id=debug_device_id or None,
                device_context=device_context,
            ),
        }
    )

    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err

    try:
        from deskbot_server.llm.runtime import chat_completion

        t0 = time.monotonic()
        raw, meta = chat_completion(
            messages,
            device_id=debug_device_id or None,
            temperature=float(payload.get("temperature", 0.7)),
            config=llm_runtime_cfg,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        parsed = parse_llm_reply(raw)
        return jsonify(
            {
                "ok": True,
                "reply": parsed["reply"],
                "raw": parsed["raw"],
                "moves": parsed.get("moves") or [],
                "anims": parsed.get("anims") or [],
                "tools": parsed.get("tools") or [],
                "scenes": parsed.get("scenes") or [],
                "json_ok": parsed["json_ok"],
                "need_reply": parsed.get("need_reply", True),
                "model": meta.get("model"),
                "model_source": meta.get("source"),
                "model_display_name": meta.get("display_name"),
                "elapsed_ms": elapsed_ms,
                "usage": meta.get("usage"),
                "quota": quota,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


def _json_object_from_llm(raw: str) -> dict:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM did not return a JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON must be an object")
    return parsed


def _normalize_generated_design(raw: dict) -> dict:
    def _items(value: object) -> list[dict]:
        if not isinstance(value, list):
            return []
        out: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            frames = item.get("frames")
            if not isinstance(frames, list):
                frames = []
            clean_frames: list[dict] = []
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                elements = frame.get("elements")
                if not isinstance(elements, dict):
                    continue
                try:
                    ms = int(frame.get("ms") or 320)
                except (TypeError, ValueError):
                    ms = 320
                clean_frames.append({"ms": max(40, min(ms, 5000)), "elements": elements})
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name or not clean_frames:
                continue
            alias = item.get("alias") if isinstance(item.get("alias"), list) else []
            out.append(
                {
                    "name": name[:64],
                    "title": str(item.get("title") or name).strip()[:80],
                    "alias": [str(v).strip() for v in alias if str(v).strip()],
                    "frames": clean_frames,
                }
            )
        return out

    design = {
        "name": str(raw.get("name") or raw.get("title") or "ai-face-design").strip()[:80],
        "description": str(raw.get("description") or "").strip()[:240],
        "phonemes": _items(raw.get("phonemes")),
        "emotions": _items(raw.get("emotions") if isinstance(raw.get("emotions"), list) else raw.get("scenes")),
    }
    if not design["phonemes"] and not design["emotions"]:
        raise ValueError("generated design has no usable phonemes or emotions")
    return design


@bp.post("/api/face_design/generate")
def api_face_design_generate():
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "请先输入想要的表情风格", "t": time.time()}), 400
    try:
        temperature = float(payload.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    temperature = max(0.1, min(temperature, 1.2))

    system_prompt = (
        "你是 Deskbot 小歪的 VisemeSync JSON 表情设计助手。"
        "只输出一个 JSON object，不要 Markdown，不要解释。"
        "JSON schema: {name, description, phonemes, emotions}。"
        "phonemes/emotions 的每一项必须包含 name, title, alias[], frames[]；"
        "frames[] 每项包含 ms 和 elements；elements 可以包含 mouth/eye_l/eye_r/nose/extra 数组。"
        "坐标范围基于 284x240 OLED，嘴部大致在 x=90..194, y=135..180。"
        "至少生成 5 个 emotions 和 8 个常用中文拼音/音素 phonemes。"
    )
    user_prompt = (
        "根据这段描述生成 VisemeSync JSON：\n"
        f"{prompt}\n\n"
        "优先使用这些 shape: ellipse_fill, circle_fill, line, round_rect_outline, round_rect_fill。"
        "请让表情适合 2C 用户配置，名字简短稳定，alias 可以包含中文拼音或英文 mood。"
    )

    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err

    try:
        from deskbot_server.llm.runtime import chat_completion

        t0 = time.monotonic()
        raw, meta = chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            device_id=None,
            temperature=temperature,
            json_mode=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        design = _normalize_generated_design(_json_object_from_llm(raw))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except json.JSONDecodeError as exc:
        return jsonify({"ok": False, "error": f"LLM JSON 解析失败: {exc}", "t": time.time()}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}", "t": time.time()}), 500

    return jsonify(
        {
            "ok": True,
            "design": design,
            "raw": raw,
            "model": meta.get("model"),
            "model_source": meta.get("source"),
            "model_display_name": meta.get("display_name"),
            "usage": meta.get("usage"),
            "elapsed_ms": elapsed_ms,
            "scope": "local",
            "quota": quota,
            "t": time.time(),
        }
    )


@bp.get("/api/camera_face_config")
def api_camera_face_config_get():
    cfg_path = resolve_json_path(CAMERA_FACE_CFG_FILE)
    cfg = load_config()
    base = dict(cfg.get("camera_face") or {})
    try:
        file_cfg = load_camera_face_cfg_file()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    merged = {**base, **(file_cfg or {})}
    try:
        norm = normalize_camera_face_document(merged)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": norm,
            "file": os.path.basename(cfg_path),
            "exists": file_cfg is not None,
            "scope": "local",
            "t": time.time(),
        }
    )


@bp.post("/api/camera_face_config")
def api_camera_face_config_post():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    cfg_path = resolve_json_path(CAMERA_FACE_CFG_FILE)
    try:
        cfg = normalize_camera_face_document(payload)
        save_camera_face_cfg_file(cfg)
        apply_camera_face_tune(cfg)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": cfg,
            "file": os.path.basename(cfg_path),
            "scope": "local",
            "hint": "检测器 num_faces/置信度需 ESP32 重连 /asr_chat 后生效",
            "t": time.time(),
        }
    )


@bp.get("/api/face_profiles")
def api_face_profiles_get():
    cfg_path = resolve_json_path(FACE_PROFILES_FILE)
    try:
        profiles = load_face_profiles()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "profiles": profiles,
            "file": os.path.basename(cfg_path),
            "scope": "local",
            "t": time.time(),
        }
    )


@bp.post("/api/face_profiles/register")
def api_face_profiles_register():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    name = str(payload.get("name") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    face_id_raw = payload.get("face_id")
    if not name:
        return jsonify({"ok": False, "error": "name required", "t": time.time()}), 400
    if not device_id or face_id_raw is None:
        return jsonify({"ok": False, "error": "device_id and face_id required", "t": time.time()}), 400
    try:
        face_id = int(face_id_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "face_id must be int", "t": time.time()}), 400
    try:
        out = register_face_for_device(device_id, name, face_id=face_id, extra=payload)
        profile = out["profile"]
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    return jsonify(
        {
            "ok": True,
            "profile": profile,
            "file": os.path.basename(resolve_json_path(FACE_PROFILES_FILE)),
            "device_id": device_id,
            "hint": (
                "档案已写入（InsightFace 512 维）；请 ESP32 重连 /asr_chat 后正对镜头识别。"
                "旧几何档案需重新「保存人名」"
            ),
            "t": time.time(),
        }
    )


@bp.get("/api/face_expr_scenes")
def api_face_expr_scenes_get():
    try:
        rows = load_face_expr_scenes_file(seed_if_missing=True) or []
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "scenes": rows,
            "scope": "local",
            "t": time.time(),
        }
    )


@bp.get("/api/scene_playbooks")
def api_scene_playbooks_get():
    cfg_path = resolve_json_path(SCENE_PLAYBOOKS_FILE)
    try:
        rows = load_scene_playbooks_file(seed_if_missing=True)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": rows or [],
            "exists": os.path.isfile(cfg_path),
            "file": os.path.basename(cfg_path),
            "scope": "local",
            "t": time.time(),
        }
    )


@bp.post("/api/scene_playbooks")
def api_scene_playbooks_post():
    payload = request.get_json(silent=True)
    cfg_path = resolve_json_path(SCENE_PLAYBOOKS_FILE)
    try:
        rows = normalize_scene_playbooks(payload if payload is not None else [])
        save_scene_playbooks_file(rows)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    missing = collect_missing_servo_presets(rows)
    out = {
        "ok": True,
        "config": rows,
        "file": os.path.basename(cfg_path),
        "scope": "local",
        "t": time.time(),
    }
    if missing:
        out["missing_servo_presets"] = missing
        out["warning"] = (
            "部分 pb 包引用的舵机 preset 未写入 servo.json，设备下发时会跳过："
            + ", ".join(missing)
        )
    return jsonify(out)


@bp.post("/api/scene_playbook/export_plan")
def api_scene_playbook_export_plan():
    """导出单条编排 + 展开后的 LLM 计划（供排查）。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload.get("playbook"):
        return jsonify({"ok": False, "error": "missing playbook", "t": time.time()}), 400
    try:
        pb = normalize_playbook(payload.get("playbook"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    from deskbot_server.scene_playbook_runner import playbook_debug_snapshot

    snap = playbook_debug_snapshot(pb)
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            **snap,
            "t": time.time(),
        }
    )


@bp.get("/api/health")
def health():
    cfg = load_config()
    deskbot_host = os.environ.get("DESKBOT_SERVER_HOST") or cfg.get("server", {}).get("host", "127.0.0.1")
    if deskbot_host == "0.0.0.0":
        deskbot_host = "127.0.0.1"
    deskbot_port = int(os.environ.get("DESKBOT_SERVER_PORT") or cfg.get("server", {}).get("port", 9000))

    from deskbot_server.tts.doubao import (
        PROTOCOL_ID,
        PROVIDER_ID,
        PROVIDER_VERSION,
        load_doubao_tts_config,
    )

    tts_cfg = load_doubao_tts_config()
    tts_provider = PROVIDER_ID
    tts_configured = bool(str(tts_cfg.api_key or "").strip())

    return jsonify(
        {
            "deskbot_server": tcp_alive(deskbot_host, deskbot_port),
            "tts_provider": tts_provider,
            "tts_protocol": PROTOCOL_ID,
            "tts_version": PROVIDER_VERSION,
            "tts_streaming": True,
            "tts_configured": tts_configured,
            "deskbot_target": f"{deskbot_host}:{deskbot_port}",
            "tts_target": tts_cfg.ws_url,
        }
    )
