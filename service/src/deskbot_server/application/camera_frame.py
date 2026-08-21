"""摄像头帧处理：单帧人脸几何量计算（无 WebSocket）。"""

from __future__ import annotations

from typing import Any, Optional

from deskbot_server.camera_face_tune import (
    get_frontal_angle_threshold_deg,
    get_frontal_threshold,
)
from deskbot_server.vision.geometry import (
    FACE_FRAME_HEIGHT,
    FACE_FRAME_WIDTH,
    FRONTAL_THRESHOLD,
    FRONTAL_YAW_THRESHOLD_DEG,
    compute_eye_iris_offsets,
    compute_face_pitch_deg,
    compute_face_score,
    compute_face_yaw_deg,
    compute_frontal_angle_deg,
    compute_frontal_score,
    compute_is_frontal_by_angle,
    decompose_facial_transform_matrix,
)


def _resolve_head_pose(
    landmarks: list,
    facial_transform: list | None,
) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """优先 MediaPipe 4×4 变换矩阵，否则回退 9 点 2D 几何。"""
    if facial_transform:
        pose = decompose_facial_transform_matrix(facial_transform)
        if pose is not None:
            return (
                pose.get("yaw_deg"),
                pose.get("pitch_deg"),
                pose.get("roll_deg"),
                "matrix",
            )
    return (
        compute_face_yaw_deg(landmarks),
        compute_face_pitch_deg(landmarks),
        None,
        "landmarks",
    )


def analyze_face_detection(detect: dict) -> dict[str, Any]:
    """从单张人脸原始检出结果计算面向角、正脸分等。"""
    points = detect.get("points") or []
    landmarks = detect.get("landmarks") or []
    image_w = int(detect.get("image_w") or 0) or FACE_FRAME_WIDTH
    image_h = int(detect.get("image_h") or 0) or FACE_FRAME_HEIGHT
    frontal_score = compute_frontal_score(points)
    is_frontal = frontal_score >= get_frontal_threshold(FRONTAL_THRESHOLD)
    face_score = compute_face_score(points, landmarks, image_w=image_w, image_h=image_h)
    yaw_deg, pitch_deg, roll_deg, pose_source = _resolve_head_pose(
        landmarks, detect.get("facial_transform")
    )
    iris_offsets = compute_eye_iris_offsets(landmarks)
    frontal_angle_deg = compute_frontal_angle_deg(yaw_deg, pitch_deg)
    is_frontal_angle = compute_is_frontal_by_angle(
        yaw_deg,
        pitch_deg,
        threshold_deg=get_frontal_angle_threshold_deg(FRONTAL_YAW_THRESHOLD_DEG),
    )
    out: dict[str, Any] = {
        "points": points,
        "landmarks": landmarks,
        "face_score": face_score,
        "frontal_score": frontal_score,
        "is_frontal": is_frontal,
        "frontal_angle_deg": frontal_angle_deg,
        "is_frontal_angle": is_frontal_angle,
        "yaw_deg": yaw_deg,
        "pitch_deg": pitch_deg,
        "roll_deg": roll_deg,
        "pose_source": pose_source,
        "iris_offsets": iris_offsets,
        "image_w": image_w,
        "image_h": image_h,
    }
    face_id = detect.get("face_id")
    if face_id is not None:
        out["face_id"] = int(face_id)
    person_id = detect.get("person_id")
    if person_id is not None:
        out["person_id"] = int(person_id)
    person_name = detect.get("person_name")
    if person_name:
        out["person_name"] = str(person_name)
    identity_score = detect.get("identity_score")
    if identity_score is not None:
        try:
            out["identity_score"] = round(float(identity_score), 3)
        except (TypeError, ValueError):
            pass
    match_source = detect.get("match_source") or detect.get("face_id_source")
    if match_source:
        out["id_match_source"] = str(match_source)
        out["match_source"] = str(match_source)
    dk = detect.get("descriptor_kind")
    if dk:
        out["descriptor_kind"] = str(dk)
    dd = detect.get("descriptor_dim")
    if dd is not None:
        try:
            out["descriptor_dim"] = int(dd)
        except (TypeError, ValueError):
            pass
    return out


def pick_primary_face(
    analyses: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """多张脸时选主脸：优先 ``is_frontal``，否则 ``frontal_score`` 最高。"""
    if not analyses:
        return None
    pool = analyses
    frontal = [a for a in analyses if a.get("is_frontal")]
    if frontal:
        pool = frontal
    return max(pool, key=lambda a: float(a.get("frontal_score") or 0.0))


def analyze_face_detections(
    faces: list[dict],
) -> dict[str, Any]:
    """多人脸：逐脸分析并附带 ``faces`` 列表；顶层字段来自主脸（兼容旧协议）。"""
    analyses = [analyze_face_detection(face) for face in (faces or [])]
    analyses = [a for a in analyses if a.get("points")]
    primary = pick_primary_face(analyses)
    if primary is None:
        return {
            "points": [],
            "landmarks": [],
            "face_score": 0.0,
            "frontal_score": 0.0,
            "is_frontal": False,
            "frontal_angle_deg": None,
            "is_frontal_angle": False,
            "yaw_deg": None,
            "pitch_deg": None,
            "roll_deg": None,
            "pose_source": None,
            "iris_offsets": {"left_eye": None, "right_eye": None},
            "image_w": FACE_FRAME_WIDTH,
            "image_h": FACE_FRAME_HEIGHT,
            "faces": [],
            "face_count": 0,
        }
    merged = dict(primary)
    merged["faces"] = analyses
    merged["face_count"] = len(analyses)
    return merged
