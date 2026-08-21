"""摄像头 JPEG 帧：检测、跟踪与视觉事件分发（经 camera_frame 上行）。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal, Optional

from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.camera_frame import analyze_face_detections
from deskbot_server.application.face_detector import CameraFaceDetector
from deskbot_server.application.face_tracker import FaceTracker
from deskbot_server.core.concurrency import face_infer_slot
from deskbot_server.face_identity import attach_descriptors_to_faces, deduplicate_overlapping_faces
from deskbot_server.face_snapshot_cache import update_device_faces
from deskbot_server.llm.vision_input import ValidatedJpeg
from deskbot_server.vision.generation import is_current_vision_generation
from deskbot_server.vision.undistort import CameraFaceRuntime

logger = logging.getLogger("deskbot-server")

FrameSource = Literal["asr_chat", "camera_uplink"]


@dataclass
class CameraJpegProcessResult:
    infer_ms: float
    detected: bool
    detect: Optional[dict] = None
    view_sent: int = 0
    view_attempted: int = 0


async def create_camera_face_session(
    runtime: CameraFaceRuntime,
    *,
    device_id: str,
    log_channel: str,
) -> tuple[CameraFaceDetector, FaceTracker]:
    """创建单连接级 detector + tracker，并按需预加载 InsightFace。"""
    detector = await asyncio.to_thread(
        CameraFaceDetector,
        num_faces=runtime.num_faces,
        undistorter=runtime.undistorter,
        min_face_detection_confidence=runtime.min_face_detection_confidence,
        min_face_presence_confidence=runtime.min_face_presence_confidence,
        frame_width=runtime.frame_width,
        frame_height=runtime.frame_height,
    )
    face_tracker = FaceTracker(
        device_id=device_id,
        max_dist_px=runtime.face_track_max_dist_px,
        max_lost_frames=runtime.face_track_max_lost_frames,
        identity_similarity_threshold=runtime.identity_similarity_threshold,
    )
    if runtime.face_embedding_enabled:
        try:
            from deskbot_server.vision.face_embedding import get_face_embedding_engine

            await asyncio.to_thread(get_face_embedding_engine)
        except Exception as exc:
            logger.warning(
                "[%s] InsightFace 预加载失败 device_id=%s: %s",
                log_channel,
                device_id,
                exc,
            )
    return detector, face_tracker


async def process_camera_jpeg_frame(
    *,
    device_id: str,
    validated_jpeg: ValidatedJpeg,
    detector: CameraFaceDetector,
    face_tracker: FaceTracker,
    runtime: CameraFaceRuntime,
    image_broker: CameraImageBroker,
    frame_source: FrameSource,
    log_channel: str,
    captured_at: float | None = None,
    connection_generation: int | None = None,
    publish_undetected: bool = True,
    decoded_rgb=None,
) -> CameraJpegProcessResult:
    """单帧 JPEG：检测 → 缓存 → broker 视觉事件分发。

    ``decoded_rgb`` 复用入口校验时的同帧解码结果，避免二次 JPEG 解码。
    """
    if not isinstance(validated_jpeg, ValidatedJpeg):
        raise TypeError("validated_jpeg must be a ValidatedJpeg")
    frame_bytes = validated_jpeg.data
    frame_captured_at = float(captured_at) if captured_at is not None else time.time()
    if connection_generation is not None and not is_current_vision_generation(
        device_id, connection_generation
    ):
        return CameraJpegProcessResult(infer_ms=0.0, detected=False)

    t0 = time.monotonic()
    try:
        async with face_infer_slot():
            if decoded_rgb is not None:
                raw_faces = await asyncio.to_thread(
                    detector.detect_faces,
                    frame_bytes,
                    decoded_rgb=decoded_rgb,
                )
            else:
                raw_faces = await asyncio.to_thread(
                    detector.detect_faces, frame_bytes
                )
            raw_faces = deduplicate_overlapping_faces(raw_faces)
            await asyncio.to_thread(
                attach_descriptors_to_faces,
                raw_faces,
                bgr_image=detector.last_bgr,
            )
            tagged_faces = face_tracker.assign_ids(raw_faces)
        snapshot_accepted = update_device_faces(
            device_id,
            tagged_faces,
            captured_at=frame_captured_at,
            generation=connection_generation,
        )
        if not snapshot_accepted:
            return CameraJpegProcessResult(
                infer_ms=(time.monotonic() - t0) * 1000.0,
                detected=False,
            )
        detect = analyze_face_detections(tagged_faces)
    except Exception as exc:
        from deskbot_server.face_snapshot_cache import clear_device_faces

        clear_device_faces(
            device_id,
            generation=connection_generation,
        )
        infer_ms = (time.monotonic() - t0) * 1000.0
        if connection_generation is not None and not is_current_vision_generation(
            device_id, connection_generation
        ):
            return CameraJpegProcessResult(infer_ms=infer_ms, detected=False)
        logger.warning(
            "[%s] 推理失败 device_id=%s: %s",
            log_channel,
            device_id,
            exc,
        )
        _s, _a = (0, 0)
        if publish_undetected:
            _s, _a = await image_broker.publish_validated(
                device_id,
                validated_jpeg,
                detected=False,
                frame_w=validated_jpeg.width,
                frame_h=validated_jpeg.height,
                captured_at=frame_captured_at,
                generation=connection_generation,
            )
        return CameraJpegProcessResult(
            infer_ms=infer_ms,
            detected=False,
            view_sent=_s,
            view_attempted=_a,
        )

    infer_ms = (time.monotonic() - t0) * 1000.0
    if not detect or not detect.get("points"):
        _s, _a = (0, 0)
        if publish_undetected:
            _s, _a = await image_broker.publish_validated(
                device_id,
                validated_jpeg,
                detected=False,
                frame_w=validated_jpeg.width,
                frame_h=validated_jpeg.height,
                captured_at=frame_captured_at,
                generation=connection_generation,
            )
        return CameraJpegProcessResult(
            infer_ms=infer_ms,
            detected=False,
            view_sent=_s,
            view_attempted=_a,
        )

    analysis = detect

    if publish_undetected:
        # Standalone mode: this pipeline is the only publisher, so the
        # detected frame must still ship with its metadata.
        view_sent, view_attempted = await image_broker.publish_validated(
            device_id,
            validated_jpeg,
            detected=True,
            landmarks=analysis["landmarks"],
            frame_w=analysis["image_w"],
            frame_h=analysis["image_h"],
            yaw_deg=analysis["yaw_deg"],
            pitch_deg=analysis["pitch_deg"],
            iris_offsets=analysis["iris_offsets"],
            face_score=analysis.get("face_score"),
            frontal_score=analysis["frontal_score"],
            is_frontal=analysis["is_frontal"],
            confidence=analysis.get("face_score"),
            points=analysis["points"],
            faces=analysis.get("faces"),
            face_count=analysis.get("face_count"),
            face_id=analysis.get("face_id"),
            captured_at=frame_captured_at,
            generation=connection_generation,
        )
    else:
        # ``publish_undetected=False`` means the ingress path already
        # published this exact JPEG as a live preview.  Re-sending the frame
        # here shipped every detected frame twice; follow up with a
        # lightweight ``face_meta`` message (no JPEG) instead.
        view_sent, view_attempted = await image_broker.publish_face_meta(
            device_id,
            landmarks=analysis["landmarks"],
            frame_w=analysis["image_w"],
            frame_h=analysis["image_h"],
            yaw_deg=analysis["yaw_deg"],
            pitch_deg=analysis["pitch_deg"],
            iris_offsets=analysis["iris_offsets"],
            face_score=analysis.get("face_score"),
            frontal_score=analysis["frontal_score"],
            is_frontal=analysis["is_frontal"],
            confidence=analysis.get("face_score"),
            points=analysis["points"],
            faces=analysis.get("faces"),
            face_count=analysis.get("face_count"),
            face_id=analysis.get("face_id"),
            captured_at=frame_captured_at,
            generation=connection_generation,
        )

    return CameraJpegProcessResult(
        infer_ms=infer_ms,
        detected=True,
        detect=analysis,
        view_sent=view_sent,
        view_attempted=view_attempted,
    )
