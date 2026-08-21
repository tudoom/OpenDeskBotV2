from __future__ import annotations

import asyncio
import io
import json
import threading
import time

import pytest
from PIL import Image

from deskbot_server.application.camera_preview import CameraPreviewLeaseManager
from deskbot_server.device_camera_frame_store import (
    capture_camera_for_device,
    capture_camera_for_device_async,
    clear_device_camera_frame,
    get_device_camera_frame,
    retire_device_camera_frame,
    update_device_camera_frame,
    wait_for_device_camera_frame,
)
from deskbot_server.face_snapshot_cache import (
    clear_device_faces,
    get_device_face_snapshot_state,
    list_device_faces,
    update_device_faces,
)
from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.llm.vision_input import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_PIXELS,
    TRANSIENT_VISION_IMAGE_KEY,
    ValidatedJpeg,
    VisionImageValidationError,
    validate_jpeg,
    validate_jpeg_bytes,
)
from deskbot_server.pb.cam_signal import build_cam_fps_signal_pb
from deskbot_server.pb.servo_pcm import make_anim_item, parse_pb_cam_fps, pb_json_messages
from deskbot_server.vision.generation import (
    VisionTombstone,
    begin_vision_generation,
    current_vision_generation,
    current_vision_source,
    forget_vision_tombstone,
)


def _fake_jpeg(
    color: tuple[int, int, int] = (40, 120, 200),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(output, format="JPEG")
    return output.getvalue()


def _fake_validated_jpeg(
    color: tuple[int, int, int] = (40, 120, 200),
) -> ValidatedJpeg:
    return validate_jpeg(_fake_jpeg(color))


def test_wait_for_device_camera_frame_notifies():
    dev = "dev_wait_cam"
    after = time.time()

    def _late_update():
        time.sleep(0.05)
        update_device_camera_frame(dev, _fake_validated_jpeg())

    threading.Thread(target=_late_update, daemon=True).start()
    row = wait_for_device_camera_frame(dev, after_ts=after, timeout=1.0)
    assert row is not None
    assert row["jpeg"].startswith(b"\xff\xd8")


def test_capture_camera_for_device_async_uses_fresh_cache():
    dev = "dev_async_cam"
    update_device_camera_frame(dev, _fake_validated_jpeg())

    async def _run():
        return await capture_camera_for_device_async(dev, hub=None, wait_timeout_s=0.5)

    cap = asyncio.run(_run())
    assert cap["ok"] is True
    assert cap["jpeg_bytes"] > 0


def test_async_capture_falls_back_to_bounded_stream_and_stops(monkeypatch):
    dev = "dev_async_camera_fallback"
    messages: list[dict] = []

    def _publish_frame():
        time.sleep(0.03)
        update_device_camera_frame(dev, _fake_validated_jpeg())

    class Hub:
        async def send(self, device_id, payload):
            assert device_id == dev
            messages.append(dict(payload))
            if payload.get("cam_fps") == 5 and not payload.get("camera_once"):
                threading.Thread(
                    target=_publish_frame,
                    daemon=True,
                ).start()
            return 1

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store._ONE_SHOT_PRIMARY_WAIT_S",
        0.03,
    )

    async def _run():
        return await capture_camera_for_device_async(
            dev,
            hub=Hub(),
            wait_timeout_s=0.3,
            require_fresh=True,
        )

    cap = asyncio.run(_run())
    assert cap["ok"] is True
    assert [
        (msg.get("cam_fps"), bool(msg.get("camera_once")))
        for msg in messages
    ] == [(5, True), (5, False), (0, False)]


def test_async_capture_stops_bounded_stream_after_fallback_timeout(monkeypatch):
    dev = "dev_async_camera_fallback_timeout"
    messages: list[dict] = []

    class Hub:
        async def send(self, device_id, payload):
            assert device_id == dev
            messages.append(dict(payload))
            return 1

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store._ONE_SHOT_PRIMARY_WAIT_S",
        0.01,
    )

    async def _run():
        return await capture_camera_for_device_async(
            dev,
            hub=Hub(),
            wait_timeout_s=0.05,
            require_fresh=True,
        )

    cap = asyncio.run(_run())
    assert cap["ok"] is False
    assert [
        (msg.get("cam_fps"), bool(msg.get("camera_once")))
        for msg in messages
    ] == [(5, True), (5, False), (0, False)]


