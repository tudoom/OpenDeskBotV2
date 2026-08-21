from __future__ import annotations

import json

from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.pb.llm_plan import (
    expand_llm_anims,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
    merge_llm_plan_anim_rows,
)


def test_parse_llm_reply_tool_only_array():
    raw = '[{"tool":"capture_camera","display":false}]'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["tools"] == [{"tool": "capture_camera", "display": False}]
    assert parsed["reply"] == ""


def test_parse_llm_reply_keeps_moves_but_discards_legacy_model_anims():
    raw = (
        '{"need_reply": true, "tts": "你好", '
        '"moves": [{"move": "nod_head", "ms": 540}], '
        '"anims": [{"anim": "default", "ms": 1500}]}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == "你好"
    assert parsed["moves"] == [{"move": "nod_head", "ms": 540}]
    assert parsed["anims"] == []


def test_parse_llm_reply_drops_unknown_moves_and_raw_servo_coordinates():
    raw = (
        '{"need_reply": false, "tts": "", '
        '"moves": [{"move": "invented_motion", "ms": 500}], '
        '"servo": [{"xm": 0, "ym": 0, "x": 180, "y": 70, "ms": 500}]}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["moves"] == []
    assert "servo" not in parsed


def test_parse_llm_reply_rejects_whole_mixed_move_lane():
    parsed = parse_llm_reply(
        '{"tts":"ok","moves":['
        '{"move":"nod_head","ms":500},'
        '{"move":"invented_motion","ms":500}]}'
    )
    assert parsed["moves"] == []


def test_expand_llm_moves_scales_preset_steps():
    steps = expand_llm_moves([{"move": "nod_head", "ms": 1080}])
    assert len(steps) == 3
    assert sum(s["ms"] for s in steps) == 1080


def test_expand_llm_anims_bg_color():
    frames = expand_llm_anims(
        [{"anim": "default", "ms": 200, "bg": "#000000", "color": "yellow"}]
    )
    assert frames
    bg = frames[0]["elements"].get("bg") or []
    assert bg and bg[0]["shape"] == "rect"
    assert bg[0].get("color") == "#000000"


def test_expand_llm_anims_fallback_default():
    frames = expand_llm_anims([{"anim": "__no_such_anim__", "ms": 800}])
    assert frames
    assert sum(f["ms"] for f in frames) == 800
    assert isinstance(frames[0].get("elements"), dict)


def test_interleave_tts_with_llm_plan_parallel():
    segs = [{"phoneme": "n", "ms": 100, "pcm": b"\x00" * 4800}]
    move_steps = [{"xm": 1, "ym": 1, "x": 0, "y": 10, "ms": 200}]
    anim_frames = [{"ms": 150, "elements": {"mouth": [], "eye_l": [], "eye_r": [], "nose": [], "extra": []}}]
    out, servo, anim = interleave_tts_segs_with_llm_plan(segs, move_steps, anim_frames, 24000)
    assert [segment["ms"] for segment in out] == [100, 50, 50]
    assert out[0]["pcm"]
    assert out[1]["pcm"] == out[2]["pcm"] == b""
    assert servo[0] == [move_steps[0]]
    assert servo[1:] == [None, None]
    assert anim[0] is not None


def test_merge_llm_plan_anim_rows_keeps_emotion_mouth_on_silence():
    """纯表情 pb 包（静音承载时长）应保留情绪口型，不被默认嘴型覆盖。"""
    segs = [{"phoneme": "", "ms": 2000, "pcm": b"\x00" * 96000}]
    phoneme_rows = [
        {
            "idx": 0,
            "chunk_ms": 2000,
            "anim": [
                {
                    "elements": {
                        "mouth": [{"shape": "round_rect", "x": 148, "y": 153, "w": 56, "h": 18}],
                        "eye_l": [],
                        "eye_r": [],
                        "nose": [],
                        "extra": [],
                    },
                    "ms": 2000,
                }
            ],
        }
    ]
    plan_el = {
        "mouth": [{"shape": "round_rect", "x": 163, "y": 147, "w": 28, "h": 32}],
        "eye_l": [{"shape": "ellipse_fill", "x": 105, "y": 96, "rw": 15, "rh": 15}],
        "eye_r": [],
        "nose": [],
        "extra": [],
    }
    merged = merge_llm_plan_anim_rows(segs, phoneme_rows, [plan_el])
    mouth = merged[0]["anim"][0]["elements"]["mouth"]
    assert mouth == plan_el["mouth"]


def test_merge_llm_plan_anim_rows_keeps_phoneme_mouth():
    segs = [{"phoneme": "a", "ms": 100, "pcm": b"\x00" * 4800}]
    phoneme_rows = [
        {
            "idx": 0,
            "chunk_ms": 100,
            "anim": [
                {
                    "elements": {
                        "mouth": [{"shape": "rect", "x": 1, "y": 2, "w": 3, "h": 4}],
                        "eye_l": [],
                        "eye_r": [],
                        "nose": [],
                        "extra": [],
                    },
                    "ms": 100,
                    "phoneme": "a",
                }
            ],
        }
    ]
    plan_el = {
        "mouth": [{"shape": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1}],
        "eye_l": [{"shape": "circle", "x": 1, "y": 2, "r": 3}],
        "eye_r": [],
        "nose": [],
        "extra": [],
    }
    merged = merge_llm_plan_anim_rows(segs, phoneme_rows, [plan_el])
    mouth = merged[0]["anim"][0]["elements"]["mouth"]
    assert mouth == phoneme_rows[0]["anim"][0]["elements"]["mouth"]
    assert merged[0]["anim"][0]["elements"]["eye_l"] == plan_el["eye_l"]


def test_llm_face_context_prompt_appendix():
    from deskbot_server.llm.utils import llm_static_context_prompt_appendix

    text = llm_static_context_prompt_appendix()
    assert "register_face" in text
    assert "长期记忆" in text
    assert "face_id=" not in text


def test_build_llm_user_message():
    from deskbot_server.face_snapshot_cache import update_device_faces
    from deskbot_server.llm.user_message import build_llm_user_message

    dev = "test_device_user_msg"
    update_device_faces(
        dev,
        [
            {
                "face_id": 1,
                "person_name": "小明",
                "identity_score": 0.82,
                "face_score": 0.95,
                "person_id": 1,
                "image_w": 320,
                "image_h": 240,
                "landmarks": [{"name": "nose", "x": 200, "y": 140}],
                "points": [],
            },
        ],
    )
    ack = '{"type":"pb_ack","servo":{"x":90,"y":75}}'
    msg = build_llm_user_message("你好", device_id=dev, device_context=ack)
    assert "舵机角度" not in msg
    assert "faceid=1" in msg
    assert "name=小明" in msg
    assert "脸中心位置=(200,140)" in msg
    assert "用户正文: 你好" in msg

    silent = build_llm_user_message("", device_id=dev, device_context=ack)
    assert "用户正文: [未说话]" in silent


def test_parse_llm_tools():
    raw = '{"tts":"好","tools":[{"tool":"memory_add","text":"喜欢猫"}]}'
    parsed = parse_llm_reply(raw)
    assert parsed["tools"] == [{"tool": "memory_add", "text": "喜欢猫"}]


def test_parse_llm_reply_volume():
    raw = '{"tts":"好","volume":75,"moves":[],"anims":[]}'
    parsed = parse_llm_reply(raw)
    assert parsed["volume"] == 75


def test_resize_jpeg_for_lcd_display():
    import base64
    import io

    from PIL import Image

    from deskbot_server.pb.llm_display import decode_llm_image_item, jpeg_blob_dimensions

    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color=(40, 120, 200)).save(buf, format="JPEG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    dec = decode_llm_image_item({"b64": b64, "x": 0, "y": 0, "w": 284, "h": 240})
    assert dec is not None
    assert dec["w"] == 284 and dec["h"] == 240
    assert jpeg_blob_dimensions(dec["bytes"]) == (284, 240)


def test_parse_llm_reply_images():
    import base64
    import io

    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (100, 80), color=(12, 34, 56)).save(image, format="JPEG")
    b64 = base64.standard_b64encode(image.getvalue()).decode()
    raw = json.dumps(
        {
            "tts": "看",
            "images": [{"b64": b64, "x": 0, "y": 0, "w": 100, "h": 80}],
        },
        ensure_ascii=False,
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert len(parsed["images"]) == 1


def test_llm_display_same_size_png_is_always_canonical_jpeg():
    import base64
    import io

    from PIL import Image

    from deskbot_server.pb.llm_display import decode_llm_image_item

    source = io.BytesIO()
    Image.new("RGBA", (100, 80), color=(40, 120, 200, 128)).save(
        source,
        format="PNG",
    )
    encoded = base64.b64encode(source.getvalue()).decode("ascii")

    decoded = decode_llm_image_item(
        {
            "b64": f"data:image/png;base64,{encoded}",
            "w": 100,
            "h": 80,
        }
    )

    assert decoded is not None
    assert decoded["bytes"].startswith(b"\xff\xd8\xff")
    assert not decoded["bytes"].startswith(b"\x89PNG")
    with Image.open(io.BytesIO(decoded["bytes"])) as image:
        assert image.format == "JPEG"
        assert image.size == (100, 80)
        image.load()


def test_llm_display_rejects_invalid_base64_truncated_images_and_mime_spoofing():
    import base64
    import io

    from PIL import Image

    from deskbot_server.pb.llm_display import decode_llm_image_item

    source = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 30, 40)).save(source, format="PNG")
    png = source.getvalue()
    encoded = base64.b64encode(png).decode("ascii")

    assert decode_llm_image_item({"b64": encoded + "!"}) is None
    assert decode_llm_image_item(
        {"b64": base64.b64encode(png[:20]).decode("ascii")}
    ) is None
    assert decode_llm_image_item(
        {"b64": f"data:image/jpeg;base64,{encoded}"}
    ) is None


