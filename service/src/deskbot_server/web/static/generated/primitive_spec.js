// AUTO-GENERATED FILE — DO NOT EDIT.
// Source of truth: service/src/deskbot_server/pb/primitive_spec.py
// Regenerate: python service/scripts/gen_primitive_spec_js.py
(function (global) {
  "use strict";

  const SPEC_VERSION = 1;
  const SHAPE_CANONICAL = [
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
    "image"
  ];
  const SHAPE_ALIASES = {
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
    "label": "text"
  };
  const NAMED_CSS_COLORS = {
    "white": "#ffffff",
    "black": "#000000",
    "red": "#ff0000",
    "green": "#00ff00",
    "blue": "#0000ff",
    "yellow": "#ffff00",
    "cyan": "#00ffff",
    "magenta": "#ff00ff",
    "orange": "#ff8800",
    "gray": "#808080",
    "grey": "#808080"
  };
  const LAYER_ORDER = [
    "bg",
    "nose",
    "mouth",
    "eye_l",
    "eye_r",
    "extra"
  ];
  const LAYER_DEFAULT_COLORS = {
    "bg": "#ffffff",
    "eye_l": "#f4f4ef",
    "eye_r": "#f4f4ef",
    "nose": "#ffd23f",
    "mouth": "#ff6700",
    "extra": "#ffd23f"
  };
  const WIRE_DEFAULT_RGB565 = 65535;
  const MAX_PRIMITIVES_PER_LAYER = 16;
  const VOICE_MOUTH_FALLBACK_RGB565 = 64288;
  const BUILTIN_FACE_SCREEN_BG_RGB565 = 0;
  const BUILTIN_FACE_ELEMENTS = {
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
        "c": 65535
      }
    ],
    "eye_l": [
      {
        "shape": "ellipse_fill",
        "x": 90,
        "y": 88,
        "rw": 18,
        "rh": 18,
        "sw": 1,
        "c": 65535
      }
    ],
    "eye_r": [
      {
        "shape": "ellipse_fill",
        "x": 194,
        "y": 88,
        "rw": 18,
        "rh": 18,
        "sw": 1,
        "c": 65535
      }
    ],
    "extra": []
  };

  const HEX8_RE = new RegExp("^#[0-9A-Fa-f]{8}$");
  const RGB_FUNC_RE = new RegExp("^rgba?\\(\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*(?:,\\s*(?:0|1|0?\\.\\d+)\\s*)?\\)$", "i");

  function num(value, fallback) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  function hexByte(n) {
    return Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
  }

  function rgb565ToCss(value) {
    const v = Math.max(0, Math.min(0xffff, Math.floor(num(value, 0))));
    const r = ((v >> 11) & 0x1f) * 255 / 31;
    const g = ((v >> 5) & 0x3f) * 255 / 63;
    const b = (v & 0x1f) * 255 / 31;
    return `#${hexByte(r)}${hexByte(g)}${hexByte(b)}`;
  }

  function cssColorToRgb565(value, fallback) {
    const normalized = normalizeCssColor(value || fallback || "#ffffff");
    const match = /^#([0-9a-fA-F]{6})$/.exec(normalized);
    if (!match) return null;
    const rgb = parseInt(match[1], 16);
    const r = (rgb >> 16) & 0xff;
    const g = (rgb >> 8) & 0xff;
    const b = rgb & 0xff;
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
  }

  function normalizeShape(value) {
    const shape = String(value || "").trim().toLowerCase();
    return SHAPE_ALIASES[shape] || shape;
  }

  // 颜色解析规则（与 pb/shapes.py、face_expr_scenes_store 白名单一致）：
  // 命名色 / #rgb / #rrggbb / #rrggbbaa（丢 alpha）/ 裸 rrggbb / 数值 rgb()/
  // rgba() / 整数（≤0xFFFF 视为 RGB565，否则 RGB888）。非法返回 ""，
  // 调用方回退图层缺省色。
  function normalizeCssColor(value) {
    if (value == null || value === "") return "";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return "";
      return value <= 0xffff
        ? rgb565ToCss(value)
        : `#${hexByte(value >> 16)}${hexByte(value >> 8)}${hexByte(value)}`;
    }
    const raw = String(value).trim().toLowerCase();
    if (!raw) return "";
    if (NAMED_CSS_COLORS[raw]) return NAMED_CSS_COLORS[raw];
    if (/^#[0-9a-f]{3}$/.test(raw)) {
      return "#" + raw.slice(1).split("").map((ch) => ch + ch).join("");
    }
    if (/^#[0-9a-f]{6}$/.test(raw)) return raw;
    if (/^[0-9a-f]{6}$/.test(raw)) return `#${raw}`;
    if (HEX8_RE.test(raw)) return raw.slice(0, 7);
    const rgb = RGB_FUNC_RE.exec(raw);
    if (rgb) return `#${hexByte(+rgb[1])}${hexByte(+rgb[2])}${hexByte(+rgb[3])}`;
    return "";
  }

  global.DeskbotPrimitiveSpec = Object.freeze({
    SPEC_VERSION,
    SHAPE_CANONICAL,
    SHAPE_ALIASES,
    NAMED_CSS_COLORS,
    LAYER_ORDER,
    LAYER_DEFAULT_COLORS,
    WIRE_DEFAULT_RGB565,
    MAX_PRIMITIVES_PER_LAYER,
    VOICE_MOUTH_FALLBACK_RGB565,
    BUILTIN_FACE_SCREEN_BG_RGB565,
    BUILTIN_FACE_ELEMENTS,
    normalizeShape,
    normalizeCssColor,
    rgb565ToCss,
    cssColorToRgb565,
  });
})(typeof window !== "undefined" ? window : globalThis);
