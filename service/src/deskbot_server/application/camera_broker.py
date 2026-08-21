"""摄像头 JPEG 帧广播（订阅 /camera_view）。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Optional

from deskbot_server.llm.vision_input import ValidatedJpeg
from deskbot_server.vision.generation import (
    camera_state_ttl_seconds,
    current_vision_generation,
    is_current_vision_generation,
)
from deskbot_server.vision.geometry import FACE_FRAME_HEIGHT, FACE_FRAME_WIDTH

WsSendFn = Callable


def _snapshot_ttl_seconds() -> float:
    return camera_state_ttl_seconds()


def _build_face_meta_fields(
    *,
    landmarks: Optional[list] = None,
    yaw_deg: Optional[float] = None,
    pitch_deg: Optional[float] = None,
    iris_offsets: Optional[dict] = None,
    face_score: Optional[float] = None,
    frontal_score: Optional[float] = None,
    is_frontal: Optional[bool] = None,
    confidence: Optional[float] = None,
    points: Optional[list] = None,
    faces: Optional[list] = None,
    face_count: Optional[int] = None,
    face_id: Optional[int] = None,
) -> dict[str, Any]:
    """Shared optional face-analysis fields for camera_frame / face_meta."""

    meta: dict[str, Any] = {}
    if landmarks:
        meta["landmarks"] = landmarks
    for key, value in (
        ("yaw_deg", yaw_deg),
        ("pitch_deg", pitch_deg),
        ("face_score", face_score),
        ("frontal_score", frontal_score),
        ("confidence", confidence),
    ):
        if value is not None:
            try:
                meta[key] = float(value)
            except (TypeError, ValueError):
                pass
    if iris_offsets:
        sanitized = {}
        for k, v in iris_offsets.items():
            if v is None:
                continue
            try:
                sanitized[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        if sanitized:
            meta["iris_offsets"] = sanitized
    if is_frontal is not None:
        meta["is_frontal"] = bool(is_frontal)
    if points:
        meta["points"] = points
    for key, value in (("face_count", face_count), ("face_id", face_id)):
        if value is not None:
            try:
                meta[key] = int(value)
            except (TypeError, ValueError):
                pass
    if faces:
        meta["faces"] = faces
    return meta


class CameraImageBroker:
    """JPEG 帧 pub/sub；通过注入的 ``send_fn`` 写出 WebSocket，不绑定 ws 包。"""

    def __init__(self, send_fn: WsSendFn) -> None:
        self._send_fn = send_fn
        self._subscribers: dict = {}
        self._last_by_device: dict = {}
        self._inflight: dict = {}
        self._lock = asyncio.Lock()

    async def _send_pair(self, ws, meta_json: str, frame: bytes) -> None:
        try:
            await self._send_fn(ws, meta_json)
            await self._send_fn(ws, frame)
        except Exception:
            pass

    async def _send_text(self, ws, meta_json: str) -> None:
        try:
            await self._send_fn(ws, meta_json)
        except Exception:
            pass

    async def add_subscriber(self, ws, device_filter: Optional[str] = None) -> None:
        async with self._lock:
            self._subscribers[ws] = device_filter
            if device_filter:
                snap = self._last_by_device.get(device_filter)
                items = [(device_filter, snap)] if snap else []
            else:
                items = list(self._last_by_device.items())
        for _device_id, payload in items:
            if not payload:
                continue
            meta_json, frame, captured_at, generation = payload
            if (
                time.time() - captured_at > _snapshot_ttl_seconds()
                or not is_current_vision_generation(_device_id, generation)
            ):
                async with self._lock:
                    if self._last_by_device.get(_device_id) is payload:
                        self._last_by_device.pop(_device_id, None)
                continue
            prev = self._inflight.get(ws)
            if prev is not None and not prev.done():

                async def _chain(prev_task, ws=ws, mj=meta_json, fr=frame):
                    try:
                        await prev_task
                    except Exception:
                        pass
                    await self._send_pair(ws, mj, fr)

                self._inflight[ws] = asyncio.create_task(_chain(prev))
            else:
                self._inflight[ws] = asyncio.create_task(
                    self._send_pair(ws, meta_json, frame)
                )

    async def remove_subscriber(self, ws) -> None:
        async with self._lock:
            self._subscribers.pop(ws, None)
        task = self._inflight.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()

    async def clear_device(
        self,
        device_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Remove a cached frame without deleting a reconnect's replacement."""

        dev = str(device_id or "").strip()
        if not dev:
            return False
        async with self._lock:
            payload = self._last_by_device.get(dev)
            if payload is None:
                return False
            stored_generation = int(payload[3])
            if generation is not None and stored_generation != int(generation):
                return False
            self._last_by_device.pop(dev, None)
            return True

    async def publish_face_meta(
        self,
        device_id: str,
        *,
        landmarks: Optional[list] = None,
        frame_w: int = FACE_FRAME_WIDTH,
        frame_h: int = FACE_FRAME_HEIGHT,
        yaw_deg: Optional[float] = None,
        pitch_deg: Optional[float] = None,
        iris_offsets: Optional[dict] = None,
        face_score: Optional[float] = None,
        frontal_score: Optional[float] = None,
        is_frontal: Optional[bool] = None,
        confidence: Optional[float] = None,
        points: Optional[list] = None,
        faces: Optional[list] = None,
        face_count: Optional[int] = None,
        face_id: Optional[int] = None,
        captured_at: float | None = None,
        generation: int | None = None,
    ) -> tuple:
        """Send face-detection metadata for an already-published frame.

        The JPEG itself reached subscribers via the ingress
        ``publish_validated`` call; re-sending it after inference would ship
        every detected frame twice.  This follow-up carries only the analysis
        result as a single ``face_meta`` text message (no binary follows).
        ``captured_at`` + ``generation`` are the pairing keys the frontend
        uses to match this metadata to the already-delivered frame.
        Sends chain behind any in-flight frame pair so a text message can
        never interleave between a pair's meta and its JPEG.
        """

        device_id = str(device_id or "unknown")
        frame_captured_at = (
            float(captured_at) if captured_at is not None else time.time()
        )
        effective_generation = int(
            generation
            if generation is not None
            else current_vision_generation(device_id) or 0
        )
        meta: dict = {
            "type": "face_meta",
            "device_id": device_id,
            "detected": True,
            "ts": time.time(),
            "captured_at": frame_captured_at,
            "generation": effective_generation,
            "frame_w": int(frame_w),
            "frame_h": int(frame_h),
        }
        meta.update(
            _build_face_meta_fields(
                landmarks=landmarks,
                yaw_deg=yaw_deg,
                pitch_deg=pitch_deg,
                iris_offsets=iris_offsets,
                face_score=face_score,
                frontal_score=frontal_score,
                is_frontal=is_frontal,
                confidence=confidence,
                points=points,
                faces=faces,
                face_count=face_count,
                face_id=face_id,
            )
        )
        meta_json = json.dumps(meta, ensure_ascii=False)
        async with self._lock:
            if not is_current_vision_generation(
                device_id, effective_generation
            ):
                return 0, 0
            if time.time() - frame_captured_at > _snapshot_ttl_seconds():
                return 0, 0
            targets = [
                ws for ws, flt in self._subscribers.items() if not flt or flt == device_id
            ]
        attempted = len(targets)
        if not targets:
            return 0, 0
        sent = 0
        for ws in targets:
            prev = self._inflight.get(ws)
            if prev is not None and not prev.done():

                async def _chain(prev_task, ws=ws, mj=meta_json):
                    try:
                        await prev_task
                    except Exception:
                        pass
                    await self._send_text(ws, mj)

                self._inflight[ws] = asyncio.create_task(_chain(prev))
            else:
                self._inflight[ws] = asyncio.create_task(
                    self._send_text(ws, meta_json)
                )
            sent += 1
        return sent, attempted

    async def publish_validated(
        self,
        device_id: str,
        validated_jpeg: ValidatedJpeg,
        *,
        detected: Optional[bool] = None,
        landmarks: Optional[list] = None,
        frame_w: int = FACE_FRAME_WIDTH,
        frame_h: int = FACE_FRAME_HEIGHT,
        yaw_deg: Optional[float] = None,
        pitch_deg: Optional[float] = None,
        iris_offsets: Optional[dict] = None,
        face_score: Optional[float] = None,
        frontal_score: Optional[float] = None,
        is_frontal: Optional[bool] = None,
        confidence: Optional[float] = None,
        points: Optional[list] = None,
        faces: Optional[list] = None,
        face_count: Optional[int] = None,
        face_id: Optional[int] = None,
        captured_at: float | None = None,
        generation: int | None = None,
    ) -> tuple:
        """Publish a JPEG already validated at the camera ingress boundary.

        唯一的帧发布入口：原先接收裸 bytes 的 ``publish`` 门面已删除，
        调用方必须先经 ``validate_jpeg`` 校验。
        """

        if not isinstance(validated_jpeg, ValidatedJpeg):
            raise TypeError("validated_jpeg must be a ValidatedJpeg")
        frame = validated_jpeg.data
        device_id = str(device_id or "unknown")
        frame_captured_at = (
            float(captured_at) if captured_at is not None else time.time()
        )
        effective_generation = int(
            generation
            if generation is not None
            else current_vision_generation(device_id) or 0
        )
        if not is_current_vision_generation(device_id, effective_generation):
            return 0, 0
        if time.time() - frame_captured_at > _snapshot_ttl_seconds():
            return 0, 0
        meta = {
            "type": "camera_frame",
            "device_id": device_id,
            "size": len(frame),
            "ts": time.time(),
            "captured_at": frame_captured_at,
            "generation": effective_generation,
            "t_mono": time.monotonic(),
            "frame_w": int(frame_w),
            "frame_h": int(frame_h),
        }
        if detected is not None:
            meta["detected"] = bool(detected)
        meta.update(
            _build_face_meta_fields(
                landmarks=landmarks,
                yaw_deg=yaw_deg,
                pitch_deg=pitch_deg,
                iris_offsets=iris_offsets,
                face_score=face_score,
                frontal_score=frontal_score,
                is_frontal=is_frontal,
                confidence=confidence,
                points=points,
                faces=faces,
                face_count=face_count,
                face_id=face_id,
            )
        )
        meta_json = json.dumps(meta, ensure_ascii=False)
        async with self._lock:
            if not is_current_vision_generation(
                device_id, effective_generation
            ):
                return 0, 0
            if time.time() - frame_captured_at > _snapshot_ttl_seconds():
                return 0, 0
            self._last_by_device[device_id] = (
                meta_json,
                frame,
                frame_captured_at,
                effective_generation,
            )
            targets = [
                ws for ws, flt in self._subscribers.items() if not flt or flt == device_id
            ]
        attempted = len(targets)
        if not targets:
            return 0, 0
        sent = 0
        for ws in targets:
            prev = self._inflight.get(ws)
            if prev is not None and not prev.done():
                continue
            self._inflight[ws] = asyncio.create_task(self._send_pair(ws, meta_json, frame))
            sent += 1
        return sent, attempted

    # 兼容别名：ws/asr_chat.py（语音智能体所有）仍按旧私有名调用，