def test_async_capture_fallback_restores_preview_lease_fps(monkeypatch):
    dev = "dev_async_camera_fallback_preview_lease"
    messages: list[dict] = []

    def _publish_frame():
        time.sleep(0.03)
        update_device_camera_frame(dev, _fake_validated_jpeg())

    class Hub:
        def add_online_listener(self, listener):
            return None

        def remove_online_listener(self, listener):
            return None

        async def send(self, device_id, payload):
            assert device_id == dev
            messages.append(dict(payload))
            if payload.get("cam_fps") == 5 and not payload.get("camera_once"):
                threading.Thread(
                    target=_publish_frame,
                    daemon=True,
                ).start()
            return 1

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store._ONE_SHOT_PRIMARY_WAIT_S",
        0.03,
    )

    async def _run():
        hub = Hub()
        leases = CameraPreviewLeaseManager(hub, fps=2, refresh_seconds=60)
        await leases.acquire(dev)
        try:
            return await capture_camera_for_device_async(
                dev,
                hub=hub,
                wait_timeout_s=0.5,
                require_fresh=True,
            )
        finally:
            await leases.release(dev)
            await leases.close()

    cap = asyncio.run(_run())
    assert cap["ok"] is True
    # 浏览器预览租约还在时，兜底流退出必须恢复租约期望的 2FPS 而不是广播
    # 0 掐死预览流；真正的停流只发生在最后一个租约释放时。
    assert [
        (msg.get("cam_fps"), bool(msg.get("camera_once")))
        for msg in messages
    ] == [
        (2, False),
        (5, True),
        (5, False),
        (2, False),
        (0, False),
    ]


def test_concurrent_captures_serialize_and_do_not_stop_each_other(monkeypatch):
    import deskbot_server.device_camera_frame_store as frame_store

    dev = "dev_async_camera_concurrent"
    messages: list[dict] = []

    def _publish_frame():
        time.sleep(0.03)
        update_device_camera_frame(dev, _fake_validated_jpeg())

    class Hub:
        async def send(self, device_id, payload):
            assert device_id == dev
            messages.append(dict(payload))
            if payload.get("cam_fps") == 5 and not payload.get("camera_once"):
                threading.Thread(
                    target=_publish_frame,
                    daemon=True,
                ).start()
            return 1

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store._ONE_SHOT_PRIMARY_WAIT_S",
        0.03,
    )

    async def _run():
        hub = Hub()
        return await asyncio.gather(
            capture_camera_for_device_async(
                dev,
                hub=hub,
                wait_timeout_s=1.0,
                require_fresh=True,
            ),
            capture_camera_for_device_async(
                dev,
                hub=hub,
                wait_timeout_s=1.0,
                require_fresh=True,
            ),
        )

    first, second = asyncio.run(_run())
    assert first["ok"] is True
    assert second["ok"] is True
    # per-device 锁串行化：第一个 capture 的 boost/停止序列完整结束后第二个
    # 才开始，谁也不会掐掉对方正在等待的兜底流。
    assert [
        (msg.get("cam_fps"), bool(msg.get("camera_once")))
        for msg in messages
    ] == [
        (5, True),
        (5, False),
        (0, False),
        (5, True),
        (5, False),
        (0, False),
    ]
    assert frame_store._capture_locks == {}
    assert frame_store._capture_lock_users == {}


def test_async_capture_without_hub_waits_full_timeout(monkeypatch):
    dev = "dev_async_camera_no_hub_full_wait"

    monkeypatch.setattr(
        "deskbot_server.device_camera_frame_store._ONE_SHOT_PRIMARY_WAIT_S",
        0.01,
    )

    def _late_update():
        time.sleep(0.1)
        update_device_camera_frame(dev, _fake_validated_jpeg())

    threading.Thread(target=_late_update, daemon=True).start()

    async def _run():
        return await capture_camera_for_device_async(
            dev,
            hub=None,
            wait_timeout_s=1.0,
            require_fresh=True,
        )

    cap = asyncio.run(_run())
    # hub 缺失时没有兜底流可用，主等待必须用满 wait_timeout_s，而不是被
    # one-shot 短窗（这里 0.01s）截断后直接失败。
    assert cap["ok"] is True
    assert cap["jpeg_bytes"] > 0


def test_capture_camera_is_transient_and_display_is_explicit():
    dev = "dev_capture_privacy"
    update_device_camera_frame(dev, _fake_validated_jpeg(), width=320, height=240)

    private_capture = capture_camera_for_device(dev)
    assert private_capture["ok"] is True
    assert private_capture["display_requested"] is False
    assert private_capture[TRANSIENT_VISION_IMAGE_KEY]["bytes"] == _fake_jpeg()
    assert private_capture[TRANSIENT_VISION_IMAGE_KEY]["mime_type"] == "image/jpeg"
    assert "jpeg_base64" not in private_capture
    assert "image_display" not in private_capture

    displayed_capture = capture_camera_for_device(dev, display=True)
    assert displayed_capture["ok"] is True
    assert displayed_capture["display_requested"] is True
    assert displayed_capture["image_display"]["bytes"]
    assert "jpeg_base64" not in displayed_capture


def test_capture_camera_rejects_wrong_mime_and_oversized_jpeg():
    # 裸 bytes 门面已删除：非法帧在 validate_jpeg 边界被拒，
    # store 只接受 ValidatedJpeg，未校验数据根本进不了缓存。
    wrong_mime_device = "dev_capture_wrong_mime"
    with pytest.raises(VisionImageValidationError):
        validate_jpeg(b"not-a-jpeg")
    with pytest.raises(TypeError, match="ValidatedJpeg"):
        update_device_camera_frame(wrong_mime_device, b"not-a-jpeg")
    assert get_device_camera_frame(wrong_mime_device) is None
    wrong_mime = capture_camera_for_device(wrong_mime_device)
    assert wrong_mime["ok"] is False

    oversized_device = "dev_capture_oversized"
    oversized = b"\xff\xd8\xff\xe0" + b"x" * MAX_VISION_IMAGE_BYTES
    with pytest.raises(VisionImageValidationError):
        validate_jpeg(oversized)
    with pytest.raises(TypeError, match="ValidatedJpeg"):
        update_device_camera_frame(oversized_device, oversized)
    assert get_device_camera_frame(oversized_device) is None
    too_large = capture_camera_for_device(oversized_device)
    assert too_large["ok"] is False


