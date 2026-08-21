"""从摄像头快照注册人脸档案。"""
from __future__ import annotations

from typing import Any, Optional

from deskbot_server.application.face_detector import face_stack_available
from deskbot_server.application.face_tracker import reload_all_trackers
from deskbot_server.camera_face_config_store import load_camera_face_cfg_file
from deskbot_server.face_profiles_store import register_face_profile
from deskbot_server.face_snapshot_cache import list_device_faces, resolve_descriptor_from_payload

FACE_STACK_MISSING_MESSAGE = (
    "人脸功能未安装：本安装包不含人脸识别组件（mediapipe/insightface），"
    "无法注册人脸。如需此功能，请使用带人脸栈的安装包"
    "（构建时加 -IncludeFaceStack，或 pip install 'deskbot-server[face]'）"
)


def register_face_for_device(
    device_id: str,
    name: str,
    *,
    face_id: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """将当前帧 ``face_id`` 的人脸写入 ``face_profiles.json``。"""
    device_id = str(device_id or "").strip()
    name = str(name or "").strip()
    if not device_id:
        raise ValueError("device_id required")
    if not name:
        raise ValueError("name required")

    if not face_stack_available():
        # faceless 安装包：检测栈都不在，快照必然为空。给出明确的
        # 「功能未安装」而不是误导性的「画面无人脸」。
        raise ValueError(FACE_STACK_MISSING_MESSAGE)

    faces = list_device_faces(device_id)
    if not faces:
        raise ValueError("当前画面无人脸，请确保 ESP32 正在推流且已检出脸")

    if face_id is None:
        if len(faces) == 1:
            face_id = next(iter(faces.keys()))
        else:
            raise ValueError(
                f"画面中有 {len(faces)} 张脸，请指定 face_id 或让用户说明是左/右/哪一位"
            )
    else:
        face_id = int(face_id)

    face = faces.get(face_id)
    if not face:
        ids = ", ".join(str(k) for k in sorted(faces.keys()))
        raise ValueError(f"face_id={face_id} 不在当前帧中（可用: {ids}）")

    payload = {**face, **(extra or {}), "device_id": device_id, "face_id": face_id, "name": name}
    desc = resolve_descriptor_from_payload(payload)
    if desc is None:
        raise ValueError(
            "无法提取人脸 embedding（InsightFace 不可用或画面质量不足），"
            "识别注册暂不可用；请确认识别模型可用并让人正对镜头后再试"
        )

    cfg = load_camera_face_cfg_file() or {}
    merge_thr = float(cfg.get("identity_similarity_threshold", 0.40))
    profile = register_face_profile(
        name=name,
        descriptor=desc,
        merge_threshold=merge_thr,
    )
    reload_all_trackers()
    return {
        "ok": True,
        "profile": profile,
        "face_id": face_id,
        "device_id": device_id,
    }
