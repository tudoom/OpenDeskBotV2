"""从 ``pb.primitive_spec`` 生成 ``web/static/generated/primitive_spec.js``。

用法（仓库根或任意目录）::

    python service/scripts/gen_primitive_spec_js.py

生成产物为构建产物，随仓库提交；页面必须在 ``face_preview_2c.js`` /
``face_parts_editor_2c.js`` 之前加载它。``tests/test_primitive_spec_lockstep.py``
断言提交的产物与本脚本的渲染结果一致（防手改 / 防漏生成）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SERVICE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVICE_SRC))

OUTPUT_PATH = (
    _SERVICE_SRC / "deskbot_server" / "web" / "static" / "generated" / "primitive_spec.js"
)


def _js(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def render_js() -> str:
    from deskbot_server.pb import primitive_spec as spec

    named_css = {
        name: "#{:02x}{:02x}{:02x}".format(*rgb)
        for name, rgb in spec.NAMED_COLORS_RGB888.items()
    }
    data = {
        "SPEC_VERSION": spec.SPEC_VERSION,
        "SHAPE_CANONICAL": list(spec.SHAPE_CANONICAL),
        "SHAPE_ALIASES": spec.SHAPE_ALIASES,
        "NAMED_CSS_COLORS": named_css,
        "LAYER_ORDER": list(spec.LAYER_ORDER),
        "LAYER_DEFAULT_COLORS": spec.LAYER_DEFAULT_COLORS,
        "WIRE_DEFAULT_RGB565": spec.WIRE_DEFAULT_RGB565,
        "MAX_PRIMITIVES_PER_LAYER": spec.MAX_PRIMITIVES_PER_LAYER,
        "VOICE_MOUTH_FALLBACK_RGB565": spec.VOICE_MOUTH_FALLBACK_RGB565,
        "BUILTIN_FACE_SCREEN_BG_RGB565": spec.BUILTIN_FACE_SCREEN_BG_RGB565,
        "BUILTIN_FACE_ELEMENTS": spec.BUILTIN_FACE_ELEMENTS,
    }
    consts = "\n".join(
        f"  const {name} = {_js(value)};".replace("\n", "\n  ").replace("  \n", "\n")
        for name, value in data.items()
    )
    hex8_re = json.dumps(spec.HEX8_PATTERN)
    rgb_func_re = json.dumps(spec.RGB_FUNC_PATTERN)
    return f"""// AUTO-GENERATED FILE — DO NOT EDIT.
// Source of truth: service/src/deskbot_server/pb/primitive_spec.py
// Regenerate: python service/scripts/gen_primitive_spec_js.py
(function (global) {{
  "use strict";

{consts}

  const HEX8_RE = new RegExp({hex8_re});
  const RGB_FUNC_RE = new RegExp({rgb_func_re}, "i");

  function num(value, fallback) {{
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }}

  function hexByte(n) {{
    return Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
  }}

  function rgb565ToCss(value) {{
    const v = Math.max(0, Math.min(0xffff, Math.floor(num(value, 0))));
    const r = ((v >> 11) & 0x1f) * 255 / 31;
    const g = ((v >> 5) & 0x3f) * 255 / 63;
    const b = (v & 0x1f) * 255 / 31;
    return `#${{hexByte(r)}}${{hexByte(g)}}${{hexByte(b)}}`;
  }}

  function cssColorToRgb565(value, fallback) {{
    const normalized = normalizeCssColor(value || fallback || "#ffffff");
    const match = /^#([0-9a-fA-F]{{6}})$/.exec(normalized);
    if (!match) return null;
    const rgb = parseInt(match[1], 16);
    const r = (rgb >> 16) & 0xff;
    const g = (rgb >> 8) & 0xff;
    const b = rgb & 0xff;
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
  }}

  function normalizeShape(value) {{
    const shape = String(value || "").trim().toLowerCase();
    return SHAPE_ALIASES[shape] || shape;
  }}

  // 颜色解析规则（与 pb/shapes.py、face_expr_scenes_store 白名单一致）：
  // 命名色 / #rgb / #rrggbb / #rrggbbaa（丢 alpha）/ 裸 rrggbb / 数值 rgb()/
  // rgba() / 整数（≤0xFFFF 视为 RGB565，否则 RGB888）。非法返回 ""，
  // 调用方回退图层缺省色。
  function normalizeCssColor(value) {{
    if (value == null || value === "") return "";
    if (typeof value === "number") {{
      if (!Number.isFinite(value)) return "";
      return value <= 0xffff
        ? rgb565ToCss(value)
        : `#${{hexByte(value >> 16)}}${{hexByte(value >> 8)}}${{hexByte(value)}}`;
    }}
    const raw = String(value).trim().toLowerCase();
    if (!raw) return "";
    if (NAMED_CSS_COLORS[raw]) return NAMED_CSS_COLORS[raw];
    if (/^#[0-9a-f]{{3}}$/.test(raw)) {{
      return "#" + raw.slice(1).split("").map((ch) => ch + ch).join("");
    }}
    if (/^#[0-9a-f]{{6}}$/.test(raw)) return raw;
    if (/^[0-9a-f]{{6}}$/.test(raw)) return `#${{raw}}`;
    if (HEX8_RE.test(raw)) return raw.slice(0, 7);
    const rgb = RGB_FUNC_RE.exec(raw);
    if (rgb) return `#${{hexByte(+rgb[1])}}${{hexByte(+rgb[2])}}${{hexByte(+rgb[3])}}`;
    return "";
  }}

  global.DeskbotPrimitiveSpec = Object.freeze({{
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
  }});
}})(typeof window !== "undefined" ? window : globalThis);
"""


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_js(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
