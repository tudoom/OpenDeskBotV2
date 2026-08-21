"""Startup/shutdown orchestration contract for deskbot_server.main.

Covers two invariants introduced by the parallelized startup:

* USB-first: the serial bridge, scheduler and WS/HTTP endpoint start without
  waiting for the Agent SDK; the RTC gateway is installed by a background
  task only after the SDK reports ready.
* Graceful shutdown: SIGBREAK (Windows) / SIGTERM (POSIX) resolves the
  run-forever wait, the background RTC startup is cancelled first, and the
  existing reverse shutdown chain runs to completion.
"""
from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace

import pytest

import deskbot_server.main as main_module


class _Ctx(SimpleNamespace):
    pass


def _install_fakes(monkeypatch) -> _Ctx:
    ctx = _Ctx(
        events=[],
        gateway_installs=[],
        sdk_gate=asyncio.Event(),
        ws_serving=asyncio.Event(),
        gateway_ready=asyncio.Event(),
    )

    class _FakeAgentSdk:
        def __init__(self, _settings):
            self.tool_bridge_token = "token"
            self.token_endpoint = "http://127.0.0.1:0/token"

        def configure_tool_bridge(self, **_kw):
            pass

        def configure_local_livekit(self, **_kw):
            pass

        def health_snapshot(self):
            return {}

        async def start(self) -> bool:
            ctx.events.append("sdk_start_begin")
            try:
                await ctx.sdk_gate.wait()
            except asyncio.CancelledError:
                ctx.events.append("sdk_start_cancelled")
                raise
            ctx.events.append("sdk_ready")
            return True

        async def stop(self):
            ctx.events.append("sdk_stop")

    class _FakeLiveKit:
        def __init__(self, _settings):
            self.enabled = False
            self.credentials = None

        async def start(self) -> bool:
            return True

        async def stop(self):
            ctx.events.append("livekit_stop")

        def health_snapshot(self):
            return {}

    class _FakeSerialBridge:
        def __init__(self, *_a, **_kw):
            pass

        async def start(self):
            ctx.events.append("serial_start")

        async def stop(self):
            ctx.events.append("serial_stop")

    class _FakeScheduler:
        def __init__(self, **_kw):
            pass

        def start(self):
            ctx.events.append("scheduler_start")

        async def stop(self):
            ctx.events.append("scheduler_stop")

    class _FakeLeases:
        def __init__(self, _hub):
            pass

        async def close(self):
            ctx.events.append("leases_close")

    class _FakeGateway:
        def __init__(self, _rtc_settings, token_endpoint=None):
            self.token_endpoint = token_endpoint

    class _FakeServeCM:
        async def __aenter__(self):
            ctx.events.append("ws_serve")
            ctx.ws_serving.set()
            return self

        async def __aexit__(self, *_exc):
            return False

    def _fake_install(gateway):
        ctx.gateway_installs.append(gateway)
        if gateway is not None:
            ctx.events.append("gateway_installed")
            ctx.gateway_ready.set()

    async def _shutdown_rtc_runtime():
        ctx.events.append("rtc_runtime_shutdown")

    async def _watch_forever():
        await asyncio.Event().wait()

    async def _idle_forever():
        await asyncio.Event().wait()

    settings = SimpleNamespace(
        server=SimpleNamespace(
            host="127.0.0.1",
            port=0,
            max_concurrent_asr=1,
            max_concurrent_face_infer=1,
            ws_ping_interval=None,
            ws_ping_timeout=10,
        ),
        audio=SimpleNamespace(input_codec="pcm", sample_rate=16000, channels=1),
        rtc=SimpleNamespace(enabled=True),
    )

    m = monkeypatch.setattr
    m(main_module, "load_dotenv", lambda: None)
    m(
        main_module,
        "apply_pending_llm_config",
        lambda: {"revision": 1, "status": "applied"},
    )
    m("deskbot_server.db.init_database", lambda: None)
    m("deskbot_server.db.engine.default_db_path", lambda: "test.db")
    m("deskbot_server.device_data.ensure_local_data_initialized", lambda: None)
    m(main_module, "load_config", lambda _path: {})
    m(main_module, "apply_debug_prefs_from_config", lambda _cfg: None)
    m(
        main_module,
        "AppSettings",
        SimpleNamespace(from_config=lambda _cfg: settings),
    )
    m(main_module, "install_rtc_gateway", _fake_install)
    m(main_module, "RtcAgentSdkManager", _FakeAgentSdk)
    m(main_module, "LocalLiveKitServerManager", _FakeLiveKit)
    m(main_module, "SerialServiceBridge", _FakeSerialBridge)
    m(main_module, "ScheduledTaskScheduler", _FakeScheduler)
    m(main_module, "AudioConfig", lambda **kw: SimpleNamespace(**kw))
    m(main_module, "configure_concurrency", lambda **_kw: None)
    m(
        main_module,
        "build_chat_service",
        lambda _cfg: SimpleNamespace(device_pb_only=True),
    )
    m(main_module, "DevicePipelineBroker", lambda: SimpleNamespace())
    m(main_module, "DeviceRegistry", lambda: SimpleNamespace())
    m(main_module, "AsrChatHub", lambda **_kw: SimpleNamespace())
    m(main_module, "CameraPreviewLeaseManager", _FakeLeases)
    m(main_module, "CameraImageBroker", lambda send_fn=None: SimpleNamespace())
    m(main_module, "build_camera_face_runtime", lambda _cfg: None)
    m(
        main_module,
        "build_server_tls",
        lambda _host: SimpleNamespace(
            scheme="ws", context=None, terminated_by_proxy=False
        ),
    )
    m(main_module, "patch_websockets_http11_for_rest_api", lambda: None)
    m(main_module, "_build_http_request_handler", lambda *_a, **_kw: None)
    m(main_module, "debug_ws_server_options", lambda: {})
    m(main_module, "DeskbotRtcGateway", _FakeGateway)
    m(main_module, "watch_llm_config", _watch_forever)
    m(main_module, "shutdown_rtc_runtime", _shutdown_rtc_runtime)
    # Sibling background loops (e.g. control-operation retention) touch the
    # real DB; keep them idle so this test stays hermetic.
    monkeypatch.setattr(
        main_module,
        "_control_operation_retention_loop",
        _idle_forever,
        raising=False,
    )
    m(
        main_module,
        "websockets",
        SimpleNamespace(serve=lambda *_a, **_kw: _FakeServeCM()),
    )
    return ctx