def test_llm_display_enforces_pixel_count_image_count_and_asset_budget(monkeypatch):
    import base64
    import io

    from PIL import Image

    import deskbot_server.pb.llm_display as display

    source = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 30, 40)).save(source, format="PNG")
    item = {"b64": base64.b64encode(source.getvalue()).decode("ascii"), "w": 8, "h": 8}

    monkeypatch.setattr(display, "MAX_LLM_DISPLAY_IMAGE_PIXELS", 63)
    assert display.decode_llm_image_item(item) is None

    monkeypatch.setattr(display, "MAX_LLM_DISPLAY_IMAGE_PIXELS", 4096 * 4096)
    monkeypatch.setattr(display, "MAX_LLM_DISPLAY_IMAGES", 2)
    parsed = display.parse_llm_images([item, item, item])
    assert len(parsed) == 2

    monkeypatch.setattr(
        display,
        "MAX_LLM_DISPLAY_TOTAL_ASSET_BYTES",
        len(parsed[0]["bytes"]) - 1,
    )
    assert display.parse_llm_images([item]) == []


def test_llm_display_rejects_oversized_base64_before_decoding(monkeypatch):
    import deskbot_server.pb.llm_display as display

    called = False

    def fail_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized payload must be rejected before base64 decode")

    monkeypatch.setattr(display, "MAX_LLM_DISPLAY_IMAGE_BYTES", 3)
    monkeypatch.setattr(display.base64, "b64decode", fail_decode)

    assert display.decode_llm_image_item({"b64": "A" * 8}) is None
    assert called is False


