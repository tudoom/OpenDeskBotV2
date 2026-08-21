from __future__ import annotations

import json

import pytest

from deskbot_server.pb.servo_pcm import (
    PB_CHUNK_MS_MAX,
    make_anim_item,
    merge_pb_subchunks,
    parse_pb_volume,
    pb_json_messages,
    resolve_pb_volume_hint,
)
from deskbot_server.pb.shapes import (
    PB_DEFAULT_RGB565,
    PB_LAYER_DEFAULT_COLORS,
    normalize_primitive_for_wire,
    parse_color_to_rgb565,
    rgb888_to_rgb565,
)


def _elements():
    return {
        "mouth": [{"shape": "rect", "x": 1, "y": 2, "w": 3, "h": 4}],
        "nose": [],
        "eye_l": [],
        "eye_r": [],
        "extra": [],
    }


def test_parse_color_to_rgb565():
    assert parse_color_to_rgb565(None) == PB_DEFAULT_RGB565
    assert parse_color_to_rgb565("#f80") == rgb888_to_rgb565(0xFF, 0x88, 0x00)
    assert parse_color_to_rgb565("red") == rgb888_to_rgb565(255, 0, 0)
    assert parse_color_to_rgb565(0xFFFF) == 0xFFFF
    assert parse_color_to_rgb565(0xFFFFFF) == 0xFFFF
    assert parse_color_to_rgb565("invalid") == PB_DEFAULT_RGB565


def test_normalize_primitive_for_wire_c_rgb565():
    wired = normalize_primitive_for_wire(
        {"shape": "circle", "x": 1, "y": 2, "r": 3, "color": "#F80"}
    )
    assert "color" not in wired
    assert isinstance(wired["c"], int)
    assert wired["c"] == rgb888_to_rgb565(0xFF, 0x88, 0x00)
    plain = normalize_primitive_for_wire({"shape": "rect", "x": 0, "y": 0, "w": 1, "h": 1})
    assert plain["c"] == PB_DEFAULT_RGB565


def test_missing_primitive_colors_follow_browser_layer_palette():
    from deskbot_server.pb.shapes import normalize_elements_for_wire

    elements = {
        layer: [{"shape": "pixel", "x": 1, "y": 1}]
        for layer in PB_LAYER_DEFAULT_COLORS
    }
    wired = normalize_elements_for_wire(elements)
    assert wired["eye_l"][0]["c"] == rgb888_to_rgb565(0xF4, 0xF4, 0xEF)
    assert wired["nose"][0]["c"] == rgb888_to_rgb565(0xFF, 0xD2, 0x3F)
    assert wired["mouth"][0]["c"] == rgb888_to_rgb565(0xFF, 0x67, 0x00)


def test_wire_rejects_unsupported_or_over_limit_device_layers():
    from deskbot_server.pb.shapes import normalize_elements_for_wire

    with pytest.raises(ValueError, match="unsupported device expression shape"):
        normalize_elements_for_wire({"extra": [{"shape": "svg_path"}]})
    with pytest.raises(ValueError, match="exceeds 16 primitives"):
        normalize_elements_for_wire(
            {"extra": [{"shape": "pixel", "x": i, "y": 1} for i in range(17)]}
        )


def test_normalize_primitive_for_wire_canonicalizes_web_shape_aliases():
    assert normalize_primitive_for_wire({"shape": "rect_fill"})["shape"] == "rect"
    assert normalize_primitive_for_wire({"shape": "ellipse_outline"})["shape"] == "ellipse"
    assert normalize_primitive_for_wire({"shape": "triangle_outline"})["shape"] == "triangle"


