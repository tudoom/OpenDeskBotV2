from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest


def _scene(name: str, marker: int, *, aliases=(), ms: int = 100):
    return {
        "name": name,
        "title": name.title(),
        "alias": list(aliases),
        "frames": [
            {
                "ms": ms,
                "elements": {
                    "extra": [
                        {"shape": "circle", "x": marker, "y": marker, "r": marker}
                    ]
                },
            }
        ],
    }


def _catalog():
    from deskbot_server.application.expression_runtime import build_expression_catalog

    return build_expression_catalog(
        {
            "mappings": {
                "idle": "neutral_face",
                "listening": "ears_open",
                "thinking": "thinking",
                "speaking": "talk_face",
                "sleepy": "sleep",
            },
            "emotions": [
                _scene("idle", 1, aliases=("neutral_face",)),
                _scene("listening", 2, aliases=("ears_open",)),
                _scene("thinking", 3),
                _scene("happy", 4, aliases=("talk_face",)),
                _scene("sleep", 5),
            ],
        }
    )


def test_catalog_validates_mapping_targets_and_legacy_is_fallback_only():
    from deskbot_server.application.expression_runtime import (
        build_expression_catalog,
        normalize_expression_state,
    )

    doc = {
        "mappings": {
            "idle": "neutral_face",
            "happy": "smile_alias",
            "thinking": "model_invented_scene",
        },
        "emotions": [
            _scene("idle", 1, aliases=("neutral_face",)),
            _scene("happy", 2, aliases=("smile_alias",)),
            _scene("thinking", 3),
            _scene("sleep", 4),
        ],
    }
    catalog = build_expression_catalog(
        doc,
        legacy_mappings={"thinking": "thinking", "sleepy": "sleep"},
    )

    assert catalog.state_mappings["happy"] == "happy"
    # The explicit bad canonical target is rejected rather than silently
    # falling through to the same-named scene.
    assert catalog.state_mappings["thinking"] == "idle"
    # Canonical top-level mappings exist, so the retired split file is ignored.
    assert catalog.state_mappings["sleepy"] == "sleep"
    assert "model_invented_scene" not in catalog.tool_values()
    assert catalog.resolve_scene("smile_alias").name == "happy"
    assert normalize_expression_state("talking") == "speaking"
    assert normalize_expression_state("sleep") == "sleepy"

    without_canonical_map = dict(doc)
    without_canonical_map.pop("mappings")
    migrated = build_expression_catalog(
        without_canonical_map,
        legacy_mappings={"thinking": "thinking"},
    )
    assert migrated.state_mappings["thinking"] == "thinking"


def test_tool_catalog_contains_only_resolvable_names_states_and_aliases():
    catalog = _catalog()
    summary = catalog.summary()

    assert {"happy", "talk_face"} <= set(summary["values"])
    assert summary["aliases"]["talk_face"] == "happy"
    assert all(catalog.resolve_scene(value) is not None for value in summary["values"])
    assert not {"idle", "listening", "thinking", "speaking"} & set(
        summary["values"]
    )


def test_only_rtc_speaking_expression_authorizes_device_pcm_mouth_overlay():
    from deskbot_server.application.expression_runtime import (
        build_expression_pb_frames,
    )

    scene = _catalog().resolve_scene("talk_face")
    normal = build_expression_pb_frames(scene, request_id="normal")
    speaking = build_expression_pb_frames(
        scene,
        request_id="speaking",
        voice_mouth=True,
    )

    assert normal[0]["voice_mouth"] is False
    assert speaking[0]["voice_mouth"] is True
    assert all("voice_mouth" not in message for message in speaking[1:])


