"""Run the existing ASR chat handler over a framed USB CDC session."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from deskbot_server.hardware_catalog import ensure_local_device
from deskbot_server.infrastructure.serial.manager import (
    SerialDeviceManager,
    SerialManagerConfig,
)
from deskbot_server.infrastructure.serial.protocol import Frame
from deskbot_server.infrastructure.serial.session import DeviceSession, HelloInfo
from deskbot_server.pb.mic_signal import build_mic_signal_pb
from deskbot_server.ws.asr_chat import handle_asr_chat

if TYPE_CHECKING:
    from deskbot_server.application.camera_broker import CameraImageBroker
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.pipeline.audio import AudioConfig
    from deskbot_server.vision.undistort import CameraFaceRuntime
    from deskbot_server.ws.asr_chat_hub import AsrChatHub
    from deskbot_server.ws.device_pipeline import DevicePipelineBroker
    from deskbot_server.ws.registry import DeviceRegistry

logger = logging.getLogger("deskbot-server")


class SerialServiceBridge:
    """Bind hello-authenticated USB sessions to the proven WS message loop."""

    def __init__(
        self,
        registry: "DeviceRegistry",
        asr_chat_hub: "AsrChatHub",
        *,
        pipeline: "ChatService",
        audio_cfg: "AudioConfig",
        dp_broker: "DevicePipelineBroker",
        camera_image_broker: "CameraImageBroker | None" = None,
        camera_face_runtime: "CameraFaceRuntime | None" = None,
        config: SerialManagerConfig | None = None,
        manager: SerialDeviceManager | None = None,
    ) -> None:
        self._registry = registry
        self._hub = asr_chat_hub
        self._pipeline = pipeline
        self._audio_cfg = audio_cfg
        self._dp_broker = dp_broker
        self._camera_image_broker = camera_image_broker
        self._camera_face_runtime = camera_face_runtime
        self._handlers: dict[DeviceSession, asyncio.Task] = {}
        self._rtc_bind_tasks: dict[DeviceSession, asyncio.Task] = {}
        self._catalog_tasks: dict[DeviceSession, asyncio.Task] = {}
        self._handler_reapers: set[asyncio.Task] = set()
        self.manager = manager or SerialDeviceManager(config)
        self.manager.set_callbacks(
            on_ready=self._on_ready,
            # DeviceSession itself translates validated frames into the
            # websocket-like ingress iterator consumed by handle_asr_chat.
            on_frame=self._on_frame,
            on_closed=self._on_closed,
        )

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()
        catalog_tasks = list(self._catalog_tasks.values())
        for task in catalog_tasks:
            if not task.done():
                task.cancel()
        if catalog_tasks:
            await asyncio.gather(*catalog_tasks, return_exceptions=True)
        self._catalog_tasks.clear()
        rtc_tasks = list(self._rtc_bind_tasks.values())
        for task in rtc_tasks:
            if not task.done():
                task.cancel()
        if rtc_tasks:
            await asyncio.gather(*rtc_tasks, return_exceptions=True)
        self._rtc_bind_tasks.clear()
        tasks = list(self._handlers.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._handlers.clear()
        reapers = list(self._handler_reapers)
        for task in reapers:
            if not task.done():
                task.cancel()
        if reapers:
            await asyncio.gather(*reapers, return_exceptions=True)
        self._handler_reapers.clear()

    async def _on_ready(
        self,
        session: DeviceSession,
        hello: HelloInfo,
    ) -> None:
        catalog_task = asyncio.create_task(
            asyncio.to_thread(ensure_local_device, hello.device_id),
            name=(
                f"deskbot-usb-catalog:{hello.device_id}:"
                f"{session.generation}"
            ),
        )
        self._catalog_tasks[session] = catalog_task
        catalog_task.add_done_callback(
            lambda completed, bound=session: self._catalog_done(
                bound,
                completed,
            )
        )
        handler = asyncio.create_task(
            handle_asr_chat(
                session,
                self._pipeline,
                self._audio_cfg,
                hello.device_id,
                self._registry,
                self._dp_broker,
                self._hub,
                self._camera_image_broker,
                self._camera_face_runtime,
                api_key_id=None,
                registry_channel="usb_cdc",
                registry_transport="usb_cdc",
                session_generation=session.generation,
            ),
            name=(
                f"deskbot-usb-asr:{hello.device_id}:"
                f"{session.generation}"
            ),
        )
        self._handlers[session] = handler
        handler.add_done_callback(
            lambda completed, bound=session: self._handler_done(
                bound,
                completed,
            )
        )
        # DeviceSession invokes this callback from its only framed RX loop.
        # The mic reset is an ACK-required PB declaration, so awaiting it here
        # deadlocks: the device sends the ACK, but this same callback prevents
        # the RX loop from consuming it.  Keep every operation that can wait
        # for inbound USB data (and the slower RTC bind) in a sibling task.
        rtc_task = asyncio.create_task(
            self._reset_mic_and_bind_rtc(session, hello),
            name=(
                f"deskbot-usb-session-bootstrap:{hello.device_id}:"
                f"{session.generation}"
            ),
        )
        self._rtc_bind_tasks[session] = rtc_task
        rtc_task.add_done_callback(
            lambda completed, bound=session: self._rtc_bind_done(
                bound,
                completed,
            )
        )

    async def _reset_mic_and_bind_rtc(
        self,
        session: DeviceSession,
        hello: HelloInfo,
    ) -> None:
        # A prior half-duplex media transfer can lose its best-effort
        # ``mic=open`` cleanup if USB disconnects mid-declaration.  Firmware
        # intentionally preserves the explicit mic preference across transport
        # epochs, so reset it on every newly authenticated local session.
        try:
            await session.send(
                json.dumps(
                    build_mic_signal_pb(mic="open"),
                    separators=(",", ":"),
                )
            )
        except Exception:
            logger.warning(
                "[usb_cdc] initial mic-open reset failed device_id=%s "
                "generation=%d",
                hello.device_id,
                session.generation,
                exc_info=True,
            )
            if session.is_closed:
                return

        if session.is_closed or not session.is_ready:
            return

        # This call first attaches the USB-owned expression arbiter, then starts
        # RTC when enabled/capable. Token lookup and room connection can take
        # seconds, so all of it remains outside DeviceSession's RX loop.
        await self._bind_rtc(session, hello)

    def _catalog_done(
        self,
        session: DeviceSession,
        task: asyncio.Task,
    ) -> None:
        if self._catalog_tasks.get(session) is task:
            self._catalog_tasks.pop(session, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # The in-memory transport registry is already live. A transient
            # catalogue/database failure must not disable real-time voice.
            logger.warning(
                "[usb_cdc] local catalogue update failed; RTC retained "
                "device_id=%s generation=%d",
                session.device_id or "",
                session.generation,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _bind_rtc(
        self,
        session: DeviceSession,
        hello: HelloInfo,
    ) -> None:
        from deskbot_server.rtc_runtime import bind_usb_device

        await bind_usb_device(
            hello.device_id,
            session,
            aec="audio_aec" in hello.capabilities,
            ns="audio_ns" in hello.capabilities,
            vad="audio_vad_esp_sr" in hello.capabilities,
            full_duplex="rtc_full_duplex" in hello.capabilities,
            gateway_capable=(
                "rtc_audio_gateway" in hello.capabilities
                and "audio_down_opus" in hello.capabilities
            ),
        )

    def _rtc_bind_done(
        self,
        session: DeviceSession,
        task: asyncio.Task,
    ) -> None:
        if self._rtc_bind_tasks.get(session) is task:
            self._rtc_bind_tasks.pop(session, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[usb_cdc] RTC bind failed device_id=%s generation=%d",
                session.device_id or "",
                session.generation,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _on_frame(
        self,
        session: DeviceSession,
        frame: Frame,
    ) -> None:
        # Translation into legacy str/bytes messages happens in DeviceSession.
        # This callback intentionally remains side-effect free so PB ACKs and
        # state transitions are processed exactly once by handle_asr_chat.
        del session, frame

    def _handler_done(
        self,
        session: DeviceSession,
        task: asyncio.Task,
    ) -> None:
        if self._handlers.get(session) is task:
            self._handlers.pop(session, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[usb_cdc] ASR handler failed device_id=%s generation=%d",
                session.device_id or "",
                session.generation,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )
        if not session.is_closed:
            asyncio.create_task(
                session.close(reason="USB ASR handler ended"),
                name=f"deskbot-usb-handler-close:{session.generation}",
            )

    async def _reap_closed_handler(
        self,
        session: DeviceSession,
        task: asyncio.Task,
    ) -> None:
        """Let a closed session's handler unwind without blocking ``close``.

        A PB worker can be awaiting ``DeviceSession.send`` at the instant that
        the frame ACK times out.  That send performs ``session.close()``, whose
        callback lands here, while the ASR handler's ``finally`` waits for the
        same PB worker.  Waiting for the handler inside the close callback
        therefore creates a task cycle::

            PB worker -> session close -> ASR handler -> PB worker

        ``Task.cancel()`` recursively follows awaited tasks, so a timeout on
        any edge of that cycle raises ``RecursionError`` and can strand the
        originating control operation in ``running``.  Reap the handler in a
        sibling task so ``session.close()`` can finish and release the worker.
        """

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except TimeoutError:
            # Never cancel this handler from the close callback's sibling.
            # Task.cancel() recursively follows the handler -> PB worker ->
            # session-close wait chain and was the remaining path to Python's
            # recursion limit after a device watchdog reset. Once close has
            # completed, normal I/O failure unwinds the handler on its own.
            logger.warning(
                "[usb_cdc] handler still unwinding after close "
                "device_id=%s generation=%d",
                session.device_id or "",
                session.generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The handler's done callback logs the original exception.
            pass

    def _schedule_closed_handler_reaper(
        self,
        session: DeviceSession,
        task: asyncio.Task,
    ) -> None:
        reaper = asyncio.create_task(
            self._reap_closed_handler(session, task),
            name=(
                f"deskbot-usb-handler-reaper:{session.device_id or 'unknown'}:"
                f"{session.generation}"
            ),
        )
        self._handler_reapers.add(reaper)
        reaper.add_done_callback(self._handler_reapers.discard)

    async def _on_closed(
        self,
        session: DeviceSession,
        error: BaseException | None,
    ) -> None:
        from deskbot_server.rtc_runtime import unbind_usb_device

        rtc_task = self._rtc_bind_tasks.pop(session, None)
        if rtc_task is not None and rtc_task is not asyncio.current_task():
            if not rtc_task.done():
                rtc_task.cancel()
            await asyncio.gather(rtc_task, return_exceptions=True)
        catalog_task = self._catalog_tasks.pop(session, None)
        if catalog_task is not None and not catalog_task.done():
            catalog_task.cancel()
            await asyncio.gather(catalog_task, return_exceptions=True)
        if session.device_id:
            await unbind_usb_device(session.device_id, session)
        task = self._handlers.pop(session, None)
        if task is not None and task is not asyncio.current_task():
            self._schedule_closed_handler_reaper(session, task)
        logger.info(
            "[usb_cdc] disconnected device_id=%s port=%s generation=%d error=%s",
            session.device_id or "",
            session.port,
            session.generation,
            type(error).__name__ if error is not None else "",
        )
