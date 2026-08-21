"""Authenticated USB device session handler (control plane only).

媒体面走 ``DeviceSession`` 专用队列直连 ``rtc_runtime``：麦克风音频经
:func:`_consume_usb_audio_media` → :func:`deskbot_server.rtc_runtime.publish_usb_pcm`
进入 RTC，摄像头 JPEG 经 :func:`_consume_usb_camera_media` 进入人脸管线。
本 handler 的主循环只处理控制消息（``user_text`` / ``pb_ack`` /
``audio_vad`` / ``audio_cancel`` / ``ping``）；旧的 WebSocket 兼容媒体
解析与传统 WS 语音轮（flush → Seed-ASR → LLM）已删除。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.application.asr_chat_uplink import (
    AsrChatCameraPipeline,
    LatestCameraInferenceWorker,
)
from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.chat_service import ChatService
from deskbot_server.application.voice_link_feedback import (
    schedule_voice_not_ready_feedback,
)
from deskbot_server.auto_reply import get_asr_voice_auto_reply_enabled
from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter
from deskbot_server.llm.vision_input import (
    VisionImageValidationError,
    validate_jpeg_with_rgb,
)
from deskbot_server.pipeline.audio import AudioConfig, ConnectionSession
from deskbot_server.util import (
    _format_ts,
    _json_msg,
    _new_request_id,
    _normalize_incoming_pb_ack,
    _peer_str,
    format_exc_detail,
)
from deskbot_server.vision.undistort import CameraFaceRuntime
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.chat_turn import publish_ws_chat_turn, run_ws_chat_turn
from deskbot_server.ws.device_pipeline import DevicePipelineBroker
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")

_device_turn_connections: dict[str, tuple[object, list[asyncio.Task]]] = {}


async def _cancel_turn_tasks(tasks: list[asyncio.Task]) -> None:
    pending = [task for task in list(tasks) if not task.done()]
    for task in pending:
        if not task.done():
            task.cancel()
    tasks.clear()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _log_media_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error(
            "[usb_cdc] dedicated media worker ended error=%s",
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )


async def _cancel_device_interaction(
    *,
    session: ConnectionSession,
    turn_tasks: list[asyncio.Task],
    device_id: Optional[str],
    asr_chat_hub: AsrChatHub,
    registry: DeviceRegistry,
) -> None:
    """Cancel uplink, queued work and all device playback as one operation."""

    session.cancel_rom_uplink()
    if not device_id:
        await _cancel_turn_tasks(turn_tasks)
        return
    from deskbot_server.application.turn_arbiter import device_turn_arbiter
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    await asyncio.gather(
        _cancel_turn_tasks(turn_tasks),
        device_turn_arbiter.cancel_device(
            device_id,
            reason="user_audio_cancel",
        ),
    )
    await pb_ack_gate.cancel_device(device_id)
    await asr_chat_hub.cancel_playback(device_id)
    await registry.set_interaction_state(device_id, "IDLE")


def _enqueue_turn(
    turn_task_holder: list[asyncio.Task],
    job_factory: Callable[[], Awaitable[None]],
    *,
    device_id: Optional[str],
    source: str,
) -> asyncio.Task:
    """Submit an interactive turn to the process-wide per-device arbiter."""
    from deskbot_server.application.turn_arbiter import (
        PRIORITY_INTERACTIVE,
        device_turn_arbiter,
    )

    _entry, task = device_turn_arbiter.submit(
        str(device_id or ""),
        job_factory,
        source=source,
        priority=PRIORITY_INTERACTIVE,
        preempt_lower=True,
        preemptible=False,
        replace_group="interactive",
    )
    turn_task_holder.append(task)

    def _discard(done: asyncio.Task) -> None:
        try:
            turn_task_holder.remove(done)
        except ValueError:
            pass

    task.add_done_callback(_discard)
    return task


async def _feed_rom_uplink(
    payload: bytes,
    codec: Optional[str],
    *,
    session: ConnectionSession,
    device_id: Optional[str],
    sample_rate: Optional[int] = None,
    channels: Optional[int] = None,
    opus_frames: Optional[int] = None,
    websocket=None,
    registry: Optional[DeviceRegistry] = None,
) -> None:
    try:
        _utterances, uplink_started, _speech_started = await session.feed_audio(
            payload,
            codec,
            sample_rate=sample_rate,
            channels=channels,
            opus_frames=opus_frames,
        )
    except ValueError as exc:
        logger.warning(
            "[/asr_chat] rejected audio format device_id=%s error=%s",
            device_id,
            exc,
        )
        if websocket is not None:
            await _safe_send(
                websocket,
                _json_msg(
                    {
                        "type": "error",
                        "error": "invalid_audio_format",
                        "message": str(exc),
                    }
                ),
            )
        return
    microphone_health = session.consume_microphone_health_update()
    from deskbot_server.rtc_runtime import publish_usb_pcm

    # Lab「自动应答」总开关：关闭时设备语音不得进入 RTC binding——机器人对
    # 说话完全不理会（不识别、不回答）。上面的解码仍然执行，麦克风健康度
    # 持续更新，且开关翻回 True 后下一帧音频立即恢复上行，无需重连。
    if get_asr_voice_auto_reply_enabled():
        publish_usb_pcm(device_id, getattr(session, "last_decoded_pcm", b""))
    if microphone_health is not None and registry is not None and device_id:
        await registry.set_microphone_health(device_id, microphone_health)
    if uplink_started:
        logger.info(
            "[/asr_chat] 首包 audio device_id=%s payload_bytes=%d codec=%s sr=%s ch=%s",
            device_id,
            len(payload),
            codec,
            sample_rate,
            channels,
        )


# 批龄门与 rtc_runtime 的发布侧上限对齐：静音期 0.75s
# （_PUBLISH_MAX_FRAME_AGE_SEC），说话中放宽到 2.5s
# （_PUBLISH_MAX_FRAME_AGE_SPEECH_SEC）——事件循环短暂卡顿时宁可
# 延迟也不截断一句话中段。
_USB_AUDIO_MAX_AGE_IDLE_SEC = 0.75
_USB_AUDIO_MAX_AGE_SPEECH_SEC = 2.5
_USB_AUDIO_DROP_LOG_INTERVAL_SEC = 2.0


class _DeviceVadGate:
    """最近一次 ESP-SR ``audio_vad`` 状态，供专用媒体消费任务读取。"""

    __slots__ = ("speech_active",)

    def __init__(self) -> None:
        self.speech_active = False


async def _consume_usb_audio_media(
    websocket,
    *,
    session: ConnectionSession,
    device_id: str,
    registry: DeviceRegistry,
    vad_gate: _DeviceVadGate | None = None,
) -> None:
    """Sequentially decode the dedicated low-latency USB media queue."""

    receive = getattr(websocket, "receive_audio_up", None)
    if not callable(receive):
        return
    stale_drop_count = 0
    last_stale_drop_log_mono = 0.0
    while True:
        try:
            batch = await receive()
        except ConnectionError:
            return
        speech_active = bool(vad_gate is not None and vad_gate.speech_active)
        max_age_ms = (
            _USB_AUDIO_MAX_AGE_SPEECH_SEC
            if speech_active
            else _USB_AUDIO_MAX_AGE_IDLE_SEC
        ) * 1000.0
        age_ms = max(0.0, (time.monotonic() - batch.received_mono) * 1000.0)
        if age_ms > max_age_ms:
            stale_drop_count += 1
            now = time.monotonic()
            # 对齐 rtc_runtime._record_publish_drop 语义：丢弃说话中的批
            # 会截断用户的句子，warning 绝不限频；静音期的陈旧批只在首次
            # 与每 2s 打一次并带累计计数。
            if (
                speech_active
                or stale_drop_count == 1
                or now - last_stale_drop_log_mono
                >= _USB_AUDIO_DROP_LOG_INTERVAL_SEC
            ):
                logger.warning(
                    "[RTC uplink] stale %s USB audio skipped device_id=%s "
                    "sequence=%d age_ms=%.1f max_age_ms=%.0f total=%d",
                    "in-speech" if speech_active else "silence",
                    device_id,
                    batch.sequence,
                    age_ms,
                    max_age_ms,
                    stale_drop_count,
                )
                last_stale_drop_log_mono = now
            continue
        await _feed_rom_uplink(
            batch.payload,
            batch.codec,
            session=session,
            device_id=device_id,
            sample_rate=batch.sample_rate,
            channels=batch.channels,
            opus_frames=batch.opus_frames,
            websocket=websocket,
            registry=registry,
        )


async def _consume_usb_camera_media(
    websocket,
    *,
    camera_pipe: AsrChatCameraPipeline | None,
    camera_image_broker: CameraImageBroker | None,
    camera_inference_worker: LatestCameraInferenceWorker,
    device_id: str,
) -> None:
    receive = getattr(websocket, "receive_camera_jpeg", None)
    if not callable(receive):
        return
    while True:
        try:
            frame = await receive()
        except ConnectionError:
            return
        if camera_pipe is None or camera_image_broker is None:
            continue
        accepted = await _schedule_camera_jpeg(
            camera_pipe,
            frame.payload,
            image_broker=camera_image_broker,
            camera_inference_worker=camera_inference_worker,
            device_id=device_id,
        )
        if accepted:
            logger.debug(
                "[/asr_chat] dedicated camera frame queued device_id=%s "
                "sequence=%d bytes=%d",
                device_id,
                frame.sequence,
                len(frame.payload),
            )


async def _schedule_camera_jpeg(
    camera_pipe: AsrChatCameraPipeline,
    frame_bytes: bytes,
    *,
    image_broker: CameraImageBroker,
    camera_inference_worker: LatestCameraInferenceWorker,
    device_id: Optional[str],
) -> bool:
    """后台做人脸推理，避免阻塞 WS 读循环（flush / audio / pb_ack）。"""
    captured_at = time.time()
    generation = camera_pipe.activate_generation()
    if generation is None:
        logger.debug(
            "[/asr_chat] camera frame ignored while authoritative source is active "
            "device_id=%s source=%s",
            device_id,
            camera_pipe.frame_source,
        )
        return False
    try:
        # One decode serves both validation and inference: the RGB array is
        # handed to the face detector so the same JPEG is never decoded twice.
        validated_jpeg, decoded_rgb = await asyncio.to_thread(
            validate_jpeg_with_rgb, frame_bytes
        )
    except VisionImageValidationError:
        return False

    from deskbot_server.device_camera_frame_store import (
        update_device_camera_frame,
    )

    if not update_device_camera_frame(
        camera_pipe.device_id,
        validated_jpeg,
        width=validated_jpeg.width,
        height=validated_jpeg.height,
        source=camera_pipe.frame_source,
        captured_at=captured_at,
        generation=generation,
    ):
        return False

    # Preview the validated ingress frame before detector/model startup. This
    # keeps the Web view live even when face inference is slow or unavailable.
    # It is also the only JPEG delivery for this frame: after inference the
    # pipeline follows up with a lightweight ``face_meta`` message instead of
    # re-sending the same frame (see camera_jpeg_pipeline).
    await image_broker.publish_validated(
        camera_pipe.device_id,
        validated_jpeg,
        frame_w=validated_jpeg.width,
        frame_h=validated_jpeg.height,
        captured_at=captured_at,
        generation=generation,
    )

    async def _job() -> None:
        try:
            await camera_pipe.process_jpeg(
                validated_jpeg,
                generation=generation,
                image_broker=image_broker,
                captured_at=captured_at,
                decoded_rgb=decoded_rgb,
            )
        except Exception:
            logger.exception(
                "[/asr_chat] 后台 camera 推理异常 device_id=%s",
                device_id,
            )

    accepted = camera_inference_worker.submit(_job)
    if accepted and camera_inference_worker.running:
        logger.debug(
            "[/asr_chat] camera inference queued latest-wins device_id=%s bytes=%d",
            device_id,
            len(frame_bytes),
        )
    return accepted


def _normalize_client_request_id(value: object) -> str | None:
    """Accept a bounded opaque idempotency key from text clients."""
    request_id = str(value or "").strip()
    if not request_id or len(request_id) > 64:
        return None
    if not all(
        char.isascii() and (char.isalnum() or char in "-_.:")
        for char in request_id
    ):
        return None
    return request_id


def _schedule_text_turn(
    websocket,
    *,
    pipeline: ChatService,
    text: str,
    request_id: Optional[str] = None,
    device_id: Optional[str],
    dp_broker: DevicePipelineBroker,
    registry: DeviceRegistry,
    asr_chat_hub: AsrChatHub,
    turn_task_holder: list[asyncio.Task],
) -> None:
    """Schedule text input without blocking the websocket ACK reader."""
    request_id = _normalize_client_request_id(request_id) or _new_request_id()
    t_asr_start = time.monotonic()
    t_asr_text = t_asr_start

    async def _job() -> None:
        text_downlink = WsDownlinkAdapter(
            websocket,
            settings=pipeline.settings,
            device_id=device_id,
            dp_broker=dp_broker,
        )
        await text_downlink.emit_stage(
            "asr_done",
            request_id=request_id,
            send_client=False,
            event_fields={"asr_text": text, "asr_ms": 0, "source": "text"},
        )
        flow = await run_ws_chat_turn(
            websocket,
            pipeline,
            text,
            request_id=request_id,
            dp_broker=dp_broker,
            registry=registry,
            device_id=device_id,
            t_asr_start=t_asr_start,
            t_asr_text=t_asr_text,
        )
        await publish_ws_chat_turn(
            dp_broker,
            registry,
            device_id,
            source="text",
            asr_text=text,
            t_asr_start=t_asr_start,
            t_asr_text=t_asr_text,
            flow=flow,
            request_id=request_id,
        )

    _enqueue_turn(
        turn_task_holder,
        _job,
        device_id=device_id,
        source="text",
    )


async def handle_asr_chat(
    websocket,
    pipeline: ChatService,
    audio_cfg: AudioConfig,
    device_id: Optional[str],
    registry: DeviceRegistry,
    dp_broker: DevicePipelineBroker,
    asr_chat_hub: AsrChatHub,
    camera_image_broker: Optional[CameraImageBroker] = None,
    camera_face_runtime: Optional[CameraFaceRuntime] = None,
    *,
    api_key_id: Optional[str] = None,
    registry_channel: str = "usb_cdc",
    registry_transport: str | None = None,
    session_generation: int | None = None,
) -> None:
    """Run one authenticated USB device session (control plane).

    ``websocket`` is a websocket-compatible ``DeviceSession`` adapter.  The
    public device WebSocket routes are disabled; physical USB presence and the
    DBOT hello handshake establish the device identity before this function is
    entered.

    新契约：媒体面走 ``DeviceSession`` 专用队列直连 ``rtc_runtime``
    （音频经 :func:`_consume_usb_audio_media`，摄像头经
    :func:`_consume_usb_camera_media`）；本 handler 的主循环只处理控制
    消息：``user_text`` / ``pb_ack`` / ``audio_vad`` / ``audio_cancel`` /
    ``ping``。
    """
    session = ConnectionSession(pipeline, audio_cfg)
    peer = _peer_str(websocket)
    turn_task_holder: list[asyncio.Task] = []
    camera_inference_worker = LatestCameraInferenceWorker(
        device_id=str(device_id or ""),
    )
    vad_gate = _DeviceVadGate()
    device_vad_last_sequence = 0
    media_tasks: list[asyncio.Task] = []

    camera_pipe: Optional[AsrChatCameraPipeline] = None
    if device_id and camera_face_runtime is not None:
        from deskbot_server.config import load_config
        from deskbot_server.vision.undistort import build_camera_face_runtime

        device_runtime = build_camera_face_runtime(load_config())
        camera_pipe = AsrChatCameraPipeline(
            runtime=device_runtime,
            device_id=device_id,
        )

    if device_id:
        previous = _device_turn_connections.get(device_id)
        if previous is not None and previous[0] is not websocket:
            await _cancel_turn_tasks(previous[1])
            logger.info(
                "[usb_cdc] cancelled turns from replaced connection "
                "device_id=%s",
                device_id,
            )
        _device_turn_connections[device_id] = (websocket, turn_task_holder)
        await registry.connect(
            device_id,
            registry_channel,
            websocket,
            transport=registry_transport or "usb_cdc",
            session_generation=session_generation,
        )
        await registry.set_microphone_health(
            device_id,
            session.microphone_health_snapshot(),
        )
        await asr_chat_hub.attach(device_id, websocket)
        logger.info(
            "[usb_cdc] attached device_id=%s peer=%s",
            device_id,
            peer,
        )
    else:
        raise ValueError("USB session requires a validated device_id")
    try:
        await _safe_send(
            websocket,
            _json_msg(
                {
                    "type": "ready",
                    "device_id": device_id,
                    "connection_mode": "usb_cdc",
                }
            ),
        )
        # DeviceSession provides atomic media queues; media never flows
        # through the control-message loop below.
        if callable(getattr(websocket, "receive_audio_up", None)):
            media_tasks.append(
                asyncio.create_task(
                    _consume_usb_audio_media(
                        websocket,
                        session=session,
                        device_id=device_id,
                        registry=registry,
                        vad_gate=vad_gate,
                    ),
                    name=f"deskbot-usb-audio:{device_id}",
                )
            )
        if callable(getattr(websocket, "receive_camera_jpeg", None)):
            media_tasks.append(
                asyncio.create_task(
                    _consume_usb_camera_media(
                        websocket,
                        camera_pipe=camera_pipe,
                        camera_image_broker=camera_image_broker,
                        camera_inference_worker=camera_inference_worker,
                        device_id=device_id,
                    ),
                    name=f"deskbot-usb-camera:{device_id}",
                )
            )
        for task in media_tasks:
            task.add_done_callback(_log_media_task_result)

        if device_id:
            await registry.set_interaction_state(device_id, "IDLE")

        async for message in websocket:
            try:
                if isinstance(message, (bytes, bytearray)):
                    # Media is consumed by the dedicated DeviceSession queues;
                    # a binary frame on the control loop is a protocol error.
                    logger.warning(
                        "[/asr_chat] unexpected binary on control loop "
                        "device_id=%s bytes=%d（媒体面已走专用队列）",
                        device_id,
                        len(message),
                    )
                    continue

                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "ping":
                    if not getattr(pipeline, "device_pb_only", False):
                        await _safe_send(websocket, _json_msg({"type": "pong"}))
                    continue

                if msg_type == "boot_connect":
                    # Startup display is owned by the USB expression runtime
                    # after the authenticated session is ready. This legacy hint is
                    # intentionally informational only.
                    continue

                if msg_type == "audio_vad":
                    vad_state = str(data.get("state") or "").strip().lower()
                    vad_source = str(data.get("source") or "").strip().lower()
                    try:
                        vad_sequence = int(data.get("sequence"))
                    except (TypeError, ValueError):
                        vad_sequence = 0
                    if (
                        vad_source != "esp_sr"
                        or vad_state not in {"speech_start", "speech_end"}
                        or vad_sequence <= 0
                    ):
                        logger.warning(
                            "[/asr_chat] ignored invalid device VAD "
                            "device_id=%s source=%s state=%s sequence=%s",
                            device_id,
                            vad_source,
                            vad_state,
                            data.get("sequence"),
                        )
                        continue
                    if vad_sequence <= device_vad_last_sequence:
                        logger.info(
                            "[/asr_chat] ignored duplicate/out-of-order ESP-SR VAD "
                            "device_id=%s sequence=%d last_sequence=%d",
                            device_id,
                            vad_sequence,
                            device_vad_last_sequence,
                        )
                        continue
                    device_vad_last_sequence = vad_sequence
                    from deskbot_server.rtc_runtime import (
                        notify_usb_vad,
                        rtc_device_active,
                    )

                    if vad_state == "speech_start":
                        if not get_asr_voice_auto_reply_enabled():
                            # 自动应答关闭：连 listening 表情也不触发，保持
                            # 「完全不理会」的直觉语义。sequence 已在上方推进，
                            # speech_end 分支仍会照常清掉 VAD 状态。
                            logger.info(
                                "[/asr_chat] auto-reply off; ESP-SR "
                                "speech_start ignored device_id=%s sequence=%d",
                                device_id,
                                vad_sequence,
                            )
                            continue
                        notify_usb_vad(device_id, active=True)
                        if device_id and not rtc_device_active(device_id):
                            # RTC 语音链路未就绪（Agent 导入中/网关未装/绑定
                            # 进行中）：给设备一个「语音启动中…」的短表情反馈，
                            # 免得用户说话完全无响应以为设备坏了。
                            # fire-and-forget + 模块内 8s/设备节流，绝不阻塞
                            # VAD 处理路径；勿扰时段不影响本反馈。
                            schedule_voice_not_ready_feedback(device_id)
                        if not vad_gate.speech_active:
                            vad_gate.speech_active = True
                            if device_id:
                                # ESP-SR on the current PDM front-end can emit
                                # short speech_start edges for the playback
                                # tail and quiet room noise.  Treat the device
                                # edge as an early hint only.  The PC Silero
                                # utterance gate is the authority that may
                                # interrupt PB/RTC playback; cancelling here
                                # used to abort a reply milliseconds after its
                                # final I2S write and then wait for a played-ACK
                                # timeout.
                                logger.info(
                                    "[/asr_chat] playback retained on raw "
                                    "ESP-SR VAD edge device_id=%s sequence=%d "
                                    "rtc_active=%s",
                                    device_id,
                                    vad_sequence,
                                    rtc_device_active(device_id),
                                )
                                await registry.set_interaction_state(
                                    device_id,
                                    "LISTENING",
                                )
                    else:
                        notify_usb_vad(device_id, active=False)
                        if vad_gate.speech_active:
                            vad_gate.speech_active = False
                    logger.info(
                        "[/asr_chat] ESP-SR VAD device_id=%s state=%s "
                        "sequence=%s volume_db_x100=%s",
                        device_id,
                        vad_state,
                        vad_sequence,
                        data.get("volume_db_x100"),
                    )
                    continue

                if msg_type == "pb_ack":
                    norm = _normalize_incoming_pb_ack(data)
                    if norm is not None and device_id:
                        await registry.record_pb_ack(device_id, norm)
                        logger.info(
                            "[pb_ack] device_id=%s req=%r idx=%s phase=%s "
                            "error=%r audio_buf_ms=%s display_crc32=%s servo=%s",
                            device_id,
                            norm.get("req"),
                            norm.get("idx"),
                            norm.get("phase"),
                            norm.get("error"),
                            norm.get("audio_buf_ms"),
                            norm.get("display_crc32"),
                            norm.get("servo"),
                        )
                        if dp_broker is not None:
                            now_ts = time.time()
                            await dp_broker.broadcast_to_device(
                                device_id,
                                {
                                    "type": "pipeline_stage",
                                    "event": {
                                        "device_id": device_id,
                                        "request_id": None,
                                        "stage": "pb_ack",
                                        "ack": norm,
                                        "ts": now_ts,
                                        "t_mono": time.monotonic(),
                                        "received_at": _format_ts(now_ts),
                                    },
                                },
                            )
                    elif norm is not None and not device_id:
                        logger.info(
                            "[pb_ack] 已解析但连接无 device_id，未入库 peer=%s",
                            peer,
                        )
                    continue

                if msg_type == "user_text":
                    ut = (data.get("text") or "").strip()
                    if not ut or not pipeline.is_valid_asr_text(ut):
                        continue
                    _schedule_text_turn(
                        websocket,
                        pipeline=pipeline,
                        text=ut,
                        request_id=(
                            data.get("idempotency_key")
                            or data.get("request_id")
                        ),
                        device_id=device_id,
                        dp_broker=dp_broker,
                        registry=registry,
                        asr_chat_hub=asr_chat_hub,
                        turn_task_holder=turn_task_holder,
                    )
                    continue

                if msg_type == "audio_cancel":
                    await _cancel_device_interaction(
                        session=session,
                        turn_tasks=turn_task_holder,
                        device_id=device_id,
                        asr_chat_hub=asr_chat_hub,
                        registry=registry,
                    )
                    continue

            except Exception as exc:
                logger.exception("处理客户端消息失败: %s", format_exc_detail(exc))
    except ConnectionClosed as closed:
        logger.info("WebSocket 已关闭: %s", closed)
    finally:
        for task in media_tasks:
            if not task.done():
                task.cancel()
        if media_tasks:
            await asyncio.gather(*media_tasks, return_exceptions=True)
        close_session = getattr(session, "close", None)
        if callable(close_session):
            await close_session()
        await _cancel_turn_tasks(turn_task_holder)
        await camera_inference_worker.close()
        # The FaceLandmarker is owned by this connection's pipeline; release
        # its native instance even when a newer socket already superseded us.
        # The inference worker is fully stopped above, so nothing uses it.
        if camera_pipe is not None:
            await camera_pipe.close_detector()
        if device_id:
            current = _device_turn_connections.get(device_id)
            connection_is_current = current is None or current[0] is websocket
            if current is not None and current[0] is websocket:
                _device_turn_connections.pop(device_id, None)
            from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

            # A superseded socket commonly reaches ``finally`` after the new
            # one has already attached.  It must not erase the replacement
            # connection's ACK gate or fresh camera/identity state.
            if connection_is_current:
                from deskbot_server.rtc_runtime import notify_usb_vad

                notify_usb_vad(device_id, active=False)
                await pb_ack_gate.cancel_device(device_id)
                if camera_pipe is not None:
                    await camera_pipe.finish_cached_state_cleanup(
                        camera_image_broker,
                    )
            await asr_chat_hub.detach(device_id, websocket)
            await registry.disconnect(websocket)
