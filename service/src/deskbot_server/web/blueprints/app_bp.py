from __future__ import annotations

from flask import Blueprint, jsonify, request

from deskbot_server.face_profiles_store import (
    delete_face_profile,
    list_face_profiles_summary,
    update_face_profile_name,
)
from deskbot_server.hardware_catalog import (
    ensure_local_device,
    get_device_by_device_id,
    list_devices,
    validate_device_id,
)
from deskbot_server.memory_store import (
    add_memory,
    delete_memory,
    get_memory,
    list_memory_entries,
    update_memory,
)
from deskbot_server.miot_service import (
    authorize_and_sync,
    error_payload,
    get_bind_url,
    get_status,
    load_homes_cache,
    miot_sdk_available,
    parse_auth_payload,
    sync_homes,
)
from deskbot_server.miot_service import (
    unbind as miot_unbind,
)
from deskbot_server.scheduled_task_service import (
    count_scheduled_tasks,
    delete_scheduled_task,
    list_scheduled_tasks,
)
from deskbot_server.web.helpers import (
    fetch_attached_usb_snapshot,
    fetch_live_device_snapshot,
)
from deskbot_server.web.session_device import (
    clear_current_device,
    get_current_device_id,
    set_current_device_id,
)

bp = Blueprint("app", __name__, url_prefix="/app")


def _fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.2f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


@bp.app_template_filter("fmt_bytes")
def fmt_bytes_filter(n):
    return _fmt_bytes(n)


def _local_device_snapshot():
    """Return the physical USB connection directory for this PC."""

    live_snapshot = fetch_live_device_snapshot()
    live_map = live_snapshot["devices"]
    usb_snapshot = fetch_attached_usb_snapshot()

    # Only the trusted realtime bridge can populate the hardware catalogue.
    if usb_snapshot["ok"]:
        for source in usb_snapshot["devices"]:
            device_id = str(source.get("device_id") or "").strip()
            if (
                validate_device_id(device_id)
                and bool(source.get("online"))
                and str(source.get("transport") or "") == "usb_cdc"
            ):
                ensure_local_device(device_id)

    devices = list_devices()
    by_id = {device.device_id: device for device in devices}
    usb_map = {
        str(source.get("device_id") or "").strip(): source
        for source in usb_snapshot["devices"]
        if str(source.get("device_id") or "").strip()
    }
    all_ids = list(by_id)
    for device_id in (*live_map.keys(), *usb_map.keys()):
        if device_id not in all_ids:
            all_ids.append(device_id)

    rows = []
    for device_id in all_ids:
        device = by_id.get(device_id)
        live = live_map.get(device_id, {})
        usb = usb_map.get(device_id, {})
        online = (
            bool(live.get("online"))
            if live_snapshot["ok"]
            else (bool(usb.get("online")) if usb_snapshot["ok"] else None)
        )
        last_seen = live.get("last_seen") or usb.get("last_seen")
        if not last_seen and device is not None and device.last_seen_at:
            last_seen = device.last_seen_at.isoformat()
        rows.append(
            {
                "id": device.id if device is not None else None,
                "device_id": device_id,
                "display_name": (
                    device.display_name or device_id
                    if device is not None
                    else device_id
                ),
                "online": online,
                "presence_state": (
                    "control_plane_down"
                    if not live_snapshot["ok"] and not usb_snapshot["ok"]
                    else ("online" if online else "offline")
                ),
                "interaction_state": live.get(
                    "interaction_state", usb.get("interaction_state")
                ),
                "microphone_health": live.get(
                    "microphone_health", usb.get("microphone_health")
                ),
                "transport": (
                    str(live.get("transport") or usb.get("transport") or "").strip()
                    or None
                ),
                "session_generation": live.get(
                    "session_generation", usb.get("session_generation")
                ),
                "last_seen": last_seen,
                "observed_at": live_snapshot["observed_at"],
                "registration_pending": device is None,
            }
        )

    current = get_current_device_id()
    known_ids = set(by_id)
    if current not in known_ids:
        current = None
        clear_current_device()
    if not current:
        online_ids = [
            row["device_id"]
            for row in rows
            if row["device_id"] in known_ids and row["online"] is True
        ]
        if len(online_ids) == 1:
            current = online_ids[0]
        elif len(known_ids) == 1:
            current = next(iter(known_ids))
        if current:
            set_current_device_id(current)
    for row in rows:
        row["is_current"] = row["device_id"] == current

    return jsonify(
        {
            "ok": True,
            "observed_at": live_snapshot["observed_at"],
            "control_plane": {
                "ok": bool(live_snapshot["ok"]),
                "error": live_snapshot["error"],
                "observed_at": live_snapshot["observed_at"],
            },
            "devices": rows,
            "available_usb_devices": [],
            "usb_discovery": {
                "ok": bool(usb_snapshot["ok"]),
                "error": usb_snapshot["error"],
                "observed_at": usb_snapshot["observed_at"],
            },
            "current_device_id": current,
        }
    )


