"""独立 /camera_uplink WebSocket：仅接收 camera_frame base64 JSON，与 /asr_chat 分离。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.application.asr_chat_uplink import (
    AsrChatCameraPipeline,
    LatestCameraInferenceWorker,
)
from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.config import load_config
from deskbot_server.util import _json_msg, _peer_str
from deskbot_server.vision.undistort import CameraFaceRuntime, build_camera_face_runtime
from deskbot_server.ws.api_key_gate import enforce_turn_usage
from deskbot_server.ws.asr_chat import _schedule_camera_jpeg
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.device_pipeline import DevicePipelineBroker
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")

_active_camera_uplink: dict[str, object] = {}
_active_camera_uplink_lock = asyncio.Lock()
_MAX_CAMERA_JPEG_BYTES = 512 * 1024
_MAX_CAMERA_B64_CHARS = ((_MAX_CAMERA_JPEG_BYTES + 2) // 3) * 4


def _append_camera_binary(
    buf: bytearray,
    chunk: bytes,
) -> tuple[list[bytes], bool]:
    """Append one connection's bytes and drain every complete JPEG."""

    if not chunk:
        return [], False
    buf.extend(chunk)
    frames: list[bytes] = []
    overflowed = False
    while buf:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            if len(buf) > _MAX_CAMERA_JPEG_BYTES:
                trailing_ff = bool(buf and buf[-1] == 0xFF)
                buf.clear()
                if trailing_ff:
                    buf.append(0xFF)
                overflowed = True
            break
        if start:
            del buf[:start]
        end = buf.find(b"\xff\xd9", 2)
        if end < 0:
            if len(buf) > _MAX_CAMERA_JPEG_BYTES:
                buf.clear()
                overflowed = True
            break
        frame_len = end + 2
        if frame_len <= _MAX_CAMERA_JPEG_BYTES:
            frames.append(bytes(buf[:frame_len]))
        else:
            overflowed = True
        del buf[:frame_len]
    return frames, overflowed


async def _supersede_camera_uplink(device_id: str, websocket) -> None:
    async with _active_camera_uplink_lock:
        prev = _active_camera_uplink.get(device_id)
        _active_camera_uplink[device_id] = websocket
    if prev is None or prev is websocket:
        return
    logger.info(
        "[/camera_uplink] 关闭旧连接 device_id=%s (新 peer 接入)",
        device_id,
    )
    try:
        await prev.close(code=1000, reason="superseded by new camera_uplink")
    except Exception:
        logger.warning(
            "[/camera_uplink] 旧连接 close 异常 device_id=%s",
            device_id,
            exc_info=True,
        )


async def _ingest_camera_frame(
    *,
    websocket,
    payload: bytes,
    enc: str,
    device_id: Optional[str],
    camera_pipe: Optional[AsrChatCameraPipeline],
    camera_image_broker: CameraImageBroker,
    dp_broker: DevicePipelineBroker,
    asr_chat_hub: AsrChatHub,
    camera_inference_worker: LatestCameraInferenceWorker,
    api_key_id: Optional[str],
) -> None:
    accepted = False
    if camera_pipe is not None:
        if api_key_id:
            if not await enforce_turn_usage(
                websocket,
                api_key_id,
                device_id=device_id,
                face_bytes=len(payload),
            ):
                return
        accepted = await _schedule_camera_jpeg(
            camera_pipe,
            payload,
            image_broker=camera_image_broker,
            camera_inference_worker=camera_inference_worker,
            device_id=device_id,
        )
    if accepted:
        logger.info(
            "[/camera_uplink] camera_frame ok device_id=%s bytes=%d enc=%s",
            device_id,
            len(payload),
            enc,
        )
    else:
        logger.warning(
            "[/camera_uplink] camera_frame rejected "
            "device_id=%s bytes=%d enc=%s",
            device_id,
            len(payload),
            enc,
        )


