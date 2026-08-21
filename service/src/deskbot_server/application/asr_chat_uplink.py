"""``/asr_chat`` 上行：``next_bin_len`` + binary（音频 / JPEG）。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.camera_jpeg_pipeline import (
    create_camera_face_session,
    process_camera_jpeg_frame,
)
from deskbot_server.application.face_detector import CameraFaceDetector
from deskbot_server.application.face_tracker import FaceTracker
from deskbot_server.llm.vision_input import ValidatedJpeg
from deskbot_server.vision.generation import (
    VisionTombstone,
    begin_vision_generation,
    forget_vision_tombstone,
    is_current_vision_generation,
)
from deskbot_server.vision.undistort import CameraFaceRuntime
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.device_pipeline import DevicePipelineBroker

logger = logging.getLogger("deskbot-server")

PendingKind = Literal["audio", "camera_frame"]

# 缺人脸栈（默认安装包不带 mediapipe/cv2）的提示进程内只记一次，
# 否则每条重连连接都会重复刷一行同样的 warning。
_face_stack_missing_logged = False


def _log_face_stack_missing_once(device_id: str, exc: BaseException) -> None:
    global _face_stack_missing_logged
    if _face_stack_missing_logged:
        return
    _face_stack_missing_logged = True
    logger.warning(
        "[/asr_chat] 人脸检测组件未安装（本安装包不含人脸功能），"
        "人脸检测/识别已关闭；相机预览与拍照问答不受影响。"
        "device_id=%s: %s",
        device_id,
        exc,
    )

_MAX_NEXT_BIN_LEN = 512 * 1024


class LatestCameraInferenceWorker:
    """Run one face inference at a time and retain only the newest pending job."""

    def __init__(self, *, device_id: str) -> None:
        self.device_id = str(device_id or "").strip()
        self._pending: Callable[[], Awaitable[None]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def submit(self, job: Callable[[], Awaitable[None]]) -> bool:
        if self._closed:
            return False
        # Capacity one, latest wins: replacing this reference immediately
        # releases any older not-yet-started frame and its JPEG bytes.
        self._pending = job
        if not self.running:
            self._task = asyncio.create_task(self._drain())
        return True

    async def _drain(self) -> None:
        try:
            while True:
                job = self._pending
                self._pending = None
                if job is None:
                    return
                try:
                    await job()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "[camera] latest-wins inference failed device_id=%s",
                        self.device_id,
                    )
        finally:
            self._task = None

    async def close(self) -> None:
        self._closed = True
        self._pending = None
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait until the running job and its latest pending replacement finish."""

        task = self._task
        if task is not None:
            await asyncio.shield(task)


def coerce_next_bin_len(data: dict[str, Any]) -> int:
    """``next_bin_len`` > 0 表示下一条为 binary；兼容旧草案 ``next_bin``+``len``。"""
    raw = data.get("next_bin_len")
    if raw is None and data.get("next_bin") in (1, True, "1"):
        raw = data.get("len")
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n > _MAX_NEXT_BIN_LEN:
        logger.warning("[/asr_chat] next_bin_len=%d 超过上限 %d，截断", n, _MAX_NEXT_BIN_LEN)
        return _MAX_NEXT_BIN_LEN
    return n


def coerce_opus_frames(data: dict[str, Any]) -> Optional[int]:
    """Opus batch 帧数；缺省或 1 表示单帧 binary。"""
    raw = data.get("frames")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 1 else None


@dataclass
class PendingUplinkBinary:
    kind: PendingKind
    length: int
    codec: Optional[str] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    opus_frames: Optional[int] = None


@dataclass(frozen=True, slots=True)
class VisionConnectionCleanup:
    connection_generation: int
    tombstone: VisionTombstone | None