def test_device_volume_persist(tmp_path, monkeypatch):
    from deskbot_server import device_volume_store as dvs

    vol_file = tmp_path / "device_volume.json"

    def _resolve(path):
        return str(vol_file)

    monkeypatch.setattr(dvs, "resolve_json_path", _resolve)
    monkeypatch.setattr(dvs, "DEVICE_VOLUME_FILE", str(vol_file))
    assert dvs.persist_device_volume(55) == 55
    assert dvs.get_device_volume() == 55
    assert dvs.persist_device_volume(90) == 90
    assert dvs.get_device_volume() == 90
    vol_file.write_text('{"devices":{"retired-hardware":12}}', encoding="utf-8")
    assert dvs.get_device_volume() == 80
    raw = '{"tts":"好","volume":75,"moves":[],"anims":[]}'
    parsed = parse_llm_reply(raw)
    assert parsed["volume"] == 75
    omit = parse_llm_reply('{"tts":"好","moves":[],"anims":[]}')
    assert omit["volume"] is None


def test_parse_llm_reply_empty_tts_not_raw_json():
    raw = (
        '{"need_reply": true, "tts": "", '
        '"moves": [{"move": "shake_head", "ms": 1280}], "anims": []}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == ""
    assert parsed["moves"] == [{"move": "shake_head", "ms": 1280}]
    assert parsed["contract_ok"] is True


def test_parse_llm_reply_rejects_empty_success_contract():
    parsed = parse_llm_reply("{}")
    assert parsed["json_ok"] is True
    assert parsed["contract_ok"] is False
    assert parsed["contract_error"]


def test_memory_store_roundtrip(tmp_path, monkeypatch):
    from deskbot_server import memory_store as ms

    mem_file = tmp_path / "memories.json"

    def _resolve(path):
        return str(mem_file)

    monkeypatch.setattr(ms, "resolve_json_path", _resolve)
    monkeypatch.setattr(ms, "MEMORIES_FILE", str(mem_file))
    e1 = ms.add_memory("主人喜欢猫")
    assert e1["text"] == "主人喜欢猫"
    rows = ms.list_memories()
    assert len(rows) == 1
    assert ms.delete_memory(e1["id"])
    assert ms.list_memories() == []