@pytest.fixture()
def restore_signal_handlers():
    names = [n for n in ("SIGBREAK", "SIGTERM") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    try:
        yield
    finally:
        for n, handler in saved.items():
            signal.signal(getattr(signal, n), handler)


def test_gateway_installs_in_background_after_serial_bridge(
    monkeypatch, restore_signal_handlers
):
    async def _run():
        ctx = _install_fakes(monkeypatch)
        task = asyncio.create_task(main_module.main())
        # Serial bridge, scheduler and the WS endpoint come up while the
        # Agent SDK is still "importing" (gate closed).
        await asyncio.wait_for(ctx.ws_serving.wait(), timeout=5)
        assert "serial_start" in ctx.events
        assert "scheduler_start" in ctx.events
        # Only the initial install_rtc_gateway(None) has happened so far.
        assert ctx.gateway_installs == [None]

        ctx.sdk_gate.set()
        await asyncio.wait_for(ctx.gateway_ready.wait(), timeout=5)
        assert len(ctx.gateway_installs) == 2
        assert ctx.gateway_installs[1] is not None
        assert ctx.events.index("serial_start") < ctx.events.index(
            "gateway_installed"
        )
        assert ctx.events.index("ws_serve") < ctx.events.index(
            "gateway_installed"
        )
        assert ctx.events.index("sdk_ready") < ctx.events.index(
            "gateway_installed"
        )

        # KeyboardInterrupt-equivalent cancellation still runs the full
        # shutdown chain.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert "serial_stop" in ctx.events
        assert "sdk_stop" in ctx.events

    asyncio.run(_run())


def test_shutdown_signal_cancels_pending_rtc_startup(
    monkeypatch, restore_signal_handlers
):
    sig = getattr(signal, "SIGBREAK", signal.SIGTERM)

    async def _run():
        ctx = _install_fakes(monkeypatch)
        task = asyncio.create_task(main_module.main())
        await asyncio.wait_for(ctx.ws_serving.wait(), timeout=5)
        # Stop the service while the Agent SDK is still starting up.
        signal.raise_signal(sig)
        await asyncio.wait_for(task, timeout=5)

        # The background RTC startup was cancelled before the shutdown chain,
        # so the gateway is never installed during shutdown.
        assert "sdk_start_cancelled" in ctx.events
        assert ctx.gateway_installs == [None]
        # The full reverse shutdown chain still executed, in order.
        for step in (
            "scheduler_stop",
            "leases_close",
            "serial_stop",
            "rtc_runtime_shutdown",
            "sdk_stop",
            "livekit_stop",
        ):
            assert step in ctx.events
        assert ctx.events.index("sdk_start_cancelled") < ctx.events.index(
            "scheduler_stop"
        )
        assert (
            ctx.events.index("scheduler_stop")
            < ctx.events.index("leases_close")
            < ctx.events.index("serial_stop")
            < ctx.events.index("rtc_runtime_shutdown")
            < ctx.events.index("sdk_stop")
            < ctx.events.index("livekit_stop")
        )

    asyncio.run(_run())