class _OrderedUsbSession:
    def __init__(
        self,
        *,
        block_first: bool = False,
        blocked_writes=(),
        auto_ack: bool = True,
        terminal_phase: str = "played",
    ) -> None:
        self.messages: list[dict] = []
        self.blocked_writes = set(blocked_writes)
        if block_first:
            self.blocked_writes.add(1)
        self.first_write = asyncio.Event()
        self.release_first = asyncio.Event()
        self.write_events: dict[int, asyncio.Event] = {}
        self.write_releases: dict[int, asyncio.Event] = {}
        self.active_chains = 0
        self.max_active_chains = 0
        self.auto_ack = auto_ack
        self.terminal_phase = terminal_phase

    @asynccontextmanager
    async def downlink_chain(self):
        self.active_chains += 1
        self.max_active_chains = max(self.max_active_chains, self.active_chains)
        try:
            yield
        finally:
            self.active_chains -= 1

    async def send_pb_wire(self, wire: str):
        message = json.loads(wire)
        self.messages.append(message)
        write_number = len(self.messages)
        event = self.write_events.setdefault(write_number, asyncio.Event())
        event.set()
        if write_number in self.blocked_writes:
            release = self.write_releases.setdefault(write_number, asyncio.Event())
            if write_number == 1:
                self.first_write.set()
                release = self.release_first
            await release.wait()
        if self.auto_ack:
            from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

            is_terminal = message.get("type") in {"pb_single", "pb_end"}
            await pb_ack_gate.notify(
                "deskbot_test",
                {
                    "req": message["req"],
                    "idx": message["idx"],
                    "phase": self.terminal_phase if is_terminal else "accepted",
                },
            )


def _marker(message: dict) -> int:
    return int(message["anim"][0]["elements"]["extra"][0]["r"])


def test_transitions_are_serialized_and_latest_pending_state_wins():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(block_first=True)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        listening = asyncio.create_task(runtime.transition("listening"))
        await session.first_write.wait()
        thinking = asyncio.create_task(runtime.transition("thinking"))
        await asyncio.sleep(0)
        speaking = asyncio.create_task(runtime.transition("speaking"))
        await asyncio.sleep(0)
        session.release_first.set()

        first_result, middle_result, final_result = await asyncio.gather(
            listening,
            thinking,
            speaking,
        )
        assert first_result.resolved == "listening"
        assert first_result.status == "coalesced"
        assert middle_result.status == "coalesced"
        assert final_result.resolved == "happy"
        assert [_marker(message) for message in session.messages] == [2, 4]
        assert all(message.get("audio") is None for message in session.messages)
        assert session.max_active_chains == 1

    asyncio.run(_run())


def test_explicit_tool_lease_defers_auto_states_then_restores_latest_desired():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(blocked_writes={1, 2})
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )

        old_state = asyncio.create_task(runtime.transition("listening"))
        await session.first_write.wait()
        stale_thinking = asyncio.create_task(runtime.transition("thinking"))
        tool = asyncio.create_task(
            runtime.play_expression(
                "talk_face",
                duration_ms=100,
                lease_ms=100,
                wait_for_played=True,
            )
        )
        await session.write_events.setdefault(2, asyncio.Event()).wait()
        stale_speaking = asyncio.create_task(runtime.transition("speaking"))
        await asyncio.sleep(0)
        session.write_releases[2].set()

        old_result, thinking_result, tool_result, speaking_result = await asyncio.gather(
            old_state,
            stale_thinking,
            tool,
            stale_speaking,
        )
        assert old_result.status == "coalesced"
        assert thinking_result.status == "coalesced"
        assert tool_result.status == "played"
        assert speaking_result.status == "deferred"
        await asyncio.sleep(0.12)
        for _attempt in range(50):
            if session.messages and _marker(session.messages[-1]) == 4:
                break
            await asyncio.sleep(0)
        assert _marker(session.messages[-1]) == 4
        assert session.messages[-1]["action"] == "replace"
        assert runtime.desired_state == "speaking"

    asyncio.run(_run())


