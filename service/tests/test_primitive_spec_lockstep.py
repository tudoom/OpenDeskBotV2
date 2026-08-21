"""图元协议 spec 与固件 / 生成产物的锁步契约。

``pb.primitive_spec`` 是三端协议数据的单一来源；固件 ``display.cpp`` 为
手写 C++，本测试用正则从源码抽取 shape strcmp 名集合、内建默认脸几何与
RTC 口型兜底色 0xFB20，与 spec 逐项断言相等；同时锁定生成的
``web/static/generated/primitive_spec.js`` 与生成器输出一致（防手改 /
防改 spec 后漏生成）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from deskbot_server.pb import primitive_spec as spec
from deskbot_server.pb.shapes import (
    _SHAPE_TO_CANONICAL,
    PB_DEVICE_SHAPES,
    PB_LAYER_DEFAULT_COLORS,
    parse_color_to_rgb888,
    rgb888_to_rgb565,
)

ROOT = Path(__file__).resolve().parents[2]
DISPLAY = (ROOT / "hardware" / "firmware" / "display.cpp").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    start_at = DISPLAY.index(start)
    return DISPLAY[start_at : DISPLAY.index(end, start_at)]


def _firmware_shape_branches() -> list[list[str]]:
    """json_fill_layer 中每个 if 分支接受的 strcmp 名（分支内按出现顺序）。"""

    section = _between(
        "static bool json_fill_layer(",
        "static bool stored_layers_have_renderable(",
    )
    token_re = re.compile(r'strcmp\(shape, "([^"]+)"\) == 0|p\.shape = PbShape::(\w+);')
    branches: list[list[str]] = []
    pending: list[str] = []
    for match in token_re.finditer(section):
        name, assigned = match.group(1), match.group(2)
        if name is not None:
            pending.append(name)
        elif assigned is not None and pending:
            branches.append(pending)
            pending = []
    return branches


def test_firmware_strcmp_shape_names_lockstep_with_spec():
    branches = _firmware_shape_branches()
    assert branches, "failed to extract shape branches from display.cpp"

    # 每个固件分支的第一个 strcmp 名即协议主名；主名集合与 spec 相等。
    firmware_canonical = {branch[0] for branch in branches}
    assert firmware_canonical == set(spec.SHAPE_CANONICAL)

    # 固件接受的每个名字（含 camelCase 与 print/label）经 spec 别名表
    # 小写归一后都落在主名集合内 —— 三端词汇表闭合。
    for branch in branches:
        for name in branch:
            low = name.lower()
            canonical = spec.SHAPE_ALIASES.get(low, low)
            assert canonical in spec.SHAPE_CANONICAL, name

    # 反向：spec 别名的归一目标必须有效，且别名键均为小写。
    for alias, canonical in spec.SHAPE_ALIASES.items():
        assert alias == alias.lower()
        assert canonical in spec.SHAPE_CANONICAL


def test_firmware_builtin_face_geometry_lockstep_with_spec():
    face = _between(
        "static void pb_build_builtin_face_layers(",
        "static void pb_show_builtin_idle(",
    )
    eye_left_src = face.split("s_pb_curr_eye_l.prims", 1)[0]
    eye_right_src = face.split("s_pb_curr_eye_l.prims", 1)[1].split(
        "s_pb_curr_eye_r.prims", 1
    )[0]
    mouth_src = face.split("StoredPrim mouth{};", 1)[1]

    def _fields(src: str, var: str) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in re.findall(rf"{var}\.(\w+) = (\d+);", src)
        }

    eye_l = _fields(eye_left_src, "eye")
    eye_r_override = _fields(eye_right_src, "eye")
    mouth = _fields(mouth_src, "mouth")

    assert "PbShape::EllipseFill" in eye_left_src
    assert "PbShape::RoundRectOutline" in mouth_src
    assert "DESKBOT_DISPLAY_COLOR_WHITE" in eye_left_src
    assert "DESKBOT_DISPLAY_COLOR_WHITE" in mouth_src
    assert "fillScreen(DESKBOT_DISPLAY_COLOR_BLACK)" in DISPLAY

    spec_eye_l = spec.BUILTIN_FACE_ELEMENTS["eye_l"][0]
    spec_eye_r = spec.BUILTIN_FACE_ELEMENTS["eye_r"][0]
    spec_mouth = spec.BUILTIN_FACE_ELEMENTS["mouth"][0]

    # StoredPrim 的 w/h 对应 wire ellipse 的 rw/rh；r 对应 round_rect radius。
    assert spec_eye_l["shape"] == "ellipse_fill"
    assert (spec_eye_l["x"], spec_eye_l["y"]) == (eye_l["x"], eye_l["y"])
    assert (spec_eye_l["rw"], spec_eye_l["rh"]) == (eye_l["w"], eye_l["h"])
    assert spec_eye_l["sw"] == eye_l["stroke_width"]
    assert spec_eye_r["shape"] == "ellipse_fill"
    assert spec_eye_r["x"] == eye_r_override["x"]
    assert (spec_eye_r["y"], spec_eye_r["rw"], spec_eye_r["rh"]) == (
        eye_l["y"],
        eye_l["w"],
        eye_l["h"],
    )

    assert spec_mouth["shape"] == "round_rect_outline"
    assert (spec_mouth["x"], spec_mouth["y"]) == (mouth["x"], mouth["y"])
    assert (spec_mouth["w"], spec_mouth["h"]) == (mouth["w"], mouth["h"])
    assert spec_mouth["radius"] == mouth["r"]
    assert spec_mouth["sw"] == mouth["stroke_width"]

    # 固件白 = ST77XX_WHITE (0xFFFF)，背景刷黑。
    panel = (ROOT / "hardware" / "firmware" / "display_panel.h").read_text(
        encoding="utf-8"
    )
    assert "#define DESKBOT_DISPLAY_COLOR_WHITE ST77XX_WHITE" in panel
    for layer in ("mouth", "eye_l", "eye_r"):
        assert spec.BUILTIN_FACE_ELEMENTS[layer][0]["c"] == 0xFFFF
    assert spec.BUILTIN_FACE_SCREEN_BG_RGB565 == 0x0000
    assert spec.BUILTIN_FACE_ELEMENTS["bg"] == []
    assert spec.BUILTIN_FACE_ELEMENTS["nose"] == []
    assert spec.BUILTIN_FACE_ELEMENTS["extra"] == []
    assert set(spec.BUILTIN_FACE_ELEMENTS) == set(spec.LAYER_ORDER)


def test_voice_mouth_fallback_color_is_the_mouth_layer_default_in_rgb565():
    voice = _between(
        "static void pb_draw_voice_mouth(",
        "static void pb_show_builtin_idle(",
    )
    assert "mouth.color = 0xFB20u;" in voice
    assert spec.VOICE_MOUTH_FALLBACK_RGB565 == 0xFB20
    mouth_rgb = parse_color_to_rgb888(spec.LAYER_DEFAULT_COLORS["mouth"])
    assert mouth_rgb is not None
    assert rgb888_to_rgb565(*mouth_rgb) == spec.VOICE_MOUTH_FALLBACK_RGB565


def test_layer_limits_and_order_lockstep():
    assert (
        f"kPbMaxPrimsPerLayer   = {spec.MAX_PRIMITIVES_PER_LAYER};" in DISPLAY
    )
    from deskbot_server.application.expression_runtime import _DISPLAY_LAYER_ORDER

    assert tuple(_DISPLAY_LAYER_ORDER) == tuple(spec.LAYER_ORDER)


def test_python_consumers_load_from_spec():
    assert _SHAPE_TO_CANONICAL == spec.SHAPE_ALIASES
    assert PB_DEVICE_SHAPES == frozenset(spec.SHAPE_CANONICAL)
    assert PB_LAYER_DEFAULT_COLORS == spec.LAYER_DEFAULT_COLORS


def test_generated_primitive_spec_js_matches_generator_output():
    gen_path = ROOT / "service" / "scripts" / "gen_primitive_spec_js.py"
    module_spec = importlib.util.spec_from_file_location(
        "gen_primitive_spec_js", gen_path
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    generated = (
        ROOT
        / "service"
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
        / "generated"
        / "primitive_spec.js"
    )
    assert generated.read_text(encoding="utf-8") == module.render_js(), (
        "generated/primitive_spec.js 与 spec 不一致："
        "运行 python service/scripts/gen_primitive_spec_js.py 重新生成"
    )
