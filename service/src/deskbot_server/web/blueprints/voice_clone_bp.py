"""声音复刻：训练提交、状态查询、任务列表（从 debug_bp 拆出，逻辑未改）。"""

from __future__ import annotations

import logging
import mimetypes
import time

from flask import Blueprint, jsonify, request

bp = Blueprint("voice_clone", __name__)
logger = logging.getLogger("deskbot-server")


from deskbot_server.web.blueprints.debug_bp import _consume_consumer_test_quota  # noqa: E402


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


def _doubao_default_voice_clone_speaker_id() -> str:
    """``.env`` 里预置的音色 ID：带内置密钥的分发版有值，普通版为空。"""
    from deskbot_server.tts.doubao import load_doubao_tts_config

    return (load_doubao_tts_config().voice_clone_speaker_id or "").strip()


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




@bp.post("/api/doubao_tts/voice-clone")
def api_doubao_tts_voice_clone():
    from deskbot_server.tts.voice_clone import clone_doubao_voice, normalize_speaker_id
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
    # 音色 ID 由用户填写（火山控制台申请所得）；表单没带就回落到 .env 里的
    # 预置值——带内置密钥的分发版在种子 .env 预置，普通版为空需用户自己填。
    try:
        speaker_id = normalize_speaker_id(
            str(form.get("speaker_id") or "").strip()
            or _doubao_default_voice_clone_speaker_id()
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    quota, limit_err = _consume_consumer_test_quota()
    if limit_err:
        return limit_err
    job = create_voice_clone_job(
        speaker_id=speaker_id,
        display_name=voice_name,
    )
    try:
        result = clone_doubao_voice(
            cfg,
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            language=language,
            display_name=voice_name,
            speaker_id=speaker_id,
            prompt_text=str(form.get("prompt_text") or "").strip(),
        )
    except ValueError as exc:
        mark_voice_clone_job_failed(job.id, exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surface provider error to the user
        mark_voice_clone_job_failed(job.id, exc)
        logger.exception("火山声音复刻训练提交失败 speaker_id=%r", speaker_id)
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