def test_play_expression_has_no_hardcoded_idle_tail_and_cancel_restores_desired():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        played = await runtime.play_expression(
            "talk_face",
            duration_ms=250,
            wait_for_played=True,
        )
        assert played.status == "played"
        assert [_marker(message) for message in session.messages] == [4]
        assert [message["action"] for message in session.messages] == ["replace"]
        assert len({message["req"] for message in session.messages}) == 1
        assert [message["idx"] for message in session.messages] == [0]
        assert [message["type"] for message in session.messages] == ["pb_single"]
        assert session.messages[0]["chunk_ms"] == 250
        assert runtime.last_scene_name == "happy"

        await runtime.transition("thinking")
        cancelled = await runtime.cancel(reason="barge_in")
        assert cancelled.resolved == "idle"
        assert _marker(session.messages[-1]) == 1
        assert session.messages[-1]["action"] == "replace"
        assert runtime.last_scene_name == "idle"

    asyncio.run(_run())


def test_play_expression_waits_for_final_accepted_and_played_ack():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run():
        session = _OrderedUsbSession(auto_ack=False)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        task = asyncio.create_task(
            runtime.play_expression("talk_face", wait_for_played=True)
        )
        for _attempt in range(50):
            if len(session.messages) == 1:
                break
            await asyncio.sleep(0)
        assert len(session.messages) == 1
        assert not task.done()

        final = session.messages[-1]
        await pb_ack_gate.notify(
            "deskbot_test",
            {"req": final["req"], "idx": final["idx"], "phase": "accepted"},
        )
        await asyncio.sleep(0)
        assert not task.done()

        await pb_ack_gate.notify(
            "deskbot_test",
            {"req": final["req"], "idx": final["idx"], "phase": "played"},
        )
        result = await task
        assert result.status == "played"
        assert runtime.last_scene_name == "happy"
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_play_expression_preserves_terminal_failure_and_pose_state():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run():
        session = _OrderedUsbSession(terminal_phase="failed")
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )

        result = await runtime.play_expression("talk_face", wait_for_played=True)

        assert result.status == "failed"
        assert result.ok is False
        assert runtime.last_scene_name is None
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_play_expression_disconnect_wakes_terminal_wait():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run():
        session = _OrderedUsbSession(auto_ack=False)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        task = asyncio.create_task(
            runtime.play_expression("talk_face", wait_for_played=True)
        )
        for _attempt in range(50):
            if len(session.messages) == 1:
                break
            await asyncio.sleep(0)
        assert len(session.messages) == 1

        await pb_ack_gate.cancel_device("deskbot_test")
        result = await task

        assert result.status == "disconnected"
        assert runtime.last_scene_name is None
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_play_expression_ack_timeout_is_not_reported_as_played(monkeypatch):
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime
    from deskbot_server.ws import pb_ack_waiter
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    async def _run():
        monkeypatch.setattr(pb_ack_waiter, "pb_wait_ack_timeout_sec", lambda: 0.01)
        session = _OrderedUsbSession(auto_ack=False)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )

        result = await runtime.play_expression("talk_face", wait_for_played=True)

        assert result.status == "timeout"
        assert result.ok is False
        assert runtime.last_scene_name is None
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_ack_deadline_starts_only_after_expression_owns_chain_lock(monkeypatch):
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime
    from deskbot_server.ws import pb_ack_waiter
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate
    from deskbot_server.ws.ws_send import _pb_ws_chain_serial_lock

    async def _run():
        monkeypatch.setattr(pb_ack_waiter, "pb_wait_ack_timeout_sec", lambda: 0.01)
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        chain_lock = _pb_ws_chain_serial_lock(session)
        await chain_lock.acquire()
        try:
            task = asyncio.create_task(
                runtime.play_expression("talk_face", wait_for_played=True)
            )
            await asyncio.sleep(0.03)
            assert session.messages == []
            assert not task.done()
            assert await pb_ack_gate.state_count() == 0
        finally:
            chain_lock.release()

        result = await task
        assert result.status == "played"
        assert await pb_ack_gate.state_count() == 0

    asyncio.run(_run())


