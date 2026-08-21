from __future__ import annotations

import asyncio
import time


def test_pcm_alignment_never_manufactures_silence_for_audio_free_segment():
    from deskbot_server.pb.servo_pcm import align_pcm_s16le_mono_to_chunk_ms

    pcm, chunk_ms = align_pcm_s16le_mono_to_chunk_ms(b"", 2000, 24000)
    assert pcm == b""
    assert chunk_ms == 2000


def test_anim_only_plan_keeps_all_frames_without_pcm():
    from deskbot_server.pb.llm_plan import interleave_tts_segs_with_llm_plan

    frames = [
        {"ms": 80, "elements": {"extra": [{"shape": "circle", "x": 1}]}},
        {"ms": 120, "elements": {"extra": [{"shape": "circle", "x": 2}]}},
    ]
    segs, servos, anims = interleave_tts_segs_with_llm_plan(
        [],
        [],
        frames,
        24000,
    )

    assert [seg["ms"] for seg in segs] == [80, 120]
    assert all(seg["pcm"] == b"" for seg in segs)
    assert servos == [None, None]
    assert [anim["extra"][0]["x"] for anim in anims if anim] == [1, 2]


def test_pure_move_and_anim_plan_builds_json_only_wire(monkeypatch):
    from deskbot_server.pb import wire

    move_steps = [
        {"xm": 0, "ym": 0, "x": 90, "y": 75, "ms": 100},
        {"xm": 0, "ym": 0, "x": 90, "y": 90, "ms": 100},
    ]
    anim_frames = [
        {
            "ms": 50,
            "elements": {
                "mouth": [{"shape": "line", "x1": 1, "y1": 1, "x2": 2, "y2": 2}]
            },
        },
        {
            "ms": 150,
            "elements": {
                "mouth": [{"shape": "circle", "x": 2, "y": 2, "r": 1}]
            },
        },
    ]
    monkeypatch.setattr(wire, "expand_llm_moves", lambda *_a, **_k: move_steps)
    monkeypatch.setattr(wire, "expand_llm_anims", lambda *_a, **_k: anim_frames)

    pairs, pb_req, count, _sample_rate = wire.build_pb_wire_pairs(
        [],
        {"sample_rate": 24000, "output_codec": "opus"},
        moves=[{"move": "nod_head", "ms": 200}],
        anims=[{"anim": "happy", "ms": 200}],
        sample_rate=24000,
        request_id="audio-free-plan",
    )

    assert pb_req == "audio-free-plan"
    assert count == len(pairs) == 1
    msg, binaries = pairs[0]
    assert binaries == []
    assert "audio" not in msg
    assert "sr" not in msg
    assert "fmt" not in msg
    assert "ch" not in msg
    assert sum(int(item["ms"]) for item in msg["anim"]) == msg["chunk_ms"] == 200
    assert msg["servo"] == move_steps
    # Both requested expression frames survive the shared motion/display clock.
    assert {item["elements"]["mouth"][0]["shape"] for item in msg["anim"]} == {
        "line",
        "circle",
    }


def test_combined_tts_and_moves_keeps_pcm_synchronization(monkeypatch):
    from deskbot_server.pb import wire

    move_steps = [
        {"xm": 0, "ym": 0, "x": 90, "y": 75, "ms": 100},
        {"xm": 0, "ym": 0, "x": 90, "y": 90, "ms": 150},
    ]
    monkeypatch.setattr(wire, "expand_llm_moves", lambda *_a, **_k: move_steps)
    monkeypatch.setattr(wire, "expand_llm_anims", lambda *_a, **_k: [])

    pairs, _pb_req, count, _sample_rate = wire.build_pb_wire_pairs(
        [{"phoneme": "a", "ms": 100, "pcm": b"\x01\x00" * 2400}],
        {"sample_rate": 24000, "output_codec": "s16le"},
        moves=[{"move": "nod_head", "ms": 250}],
        sample_rate=24000,
        request_id="tts-plus-motion",
    )

    assert count == len(pairs) == 2
    msg, binaries = pairs[0]
    tail, tail_binaries = pairs[1]
    assert msg["chunk_ms"] == 100
    assert msg["audio"]["next_bin_len"] == 4800
    assert len(binaries) == 1 and len(binaries[0]) == 4800
    assert msg["servo"] == move_steps
    assert tail["chunk_ms"] == 150
    assert "audio" not in tail
    assert tail_binaries == []


