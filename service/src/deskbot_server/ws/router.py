from __future__ import annotations

import logging

from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.camera_preview import CameraPreviewLeaseManager
from deskbot_server.application.chat_service import ChatService
from deskbot_server.constants import (
    CAMERA_UPLINK_PATH,
    CAMERA_VIEW_PATH,
    DEVICE_PIPELINE_PATH,
)
from deskbot_server.pipeline.audio import AudioConfig
from deskbot_server.util import (
    _extract_device_id,
    _json_msg,
    _parse_query,
    _peer_str,
    _split_path,
    _ws_request_path,
)
from deskbot_server.vision.undistort import CameraFaceRuntime
from deskbot_server.ws.api_key_gate import (
    run_with_debug_subscriber_auth_watchdog,
    ws_require_debug_subscriber_auth,
)
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.camera import handle_camera_view
from deskbot_server.ws.device_pipeline import DevicePipelineBroker, handle_device_pipeline
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")

_LEGACY_CAMERA_PATH = "/camera"
_LEGACY_DEVICE_PATH = "/asr_chat"


async def _reject_device_network_transport(
    websocket,
    *,
    path: str,
    peer: str,
) -> None:
    logger.warning(
        "[WS] rejected device network transport peer=%s path=%s; "
        "physical devices must use USB CDC",
        peer,
        path,
    )
    await _safe_send(
        websocket,
        _json_msg(
            {
                "type": "error",
                "error": "usb_cdc_required",
                "message": "设备网络连接已停用，请通过 USB 连接机器人与 PC 服务",
            }
        ),
    )
    await websocket.close(
        code=1008,
        reason="physical devices must use USB CDC",
    )


async def handle_client(
    websocket,
    pipeline: ChatService,
    audio_cfg: AudioConfig,
    device_pipeline_broker: DevicePipelineBroker,
    registry: DeviceRegistry,
    asr_chat_hub: AsrChatHub,
    camera_image_broker: CameraImageBroker,
    camera_face_runtime: CameraFaceRuntime,
    camera_preview_leases: CameraPreviewLeaseManager | None = None,
):
    raw_path = _ws_request_path(websocket)
    path_only, query = _split_path(raw_path)
    peer = _peer_str(websocket)
    # Legacy clients may put API keys/debug tokens in the query string.
    # Log only the route so credentials never reach application logs.
    logger.info("[WS] 收到连接 peer=%s path=%s", peer, path_only)

    if path_only in (_LEGACY_DEVICE_PATH, _LEGACY_CAMERA_PATH):
        await _reject_device_network_transport(
            websocket,
            path=path_only,
            peer=peer,
        )
        return

    qargs = _parse_query(query)

    if path_only == CAMERA_VIEW_PATH:
        device_id = _extract_device_id(qargs)
        subscriber_auth = await ws_require_debug_subscriber_auth(
            websocket,
            qargs,
            device_id=device_id,
            require_device=True,
        )
        if subscriber_auth is None:
            return
        await registry.add_observer(device_id or "", websocket)
        try:
            await run_with_debug_subscriber_auth_watchdog(
                websocket,
                subscriber_auth,
                handle_camera_view(
                    websocket,
                    camera_image_broker,
                    camera_preview_leases,
                ),
            )
        finally:
            await registry.remove_observer(websocket)
        return

    if path_only == CAMERA_UPLINK_PATH:
        await _reject_device_network_transport(
            websocket,
            path=path_only,
            peer=peer,
        )
        return

    if path_only == DEVICE_PIPELINE_PATH:
        role = (qargs.get("role") or "").lower()
        is_subscriber = role in ("subscriber", "sub", "viewer", "consumer")
        if is_subscriber:
            device_id = _extract_device_id(qargs)
            subscriber_auth = await ws_require_debug_subscriber_auth(
                websocket,
                qargs,
                device_id=device_id,
                require_device=True,
            )
            if subscriber_auth is None:
                return
        else:
            await _reject_device_network_transport(
                websocket,
                path=path_only,
                peer=peer,
            )
            return
        if is_subscriber:
            await registry.add_observer(device_id or "", websocket)
            try:
                await run_with_debug_subscriber_auth_watchdog(
                    websocket,
                    subscriber_auth,
                    handle_device_pipeline(
                        websocket,
                        device_pipeline_broker,
                    ),
                )
            finally:
                await registry.remove_observer(websocket)
        return

    if path_only:
        logger.warning(
            "[WS] 拒绝非法路径 peer=%s path=%s "
            "(允许 camera_view=%s, device_pipeline subscriber=%s)",
            peer,
            path_only,
            CAMERA_VIEW_PATH,
            DEVICE_PIPELINE_PATH,
        )
        await websocket.close(code=1008, reason=f"unsupported path: {path_only}")
        return

    await _reject_device_network_transport(
        websocket,
        path=path_only or _LEGACY_DEVICE_PATH,
        peer=peer,
    )