def test_cancel_supersedes_an_active_tool_and_replaces_with_idle():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(block_first=True)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        tool = asyncio.create_task(
            runtime.play_expression("talk_face", wait_for_played=True)
        )
        await session.first_write.wait()
        cancelled_to_idle = asyncio.create_task(runtime.cancel(reason="disconnect"))
        tool_result, idle_result = await asyncio.gather(tool, cancelled_to_idle)

        assert tool_result.status == "cancelled"
        assert tool_result.ok is False
        assert idle_result.status == "played"
        assert [_marker(message) for message in session.messages] == [4, 1]
        assert session.messages[-1]["action"] == "replace"
        assert runtime.last_scene_name == "idle"

    asyncio.run(_run())


def test_cancelling_tool_await_still_restores_idle():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(block_first=True)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        tool = asyncio.create_task(
            runtime.play_expression("talk_face", wait_for_played=True)
        )
        await session.first_write.wait()
        tool.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tool

        assert [_marker(message) for message in session.messages] == [4, 1]
        assert session.messages[-1]["action"] == "replace"
        assert runtime.last_scene_name == "idle"

    asyncio.run(_run())


def test_rtc_playback_control_drives_state_and_restores_idle(monkeypatch):
    from types import SimpleNamespace

    from deskbot_server import rtc_runtime

    expression_events: list[tuple[str, str]] = []

    class _ExpressionRuntime:
        def __init__(self, device_id, _session):
            self.device_id = device_id

        async def transition(self, state, *, reason):
            expression_events.append(("state", state))

        async def cancel(self, *, reason):
            expression_events.append(("cancel", reason))

        async def close(self, *, restore_idle):
            expression_events.append(("close", str(restore_idle)))

    class _RtcSession:
        device_id = "deskbot_test"
        effective_call_mode = "stable"

        async def connect(self):
            return None

        async def close(self):
            return None

    class _Gateway:
        settings = SimpleNamespace(enabled=True)

        def __init__(self):
            self.control = None

        async def get_or_create(self, _device_id, **kwargs):
            self.control = kwargs["playback_control_sink"]
            return _RtcSession()

        async def close_device(self, _device_id):
            return None

    class _Device:
        async def send(self, _wire):
            return None

    async def _run():
        gateway = _Gateway()
        monkeypatch.setattr(rtc_runtime, "RtcExpressionRuntime", _ExpressionRuntime)
        rtc_runtime.install_rtc_gateway(gateway)
        device = _Device()
        await rtc_runtime.bind_usb_device(
            "deskbot_test",
            device,
        )
        assert gateway.control is not None
        # Agent attachment reports listening even before anyone has spoken.
        # It is a ready state and must leave the boot idle expression intact.
        await gateway.control("agent_state", {"state": "initializing"})
        await gateway.control("agent_state", {"state": "listening"})
        await gateway.control("user_speech_start", {})
        await gateway.control("user_speech_confirmed_end", {})
        for state in ("thinking", "speaking", "listening"):
            await gateway.control("agent_state", {"state": state})
        await gateway.control("barge_in", {})
        await gateway.control("connection_closed", {})
        await rtc_runtime.unbind_usb_device("deskbot_test", device)

        state_events = [event for event in expression_events if event[0] == "state"]
        assert state_events[:6] == [
            ("state", "idle"),
            ("state", "listening"),
            ("state", "thinking"),
            ("state", "thinking"),
            ("state", "speaking"),
            ("state", "idle"),
        ]
        assert state_events[-2:] == [
            ("state", "listening"),
            ("state", "idle"),
        ]
        assert ("cancel", "rtc_disconnect") not in expression_events
        assert expression_events[-1] == ("close", "False")
        rtc_runtime.install_rtc_gateway(None)

    asyncio.run(_run())


