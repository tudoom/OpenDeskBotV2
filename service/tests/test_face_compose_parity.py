"""Web 预览合成 与 Python(固件语义)合成 的对拍契约。

同一多帧场景：

- Python 侧用 ``expression_runtime.display_semantic_crc32``（固件
  ``StoredLayer`` 语义模型）对多分片序列取最终 CRC；
- JS 侧用 ``face_preview_2c.frameElements`` 逐帧合成出最终各层，
  再把该合成结果作为 **单帧** 交回 Python 取 CRC。

两个 CRC 相等 ⇔ 预览页的「缺层继承 / 显式 [] 清层」与固件语义模型合成
到同一张脸。另用 SVG 渲染对拍「无色图元回退图层默认色」的 RGB565 语义。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deskbot_server.application.expression_runtime import display_semantic_crc32
from deskbot_server.face_expr_scenes_store import _sanitize_frame_elements
from deskbot_server.pb.shapes import (
    PB_LAYER_DEFAULT_COLORS,
    normalize_elements_for_wire,
    parse_color_to_rgb565,
)


def _wire(elements: dict) -> dict:
    """生产链路顺序：场景文档白名单清洗（rgb()/命名色 → #rrggbb）→ wire 归一。"""

    return normalize_elements_for_wire(_sanitize_frame_elements(elements))

_NODE_DRIVER = Path(__file__).with_name("js") / "face_compose_parity.node.js"


def _run_node_driver(payload: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the compose parity test")
    completed = subprocess.run(
        [node, str(_NODE_DRIVER)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _rgb565_to_css(value: int) -> str:
    """与 face_preview_2c.js ``rgb565ToCss`` 逐位一致（Math.round 半上取整）。"""

    v = max(0, min(0xFFFF, int(value)))
    r = int(((v >> 11) & 0x1F) * 255 / 31 + 0.5)
    g = int(((v >> 5) & 0x3F) * 255 / 63 + 0.5)
    b = int((v & 0x1F) * 255 / 31 + 0.5)
    return f"#{r:02x}{g:02x}{b:02x}"


def _wire_frames() -> list[dict]:
    """三帧，wire 归一化后（c 为 RGB565 整数；含继承/清层/默认色三语义）。"""

    frame0 = _wire(
        {
            "bg": [],
            "nose": [{"shape": "circle", "x": 142, "y": 124, "r": 11, "color": "red"}],
            "mouth": [
                # 无色 → mouth 图层默认色 #ff6700
                {"shape": "rect", "x": 120, "y": 150, "w": 44, "h": 12},
                {"shape": "line", "x1": 118, "y1": 170, "x2": 166, "y2": 170,
                 "color": "rgb(0, 128, 255)"},
            ],
            "eye_l": [{"shape": "ellipse_fill", "x": 90, "y": 88, "rw": 18, "rh": 18,
                       "c": 0xFFE0}],
            "eye_r": [{"shape": "ellipse_fill", "x": 194, "y": 88, "rw": 18, "rh": 18}],
            "extra": [{"shape": "text", "x": 8, "y": 8, "text": "hi", "size": 2,
                       "color": "#ffd23f"}],
        }
    )
    frame1 = _wire(
        {
            # mouth 显式清层；eye_l 整层替换；其余缺失 → 继承
            "mouth": [],
            "eye_l": [{"shape": "ellipse_fill", "x": 90, "y": 88, "rw": 18, "rh": 4,
                       "c": 0xFFE0}],
        }
    )
    frame2 = _wire(
        {
            # nose 整层替换；mouth 保持清空（缺失继承 frame1 的 []）
            "nose": [{"shape": "circle", "x": 142, "y": 120, "r": 8, "color": "#00ff00"}],
        }
    )
    return [frame0, frame1, frame2]


def test_multi_frame_inherit_and_clear_compose_to_the_same_final_face():
    frames = _wire_frames()
    # 分两个 pb 分片：前两帧一片、末帧一片（合成语义须跨分片保持）。
    messages_multi = [
        {
            "chunk_ms": 400,
            "anim": [
                {"elements": frames[0], "ms": 200},
                {"elements": frames[1], "ms": 200},
            ],
        },
        {"chunk_ms": 300, "anim": [{"elements": frames[2], "ms": 300}]},
    ]
    crc_multi = display_semantic_crc32(messages_multi)
    assert crc_multi is not None

    scene = {"name": "parity", "frames": [{"elements": f} for f in frames]}
    result = _run_node_driver({"scene": scene, "frameIndex": len(frames) - 1})
    composed = result["composed"]

    # 结构自检：清层、继承、替换各就位。
    assert composed["mouth"] == []
    assert composed["eye_r"] == frames[0]["eye_r"]
    assert composed["extra"] == frames[0]["extra"]
    assert composed["eye_l"] == frames[1]["eye_l"]
    assert composed["nose"] == frames[2]["nose"]
    assert composed["bg"] == []

    # 把 JS 合成的最终脸作为单帧回灌固件语义模型：CRC 必须与多帧序列一致。
    crc_single = display_semantic_crc32(
        [{"chunk_ms": 100, "anim": [{"elements": composed, "ms": 100}]}]
    )
    assert crc_single == crc_multi


def test_default_layer_colors_render_identically_on_both_sides():
    """配置侧（命名色 / rgb() / 缺色）帧：JS 渲染色 == Python wire 色的 565 往返。"""

    config_elements = {
        "nose": [{"shape": "circle", "x": 142, "y": 124, "r": 11, "color": "red"}],
        "mouth": [{"shape": "rect", "x": 120, "y": 150, "w": 44, "h": 12}],
        "eye_l": [{"shape": "ellipse_fill", "x": 90, "y": 88, "rw": 18, "rh": 18}],
        "extra": [{"shape": "line", "x1": 118, "y1": 170, "x2": 166, "y2": 170,
                   "color": "rgb(0, 128, 255)"}],
    }
    scene = {"name": "colors", "frames": [{"elements": config_elements}]}
    svg = _run_node_driver({"scene": scene, "frameIndex": 0})["svg"]

    wire = _wire(config_elements)
    expectations = {
        "circle": _rgb565_to_css(wire["nose"][0]["c"]),
        "rect": _rgb565_to_css(wire["mouth"][0]["c"]),
        "ellipse": _rgb565_to_css(wire["eye_l"][0]["c"]),
        "line": _rgb565_to_css(wire["extra"][0]["c"]),
    }
    # 缺色图元的 wire 色 == 图层默认色折算（默认色语义本身）。
    assert wire["mouth"][0]["c"] == parse_color_to_rgb565(
        PB_LAYER_DEFAULT_COLORS["mouth"]
    )
    assert wire["eye_l"][0]["c"] == parse_color_to_rgb565(
        PB_LAYER_DEFAULT_COLORS["eye_l"]
    )

    for tag, expected_css in expectations.items():
        match = re.search(rf"<{tag} [^>]*>", svg)
        assert match is not None, f"missing <{tag}> in svg"
        colors = re.findall(r'(?:fill|stroke)="(#[0-9a-f]{6})"', match.group(0))
        assert expected_css in colors, (tag, expected_css, match.group(0))