def test_validate_jpeg_requires_full_decode_and_rejects_bomb_warning(monkeypatch):
    valid = _fake_jpeg()
    assert validate_jpeg_bytes(valid) == valid

    marker_only = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
    truncated = valid[: len(valid) // 2]
    for invalid in (marker_only, truncated):
        with pytest.raises(VisionImageValidationError, match="JPEG"):
            validate_jpeg_bytes(invalid)

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 200)
    with pytest.raises(VisionImageValidationError, match="JPEG"):
        validate_jpeg_bytes(valid)


def test_validated_jpeg_is_controlled_and_has_explicit_pixel_limit(monkeypatch):
    import deskbot_server.llm.vision_input as vision_input

    valid = _fake_jpeg()
    validated = validate_jpeg(valid)

    assert isinstance(validated, ValidatedJpeg)
    assert validated.data == valid
    assert (validated.width, validated.height) == (16, 16)
    assert validated.pixel_count == 256
    assert MAX_VISION_IMAGE_PIXELS == 4096 * 4096
    with pytest.raises(TypeError, match="validate_jpeg"):
        ValidatedJpeg()
    with pytest.raises(AttributeError, match="immutable"):
        validated._width = 1

    monkeypatch.setattr(vision_input, "MAX_VISION_IMAGE_PIXELS", 200)
    with pytest.raises(VisionImageValidationError, match="pixel limit"):
        vision_input.validate_jpeg(valid)


def test_invalid_camera_frame_preserves_last_good_frame():
    dev = "dev_camera_preserve_last_good"
    valid = _fake_jpeg()
    truncated = valid[: len(valid) // 2]

    assert update_device_camera_frame(dev, validate_jpeg(valid))
    with pytest.raises(VisionImageValidationError):
        validate_jpeg(truncated)
    with pytest.raises(TypeError, match="ValidatedJpeg"):
        update_device_camera_frame(dev, truncated)
    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["jpeg"] == valid


def test_camera_frame_generation_rejects_late_old_connection_write():
    dev = "dev_camera_generation"
    generation_1 = begin_vision_generation(dev)
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        captured_at=100.0,
        generation=generation_1,
    )

    generation_2 = begin_vision_generation(dev)
    assert get_device_camera_frame(dev) is None
    assert not update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        captured_at=200.0,
        generation=generation_1,
    )
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        captured_at=300.0,
        generation=generation_2,
    )

    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["captured_at"] == 300.0
    assert row["generation"] == generation_2
    assert not clear_device_camera_frame(dev, generation=generation_1)
    assert get_device_camera_frame(dev) is not None


def test_disconnect_cleanup_revokes_inflight_generation():
    dev = "dev_camera_disconnect_generation"
    generation = begin_vision_generation(dev)
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=generation,
    )
    assert clear_device_camera_frame(dev, generation=generation)
    assert get_device_camera_frame(dev) is None
    assert not update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=generation,
    )


def test_finished_disconnect_cleanup_reclaims_tombstone_and_rejects_late_write():
    pytest.importorskip("cv2")
    from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline

    dev = "dev_camera_disconnect_reclaim"
    camera_pipe = AsrChatCameraPipeline(
        runtime=object(),  # type: ignore[arg-type]
        device_id=dev,
        frame_source="camera_uplink",
    )
    generation = camera_pipe.activate_generation()
    assert generation is not None
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=generation,
    )

    retired = asyncio.run(camera_pipe.finish_cached_state_cleanup(None))

    assert retired == generation
    assert current_vision_generation(dev) is None
    assert current_vision_source(dev) is None
    assert get_device_camera_frame(dev) is None
    assert not update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=generation,
    )


def test_forget_tombstone_requires_exact_retired_generation():
    dev = "dev_camera_tombstone_retired_generation_guard"
    generation = begin_vision_generation(dev)
    assert generation is not None
    tombstone = retire_device_camera_frame(
        dev,
        generation=generation,
    )
    assert tombstone is not None

    forged = VisionTombstone(
        device_id=dev,
        retired_generation=None,
        tombstone_generation=tombstone.tombstone_generation,
    )
    assert not forget_vision_tombstone(forged)
    assert (
        current_vision_generation(dev)
        == tombstone.tombstone_generation
    )
    assert forget_vision_tombstone(tombstone)
    assert current_vision_generation(dev) is None


