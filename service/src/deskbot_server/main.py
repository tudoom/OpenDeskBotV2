from __future__ import annotations

import asyncio
import logging
import os
import signal

import websockets

from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.camera_preview import CameraPreviewLeaseManager
from deskbot_server.application.scheduled_task_scheduler import ScheduledTaskScheduler
from deskbot_server.auth.debug_ws_token import debug_ws_server_options
from deskbot_server.config import load_config
from deskbot_server.constants import CAMERA_VIEW_PATH, DEVICE_PIPELINE_PATH
from deskbot_server.core.concurrency import configure_concurrency
from deskbot_server.core.settings import AppSettings
from deskbot_server.debug_prefs_store import apply_debug_prefs_from_config
from deskbot_server.env import load_dotenv
from deskbot_server.infrastructure.bootstrap import build_chat_service
from deskbot_server.infrastructure.serial.integration import SerialServiceBridge
from deskbot_server.llm.config_state import (
    apply_pending_llm_config,
    watch_llm_config,
)
from deskbot_server.local_livekit import LocalLiveKitServerManager
from deskbot_server.pipeline.audio import AudioConfig
from deskbot_server.rtc_agent_sdk import RtcAgentSdkManager
from deskbot_server.rtc_gateway import DeskbotRtcGateway
from deskbot_server.rtc_runtime import (
    install_rtc_gateway,
    rtc_health_snapshot,
    shutdown_rtc_runtime,
)
from deskbot_server.vision.undistort import build_camera_face_runtime
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.device_pipeline import DevicePipelineBroker
from deskbot_server.ws.http11_compat import patch_websockets_http11_for_rest_api
from deskbot_server.ws.http_api import _build_http_request_handler
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.router import handle_client
from deskbot_server.ws.tls import build_server_tls
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")