def test_pb_json_messages_anim_is_array():
    row = {
        "chunk_ms": 100,
        "anim": [
            make_anim_item(
                {
                    "mouth": [
                        {
                            "shape": "rect",
                            "x": 1,
                            "y": 2,
                            "w": 3,
                            "h": 4,
                            "color": "red",
                        }
                    ],
                    "nose": [],
                    "eye_l": [],
                    "eye_r": [],
                    "extra": [],
                },
                100,
                phoneme="a",
            )
        ],
    }
    pairs = pb_json_messages(
        pb_req="abc",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b"\x00" * 4800],
    )
    msg, bins = pairs[0]
    assert msg["type"] == "pb_single"
    assert isinstance(msg["anim"], list)
    assert isinstance(bins, list)
    assert msg["anim"][0]["ms"] == 100
    assert msg["anim"][0]["phoneme"] == "a"
    assert "phoneme" not in msg
    mouth = msg["anim"][0]["elements"]["mouth"][0]
    assert mouth["c"] == rgb888_to_rgb565(255, 0, 0)
    assert "color" not in mouth
    assert msg["audio"]["next_bin_len"] == 4800
    assert len(bins[0]) == 4800


def test_mouth_only_and_voice_mouth_are_explicit_chain_head_capabilities():
    rows = [
        {"chunk_ms": 100, "anim": [make_anim_item({"mouth": []}, 100)]},
        {"chunk_ms": 100, "anim": [make_anim_item({"mouth": []}, 100)]},
    ]
    messages = [
        msg
        for msg, _bins in pb_json_messages(
            pb_req="mouth-only",
            sample_rate=24000,
            fmt="s16le",
            channels=1,
            anim_rows=rows,
            pcm_per_idx=[b"", b""],
            mouth_only=True,
        )
    ]

    assert messages[0]["mouth_only"] is True
    assert "mouth_only" not in messages[1]
    assert all("voice_mouth" not in message for message in messages)


def test_pb_json_messages_servo_is_array():
    row = {
        "chunk_ms": 100,
        "anim": [make_anim_item(_elements(), 100)],
        "servo": [{"xm": 1, "ym": 1, "x": 0, "y": 10, "ms": 200}],
    }
    msg, _bins = pb_json_messages(
        pb_req="abc",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b""],
    )[0]
    assert isinstance(msg["servo"], list)
    assert msg["servo"][0]["ms"] == 200
    assert "audio" not in msg
    assert (
        msg["anim"][0]["elements"]["mouth"][0]["c"]
        == parse_color_to_rgb565(PB_LAYER_DEFAULT_COLORS["mouth"])
    )


def test_pb_json_messages_does_not_invent_anim_for_servo_only_row():
    row = {
        "chunk_ms": 200,
        "servo": [{"xm": 0, "ym": 0, "x": 90, "y": 90, "ms": 200}],
    }
    msg, bins = pb_json_messages(
        pb_req="servo-only",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b""],
    )[0]

    assert bins == []
    assert "anim" not in msg
    assert msg["servo"] == row["servo"]


def test_merge_pb_subchunks_respects_max_ms():
    rows = []
    pcm = []
    for i in range(20):
        ms = 113
        rows.append(
            {
                "chunk_ms": ms,
                "anim": [make_anim_item(_elements(), ms, phoneme=f"p{i}")],
            }
        )
        pcm.append(b"\x00" * (ms * 48))
    merged_rows, merged_pcm = merge_pb_subchunks(
        rows, pcm, sample_rate=24000, max_chunk_ms=PB_CHUNK_MS_MAX
    )
    assert len(merged_rows) < len(rows)
    assert all(int(r["chunk_ms"]) <= PB_CHUNK_MS_MAX for r in merged_rows)
    assert sum(len(p) for p in merged_pcm) == sum(len(x) for x in pcm)
    for row in merged_rows:
        assert isinstance(row["anim"], list)
        assert len(row["anim"]) >= 1
        assert sum(int(x["ms"]) for x in row["anim"]) <= PB_CHUNK_MS_MAX + 113