def test_tombstone_cleanup_does_not_delete_racing_reconnect():
    pytest.importorskip("cv2")
    from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline

    dev = "dev_camera_tombstone_reconnect_race"
    camera_pipe = AsrChatCameraPipeline(
        runtime=object(),  # type: ignore[arg-type]
        device_id=dev,
        frame_source="camera_uplink",
    )
    old_generation = camera_pipe.activate_generation()
    assert old_generation is not None
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg((120, 80, 40)),
        generation=old_generation,
    )
    replacement: dict[str, int] = {}

    class RacingBroker:
        async def clear_device(self, device_id, *, generation=None):
            assert device_id == dev
            assert generation == old_generation
            new_generation = begin_vision_generation(
                device_id,
                source="camera_uplink",
            )
            assert new_generation is not None
            replacement["generation"] = new_generation
            assert update_device_camera_frame(
                device_id,
                _fake_validated_jpeg(),
                generation=new_generation,
                source="camera_uplink",
            )
            return False

    retired = asyncio.run(
        camera_pipe.finish_cached_state_cleanup(
            RacingBroker(),  # type: ignore[arg-type]
        )
    )

    new_generation = replacement["generation"]
    assert retired == old_generation
    assert current_vision_generation(dev) == new_generation
    assert current_vision_source(dev) == "camera_uplink"
    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["generation"] == new_generation

    replacement_tombstone = retire_device_camera_frame(
        dev,
        generation=new_generation,
    )
    assert replacement_tombstone is not None
    assert forget_vision_tombstone(replacement_tombstone)


def test_camera_cleanup_path_churn_reclaims_both_generation_maps():
    pytest.importorskip("cv2")
    from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline
    from deskbot_server.application.camera_broker import CameraImageBroker
    from deskbot_server.vision import generation as generation_state

    device_ids = {f"dev_camera_cleanup_churn_{index}" for index in range(128)}

    async def _send(_ws, _payload):
        return None

    async def _run():
        broker = CameraImageBroker(_send)
        for index, dev in enumerate(device_ids):
            camera_pipe = AsrChatCameraPipeline(
                runtime=object(),  # type: ignore[arg-type]
                device_id=dev,
                frame_source=(
                    "camera_uplink" if index % 2 == 0 else "asr_chat"
                ),
            )
            generation = camera_pipe.activate_generation()
            assert generation is not None
            assert update_device_camera_frame(
                dev,
                _fake_validated_jpeg(),
                generation=generation,
            )
            assert await broker.publish_validated(
                dev,
                _fake_validated_jpeg(),
                generation=generation,
            ) == (0, 0)
            assert (
                await camera_pipe.finish_cached_state_cleanup(broker)
                == generation
            )
        assert device_ids.isdisjoint(broker._last_by_device)

    asyncio.run(_run())

    assert all(current_vision_generation(dev) is None for dev in device_ids)
    assert device_ids.isdisjoint(generation_state._active_by_device)
    assert device_ids.isdisjoint(generation_state._active_source_by_device)


def test_device_lifecycle_cache_cleanup_reclaims_vision_tombstone():
    dev = "dev_lifecycle_vision_reclaim"
    generation = begin_vision_generation(dev)
    assert generation is not None
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=generation,
    )
    assert update_device_faces(
        dev,
        [{"face_id": 1, "person_name": "Alice"}],
        generation=generation,
    )

    tombstone = retire_device_camera_frame(dev, generation=generation)
    assert tombstone is not None
    assert clear_device_faces(dev, generation=generation)
    assert forget_vision_tombstone(tombstone)

    assert current_vision_generation(dev) is None
    assert current_vision_source(dev) is None
    assert get_device_camera_frame(dev) is None
    assert list_device_faces(dev) == {}


def test_unconditional_cleanup_does_not_delete_frame_from_racing_reconnect(
    monkeypatch,
):
    import deskbot_server.device_camera_frame_store as frame_store

    dev = "dev_camera_cleanup_reconnect_race"
    old_generation = begin_vision_generation(dev)
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg((120, 80, 40)),
        generation=old_generation,
    )
    real_invalidate = frame_store.invalidate_vision_generation
    replacement: dict[str, int] = {}

    def _invalidate_then_reconnect(device_id, *, expected_generation=None):
        result = real_invalidate(
            device_id,
            expected_generation=expected_generation,
        )
        new_generation = begin_vision_generation(device_id)
        replacement["generation"] = new_generation
        assert update_device_camera_frame(
            device_id,
            _fake_validated_jpeg(),
            generation=new_generation,
            source="camera_uplink",
        )
        return result

    monkeypatch.setattr(
        frame_store,
        "invalidate_vision_generation",
        _invalidate_then_reconnect,
    )
    assert frame_store.clear_device_camera_frame(dev)

    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["generation"] == replacement["generation"]
    assert row["source"] == "camera_uplink"


def test_asr_connection_without_camera_frames_does_not_claim_or_clear_vision_state(
):
    pytest.importorskip("cv2")
    from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline

    dev = "dev_lazy_asr_camera_generation"
    dedicated_generation = begin_vision_generation(dev)
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=dedicated_generation,
        source="camera_uplink",
    )

    asr_camera = AsrChatCameraPipeline(runtime=object(), device_id=dev)  # type: ignore[arg-type]
    assert asr_camera.connection_generation is None
    asr_camera.clear_cached_state()

    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["generation"] == dedicated_generation
    assert row["source"] == "camera_uplink"