def test_agent_listening_does_not_override_a_persistent_web_face():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        result = await runtime.play_frames(
            [
                {
                    "ms": 100,
                    "elements": {
                        "extra": [
                            {"shape": "circle", "x": 9, "y": 9, "r": 4}
                        ]
                    },
                }
            ],
            name="web_face",
            source="web",
            reason="web_preview",
            persist_until_preempted=True,
        )
        assert result.status == "played"

        deferred = await runtime.transition("idle", reason="rtc_disconnect")
        snapshot = runtime.snapshot()

        assert deferred.status == "deferred"
        assert snapshot["displayed_expression"] == "web_face"
        assert snapshot["displayed_source"] == "web"
        assert snapshot["desired_state"] == "idle"
        assert snapshot["active_source"] == "web"
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_usb_expression_runtime_exists_without_rtc_gateway(monkeypatch):
    from deskbot_server import rtc_runtime

    events: list[tuple[str, object]] = []

    class _ExpressionRuntime:
        def __init__(self, device_id, session):
            self.device_id = device_id
            self.device_session = session

        async def transition(self, state, **_kwargs):
            events.append(("state", state))

        async def close(self, *, restore_idle):
            events.append(("close", restore_idle))

    async def _run():
        monkeypatch.setattr(rtc_runtime, "RtcExpressionRuntime", _ExpressionRuntime)
        rtc_runtime.install_rtc_gateway(None)
        # RTC 关闭的部署：声明本次运行不会安装网关，bind 不等待、立即返回。
        rtc_runtime.mark_rtc_gateway_unavailable()
        device = object()

        await rtc_runtime.bind_usb_device("usb_only", device)
        runtime = rtc_runtime.get_expression_runtime("usb_only")

        assert runtime is not None
        assert runtime.device_session is device
        assert events == [("state", "idle")]

        await rtc_runtime.unbind_usb_device("usb_only", device)
        assert rtc_runtime.get_expression_runtime("usb_only") is None
        assert events[-1] == ("close", False)

    asyncio.run(_run())


def test_explicit_lease_covers_playback_plus_restore_margin():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )

        result = await runtime.play_expression(
            "talk_face",
            duration_ms=250,
            wait_for_played=True,
        )

        assert result.status == "played"
        assert runtime._lease is not None
        remaining = runtime._lease.expires_at - asyncio.get_running_loop().time()
        assert remaining > 0.4
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_web_helpers_fail_closed_without_usb_expression_runtime():
    from deskbot_server.application.expression_runtime import (
        play_web_expression_frames,
        play_web_expression_scene,
    )

    async def _run():
        frames = await play_web_expression_frames(
            "missing-device",
            [{"ms": 100, "elements": {"mouth": []}}],
            reason="http_device_pb_anim",
        )
        scene = await play_web_expression_scene(
            "missing-device",
            "happy",
            reason="http_device_face_play",
        )

        assert frames.status == "device_unavailable"
        assert frames.source == "web"
        assert frames.reason == "http_device_pb_anim"
        assert scene.status == "device_unavailable"
        assert scene.source == "web"
        assert scene.reason == "http_device_face_play"

    asyncio.run(_run())