def test_pure_move_plan_never_claims_the_display_lane(monkeypatch):
    from deskbot_server.pb import wire

    move_steps = [
        {"xm": 0, "ym": 0, "x": 20, "y": 90, "ms": 180},
        {"xm": 0, "ym": 0, "x": 160, "y": 90, "ms": 220},
    ]
    monkeypatch.setattr(wire, "expand_llm_moves", lambda *_a, **_k: move_steps)
    monkeypatch.setattr(wire, "expand_llm_anims", lambda *_a, **_k: [])

    pairs, _pb_req, count, _sample_rate = wire.build_pb_wire_pairs(
        [],
        {"sample_rate": 24000, "output_codec": "s16le"},
        moves=[{"move": "look_left", "ms": 400}],
        sample_rate=24000,
        request_id="motor-only",
    )

    assert count == len(pairs) == 1
    message, binaries = pairs[0]
    assert binaries == []
    assert message["servo"] == move_steps
    assert message["chunk_ms"] == 400
    assert "anim" not in message
    assert "audio" not in message
    assert "mouth_only" not in message


def test_image_only_plan_creates_an_explicit_display_row():
    import io

    from PIL import Image

    from deskbot_server.pb import wire
    from deskbot_server.pb.llm_display import LLM_DISPLAY_IMAGE_HOLD_MS

    output = io.BytesIO()
    Image.new("RGB", (16, 16), color=(12, 34, 56)).save(output, format="JPEG")
    image = {"bytes": output.getvalue(), "x": 0, "y": 0, "w": 16, "h": 16}

    pairs, pb_req, count, _sample_rate = wire.build_pb_wire_pairs(
        [],
        {"sample_rate": 24000, "output_codec": "s16le"},
        images=[image],
        sample_rate=24000,
        request_id="image-only",
    )

    assert pb_req == "image-only"
    assert count == len(pairs) == 1
    message, binaries = pairs[0]
    assert message["chunk_ms"] == LLM_DISPLAY_IMAGE_HOLD_MS
    assert message["anim"][0]["elements"]["extra"][0]["shape"] == "image"
    assert message["assets"][0]["next_bin_len"] == len(image["bytes"])
    assert binaries == [image["bytes"]]
    assert "audio" not in message
    assert "servo" not in message


def test_empty_tts_with_explicit_motion_never_calls_tts(monkeypatch):
    import deskbot_server.application.chat_flow as chat_flow

    parsed = {
        "reply": "",
        "raw": '{"tts":"","moves":[{"move":"nod_head","ms":600}]}',
        "json_ok": True,
        "need_reply": True,
        "scenes": [],
        "moves": [{"move": "nod_head", "ms": 600}],
        "anims": [],
        "images": [],
        "tools": [],
        "volume": None,
    }
    playback_calls: list[dict] = []

    async def fake_complete(*_args, **_kwargs):
        return parsed, [], [], parsed["raw"]

    async def fake_playback(_downlink, _chat, **kwargs):
        playback_calls.append(kwargs)
        kwargs["result"].playback_status = "played"
        kwargs["result"].t_tts_end = time.monotonic()

    monkeypatch.setattr(chat_flow, "complete_llm_with_tool_loop", fake_complete)
    monkeypatch.setattr(chat_flow, "_run_pb_playback", fake_playback)

    class _Downlink:
        def __init__(self) -> None:
            self.stages: list[str] = []

        async def emit_stage(self, stage, **_kwargs):
            self.stages.append(stage)

    class _Chat:
        async def tts_phoneme_segments(self, *_args, **_kwargs):
            raise AssertionError("pure motion must not invoke TTS")

    async def _run() -> None:
        downlink = _Downlink()
        result = await chat_flow.run_chat_turn(
            downlink,
            _Chat(),
            "点个头",
            request_id="pure-motion-turn",
            force_voice=True,
        )
        assert result.playback_status == "played"
        assert len(playback_calls) == 1
        assert playback_calls[0]["motion_only"] is True
        assert playback_calls[0]["reply_text"] == ""
        assert "tts_start" not in downlink.stages

    asyncio.run(_run())


def test_expression_only_playbook_does_not_send_empty_legacy_pb(monkeypatch):
    import deskbot_server.application.chat_flow as chat_flow
    import deskbot_server.application.expression_runtime as expression_runtime

    expression_calls: list[dict] = []

    class _Runtime:
        async def play_scene(self, name, **kwargs):
            expression_calls.append({"name": name, **kwargs})
            return type("ExpressionResult", (), {"ok": True, "error": None})()

    async def fail_playback(*_args, **_kwargs):
        raise AssertionError("expression-only phase must not create an empty PB")

    monkeypatch.setattr(expression_runtime, "get_expression_runtime", lambda _device_id: _Runtime())
    monkeypatch.setattr(chat_flow, "_run_pb_playback", fail_playback)

    async def _run() -> None:
        result = await chat_flow.run_device_playbook(
            object(),
            object(),
            {
                "name": "face_only",
                "chunks": [
                    {
                        "id": "face",
                        "text": "",
                        "expr": {"scene": "happy", "ms": 800},
                    }
                ],
            },
            request_id="face-only",
            device_id="deskbot-local",
        )
        assert result.status == "ok"
        assert result.error is None
        assert [call["name"] for call in expression_calls] == ["happy"]

    asyncio.run(_run())