def test_dedicated_camera_source_blocks_asr_frames_until_disconnect():
    pytest.importorskip("cv2")
    from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline

    dev = "dev_camera_source_priority"
    dedicated = AsrChatCameraPipeline(
        runtime=object(),  # type: ignore[arg-type]
        device_id=dev,
        frame_source="camera_uplink",
    )
    dedicated_generation = dedicated.activate_generation()
    assert dedicated_generation is not None

    asr_camera = AsrChatCameraPipeline(
        runtime=object(),  # type: ignore[arg-type]
        device_id=dev,
        frame_source="asr_chat",
    )
    assert asr_camera.activate_generation() is None
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=dedicated_generation,
        source="camera_uplink",
    )

    assert (
        asyncio.run(dedicated.finish_cached_state_cleanup(None))
        == dedicated_generation
    )
    resumed_generation = asr_camera.activate_generation()
    assert resumed_generation is not None
    assert resumed_generation != dedicated_generation
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        generation=resumed_generation,
        source="asr_chat",
    )


def test_camera_binary_parser_is_connection_local_and_drains_multiple_frames():
    from deskbot_server.ws.camera_uplink import _append_camera_binary

    jpeg_a = b"\xff\xd8" + b"a" * 48 + b"\xff\xd9"
    jpeg_b = b"\xff\xd8" + b"b" * 48 + b"\xff\xd9"
    split = len(jpeg_a) // 2
    first_connection = bytearray()
    replacement_connection = bytearray()

    frames, overflowed = _append_camera_binary(
        first_connection,
        jpeg_a[:split],
    )
    assert frames == []
    assert overflowed is False

    replacement_frames, overflowed = _append_camera_binary(
        replacement_connection,
        jpeg_a[split:],
    )
    assert replacement_frames == []
    assert overflowed is False

    frames, overflowed = _append_camera_binary(
        first_connection,
        jpeg_a[split:] + jpeg_b,
    )
    assert frames == [jpeg_a, jpeg_b]
    assert overflowed is False


def test_camera_binary_parser_bounds_incomplete_frame():
    from deskbot_server.ws.camera_uplink import (
        _MAX_CAMERA_JPEG_BYTES,
        _append_camera_binary,
    )

    buffer = bytearray()
    frames, overflowed = _append_camera_binary(
        buffer,
        b"\xff\xd8" + b"x" * _MAX_CAMERA_JPEG_BYTES,
    )
    assert frames == []
    assert overflowed is True
    assert buffer == bytearray()


def test_camera_view_snapshot_metadata_and_generation_replay_guard():
    from deskbot_server.application.camera_broker import CameraImageBroker

    dev = "dev_camera_view_generation"
    sent: dict[str, list] = {"first": [], "replacement": [], "new": []}

    async def _send(ws, payload):
        sent[ws].append(payload)

    async def _run():
        broker = CameraImageBroker(_send)
        generation_1 = begin_vision_generation(dev)
        captured_at = time.time()
        assert await broker.publish_validated(
            dev,
            _fake_validated_jpeg(),
            captured_at=captured_at,
            generation=generation_1,
        ) == (0, 0)
        await broker.add_subscriber("first", dev)
        await asyncio.sleep(0)

        generation_2 = begin_vision_generation(dev)
        assert generation_2 != generation_1
        await broker.add_subscriber("replacement", dev)
        await asyncio.sleep(0)
        assert await broker.publish_validated(
            dev,
            _fake_validated_jpeg(),
            captured_at=time.time(),
            generation=generation_2,
        ) == (2, 2)
        assert not await broker.clear_device(dev, generation=generation_1)
        await broker.add_subscriber("new", dev)
        await asyncio.sleep(0)
        return captured_at, generation_1, generation_2

    captured_at, generation, new_generation = asyncio.run(_run())
    meta = json.loads(sent["first"][0])
    assert meta["captured_at"] == captured_at
    assert meta["generation"] == generation
    assert sent["first"][1] == _fake_jpeg()
    assert json.loads(sent["replacement"][0])["generation"] == new_generation
    assert json.loads(sent["new"][0])["generation"] == new_generation