def test_expression_result_and_snapshot_expose_display_owner():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )

        result = await runtime.play_frames(
            [
                {
                    "ms": 100,
                    "elements": {
                        "extra": [
                            {"shape": "circle", "x": 7, "y": 7, "r": 7}
                        ]
                    },
                }
            ],
            source="web",
            reason="http_device_pb_anim",
            persist_until_preempted=True,
            operation_id="expr-preview:test-operation",
        )

        assert result.status == "played"
        assert result.source == "web"
        assert result.reason == "http_device_pb_anim"
        assert result.lease_token
        assert result.frame_fingerprint
        assert result.final_frame_fingerprint
        assert result.final_frame_index == 0
        assert result.operation_id == "expr-preview:test-operation"
        tool_result = result.as_tool_result()
        assert tool_result["source"] == "web"
        assert tool_result["reason"] == "http_device_pb_anim"
        assert tool_result["operation_id"] == "expr-preview:test-operation"
        assert tool_result["lease_id"] == result.lease_token[:8]
        assert "lease_token" not in tool_result

        snapshot = runtime.snapshot()
        assert snapshot["displayed_expression"] == "adhoc_web"
        assert snapshot["displayed_title"] == "adhoc_web"
        assert snapshot["displayed_source"] == "web"
        assert snapshot["displayed_reason"] == "http_device_pb_anim"
        assert snapshot["displayed_operation_id"] == "expr-preview:test-operation"
        assert snapshot["active_source"] == "web"
        assert snapshot["frame_fingerprint"] == result.frame_fingerprint
        assert snapshot["final_frame_fingerprint"] == result.final_frame_fingerprint
        assert snapshot["history"][0]["expression"] == "adhoc_web"
        assert snapshot["history"][0]["source"] == "web"
        assert snapshot["history"][0]["reason"] == "http_device_pb_anim"
        assert snapshot["history"][0]["operation_id"] == "expr-preview:test-operation"
        assert snapshot["history"][0]["generation"] == snapshot["display_generation"]
        assert snapshot["lease"]["source"] == "web"
        assert snapshot["lease"]["reason"] == "http_device_pb_anim"
        assert snapshot["lease"]["lease_id"] == result.lease_token[:8]
        assert "token" not in snapshot["lease"]
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_expression_fingerprint_is_stable_across_request_ids_and_rgb565_normalized():
    from deskbot_server.application.expression_runtime import (
        ExpressionScene,
        build_expression_pb_frames,
        fingerprint_expression_messages,
    )

    scene = ExpressionScene(
        name="same",
        title="Same",
        aliases=(),
        frames=(
            {
                "ms": 120,
                "elements": {
                    "mouth": [
                        {
                            "shape": "draw_line",
                            "x1": 1,
                            "y1": 2,
                            "x2": 3,
                            "y2": 4,
                            "color": "#f4f4ef",
                        }
                    ]
                },
            },
        ),
    )
    first = build_expression_pb_frames(scene, request_id="first")
    second = build_expression_pb_frames(scene, request_id="second")

    one = fingerprint_expression_messages(first)
    two = fingerprint_expression_messages(second)
    assert one == two
    assert first[0]["anim"][0]["elements"]["mouth"][0]["c"] == 0xF7BD
    assert one["frame_count"] == 1
    assert one["timeline_ms"] == 120
    assert one["expected_display_crc32"]


def test_display_crc_drops_images_between_request_owned_asset_chunks():
    from deskbot_server.application.expression_runtime import display_semantic_crc32

    image = {
        "shape": "image",
        "asset": 0,
        "x": 0,
        "y": 0,
        "w": 10,
        "h": 10,
    }
    marker = {"shape": "circle", "x": 5, "y": 5, "r": 2}
    first = {
        "anim": [{"ms": 10, "elements": {"extra": [image]}}],
    }
    second = {
        "anim": [{"ms": 10, "elements": {"eye_l": [marker]}}],
    }
    explicit_image_clear = {
        "anim": [
            {
                "ms": 10,
                "elements": {"extra": [], "eye_l": [marker]},
            }
        ],
    }

    chunked = display_semantic_crc32(
        [first, second],
        asset_crc32_by_message=[(0x12345678,), ()],
    )
    cleared = display_semantic_crc32(
        [first, explicit_image_clear],
        asset_crc32_by_message=[(0x12345678,), ()],
    )
    assert chunked == cleared

    audio_only = {"audio": {"next_bin_len": 320}}
    assert display_semantic_crc32(
        [first, audio_only],
        asset_crc32_by_message=[(0x12345678,), ()],
    ) == display_semantic_crc32(
        [first],
        asset_crc32_by_message=[(0x12345678,)],
    )