@dataclass
class AsrChatCameraPipeline:
    """单条 ``/asr_chat`` 连接复用的人脸检测上下文（懒加载）。"""

    runtime: CameraFaceRuntime
    device_id: str
    frame_source: Literal["asr_chat", "camera_uplink"] = "asr_chat"
    detector: Optional[CameraFaceDetector] = None
    face_tracker: Optional[FaceTracker] = None
    frame_count: int = 0
    connection_generation: int | None = field(init=False, default=None)
    detector_closed: bool = field(init=False, default=False)

    def activate_generation(self) -> int | None:
        """Lazily claim vision state when this connection actually has frames."""

        if self.connection_generation is not None:
            if is_current_vision_generation(
                self.device_id,
                self.connection_generation,
            ):
                return self.connection_generation
            return None
        generation = begin_vision_generation(
            self.device_id,
            source=self.frame_source,
        )
        if generation is not None:
            self.connection_generation = generation
        return generation

    async def ensure_ready(self) -> bool:
        if self.detector_closed:
            return False
        if self.detector is not None:
            return True
        try:
            detector, face_tracker = await create_camera_face_session(
                self.runtime,
                device_id=self.device_id,
                log_channel="/asr_chat",
            )
        except ImportError as exc:
            # 人脸视觉栈未随安装包提供（faceless 构建）：记一次 warning 后
            # 关闭本连接的人脸检测，不再逐帧重试导入。相机预览由入口路径
            # 照常发布，拍照问答不依赖检测器。
            self.detector_closed = True
            _log_face_stack_missing_once(self.device_id, exc)
            return False
        except Exception as exc:
            # 其他初始化失败（如模型文件缺失）同样非瞬态：记一次后关闭
            # 检测，避免每帧重复初始化 + 刷屏；设备重连会重新尝试。
            self.detector_closed = True
            logger.error(
                "[/asr_chat] 人脸检测器初始化失败，本连接关闭人脸检测"
                "（预览/拍照不受影响） device_id=%s: %s",
                self.device_id,
                exc,
            )
            return False
        if self.detector_closed:
            # The owning connection tore down while the detector was being
            # created off-loop; release the native FaceLandmarker instead of
            # abandoning it.
            await asyncio.to_thread(detector.close)
            return False
        self.detector, self.face_tracker = detector, face_tracker
        return True

    async def close_detector(self) -> None:
        """Release the MediaPipe FaceLandmarker owned by this connection.

        Idempotent; the owning WebSocket handler calls this after its camera
        inference worker has fully stopped, so no job can still be using the
        detector.
        """

        self.detector_closed = True
        detector = self.detector
        self.detector = None
        self.face_tracker = None
        if detector is not None:
            try:
                await asyncio.to_thread(detector.close)
            except Exception:
                logger.exception(
                    "[/asr_chat] 人脸检测器释放失败 device_id=%s",
                    self.device_id,
                )

    async def process_jpeg(
        self,
        validated_jpeg: ValidatedJpeg,
        *,
        generation: int,
        image_broker: CameraImageBroker,
        captured_at: float | None = None,
        decoded_rgb=None,
    ) -> None:
        if not is_current_vision_generation(self.device_id, generation):
            return
        frame_captured_at = (
            float(captured_at) if captured_at is not None else time.time()
        )
        if not await self.ensure_ready():
            from deskbot_server.face_snapshot_cache import clear_device_faces

            clear_device_faces(
                self.device_id,
                generation=generation,
            )
            return
        assert self.detector is not None and self.face_tracker is not None

        self.frame_count += 1
        result = await process_camera_jpeg_frame(
            device_id=self.device_id,
            validated_jpeg=validated_jpeg,
            detector=self.detector,
            face_tracker=self.face_tracker,
            runtime=self.runtime,
            image_broker=image_broker,
            frame_source=self.frame_source,
            log_channel="/asr_chat",
            captured_at=frame_captured_at,
            connection_generation=generation,
            publish_undetected=False,
            decoded_rgb=decoded_rgb,
        )
        if self.frame_count == 1 or self.frame_count % 30 == 0:
            logger.info(
                "[/asr_chat] camera_frame device_id=%s frame=%d infer_ms=%.1f faces=%s",
                self.device_id,
                self.frame_count,
                result.infer_ms,
                (result.detect or {}).get("face_count") if result.detected else None,
            )

    def clear_cached_state(self) -> VisionConnectionCleanup | None:
        """Retire this connection and clear only state produced by it."""

        generation = self.connection_generation
        self.connection_generation = None
        if generation is None:
            return None
        from deskbot_server.device_camera_frame_store import (
            retire_device_camera_frame,
        )
        from deskbot_server.face_snapshot_cache import clear_device_faces

        tombstone = retire_device_camera_frame(
            self.device_id,
            generation=generation,
        )
        clear_device_faces(
            self.device_id,
            generation=generation,
        )
        return VisionConnectionCleanup(
            connection_generation=generation,
            tombstone=tombstone,
        )

    async def finish_cached_state_cleanup(
        self,
        image_broker: CameraImageBroker | None,
    ) -> int | None:
        """Clear broker state, then reclaim this connection's tombstone.

        The owning WebSocket handler must cancel and await its camera producer
        tasks before calling this method.
        """

        cleanup = self.clear_cached_state()
        if cleanup is None:
            return None
        if image_broker is not None:
            await image_broker.clear_device(
                self.device_id,
                generation=cleanup.connection_generation,
            )
        if cleanup.tombstone is not None:
            forget_vision_tombstone(cleanup.tombstone)
        return cleanup.connection_generation
