"""图元协议单一 spec：三端（Python / Web JS / 固件）共同消费的协议数据。

本模块只放 **数据**（shape 主名与别名、颜色解析规则、图层语义、内建默认脸），
不放业务逻辑。三端消费方式：

- Python：``pb/shapes.py``（别名归一、命名色、逐层默认色）、
  ``face_expr_scenes_store``（颜色白名单正则）直接 import 本模块。
- Web JS：``service/scripts/gen_primitive_spec_js.py`` 从本模块生成
  ``web/static/generated/primitive_spec.js``（构建产物，页面在
  ``face_preview_2c.js`` / ``face_parts_editor_2c.js`` 之前加载）。
- 固件：``hardware/firmware/display.cpp`` 为手写 C++，无法 import；
  由 ``tests/test_primitive_spec_lockstep.py`` 用正则抽取 strcmp 主名集合、
  内建脸几何与 0xFB20 兜底色，与本模块逐项断言锁步。

图层语义（与固件 ``stored_from_elements_v`` / 预览页 ``frameElements`` 一致）：

- 图层绘制顺序 = ``LAYER_ORDER``（bg 最底，extra 最顶）。
- 帧内 **缺失** 某层键：继承上一完整帧该层内容（inherit）。
- 帧内显式 ``[]``：清空该层（clear）。
- 图元无 ``c``/``color``：wire 归一化时按 ``LAYER_DEFAULT_COLORS[layer]``
  填充；无图层缺省时用 ``WIRE_DEFAULT_RGB565``（白）。
"""

from __future__ import annotations

SPEC_VERSION = 1

# ---------------------------------------------------------------------------
# shape 主名（协议 wire 名，与固件 json_fill_layer 每个分支的第一个 strcmp 名、
# 以及 docs/esp32_pb_protocol.md 一致）
# ---------------------------------------------------------------------------
SHAPE_CANONICAL: tuple[str, ...] = (
    "rect",
    "rect_outline",
    "circle",
    "circle_outline",
    "line",
    "pixel",
    "hline",
    "vline",
    "ellipse",
    "ellipse_fill",
    "triangle",
    "triangle_fill",
    "round_rect",
    "round_rect_outline",
    "rotated_rect_outline",
    "rotated_rect_fill",
    "text",
    "image",
)

# 别名（小写键）→ 主名。消费方先把输入 lower() 再查表；固件的 camelCase
# 变体（``fillRect`` 等）在此以小写形式出现。``print`` / ``label`` 为固件
# 同样接受的 text 别名。
SHAPE_ALIASES: dict[str, str] = {
    "rect_fill": "rect",
    "fill_rect": "rect",
    "fillrect": "rect",
    "draw_rect": "rect_outline",
    "drawrect": "rect_outline",
    "circle_fill": "circle",
    "fill_circle": "circle",
    "fillcircle": "circle",
    "draw_circle": "circle_outline",
    "drawcircle": "circle_outline",
    "point": "pixel",
    "drawpixel": "pixel",
    "h_line": "hline",
    "drawfasthline": "hline",
    "v_line": "vline",
    "drawfastvline": "vline",
    "draw_ellipse": "ellipse",
    "drawellipse": "ellipse",
    "ellipse_outline": "ellipse",
    "fill_ellipse": "ellipse_fill",
    "fillellipse": "ellipse_fill",
    "draw_triangle": "triangle",
    "drawtriangle": "triangle",
    "triangle_outline": "triangle",
    "fill_triangle": "triangle_fill",
    "filltriangle": "triangle_fill",
    "fill_round_rect": "round_rect",
    "fillroundrect": "round_rect",
    "round_rect_fill": "round_rect",
    "draw_round_rect": "round_rect_outline",
    "drawroundrect": "round_rect_outline",
    "draw_rotated_rect": "rotated_rect_outline",
    "drawrotatedrect": "rotated_rect_outline",
    "fill_rotated_rect": "rotated_rect_fill",
    "fillrotatedrect": "rotated_rect_fill",
    "draw_line": "line",
    "drawline": "line",
    "print": "text",
    "label": "text",
}

# ---------------------------------------------------------------------------
# 颜色解析规则
# 配置源可写：命名色 / ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``（丢弃 alpha）/
# 裸 ``rrggbb`` / 数值 ``rgb()``/``rgba()`` / 整数（≤0xFFFF 视为 RGB565，
# 否则视为 RGB888）。wire 统一下发 ``c``（RGB565 整数）。
# ---------------------------------------------------------------------------
NAMED_COLORS_RGB888: dict[str, tuple[int, int, int]] = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "orange": (255, 136, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}

# 正则（Python 与 JS 语法兼容；rgb() 匹配需忽略大小写）
HEX6_BODY_PATTERN = r"^[0-9A-Fa-f]{6}$"
HEX8_PATTERN = r"^#[0-9A-Fa-f]{8}$"
RGB_FUNC_PATTERN = (
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*"
    r"(?:,\s*(?:0|1|0?\.\d+)\s*)?\)$"
)

# ---------------------------------------------------------------------------
# 图层
# ---------------------------------------------------------------------------
LAYER_ORDER: tuple[str, ...] = ("bg", "nose", "mouth", "eye_l", "eye_r", "extra")

LAYER_DEFAULT_COLORS: dict[str, str] = {
    "bg": "#ffffff",
    "eye_l": "#f4f4ef",
    "eye_r": "#f4f4ef",
    "nose": "#ffd23f",
    "mouth": "#ff6700",
    "extra": "#ffd23f",
}

WIRE_DEFAULT_RGB565 = 0xFFFF
MAX_PRIMITIVES_PER_LAYER = 16

# 固件 RTC 本地口型兜底色（``pb_draw_voice_mouth``）：mouth 图层缺省色
# ``#ff6700`` 的 RGB565 形式。锁步测试断言两者换算一致。
VOICE_MOUTH_FALLBACK_RGB565 = 0xFB20

# ---------------------------------------------------------------------------
# 内建默认脸（以固件 display.cpp ``pb_build_builtin_face_layers`` 为准抄录）。
# 固件在 render runtime 创建失败的硬件兜底 idle 与「请连接PC服务」待机屏
# 使用该几何；预览页 fallback 脸由此生成，保持三端同像素。
# 坐标系为 284×240 面板逻辑坐标；颜色为 RGB565 白（0xFFFF），背景刷黑。
# ---------------------------------------------------------------------------
BUILTIN_FACE_SCREEN_BG_RGB565 = 0x0000

BUILTIN_FACE_ELEMENTS: dict[str, list[dict[str, object]]] = {
    "bg": [],
    "nose": [],
    "mouth": [
        {
            "shape": "round_rect_outline",
            "x": 117,
            "y": 151,
            "w": 50,
            "h": 16,
            "radius": 8,
            "sw": 2,
            "c": 0xFFFF,
        }
    ],
    "eye_l": [
        {"shape": "ellipse_fill", "x": 90, "y": 88, "rw": 18, "rh": 18, "sw": 1, "c": 0xFFFF}
    ],
    "eye_r": [
        {"shape": "ellipse_fill", "x": 194, "y": 88, "rw": 18, "rh": 18, "sw": 1, "c": 0xFFFF}
    ],
    "extra": [],
}