def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """Register SIGBREAK (Windows) and SIGTERM (POSIX) graceful shutdown.

    The desktop launcher stops the Core process group with CTRL_BREAK_EVENT,
    which Python surfaces as SIGBREAK.  The contract is the same full
    shutdown sequence KeyboardInterrupt triggers, so the handler only
    resolves the run-forever wait in main() and lets the existing finally
    chain execute.  SIGINT is left untouched to keep Ctrl+C behaviour
    identical.
    """

    def _begin_shutdown(signum: int) -> None:
        logger.info(
            "[server] received signal %s; starting graceful shutdown", signum
        )
        shutdown_event.set()

    def _handler(signum: int, _frame: object) -> None:
        # Signal handlers run outside the event loop's callback context; the
        # loop must only be touched through call_soon_threadsafe here.
        loop.call_soon_threadsafe(_begin_shutdown, signum)

    for name in ("SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except ValueError:
            # signal.signal is main-thread only (e.g. embedded test runs);
            # keep the previous KeyboardInterrupt-driven behaviour there.
            logger.warning(
                "[server] cannot register %s outside the main thread", name
            )


async def _control_operation_retention_loop(
    interval_seconds: float = 600.0,
) -> None:
    """Periodically run control-ledger retention cleanup off the accept path.

    ``maybe_purge_expired_control_operations`` still rate-limits itself to the
    configured cleanup interval (default 1h); this loop only provides the
    trigger so the multi-commit purge never runs inline in an HTTP control
    request, and its SQLite work always happens on a worker thread.
    """
    from deskbot_server.application.control_operations import (
        maybe_purge_expired_control_operations,
    )

    while True:
        try:
            removed = await asyncio.to_thread(
                maybe_purge_expired_control_operations,
            )
            if removed:
                logger.info(
                    "[server] purged %d expired control operation rows", removed
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[server] control operation retention pass failed")
        await asyncio.sleep(interval_seconds)


async def main():
    load_dotenv()
    try:
        config_status = apply_pending_llm_config()
        logger.info(
            "[config] initial realtime LLM configuration revision=%d status=%s",
            config_status["revision"],
            config_status["status"],
        )
    except Exception:
        # Keep the service available with its previous process environment; the
        # watcher retries and the console continues to report the revision as
        # pending rather than falsely acknowledging it.
        logger.exception("[config] initial LLM configuration reload failed")
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import default_db_path
    from deskbot_server.device_data import ensure_local_data_initialized

    init_database()
    ensure_local_data_initialized()
    logger.info("[server] local state DB ready path=%s", default_db_path())
    config = load_config(os.environ.get("DESKBOT_SERVER_CONFIG", "config.yaml"))
    apply_debug_prefs_from_config(config)
    app_settings = AppSettings.from_config(config)
    install_rtc_gateway(None)
    rtc_agent_sdk = RtcAgentSdkManager(app_settings)
    local_livekit = LocalLiveKitServerManager(app_settings)
    audio_cfg = AudioConfig(
        input_codec=app_settings.audio.input_codec,
        sample_rate=app_settings.audio.sample_rate,
        channels=app_settings.audio.channels,
    )
    logger.info(
        "[RTC AUDIO] codec=%s sample_rate=%d channels=%d | "
        "asr_text_filter: min_text_len=%s min_chinese_ratio=%s",
        audio_cfg.input_codec,
        audio_cfg.sample_rate,
        audio_cfg.channels,
        config.get("asr", {}).get("text_filter", {}).get("min_text_len"),
        config.get("asr", {}).get("text_filter", {}).get("min_chinese_ratio"),
    )
    configure_concurrency(
        max_concurrent_asr=app_settings.server.max_concurrent_asr,
        max_concurrent_face_infer=app_settings.server.max_concurrent_face_infer,
    )
    pipeline = build_chat_service(config)
    device_pipeline_broker = DevicePipelineBroker()
    registry = DeviceRegistry()
    asr_chat_hub = AsrChatHub(
        device_pb_only=pipeline.device_pb_only,
        pipeline_broker=device_pipeline_broker,
    )
    camera_preview_leases = CameraPreviewLeaseManager(asr_chat_hub)
    camera_image_broker = CameraImageBroker(send_fn=_safe_send)
    camera_face_runtime = build_camera_face_runtime(config)

    host = app_settings.server.host
    port = app_settings.server.port
    server_tls = build_server_tls(host)
    rtc_agent_sdk.configure_tool_bridge(
        scheme=server_tls.scheme,
        port=port,
    )
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(
        lambda _loop, context: logger.error(
            "未捕获事件循环异常: %s",
            context.get("message", "unknown"),
            exc_info=context.get("exception"),
        )
    )
    shutdown_event = asyncio.Event()
    _install_shutdown_signal_handlers(loop, shutdown_event)
    # These keepalive settings apply only to browser-facing debug subscriber
    # WebSockets. Device traffic is carried exclusively by USB CDC.
    ping_interval = app_settings.server.ws_ping_interval
    if ping_interval is not None:
        ping_interval = int(max(5, ping_interval))

    ping_timeout = int(max(5, app_settings.server.ws_ping_timeout))

    patch_websockets_http11_for_rest_api()

    http_handler = _build_http_request_handler(
        device_pipeline_broker,
        registry,
        asr_chat_hub=asr_chat_hub,
        chat=pipeline,
        camera_image_broker=camera_image_broker,
        rtc_tool_token=rtc_agent_sdk.tool_bridge_token,
        rtc_status_provider=lambda: {
            "server": local_livekit.health_snapshot(),
            "agent": rtc_agent_sdk.health_snapshot(),
            **rtc_health_snapshot(),
        },
    )

    scheduler = ScheduledTaskScheduler(
        chat=pipeline,
        asr_chat_hub=asr_chat_hub,
        registry=registry,
        dp_broker=device_pipeline_broker,
    )
    serial_bridge = SerialServiceBridge(
        registry,
        asr_chat_hub,
        pipeline=pipeline,
        audio_cfg=audio_cfg,
        dp_broker=device_pipeline_broker,
        camera_image_broker=camera_image_broker,
        camera_face_runtime=camera_face_runtime,
    )

    async def _start_rtc_stack() -> None:
        # Cold-start imports inside the Agent SDK can exceed 20-30s on
        # Windows (LiveKit/AV/Silero), and wait_ready allows up to 120s.
        # Bringing the RTC stack up in the background lets the USB serial
        # bridge, the scheduler and the HTTP/WS endpoint start immediately.
        # Devices attached before the gateway exists recover on their own:
        # the RTC binding path retries until the gateway is installed.
        livekit_ready = await local_livekit.start()
        if local_livekit.enabled and local_livekit.credentials is not None:
            rtc_agent_sdk.configure_local_livekit(
                api_key=local_livekit.credentials.api_key,
                api_secret=local_livekit.credentials.api_secret,
            )
        if local_livekit.enabled and not livekit_ready:
            logger.error(
                "[rtc-local] initial LiveKit startup failed; background recovery active"
            )
        # When the SFU is temporarily unavailable, LampGo's own background
        # recovery runs in parallel and succeeds once the local server returns.
        await rtc_agent_sdk.start()
        if app_settings.rtc.enabled:
            install_rtc_gateway(
                DeskbotRtcGateway(
                    app_settings.rtc,
                    token_endpoint=rtc_agent_sdk.token_endpoint,
                )
            )
            logger.info("[rtc] RTC gateway installed (background startup)")
        else:
            from deskbot_server.rtc_runtime import mark_rtc_gateway_unavailable

            mark_rtc_gateway_unavailable()

    def _rtc_startup_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # The service keeps running without RTC, matching the previous
            # start-failure semantics; SDK/SFU recovery loops stay active.
            logger.error(
                "[rtc] background RTC startup failed: %s",
                error,
                exc_info=error,
            )

    llm_config_watcher = asyncio.create_task(
        watch_llm_config(),
        name="llm-config-watcher",
    )
    control_op_retention = asyncio.create_task(
        _control_operation_retention_loop(),
        name="control-operation-retention",
    )
    rtc_startup_task = asyncio.create_task(
        _start_rtc_stack(),
        name="rtc-startup",
    )
    rtc_startup_task.add_done_callback(_rtc_startup_done)
    try:
        await serial_bridge.start()
        scheduler.start()
        logger.info(
            "deskbot-server started on %s://%s:%s "
            "(device_transport=usb_cdc; browser_debug_ws=%s,%s?role=subscriber; "
            "ping_interval=%s ping_timeout=%s tls_terminated_by_proxy=%s)",
            server_tls.scheme,
            host,
            port,
            CAMERA_VIEW_PATH,
            DEVICE_PIPELINE_PATH,
            ping_interval,
            ping_timeout,
            server_tls.terminated_by_proxy,
        )
        async with websockets.serve(
            lambda ws: handle_client(
                ws,
                pipeline,
                audio_cfg,
                device_pipeline_broker,
                registry,
                asr_chat_hub,
                camera_image_broker,
                camera_face_runtime,
                camera_preview_leases,
            ),
            host,
            port,
            max_size=None,
            max_queue=128,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            process_request=http_handler,
            ssl=server_tls.context,
            **debug_ws_server_options(),
        ):
            # Resolved by SIGBREAK/SIGTERM for a graceful shutdown;
            # KeyboardInterrupt still interrupts the wait directly.
            await shutdown_event.wait()
    finally:
        try:
            # Stop the background RTC bring-up first so the gateway cannot be
            # installed while the shutdown sequence below is running.
            rtc_startup_task.cancel()
            await asyncio.gather(rtc_startup_task, return_exceptions=True)
        finally:
            try:
                await scheduler.stop()
            finally:
                try:
                    await camera_preview_leases.close()
                finally:
                    try:
                        await serial_bridge.stop()
                    finally:
                        try:
                            await shutdown_rtc_runtime()
                        finally:
                            # Each stop below must run even if the previous one
                            # raises: an rtc_agent_sdk.stop failure must not
                            # leave the local LiveKit child running, and a
                            # local_livekit.stop failure must not leak the
                            # watcher/retention tasks.
                            try:
                                await rtc_agent_sdk.stop()
                            finally:
                                try:
                                    await local_livekit.stop()
                                finally:
                                    llm_config_watcher.cancel()
                                    control_op_retention.cancel()
                                    await asyncio.gather(
                                        llm_config_watcher,
                                        control_op_retention,
                                        return_exceptions=True,
                                    )
