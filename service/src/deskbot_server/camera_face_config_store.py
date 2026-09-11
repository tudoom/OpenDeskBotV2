"""摄像头人脸检测配置持久化（``data/camera_face.json``）。"""
from __future__ import annotations

from typing import Any, Optional

from deskbot_server.constants import CAMERA_FACE_CFG_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path


def normalize_camera_face_document(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("body must be a JSON object")
    out: dict[str, Any] = {}
    nf = int(raw.get("num_faces", 5))
    out["num_faces"] = max(1, min(10, nf))
    md = float(raw.get("min_face_detection_confidence", 0.15))
    mp = float(raw.get("min_face_presence_confidence", 0.15))
    out["min_face_detection_confidence"] = max(0.05, min(0.95, md))
    out["min_face_presence_confidence"] = max(0.05, min(0.95, mp))
    ft = float(raw.get("frontal_threshold", 0.4))
    out["frontal_threshold"] = max(0.05, min(0.95, ft))
    td = float(raw.get("face_track_max_dist_px", 60))
    tl = int(raw.get("face_track_max_lost_frames", 5))
    out["face_track_max_dist_px"] = max(16.0, min(240.0, td))
    out["face_track_max_lost_frames"] = max(1, min(60, tl))
    hfov = float(raw.get("horizontal_fov_deg", 120.0))
    out["horizontal_fov_deg"] = max(30.0, min(170.0, hfov))
    fa = float(raw.get("frontal_angle_threshold_deg", 15.0))
    out["frontal_angle_threshold_deg"] = max(1.0, min(90.0, fa))
    fe = raw.get("face_embedding_enabled", True)
    out["face_embedding_enabled"] = str(fe).strip().lower() not in ("0", "false", "no", "off")
    # 身份判定只走 embedding；几何回退阈值已随几何 descriptor 一并移除。
    ist = float(raw.get("identity_similarity_threshold", 0.40))
    out["identity_similarity_threshold"] = max(0.25, min(0.99, ist))
    fw = int(raw.get("frame_width", 320))
    fh = int(raw.get("frame_height", 240))
    out["frame_width"] = max(160, min(640, fw))
    out["frame_height"] = max(120, min(480, fh))
    return out


_STORE = JsonDocumentStore(
    lambda: resolve_json_path(CAMERA_FACE_CFG_FILE),
    normalize=normalize_camera_face_document,
    # 保持既有语义：save 不做二次 normalize（调用方已 normalize）。
    normalize_save=lambda doc: doc,
)


def load_camera_face_cfg_file() -> Optional[dict[str, Any]]:
    return _STORE.load()


def save_camera_face_cfg_file(cfg: dict[str, Any]) -> None:
    _STORE.save(cfg)