def test_merge_pb_subchunks_splits_before_serial_json_limit():
    elements = {
        "mouth": [
            {
                "shape": "round_rect",
                "x": 140,
                "y": 145,
                "w": 72,
                "h": 28,
                "radius": 14,
                "c": 2047,
            },
            {
                "shape": "round_rect",
                "x": 146,
                "y": 152,
                "w": 60,
                "h": 13,
                "radius": 6,
                "c": 0,
            },
        ],
        "nose": [],
        "eye_l": [
            {
                "shape": "ellipse_fill",
                "x": 105,
                "y": 96,
                "rw": 11,
                "rh": 11,
                "c": 2047,
            }
        ],
        "eye_r": [
            {
                "shape": "ellipse_fill",
                "x": 181,
                "y": 97,
                "rw": 11,
                "rh": 11,
                "c": 2047,
            }
        ],
        "extra": [
            {
                "shape": "line",
                "x1": 89,
                "x2": 119,
                "y1": 82,
                "y2": 88,
                "c": 65535,
            },
            {
                "shape": "line",
                "x1": 197,
                "x2": 167,
                "y1": 82,
                "y2": 88,
                "c": 65535,
            },
        ],
    }
    rows = [
        {
            "chunk_ms": 120,
            "anim": [make_anim_item(elements, 120, phoneme=f"p{i}")],
        }
        for i in range(55)
    ]
    pcm = [b"\x00" * (120 * 48) for _ in rows]
    merged_rows, merged_pcm = merge_pb_subchunks(
        rows,
        pcm,
        sample_rate=24000,
        max_wire_json_bytes=14000,
    )

    assert len(merged_rows) >= 2
    assert sum(len(part) for part in merged_pcm) == sum(
        len(part) for part in pcm
    )
    assert all(
        len(
            json.dumps(
                row,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        <= 12000
        for row in merged_rows
    )


def test_pb_json_messages_volume_on_chain_types():
    row = {"chunk_ms": 50, "anim": [make_anim_item(_elements(), 50)]}
    pairs = pb_json_messages(
        pb_req="req1",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row, row, row],
        pcm_per_idx=[b"", b"", b""],
        volume=70,
    )
    assert len(pairs) == 3
    start, chunk, end = [m for m, _ in pairs]
    assert start["type"] == "pb_start"
    assert start["volume"] == 70
    assert "cam_fps" not in start
    assert chunk["volume"] == 70
    assert end["volume"] == 70


def test_pb_json_messages_omits_volume_when_unset():
    row = {"chunk_ms": 50, "anim": [make_anim_item(_elements(), 50)]}
    msg, _ = pb_json_messages(
        pb_req="req1",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b""],
    )[0]
    assert "volume" not in msg


def test_resolve_pb_volume_hint_only_uses_explicit_volume():
    assert resolve_pb_volume_hint() is None
    assert resolve_pb_volume_hint(55) == 55


def test_pb_json_messages_assets_binary_order():
    jpeg = b"\xff\xd8\xff" + b"\x00" * 100
    row = {
        "chunk_ms": 200,
        "anim": [make_anim_item({"extra": [{"shape": "image", "asset": 0, "x": 0, "y": 0, "w": 10, "h": 10}]}, 200)],
        "_assets": [jpeg],
    }
    msg, bins = pb_json_messages(
        pb_req="abc",
        sample_rate=24000,
        fmt="s16le",
        channels=1,
        anim_rows=[row],
        pcm_per_idx=[b""],
    )[0]
    assert msg["assets"][0]["next_bin_len"] == len(jpeg)
    assert bins == [jpeg]


def test_parse_pb_volume():
    assert parse_pb_volume(None) is None
    assert parse_pb_volume(85) == 85
    assert parse_pb_volume(150) == 100


def test_text_primitives_wrap():
    from deskbot_server.pb.text_layout import text_primitives_from_block

    prims = text_primitives_from_block("你好世界" * 20, max_width_px=80, size=1)
    assert len(prims) >= 2
    assert all(p["shape"] == "text" for p in prims)


def test_text_primitives_bottom_center():
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.text_layout import _line_width_px, text_primitives_from_block

    prims = text_primitives_from_block("你好", size=1)
    assert len(prims) == 1
    line_w = _line_width_px("你好", size=1)
    assert prims[0]["x"] == (FACE_LCD_WIDTH - line_w) // 2
    assert prims[0]["y"] == FACE_LCD_HEIGHT - 8 - 9

    prims2 = text_primitives_from_block("第一行\n第二行", size=1)
    assert len(prims2) == 2
    assert prims2[0]["y"] < prims2[1]["y"]
    assert prims2[1]["y"] == FACE_LCD_HEIGHT - 8 - 9