def test_camera_broker_publish_accepts_only_validated_jpeg():
    """裸 bytes 门面已删除：广播入口只接受 ValidatedJpeg 校验凭据。"""

    from deskbot_server.application.camera_broker import CameraImageBroker

    dev = "dev_camera_broker_reject_bad_jpeg"
    valid = _fake_jpeg()
    truncated = valid[: len(valid) // 2]
    sent: list[object] = []

    async def _send(_ws, payload):
        sent.append(payload)

    async def _run():
        broker = CameraImageBroker(_send)
        generation = begin_vision_generation(dev)
        await broker.add_subscriber("viewer", dev)
        await asyncio.sleep(0)
        with pytest.raises(TypeError, match="ValidatedJpeg"):
            await broker.publish_validated(
                dev,
                truncated,
                captured_at=time.time(),
                generation=generation,
            )
        assert sent == []

        assert await broker.publish_validated(
            dev,
            validate_jpeg(valid),
            captured_at=time.time(),
            generation=generation,
        ) == (1, 1)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert len(sent) == 2
    assert json.loads(sent[0])["type"] == "camera_frame"
    assert sent[1] == valid


def test_camera_schedule_validates_each_frame_once_off_event_loop(monkeypatch):
    from types import SimpleNamespace

    import deskbot_server.llm.vision_input as vision_input
    import deskbot_server.ws.asr_chat as asr_chat_module
    from deskbot_server.application.asr_chat_uplink import (
        AsrChatCameraPipeline,
        LatestCameraInferenceWorker,
    )
    from deskbot_server.application.camera_broker import CameraImageBroker
    from deskbot_server.ws.asr_chat import _schedule_camera_jpeg

    dev = "dev_camera_schedule_validation"
    valid = _fake_jpeg()
    truncated = valid[: len(valid) // 2]
    validation_calls: list[tuple[bytes, int]] = []
    event_loop_thread = threading.get_ident()
    real_validate = vision_input.validate_jpeg_with_rgb

    def _counted_validate(jpeg_bytes):
        validation_calls.append((bytes(jpeg_bytes), threading.get_ident()))
        return real_validate(jpeg_bytes)

    monkeypatch.setattr(
        asr_chat_module,
        "validate_jpeg_with_rgb",
        _counted_validate,
    )
    # Trusted camera internals no longer import validate_jpeg at all: the
    # store facade and broker accept only ValidatedJpeg proofs, so re-decode
    # is impossible by construction (enforced by their TypeError guards).

    class _Detector:
        last_bgr = None

        def detect_faces(self, frame, *, decoded_rgb=None):
            assert frame == valid
            # The ingress validation decode is reused for inference.
            assert decoded_rgb is not None
            return []

    class _Tracker:
        def assign_ids(self, faces):
            return faces

    camera_pipe = AsrChatCameraPipeline(
        runtime=SimpleNamespace(frame_width=320, frame_height=240),
        device_id=dev,
        detector=_Detector(),
        face_tracker=_Tracker(),
    )

    class _Hub:
        pb_idle_snore = None

    async def _send(_ws, _payload):
        return None

    async def _run():
        worker = LatestCameraInferenceWorker(device_id=dev)
        broker = CameraImageBroker(_send)
        common = {
            "image_broker": broker,
            "camera_inference_worker": worker,
            "device_id": dev,
        }
        assert not await _schedule_camera_jpeg(
            camera_pipe,
            truncated,
            **common,
        )
        assert not worker.running
        assert await _schedule_camera_jpeg(
            camera_pipe,
            valid,
            **common,
        )
        # Ingress preview is visible before the background face worker runs.
        assert broker._last_by_device[dev][1] == valid
        await worker.wait_idle()
        await worker.close()
        return broker

    broker = asyncio.run(_run())
    assert [payload for payload, _thread in validation_calls] == [truncated, valid]
    assert all(thread_id != event_loop_thread for _, thread_id in validation_calls)
    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["jpeg"] == valid
    assert broker._last_by_device[dev][1] == valid


def test_camera_task_does_not_reclaim_generation_after_same_source_reconnect(
    monkeypatch,
):
    from types import SimpleNamespace

    from deskbot_server.application.asr_chat_uplink import (
        AsrChatCameraPipeline,
        LatestCameraInferenceWorker,
    )
    from deskbot_server.ws.asr_chat import _schedule_camera_jpeg

    dev = "dev_camera_same_source_reconnect_before_task_start"
    old_frame = _fake_jpeg((120, 80, 40))
    replacement_frame = _fake_jpeg((10, 200, 30))
    camera_pipe = AsrChatCameraPipeline(
        runtime=SimpleNamespace(frame_width=320, frame_height=240),
        device_id=dev,
        frame_source="asr_chat",
    )
    activate_calls = 0
    ensure_ready_calls = 0

    real_activate = camera_pipe.activate_generation

    def _counted_activate():
        nonlocal activate_calls
        activate_calls += 1
        return real_activate()

    async def _must_not_initialize_detector():
        nonlocal ensure_ready_calls
        ensure_ready_calls += 1
        return False

    monkeypatch.setattr(camera_pipe, "activate_generation", _counted_activate)
    monkeypatch.setattr(
        camera_pipe,
        "ensure_ready",
        _must_not_initialize_detector,
    )

    async def _run():
        worker = LatestCameraInferenceWorker(device_id=dev)

        class _Broker:
            async def publish_validated(self, *_args, **_kwargs):
                return 0, 0

        assert await _schedule_camera_jpeg(
            camera_pipe,
            old_frame,
            image_broker=_Broker(),
            camera_inference_worker=worker,
            device_id=dev,
        )
        old_generation = camera_pipe.connection_generation
        assert old_generation is not None

        # ``create_task`` has queued the old frame but has not run it yet.
        replacement_generation = begin_vision_generation(
            dev,
            source="asr_chat",
        )
        assert replacement_generation is not None
        assert replacement_generation != old_generation
        assert update_device_camera_frame(
            dev,
            validate_jpeg(replacement_frame),
            source="asr_chat",
            generation=replacement_generation,
        )

        await worker.wait_idle()
        await worker.close()
        return old_generation, replacement_generation

    old_generation, replacement_generation = asyncio.run(_run())

    assert activate_calls == 1
    assert ensure_ready_calls == 0
    assert camera_pipe.connection_generation == old_generation
    assert current_vision_generation(dev) == replacement_generation
    assert current_vision_source(dev) == "asr_chat"
    row = get_device_camera_frame(dev)
    assert row is not None
    assert row["generation"] == replacement_generation
    assert row["jpeg"] == replacement_frame


def test_latest_camera_inference_worker_keeps_only_newest_pending_job():
    from deskbot_server.application.asr_chat_uplink import (
        LatestCameraInferenceWorker,
    )

    async def _run():
        worker = LatestCameraInferenceWorker(device_id="latest-wins")
        started = asyncio.Event()
        release = asyncio.Event()
        observed: list[str] = []

        async def _first():
            observed.append("first")
            started.set()
            await release.wait()

        async def _second():
            observed.append("second")

        async def _third():
            observed.append("third")

        assert worker.submit(_first)
        await started.wait()
        assert worker.submit(_second)
        assert worker.submit(_third)
        release.set()
        await worker.wait_idle()
        await worker.close()
        return observed

    assert asyncio.run(_run()) == ["first", "third"]


def test_camera_frame_bytes_are_actively_expired():
    import time

    import deskbot_server.device_camera_frame_store as store

    dev = "dev_camera_byte_ttl"
    assert store.update_device_camera_frame(dev, _fake_validated_jpeg())
    with store._frame_ready:
        store._by_device[dev]["expires_monotonic"] = time.monotonic() - 1.0
        assert store._purge_expired_frames_locked(time.monotonic()) == 1
    assert store.get_device_camera_frame(dev) is None


def test_camera_pipeline_detector_failure_publishes_only_validated_frame():
    from deskbot_server.application.camera_jpeg_pipeline import (
        process_camera_jpeg_frame,
    )

    dev = "dev_camera_pipeline_validated_detector_failure"
    generation = begin_vision_generation(dev)
    valid = _fake_jpeg()
    validated = validate_jpeg(valid)
    published: list[bytes] = []

    class _Detector:
        last_bgr = None

        def detect_faces(self, _frame):
            raise RuntimeError("simulated detector failure")

    class _Broker:
        async def publish_validated(self, _device_id, frame, **_kwargs):
            published.append(frame.data)
            return 1, 1

    async def _run():
        return await process_camera_jpeg_frame(
            device_id=dev,
            validated_jpeg=validated,
            detector=_Detector(),
            face_tracker=object(),
            runtime=object(),
            image_broker=_Broker(),
            frame_source="asr_chat",
            log_channel="test",
            connection_generation=generation,
        )

    result = asyncio.run(_run())
    assert result.detected is False
    assert result.view_attempted == 1
    assert published == [valid]


def test_camera_pipeline_detected_frame_follows_up_meta_only_after_ingress():
    """Ingress already previewed the JPEG; detection must not re-send it."""

    from deskbot_server.application.camera_jpeg_pipeline import (
        process_camera_jpeg_frame,
    )

    dev = "dev_camera_pipeline_face_meta_single_send"
    generation = begin_vision_generation(dev)
    valid = _fake_jpeg()
    validated = validate_jpeg(valid)
    face = {
        "points": [{"name": "nose", "x": 160.0, "y": 120.0}],
        "landmarks": [],
        "image_w": 320,
        "image_h": 240,
    }

    class _Detector:
        last_bgr = None

        def detect_faces(self, _frame, *, decoded_rgb=None):
            return [dict(face)]

    class _Tracker:
        def assign_ids(self, faces):
            for idx, item in enumerate(faces):
                item["face_id"] = idx + 1
            return faces

    frame_publishes: list = []
    meta_publishes: list = []

    class _Broker:
        async def publish_validated(self, _device_id, frame, **_kwargs):
            frame_publishes.append(frame.data)
            return 1, 1

        async def publish_face_meta(self, _device_id, **kwargs):
            meta_publishes.append(kwargs)
            return 1, 1

    async def _run():
        return await process_camera_jpeg_frame(
            device_id=dev,
            validated_jpeg=validated,
            detector=_Detector(),
            face_tracker=_Tracker(),
            runtime=object(),
            image_broker=_Broker(),
            frame_source="asr_chat",
            log_channel="test",
            connection_generation=generation,
            publish_undetected=False,
        )

    result = asyncio.run(_run())
    assert result.detected is True
    assert result.view_attempted == 1
    # The JPEG must reach /camera_view exactly once (the ingress preview);
    # the post-inference follow-up carries analysis metadata only.
    assert frame_publishes == []
    assert len(meta_publishes) == 1
    assert meta_publishes[0]["points"]
    # face_meta 配对键：随 publish_face_meta 消息透传给前端。
    assert meta_publishes[0]["captured_at"] is not None
    assert meta_publishes[0]["generation"] == generation


def test_camera_broker_face_meta_is_text_only():
    from deskbot_server.application.camera_broker import CameraImageBroker

    dev = "dev_camera_broker_face_meta_text_only"
    sent: list[object] = []

    async def _send(_ws, payload):
        sent.append(payload)

    async def _run():
        broker = CameraImageBroker(_send)
        generation = begin_vision_generation(dev)
        await broker.add_subscriber("viewer", dev)
        captured_at = time.time()
        assert await broker.publish_validated(
            dev,
            _fake_validated_jpeg(),
            captured_at=captured_at,
            generation=generation,
        ) == (1, 1)
        await asyncio.sleep(0)
        assert await broker.publish_face_meta(
            dev,
            landmarks=[{"name": "left_eye", "x": 1.0, "y": 2.0}],
            frame_w=320,
            frame_h=240,
            points=[{"name": "nose", "x": 160.0, "y": 120.0}],
            face_count=1,
            face_id=7,
            captured_at=captured_at,
            generation=generation,
        ) == (1, 1)
        await asyncio.sleep(0.01)

        # A stale generation can never publish trailing metadata.
        stale = await broker.publish_face_meta(
            dev,
            frame_w=320,
            frame_h=240,
            captured_at=time.time(),
            generation=generation - 1,
        )
        assert stale == (0, 0)

    asyncio.run(_run())
    assert len(sent) == 3
    assert json.loads(sent[0])["type"] == "camera_frame"
    assert isinstance(sent[1], (bytes, bytearray))
    face_meta = json.loads(sent[2])
    assert face_meta["type"] == "face_meta"
    assert face_meta["detected"] is True
    assert face_meta["face_id"] == 7
    assert face_meta["landmarks"]
    # No binary follows a face_meta message.
    assert all(not isinstance(item, (bytes, bytearray)) for item in sent[2:])


def test_face_snapshot_has_capture_metadata_and_generation_isolation():
    dev = "dev_face_generation"
    generation_1 = begin_vision_generation(dev)
    captured_1 = time.time()
    assert update_device_faces(
        dev,
        [{"face_id": 1, "person_id": 1, "person_name": "Alice"}],
        captured_at=captured_1,
        generation=generation_1,
    )
    first = list_device_faces(dev)[1]
    assert first["captured_at"] == captured_1
    assert first["generation"] == generation_1

    generation_2 = begin_vision_generation(dev)
    assert list_device_faces(dev) == {}
    assert not update_device_faces(
        dev,
        [{"face_id": 1, "person_id": 1, "person_name": "Alice"}],
        captured_at=time.time(),
        generation=generation_1,
    )
    assert update_device_faces(
        dev,
        [{"face_id": 2, "person_id": 2, "person_name": "Bob"}],
        captured_at=time.time(),
        generation=generation_2,
    )
    assert not clear_device_faces(dev, generation=generation_1)
    assert list_device_faces(dev)[2]["person_name"] == "Bob"


def test_face_snapshot_rejects_result_from_an_already_stale_frame():
    dev = "dev_stale_face_inference"
    generation = begin_vision_generation(dev)
    assert not update_device_faces(
        dev,
        [{"face_id": 1, "person_id": 1, "person_name": "Alice"}],
        captured_at=time.time() - 30.0,
        generation=generation,
    )
    assert list_device_faces(dev) == {}


def test_async_capture_timeout_returns_stale_error_not_old_frame():
    dev = "dev_stale_capture"
    generation = begin_vision_generation(dev)
    assert update_device_camera_frame(
        dev,
        _fake_validated_jpeg(),
        captured_at=time.time() - 30.0,
        generation=generation,
    )

    async def _run():
        return await capture_camera_for_device_async(
            dev,
            hub=None,
            max_age_s=0.05,
            wait_timeout_s=0.05,
        )

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert result["stale"] is True
    assert result["captured_at"] < time.time() - 20.0


def test_parse_llm_reply_ignores_cam_fps():
    # A1：cam_fps 已从 LLM 回复协议移除，模型写了也不进入 parsed。
    parsed = parse_llm_reply('{"tts":"好","cam_fps":5,"tools":[]}')
    assert parsed["json_ok"] is True
    assert "cam_fps" not in parsed


def test_build_cam_fps_signal_pb():
    msg = build_cam_fps_signal_pb(cam_fps=5)
    assert msg["type"] == "pb_single"
    assert msg["cam_fps"] == 5
    assert "camera_once" not in msg

    one_shot = build_cam_fps_signal_pb(cam_fps=2, one_shot=True)
    assert one_shot["camera_once"] is True
    assert one_shot["cam_fps"] == 2


def test_face_snapshot_state_distinguishes_completed_no_face_frame():
    dev = "dev_completed_no_face"
    generation = begin_vision_generation(dev)
    captured_at = time.time()
    assert update_device_faces(
        dev,
        [],
        captured_at=captured_at,
        generation=generation,
    )
    state = get_device_face_snapshot_state(dev)
    assert state is not None
    assert state["faces"] == {}
    assert state["captured_at"] == pytest.approx(captured_at)


def test_pb_json_messages_cam_fps_on_chain():
    row = {"chunk_ms": 50, "anim": [make_anim_item({}, 50)]}
    pairs = pb_json_messages(
        pb_req="req1",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b""],
        cam_fps=parse_pb_cam_fps(4),
    )
    msg, _ = pairs[0]
    assert msg["cam_fps"] == 4
