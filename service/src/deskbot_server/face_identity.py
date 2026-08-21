"""人脸相似性特征与跨帧/跨次 re-id（仅 InsightFace embedding）。

InsightFace 不可用时人脸仍会被检测与跟踪（bbox/IoU + 鼻尖距离，见
``application.face_tracker``），但不再做档案匹配：识别功能明确降级，
不再回退到 9 维几何特征参与身份判定。
"""
from __future__ import annotations

import base64
import io
import math
from typing import Any, Optional

from deskbot_server.camera_face_tune import get_face_embedding_enabled
from deskbot_server.vision.face_embedding import (
    is_embedding_vector,
)


def descriptor_cosine_similarity(a: list[float], b: list[float]) -> float:
    """两特征向量余弦相似度 [-1, 1]；输入须等长。"""
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot))


def ema_update_descriptor(
    prev: list[float] | None,
    sample: list[float],
    *,
    alpha: float = 0.2,
) -> list[float]:
    """对 profile / track 特征做指数滑动平均并重新归一化。"""
    if prev is None or len(prev) != len(sample):
        return list(sample)
    a = max(0.05, min(0.5, float(alpha)))
    merged = [(1.0 - a) * p + a * s for p, s in zip(prev, sample)]
    norm = math.sqrt(sum(x * x for x in merged))
    if norm < 1e-6:
        return list(sample)
    return [round(x / norm, 6) for x in merged]


def attach_descriptor(
    face: dict[str, Any],
    *,
    bgr_image: Any = None,
) -> Optional[list[float]]:
    """为单张检出脸附加 ``face_descriptor``（仅 InsightFace embedding）。

    embedding 不可用（模型未加载 / 显式关闭 / 无图像）时返回 ``None``；
    调用方须继续用 bbox/IoU + 鼻尖距离做跟踪，且不做身份匹配。
    """
    existing = face.get("face_descriptor")
    if is_embedding_vector(existing) and bgr_image is None:
        return existing

    desc: Optional[list[float]] = None
    if get_face_embedding_enabled() and bgr_image is not None:
        from deskbot_server.vision.face_embedding import compute_face_embedding

        desc = compute_face_embedding(
            bgr_image,
            face.get("points") or [],
            landmarks=face.get("landmarks") or [],
        )

    if desc is None and is_embedding_vector(existing):
        return existing

    if desc is not None:
        face["face_descriptor"] = desc
        face["descriptor_kind"] = "embedding"
    return desc


def attach_descriptors_to_faces(
    faces: list[dict[str, Any]],
    *,
    bgr_image: Any = None,
) -> None:
    for face in faces or []:
        if isinstance(face, dict):
            attach_descriptor(face, bgr_image=bgr_image)


def descriptor_from_jpeg_bytes(
    jpeg_bytes: bytes,
    points: list,
    *,
    landmarks: list | None = None,
) -> Optional[list[float]]:
    if not get_face_embedding_enabled() or not jpeg_bytes:
        return None
    try:
        import numpy as np
        from PIL import Image  # type: ignore

        from deskbot_server.vision.face_embedding import compute_face_embedding, rgb_to_bgr

        with Image.open(io.BytesIO(jpeg_bytes)) as im:
            rgb = np.array(im.convert("RGB"), dtype=np.uint8)
        return compute_face_embedding(rgb_to_bgr(rgb), points, landmarks=landmarks)
    except Exception:
        return None


def descriptor_from_jpeg_base64(
    b64: str,
    points: list,
    *,
    landmarks: list | None = None,
) -> Optional[list[float]]:
    raw = (b64 or "").strip()
    if not raw:
        return None
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        return descriptor_from_jpeg_bytes(base64.b64decode(raw), points, landmarks=landmarks)
    except Exception:
        return None


def _points_bbox(face: dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    """返回 (min_x, min_y, max_x, max_y)。"""
    xs: list[float] = []
    ys: list[float] = []
    for p in (face.get("landmarks") or face.get("points") or []):
        if not isinstance(p, dict) or "x" not in p or "y" not in p:
            continue
        try:
            xs.append(float(p["x"]))
            ys.append(float(p["y"]))
        except (TypeError, ValueError):
            continue
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _eye_distance(face: dict[str, Any]) -> Optional[float]:
    by = {
        p["name"]: p
        for p in (face.get("points") or [])
        if isinstance(p, dict) and p.get("name") and "x" in p and "y" in p
    }
    le = by.get("left_eye")
    re_ = by.get("right_eye")
    if not (le and re_):
        return None
    try:
        return math.hypot(float(re_["x"]) - float(le["x"]), float(re_["y"]) - float(le["y"]))
    except (TypeError, ValueError):
        return None


def _nose_xy(face: dict[str, Any]) -> Optional[tuple[float, float]]:
    for p in face.get("points") or []:
        if isinstance(p, dict) and p.get("name") == "nose":
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError, KeyError):
                return None
    for p in face.get("landmarks") or []:
        if isinstance(p, dict) and p.get("name") == "nose":
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError, KeyError):
                return None
    return None


def face_quality_score(face: dict[str, Any]) -> float:
    from deskbot_server.vision.geometry import compute_face_score

    w = int(face.get("image_w") or 320)
    h = int(face.get("image_h") or 240)
    return compute_face_score(face.get("points") or [], face.get("landmarks") or [], image_w=w, image_h=h)


def deduplicate_overlapping_faces(
    faces: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.35,
    embedding_sim_threshold: float = 0.55,
    nose_dist_ratio: float = 0.4,
) -> list[dict[str, Any]]:
    """去掉同一帧内重叠/重复的检出（MediaPipe 偶发一张脸多个框）。

    按 ``face_quality_score`` 从高到低贪心保留；与已保留框 IoU 过高视为重复。
    鼻尖距离 < 眼距×ratio 时：两张脸都有 embedding 则以相似度裁决；
    没有 embedding（识别降级）时鼻尖如此接近只可能是重复框，直接判重。
    """
    if len(faces) <= 1:
        return list(faces or [])

    candidates: list[tuple[float, dict[str, Any], tuple[float, float, float, float], list[float]]] = []
    for face in faces:
        if not isinstance(face, dict):
            continue
        bbox = _points_bbox(face)
        if bbox is None:
            continue
        desc = attach_descriptor(face) or []
        q = face_quality_score(face)
        candidates.append((q, face, bbox, desc))

    if not candidates:
        return []

    candidates.sort(key=lambda x: -x[0])

    kept: list[dict[str, Any]] = []
    kept_meta: list[tuple[tuple[float, float, float, float], list[float], Optional[tuple[float, float]], Optional[float]]] = []

    for q, face, bbox, desc in candidates:
        is_dup = False
        nose = _nose_xy(face)
        eye_d = _eye_distance(face)
        for kbbox, kdesc, knose, keye in kept_meta:
            if _bbox_iou(bbox, kbbox) >= iou_threshold:
                is_dup = True
                break
            if nose and knose and eye_d and keye:
                ref = min(eye_d, keye) * nose_dist_ratio
                near = (
                    math.hypot(nose[0] - knose[0], nose[1] - knose[1]) < ref
                )
                if not near:
                    continue
                if is_embedding_vector(desc) and is_embedding_vector(kdesc):
                    same_person = (
                        descriptor_cosine_similarity(desc, kdesc)
                        >= embedding_sim_threshold
                    )
                else:
                    # 无 embedding 时鼻尖近到这个程度只可能是同一张脸的重复框。
                    same_person = True
                if same_person:
                    is_dup = True
                    break
        if is_dup:
            continue
        kept.append(face)
        kept_meta.append((bbox, desc, nose, eye_d))

    return kept