async def handle_camera_uplink(
    websocket,
    device_id: Optional[str],
    registry: DeviceRegistry,
    dp_broker: DevicePipelineBroker,
    asr_chat_hub: AsrChatHub,
    camera_image_broker: CameraImageBroker,
    camera_face_runtime: CameraFaceRuntime,
    *,
    api_key_id: Optional[str] = None,
) -> None:
    peer = _peer_str(websocket)
    camera_inference_worker = LatestCameraInferenceWorker(
        device_id=str(device_id or ""),
    )
    camera_binary_buffer = bytearray()
    camera_pipe: Optional[AsrChatCameraPipeline] = None
    if device_id and camera_face_runtime is not None:
        device_runtime = build_camera_face_runtime(load_config())
        camera_pipe = AsrChatCameraPipeline(
            runtime=device_runtime,
            device_id=device_id,
            frame_source="camera_uplink",
        )
        camera_pipe.activate_generation()

    if device_id:
        await _supersede_camera_uplink(device_id, websocket)
        await registry.connect(device_id, "camera_uplink", websocket)
        logger.info(
            "[/camera_uplink] 接入 device_id=%s peer=%s (独立相机 WS，不经 asr_chat_hub)",
            device_id,
            peer,
        )
    else:
        logger.warning(
            "[/camera_uplink] 缺失 device_id peer=%s —— 帧不入库",
            peer,
        )

    try:
        await _safe_send(
            websocket,
            _json_msg(
                {
                    "type": "ready",
                    "channel": "camera_uplink",
                    "device_id": device_id,
                    "accept_binary_jpeg": True,
                }
            ),
        )

        async for message in websocket:
            frames: list[tuple[bytes, str]] = []
            if isinstance(message, (bytes, bytearray)):
                chunk = bytes(message)
                if not chunk or not device_id:
                    continue
                payloads, overflowed = _append_camera_binary(
                    camera_binary_buffer,
                    chunk,
                )
                if overflowed:
                    logger.warning(
                        "[/camera_uplink] camera binary exceeded %d bytes "
                        "device_id=%s",
                        _MAX_CAMERA_JPEG_BYTES,
                        device_id,
                    )
                    await _safe_send(
                        websocket,
                        _json_msg(
                            {
                                "type": "error",
                                "code": "camera_frame_too_large",
                                "max_bytes": _MAX_CAMERA_JPEG_BYTES,
                            }
                        ),
                    )
                frames.extend((payload, "binary") for payload in payloads)
            else:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("[/camera_uplink] device_id=%s JSON 解析失败", device_id)
                    continue

                msg_type = data.get("type")
                if msg_type == "ping":
                    await _safe_send(websocket, _json_msg({"type": "pong"}))
                    continue

                if msg_type != "camera_frame":
                    continue

                raw_b64 = data.get("data")
                if not isinstance(raw_b64, str) or not raw_b64:
                    logger.warning(
                        "[/camera_uplink] camera_frame 缺少 data device_id=%s",
                        device_id,
                    )
                    continue
                if len(raw_b64) > _MAX_CAMERA_B64_CHARS:
                    await _safe_send(
                        websocket,
                        _json_msg(
                            {
                                "type": "error",
                                "code": "camera_frame_too_large",
                                "max_bytes": _MAX_CAMERA_JPEG_BYTES,
                            }
                        ),
                    )
                    continue
                try:
                    payload = base64.b64decode(raw_b64, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    logger.warning(
                        "[/camera_uplink] camera_frame base64 解码失败 device_id=%s",
                        device_id,
                    )
                    continue
                frames.append((payload, "base64"))

            if device_id:
                for payload, enc in frames:
                    if len(payload) < 64:
                        logger.warning(
                            "[/camera_uplink] device_id=%s JPEG 过短 bytes=%d",
                            device_id,
                            len(payload),
                        )
                        continue
                    await _ingest_camera_frame(
                        websocket=websocket,
                        payload=payload,
                        enc=enc,
                        device_id=device_id,
                        camera_pipe=camera_pipe,
                        camera_image_broker=camera_image_broker,
                        dp_broker=dp_broker,
                        asr_chat_hub=asr_chat_hub,
                        camera_inference_worker=camera_inference_worker,
                        api_key_id=api_key_id,
                    )

    except ConnectionClosed:
        logger.info("[/camera_uplink] 连接关闭 device_id=%s peer=%s", device_id, peer)
    finally:
        await camera_inference_worker.close()
        async with _active_camera_uplink_lock:
            connection_is_current = bool(
                device_id and _active_camera_uplink.get(device_id) is websocket
            )
            if connection_is_current:
                _active_camera_uplink.pop(device_id, None)
        if connection_is_current:
            if camera_pipe is not None:
                await camera_pipe.finish_cached_state_cleanup(
                    camera_image_broker,
                )
        if device_id:
            await registry.disconnect(websocket)