def test_superseded_accepted_operation_gets_a_terminal_history_status():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(block_first=True)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        listening = asyncio.create_task(runtime.transition("listening"))
        await session.first_write.wait()
        speaking = asyncio.create_task(runtime.transition("speaking"))
        await asyncio.sleep(0)
        session.release_first.set()
        old_result, new_result = await asyncio.gather(listening, speaking)

        assert old_result.status == "coalesced"
        assert new_result.status == "played"
        operations = runtime.snapshot()["operations"]
        by_state = {entry["state"]: entry["status"] for entry in operations}
        assert by_state["listening"] == "coalesced"
        assert by_state["speaking"] == "played"
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_played_ack_carries_independent_device_display_crc():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(auto_ack=False)
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        task = asyncio.create_task(
            runtime.play_expression("talk_face", wait_for_played=True)
        )
        while not session.messages:
            await asyncio.sleep(0)
        final = session.messages[-1]
        from deskbot_server.application.expression_runtime import (
            fingerprint_expression_messages,
        )
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        expected = fingerprint_expression_messages(session.messages)[
            "expected_display_crc32"
        ]
        await pb_ack_gate.notify(
            "deskbot_test",
            {"req": final["req"], "idx": final["idx"], "phase": "accepted"},
        )
        await pb_ack_gate.notify(
            "deskbot_test",
            {
                "req": final["req"],
                "idx": final["idx"],
                "phase": "played",
                "display_crc32": expected,
            },
        )
        result = await task
        assert result.expected_display_crc32 == expected
        assert result.device_display_crc32 == expected
        assert result.display_crc_match is True
        snapshot = runtime.snapshot()
        assert snapshot["expected_display_crc32"] == expected
        assert snapshot["device_display_crc32"] == expected
        assert snapshot["display_crc_match"] is True
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_external_display_lease_can_release_without_overwriting_firmware_restored_face():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession()
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        initial = await runtime.transition("idle", force=True)
        assert initial.status == "played"
        baseline_writes = len(session.messages)

        token = await runtime.acquire_external_display(
            name="tts_image",
            title="Image",
            source="tts_image",
            priority=25,
            reason="chat_tts_image",
        )
        assert token
        await runtime.release_external_display(
            token,
            reason="tts_image_complete",
            restore=True,
        )
        assert len(session.messages) == baseline_writes + 1
        assert _marker(session.messages[-1]) == 1
        assert runtime.snapshot()["lease"] is None
        assert runtime.snapshot()["displayed_expression"] == "idle"
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_accepted_candidate_only_updates_current_face_after_played():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        session = _OrderedUsbSession(terminal_phase="accepted")
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            session,
            catalog_loader=_catalog,
        )
        result = await runtime.play_expression(
            "talk_face",
            wait_for_played=False,
            lease_ms=5000,
        )
        assert result.status == "accepted"
        snapshot = runtime.snapshot()
        assert snapshot["history"] == []
        assert snapshot["displayed_expression"] is None
        assert len(snapshot["operations"]) == 1
        assert snapshot["operations"][0]["status"] == "accepted"

        request = session.messages[-1]
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        await pb_ack_gate.notify(
            "deskbot_test",
            {"req": request["req"], "idx": request["idx"], "phase": "played"},
        )
        async def _played_snapshot():
            for _ in range(100):
                snapshot = runtime.snapshot()
                if snapshot["history"] and snapshot["history"][0]["status"] == "played":
                    return snapshot
                await asyncio.sleep(0.005)
            raise AssertionError("background played ACK did not update history")

        snapshot = await asyncio.wait_for(_played_snapshot(), timeout=1.0)
        assert len(snapshot["history"]) == 1
        assert snapshot["history"][0]["status"] == "played"
        assert snapshot["operations"][0]["status"] == "played"
        await runtime.close(restore_idle=False)

    asyncio.run(_run())


def test_snapshot_separates_mouth_overlay_from_full_face_history():
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    async def _run():
        runtime = RtcExpressionRuntime(
            "deskbot_test",
            _OrderedUsbSession(),
            catalog_loader=_catalog,
        )
        await runtime.transition("idle", force=True)
        before = runtime.snapshot()
        token = runtime.begin_mouth_overlay(
            source="tts",
            reason="phoneme_playback",
        )
        active = runtime.snapshot()
        assert active["mouth_overlay"]["active"] is True
        assert active["mouth_overlay"]["kind"] == "phoneme"
        assert active["display_generation"] == before["display_generation"]
        assert active["history"] == before["history"]
        runtime.end_mouth_overlay(token)
        assert runtime.snapshot()["mouth_overlay"]["active"] is False
        await runtime.close(restore_idle=False)

    asyncio.run(_run())