@bp.get("/api/devices")
def api_list_devices():
    return _local_device_snapshot()


@bp.post("/api/devices/select")
def api_select_device():
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        clear_current_device()
        return jsonify({"ok": True, "current_device_id": None})
    if get_device_by_device_id(device_id) is None:
        return jsonify(
            {
                "ok": False,
                "error": "unknown_usb_device",
                "message": "设备尚未由本机 USB 服务发现",
            }
        ), 404
    set_current_device_id(device_id)
    return jsonify({"ok": True, "current_device_id": device_id})


@bp.get("/api/scheduled-tasks")
def api_list_scheduled_tasks():
    page = max(1, int(request.args.get("page") or 1))
    per_page = int(request.args.get("per_page") or 10)
    if per_page not in (10, 50, 100, 200):
        per_page = 10
    status_filter = str(request.args.get("status") or "").strip().lower() or None
    try:
        total = count_scheduled_tasks(status=status_filter)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    tasks = list_scheduled_tasks(
        status=status_filter,
        limit=per_page,
        offset=offset,
    )
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "status": status_filter or "",
            "tasks": tasks,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )


@bp.get("/api/face-profiles")
def api_list_face_profiles():
    from deskbot_server.application.face_detector import face_stack_available

    profiles = list_face_profiles_summary()
    return jsonify(
        {
            "ok": True,
            "scope": "local",
            "profiles": profiles,
            # faceless 安装包（默认客户端构建）不带 mediapipe/insightface：
            # 前端据此在空档案时提示「组件未随本安装包提供」。
            "face_stack_available": face_stack_available(),
        }
    )


@bp.delete("/api/face-profiles/<int:person_id>")
def api_delete_face_profile(person_id: int):
    if not delete_face_profile(person_id):
        return jsonify({"ok": False, "error": "人脸档案不存在"}), 404
    return jsonify({"ok": True})


@bp.put("/api/face-profiles/<int:person_id>")
@bp.patch("/api/face-profiles/<int:person_id>")
def api_update_face_profile(person_id: int):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name 不能为空"}), 400
    try:
        profile = update_face_profile_name(person_id, name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if profile is None:
        return jsonify({"ok": False, "error": "人脸档案不存在"}), 404
    return jsonify({"ok": True, "profile": profile})


def _consume_settings_test_quota():
    from deskbot_server.application.settings_test_limit import (
        SETTINGS_TEST_DAILY_LIMIT,
        SettingsTestLimitExceeded,
        check_and_consume_settings_test,
    )

    try:
        snap = check_and_consume_settings_test()
    except SettingsTestLimitExceeded as exc:
        return None, (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "daily_limit": SETTINGS_TEST_DAILY_LIMIT,
                }
            ),
            429,
        )
    return {
        "daily_limit": SETTINGS_TEST_DAILY_LIMIT,
        "remaining": snap.remaining,
    }, None


@bp.get("/api/memories")
def api_list_memories():
    entries = list_memory_entries()
    return jsonify(
        {"ok": True, "scope": "local", "memories": entries, "count": len(entries)}
    )


@bp.post("/api/memories")
def api_create_memory():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or request.form.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 不能为空"}), 400
    try:
        entry = add_memory(text)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "memory": entry})


@bp.get("/api/memories/<entry_id>")
def api_get_memory(entry_id: str):
    entry = get_memory(entry_id)
    if entry is None:
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True, "memory": entry})


@bp.put("/api/memories/<entry_id>")
@bp.patch("/api/memories/<entry_id>")
def api_update_memory(entry_id: str):
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 不能为空"}), 400
    try:
        entry = update_memory(entry_id, text)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if entry is None:
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True, "memory": entry})


@bp.delete("/api/memories/<entry_id>")
def api_delete_memory(entry_id: str):
    if not delete_memory(entry_id):
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True})


def _tts_cfg_from_payload(payload: dict):
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
        ws_url=base.ws_url,
        sample_rate=sample_rate,
        audio_format=str(payload.get("audio_format") or base.audio_format).strip(),
        enable_timestamp=base.enable_timestamp,
    )


@bp.get("/api/tts/config")
def api_tts_config_get():
    from deskbot_server.tts.doubao import load_doubao_tts_config

    cfg = load_doubao_tts_config()
    return jsonify({"ok": True, "config": cfg.masked()})


@bp.post("/api/tts/config")
def api_tts_config_post():
    from deskbot_server.tts.doubao import load_doubao_tts_config
    from deskbot_server.tts.env_store import save_doubao_tts_env

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400

    from deskbot_server.tts.doubao import resolve_optional_secret

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
    return jsonify({"ok": True, "config": cfg.masked()})


@bp.get("/api/tts/speakers")
def api_tts_speakers():
    from deskbot_server.tts.speakers import list_doubao_tts_speaker_presets

    return jsonify({"ok": True, "speakers": list_doubao_tts_speaker_presets()})


@bp.post("/api/tts/preview")
def api_tts_preview():
    import asyncio
    import base64

    from deskbot_server.tts.doubao import synthesize_doubao_tts
    from deskbot_server.util import pcm_to_wav_bytes

    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "试听文本不能为空"}), 400
    cfg = _tts_cfg_from_payload(payload)
    if not cfg.api_key:
        return jsonify({"ok": False, "error": "请先配置豆包语音 API Key（DOUBAO_TTS_API_KEY）"}), 400
    quota, limit_err = _consume_settings_test_quota()
    if limit_err:
        return limit_err
    try:
        result = asyncio.run(synthesize_doubao_tts(text, cfg))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    wav = pcm_to_wav_bytes(result.pcm, result.sample_rate)
    return jsonify(
        {
            "ok": True,
            "sample_rate": result.sample_rate,
            "elapsed_ms": result.elapsed_ms,
            "speaker": cfg.speaker,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "quota": quota,
        }
    )


@bp.delete("/api/scheduled-tasks/<task_id>")
def api_delete_scheduled_task(task_id: str):
    if not delete_scheduled_task(task_id):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True})


# ----- 米家 IoT -----


@bp.get("/api/miot/status")
def api_miot_status():
    ok_sdk, sdk_err = miot_sdk_available()
    if not ok_sdk:
        return jsonify({"ok": False, "error": sdk_err, "sdk_ok": False}), 503
    refresh = str(request.args.get("refresh") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        status = get_status(refresh=refresh)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    homes = load_homes_cache()
    return jsonify(
        {
            "ok": True,
            "sdk_ok": True,
            "scope": "local",
            "status": status,
            "homes": homes,
            "device_count": homes.get("device_count") or len(homes.get("devices") or []),
            "scene_count": homes.get("scene_count") or len(homes.get("scenes") or []),
        }
    )


@bp.post("/api/miot/bind-url")
def api_miot_bind_url():
    ok_sdk, sdk_err = miot_sdk_available()
    if not ok_sdk:
        return jsonify({"ok": False, "error": sdk_err}), 503
    try:
        url = get_bind_url()
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "auth_url": url,
            "hint": (
                "请在浏览器打开授权链接并登录小米账号。"
                "授权完成后会进入「小米账号授权完成」页面，"
                "点击「复制授权码」，回到本页粘贴即可（也可粘贴完整回调 URL）。"
            ),
        }
    )


@bp.post("/api/miot/authorize")
def api_miot_authorize():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "").strip()
    state = str(payload.get("state") or "").strip()
    callback = str(
        payload.get("callback_url")
        or payload.get("url")
        or payload.get("payload")
        or ""
    ).strip()
    try:
        if code and state:
            auth_code, auth_state = code, state
        elif callback:
            auth_code, auth_state = parse_auth_payload(callback)
        else:
            raise ValueError("请粘贴授权回调完整 URL，或提供 code 与 state")
        result = authorize_and_sync(auth_code, auth_state)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(result)


@bp.post("/api/miot/sync")
def api_miot_sync():
    try:
        homes = sync_homes()
        status = get_status(refresh=True)
    except Exception as exc:
        import logging

        logging.getLogger("deskbot-server").exception("[miot] sync failed")
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "status": status,
            "homes": homes,
            "device_count": homes.get("device_count"),
            "scene_count": homes.get("scene_count"),
        }
    )


@bp.post("/api/miot/unbind")
def api_miot_unbind():
    try:
        miot_unbind()
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify({"ok": True})


@bp.get("/api/miot/homes")
def api_miot_homes():
    homes = load_homes_cache()
    return jsonify({"ok": True, "scope": "local", "homes": homes})
