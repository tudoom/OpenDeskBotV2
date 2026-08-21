(function (global) {
  "use strict";

  // Single protocol source: generated/primitive_spec.js (built from
  // service/src/deskbot_server/pb/primitive_spec.py) must load first.
  const SPEC = global.DeskbotPrimitiveSpec;
  if (!SPEC) {
    throw new Error("generated/primitive_spec.js must be loaded before face_parts_editor_2c.js");
  }

  const CANVAS_WIDTH = 284;
  const CANVAS_HEIGHT = 240;
  const EDITOR_META_KEY = "editor_parts";
  const BRUSH_STROKES_KEY = "_brush_strokes";
  const MAX_BRUSH_UNDO_STEPS = 5;
  // One rendered device layer is a fixed 16-entry array in firmware. Brush
  // strokes share the extra layer with cheeks, brows and tears.
  const MAX_PRIMITIVES_PER_LAYER = SPEC.MAX_PRIMITIVES_PER_LAYER;
  const MAX_BRUSH_SEGMENTS = MAX_PRIMITIVES_PER_LAYER;

  const PARTS = Object.freeze([
    { id: "eye_l", label: "左眼", layer: "eye_l", defaultVisible: true, color: "#f4f4ef" },
    { id: "eye_r", label: "右眼", layer: "eye_r", defaultVisible: true, color: "#f4f4ef" },
    { id: "mouth", label: "嘴巴", layer: "mouth", defaultVisible: true, color: "#f4f4ef" },
    { id: "nose", label: "鼻子", layer: "nose", defaultVisible: true, color: "#ffd23f" },
    { id: "cheek_l", label: "左腮红", layer: "extra", defaultVisible: false, color: "#fa6040" },
    { id: "cheek_r", label: "右腮红", layer: "extra", defaultVisible: false, color: "#fa6040" },
    { id: "brow", label: "眉毛", layer: "extra", defaultVisible: false, color: "#f4f4ef" },
    { id: "tear", label: "眼泪", layer: "extra", defaultVisible: false, color: "#69e7ff" },
    { id: "sweat", label: "汗滴", layer: "extra", defaultVisible: false, color: "#69e7ff" },
  ]);
  const PART_BY_ID = Object.freeze(Object.fromEntries(PARTS.map((part) => [part.id, part])));
  const EXTRA_PART_IDS = Object.freeze(["cheek_l", "cheek_r", "brow", "tear", "sweat"]);

  // Full device alias table from the shared spec (previously a narrower
  // editor-local subset).
  const SHAPE_ALIASES = SPEC.SHAPE_ALIASES;

  const SHAPE_OPTIONS = Object.freeze([
    { value: "original", label: "保留原形" },
    { value: "circle_fill", label: "圆形（实心）" },
    { value: "circle_outline", label: "圆形（空心）" },
    { value: "ellipse_fill", label: "椭圆（实心）" },
    { value: "ellipse_outline", label: "椭圆（空心）" },
    { value: "triangle_fill", label: "三角（实心）" },
    { value: "triangle_outline", label: "三角（空心）" },
    { value: "square_fill", label: "正方形（实心）" },
    { value: "square_outline", label: "正方形（空心）" },
    { value: "rect_fill", label: "长方形（实心）" },
    { value: "rect_outline", label: "长方形（空心）" },
    { value: "round_rect_fill", label: "圆角矩形（实心）" },
    { value: "round_rect_outline", label: "圆角矩形（空心）" },
    { value: "hline", label: "横线（实心）" },
    { value: "vline", label: "竖线" },
    { value: "diagonal", label: "斜线" },
    { value: "diamond_fill", label: "菱形（实心）" },
    { value: "diamond_outline", label: "菱形（空心）" },
    { value: "drop_fill", label: "水滴" },
    { value: "heart_fill", label: "爱心" },
    { value: "cross", label: "十字" },
  ]);
  const EDITOR_SHAPES = Object.freeze(new Set(SHAPE_OPTIONS.map((item) => item.value)));

  const CANONICAL = Object.freeze({
    eye_l: [{ shape: "ellipse_fill", x: 86, y: 96, rw: 17, rh: 17, color: "#f4f4ef" }],
    eye_r: [{ shape: "ellipse_fill", x: 198, y: 96, rw: 17, rh: 17, color: "#f4f4ef" }],
    mouth: [{ shape: "round_rect_outline", x: 115, y: 149, w: 54, h: 14, radius: 7, sw: 3, color: "#f4f4ef" }],
    nose: [{ shape: "circle", x: 142, y: 124, r: 11, color: "#ffd23f" }],
    cheek_l: [{ shape: "ellipse_fill", x: 58, y: 137, rw: 12, rh: 6, color: "#fa6040" }],
    cheek_r: [{ shape: "ellipse_fill", x: 226, y: 137, rw: 12, rh: 6, color: "#fa6040" }],
    brow: [
      { shape: "line", x1: 64, y1: 72, x2: 108, y2: 66, sw: 3, color: "#f4f4ef" },
      { shape: "line", x1: 176, y1: 66, x2: 220, y2: 72, sw: 3, color: "#f4f4ef" },
    ],
    tear: [
      { shape: "ellipse_fill", x: 88, y: 132, rw: 4, rh: 10, color: "#69e7ff" },
      { shape: "ellipse_fill", x: 196, y: 132, rw: 4, rh: 10, color: "#69e7ff" },
    ],
    sweat: [
      { shape: "ellipse_fill", x: 232, y: 70, rw: 5, rh: 9, color: "#69e7ff" },
      { shape: "ellipse_fill", x: 246, y: 91, rw: 3, rh: 6, color: "#69e7ff" },
    ],
  });

  function clone(value, fallback) {
    if (value == null) return fallback;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return fallback;
    }
  }

  function num(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, num(value, min)));
  }

  function round(value) {
    return Math.round(num(value, 0) * 10) / 10;
  }

  const normalizeShape = SPEC.normalizeShape;

  function normalizeEditorShape(value) {
    const shape = String(value || "original").trim().toLowerCase();
    return EDITOR_SHAPES.has(shape) ? shape : "original";
  }

  const rgb565ToHex = SPEC.rgb565ToCss;

  function normalizeColor(value, fallback) {
    // Shared spec color rule: named palette, hex forms, numeric rgb()/rgba()
    // and RGB565/RGB888 integers — same gamut as the preview renderer.
    const normalized = SPEC.normalizeCssColor(value);
    return normalized || fallback || "#f4f4ef";
  }

  function primitiveColor(primitive, fallback) {
    if (primitive && primitive.c != null) return rgb565ToHex(primitive.c);
    return normalizeColor(primitive && primitive.color, fallback);
  }

  function isDarkColor(value) {
    const color = normalizeColor(value, "#ffffff");
    const r = parseInt(color.slice(1, 3), 16);
    const g = parseInt(color.slice(3, 5), 16);
    const b = parseInt(color.slice(5, 7), 16);
    return r + g + b < 72;
  }

  function primitiveBounds(primitive) {
    const row = primitive && typeof primitive === "object" ? primitive : {};
    const shape = normalizeShape(row.shape);
    const sw = Math.max(1, num(row.sw != null ? row.sw : row.stroke_width, 1));
    if (shape === "circle" || shape === "circle_outline") {
      const r = Math.max(0.5, num(row.r, 1));
      return { left: num(row.x, 0) - r, right: num(row.x, 0) + r, top: num(row.y, 0) - r, bottom: num(row.y, 0) + r };
    }
    if (shape === "ellipse" || shape === "ellipse_fill") {
      const rw = Math.max(0.5, num(row.rw != null ? row.rw : row.w, 1));
      const rh = Math.max(0.5, num(row.rh != null ? row.rh : row.h, 1));
      return { left: num(row.x, 0) - rw, right: num(row.x, 0) + rw, top: num(row.y, 0) - rh, bottom: num(row.y, 0) + rh };
    }
    if (["rect", "rect_outline", "round_rect", "round_rect_outline", "image"].includes(shape)) {
      const x = num(row.x, 0);
      const y = num(row.y, 0);
      return { left: x, right: x + Math.max(1, num(row.w, 1)), top: y, bottom: y + Math.max(1, num(row.h, 1)) };
    }
    if (shape === "rotated_rect_outline" || shape === "rotated_rect_fill") {
      const halfW = Math.max(0.5, num(row.w, 1) / 2);
      const halfH = Math.max(0.5, num(row.h, 1) / 2);
      return { left: num(row.x, 0) - halfW, right: num(row.x, 0) + halfW, top: num(row.y, 0) - halfH, bottom: num(row.y, 0) + halfH };
    }
    if (shape === "line") {
      const pad = sw / 2;
      return {
        left: Math.min(num(row.x1, 0), num(row.x2, 0)) - pad,
        right: Math.max(num(row.x1, 0), num(row.x2, 0)) + pad,
        top: Math.min(num(row.y1, 0), num(row.y2, 0)) - pad,
        bottom: Math.max(num(row.y1, 0), num(row.y2, 0)) + pad,
      };
    }
    if (shape === "hline") {
      const x = num(row.x, 0);
      const y = num(row.y, 0);
      return { left: x, right: x + Math.max(1, num(row.w, 1)), top: y - sw / 2, bottom: y + sw / 2 };
    }
    if (shape === "vline") {
      const x = num(row.x, 0);
      const y = num(row.y, 0);
      return { left: x - sw / 2, right: x + sw / 2, top: y, bottom: y + Math.max(1, num(row.h, 1)) };
    }
    if (shape === "triangle" || shape === "triangle_fill") {
      const xs = [num(row.x0 != null ? row.x0 : row.x, 0), num(row.x1, 0), num(row.x2, 0)];
      const ys = [num(row.y0 != null ? row.y0 : row.y, 0), num(row.y1, 0), num(row.y2, 0)];
      return { left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) };
    }
    const x = num(row.x, 0);
    const y = num(row.y, 0);
    return { left: x - 0.5, right: x + 0.5, top: y - 0.5, bottom: y + 0.5 };
  }

  function groupBounds(primitives) {
    const rows = Array.isArray(primitives) && primitives.length ? primitives : [{ shape: "pixel", x: CANVAS_WIDTH / 2, y: CANVAS_HEIGHT / 2 }];
    const boxes = rows.map(primitiveBounds);
    const left = Math.min(...boxes.map((box) => box.left));
    const right = Math.max(...boxes.map((box) => box.right));
    const top = Math.min(...boxes.map((box) => box.top));
    const bottom = Math.max(...boxes.map((box) => box.bottom));
    return {
      left,
      right,
      top,
      bottom,
      width: Math.max(1, right - left),
      height: Math.max(1, bottom - top),
      x: (left + right) / 2,
      y: (top + bottom) / 2,
    };
  }

  function shapePrimitivesForBox(shape, box) {
    const left = num(box.left, 0);
    const top = num(box.top, 0);
    const width = Math.max(2, num(box.width, 2));
    const height = Math.max(2, num(box.height, 2));
    const right = left + width;
    const bottom = top + height;
    const cx = left + width / 2;
    const cy = top + height / 2;
    const stroke = Math.max(1, round(Math.min(width, height) / 6));
    const squareSide = Math.max(2, Math.min(width, height));
    const squareLeft = cx - squareSide / 2;
    const squareTop = cy - squareSide / 2;
    const radius = Math.max(1, round(Math.min(width, height) / 4));
    const common = { color: "#f4f4ef" };

    if (shape === "circle_fill" || shape === "circle_outline") {
      return [Object.assign({ shape: shape === "circle_fill" ? "circle" : "circle_outline", x: cx, y: cy, r: squareSide / 2, sw: stroke }, common)];
    }
    if (shape === "ellipse_fill" || shape === "ellipse_outline") {
      return [Object.assign({ shape: shape === "ellipse_fill" ? "ellipse_fill" : "ellipse", x: cx, y: cy, rw: width / 2, rh: height / 2, sw: stroke }, common)];
    }
    if (shape === "triangle_fill" || shape === "triangle_outline") {
      return [Object.assign({ shape: shape === "triangle_fill" ? "triangle_fill" : "triangle", x0: cx, y0: top, x1: right, y1: bottom, x2: left, y2: bottom, sw: stroke }, common)];
    }
    if (shape === "square_fill" || shape === "square_outline") {
      return [Object.assign({ shape: shape === "square_fill" ? "rect" : "rect_outline", x: squareLeft, y: squareTop, w: squareSide, h: squareSide, sw: stroke }, common)];
    }
    if (shape === "rect_fill" || shape === "rect_outline") {
      return [Object.assign({ shape: shape === "rect_fill" ? "rect" : "rect_outline", x: left, y: top, w: width, h: height, sw: stroke }, common)];
    }
    if (shape === "round_rect_fill" || shape === "round_rect_outline") {
      return [Object.assign({ shape: shape === "round_rect_fill" ? "round_rect" : "round_rect_outline", x: left, y: top, w: width, h: height, radius, sw: stroke }, common)];
    }
    if (shape === "hline") {
      return [Object.assign({ shape: "line", x1: left, y1: cy, x2: right, y2: cy, sw: Math.max(1, round(height)) }, common)];
    }
    if (shape === "vline") {
      return [Object.assign({ shape: "line", x1: cx, y1: top, x2: cx, y2: bottom, sw: Math.max(1, round(width)) }, common)];
    }
    if (shape === "diagonal") {
      return [Object.assign({ shape: "line", x1: left, y1: bottom, x2: right, y2: top, sw: stroke }, common)];
    }
    if (shape === "diamond_fill") {
      return [
        Object.assign({ shape: "triangle_fill", x0: left, y0: cy, x1: cx, y1: top, x2: cx, y2: bottom }, common),
        Object.assign({ shape: "triangle_fill", x0: right, y0: cy, x1: cx, y1: bottom, x2: cx, y2: top }, common),
      ];
    }
    if (shape === "diamond_outline") {
      return [
        Object.assign({ shape: "line", x1: left, y1: cy, x2: cx, y2: top, sw: stroke }, common),
        Object.assign({ shape: "line", x1: cx, y1: top, x2: right, y2: cy, sw: stroke }, common),
        Object.assign({ shape: "line", x1: right, y1: cy, x2: cx, y2: bottom, sw: stroke }, common),
        Object.assign({ shape: "line", x1: cx, y1: bottom, x2: left, y2: cy, sw: stroke }, common),
      ];
    }
    if (shape === "drop_fill") {
      const headY = top + height * 0.38;
      const headR = Math.min(width * 0.34, height * 0.3);
      return [
        Object.assign({ shape: "circle", x: cx, y: headY, r: headR }, common),
        Object.assign({ shape: "triangle_fill", x0: cx - headR, y0: headY, x1: cx + headR, y1: headY, x2: cx, y2: bottom }, common),
      ];
    }
    if (shape === "heart_fill") {
      const lobeR = Math.min(width * 0.24, height * 0.24);
      const lobeY = top + height * 0.3;
      return [
        Object.assign({ shape: "circle", x: cx - lobeR * 0.82, y: lobeY, r: lobeR }, common),
        Object.assign({ shape: "circle", x: cx + lobeR * 0.82, y: lobeY, r: lobeR }, common),
        Object.assign({ shape: "triangle_fill", x0: left + width * 0.09, y0: lobeY, x1: right - width * 0.09, y1: lobeY, x2: cx, y2: bottom }, common),
      ];
    }
    if (shape === "cross") {
      return [
        Object.assign({ shape: "line", x1: left, y1: cy, x2: right, y2: cy, sw: stroke }, common),
        Object.assign({ shape: "line", x1: cx, y1: top, x2: cx, y2: bottom, sw: stroke }, common),
      ];
    }
    return [];
  }

  function shapeSource(partId, shape) {
    const normalized = normalizeEditorShape(shape);
    if (normalized === "original") return clone(CANONICAL[partId], []);
    const canonical = clone(CANONICAL[partId], []);
    return canonical.flatMap((primitive) => {
      const bounds = primitiveBounds(primitive);
      return shapePrimitivesForBox(normalized, {
        left: bounds.left,
        top: bounds.top,
        width: bounds.right - bounds.left,
        height: bounds.bottom - bounds.top,
      });
    });
  }

  function transformPrimitive(primitive, sourceBounds, editor) {
    const row = clone(primitive, {});
    const shape = normalizeShape(row.shape);
    const uniform = clamp(editor.size, 25, 200) / 100;
    const scaleX = Math.max(0.01, clamp(editor.width, 1, CANVAS_WIDTH) / Math.max(1, sourceBounds.width) * uniform);
    const scaleY = Math.max(0.01, clamp(editor.height, 1, CANVAS_HEIGHT) / Math.max(1, sourceBounds.height) * uniform);
    const targetX = clamp(editor.x, 0, CANVAS_WIDTH);
    const targetY = clamp(editor.y, 0, CANVAS_HEIGHT);
    const mapX = (value) => round(targetX + (num(value, sourceBounds.x) - sourceBounds.x) * scaleX);
    const mapY = (value) => round(targetY + (num(value, sourceBounds.y) - sourceBounds.y) * scaleY);
    const scaleStroke = () => {
      if (row.sw != null) row.sw = Math.max(1, round(num(row.sw, 1) * Math.sqrt(scaleX * scaleY)));
      if (row.stroke_width != null) row.stroke_width = Math.max(1, round(num(row.stroke_width, 1) * Math.sqrt(scaleX * scaleY)));
    };

    if (shape === "circle" || shape === "circle_outline") {
      const r = Math.max(0.5, num(row.r, 1));
      row.x = mapX(row.x);
      row.y = mapY(row.y);
      if (Math.abs(scaleX - scaleY) < 0.01) {
        row.r = Math.max(0.5, round(r * scaleX));
      } else {
        row.shape = shape === "circle_outline" ? "ellipse" : "ellipse_fill";
        delete row.r;
        row.rw = Math.max(0.5, round(r * scaleX));
        row.rh = Math.max(0.5, round(r * scaleY));
      }
      scaleStroke();
      return row;
    }
    if (shape === "ellipse" || shape === "ellipse_fill") {
      row.x = mapX(row.x);
      row.y = mapY(row.y);
      row.rw = Math.max(0.5, round(num(row.rw != null ? row.rw : row.w, 1) * scaleX));
      row.rh = Math.max(0.5, round(num(row.rh != null ? row.rh : row.h, 1) * scaleY));
      delete row.w;
      delete row.h;
      scaleStroke();
      return row;
    }
    if (["rect", "rect_outline", "round_rect", "round_rect_outline", "image"].includes(shape)) {
      row.x = mapX(row.x);
      row.y = mapY(row.y);
      row.w = Math.max(1, round(num(row.w, 1) * scaleX));
      row.h = Math.max(1, round(num(row.h, 1) * scaleY));
      if (row.radius != null) row.radius = Math.max(0, round(num(row.radius, 0) * Math.min(scaleX, scaleY)));
      if (row.r != null) row.r = Math.max(0, round(num(row.r, 0) * Math.min(scaleX, scaleY)));
      scaleStroke();
    } else if (shape === "rotated_rect_outline" || shape === "rotated_rect_fill") {
      row.x = mapX(row.x);
      row.y = mapY(row.y);
      row.w = Math.max(1, round(num(row.w, 1) * scaleX));
      row.h = Math.max(1, round(num(row.h, 1) * scaleY));
      scaleStroke();
    } else if (shape === "line") {
      row.x1 = mapX(row.x1);
      row.y1 = mapY(row.y1);
      row.x2 = mapX(row.x2);
      row.y2 = mapY(row.y2);
      scaleStroke();
    } else if (shape === "hline") {
      const startX = mapX(row.x);
      const endX = mapX(num(row.x, 0) + Math.max(1, num(row.w, 1)));
      row.shape = "line";
      row.x1 = startX;
      row.y1 = mapY(row.y);
      row.x2 = endX;
      row.y2 = mapY(row.y);
      delete row.x;
      delete row.y;
      delete row.w;
      scaleStroke();
    } else if (shape === "vline") {
      const startY = mapY(row.y);
      const endY = mapY(num(row.y, 0) + Math.max(1, num(row.h, 1)));
      row.shape = "line";
      row.x1 = mapX(row.x);
      row.y1 = startY;
      row.x2 = mapX(row.x);
      row.y2 = endY;
      delete row.x;
      delete row.y;
      delete row.h;
      scaleStroke();
    } else if (shape === "triangle" || shape === "triangle_fill") {
      row.x0 = mapX(row.x0 != null ? row.x0 : row.x);
      row.y0 = mapY(row.y0 != null ? row.y0 : row.y);
      row.x1 = mapX(row.x1);
      row.y1 = mapY(row.y1);
      row.x2 = mapX(row.x2);
      row.y2 = mapY(row.y2);
      delete row.x;
      delete row.y;
      scaleStroke();
    } else {
      if (row.x != null) row.x = mapX(row.x);
      if (row.y != null) row.y = mapY(row.y);
    }
    if (row.rot_cx != null) row.rot_cx = mapX(row.rot_cx);
    if (row.rot_cy != null) row.rot_cy = mapY(row.rot_cy);
    if (row.cx != null) row.cx = mapX(row.cx);
    if (row.cy != null) row.cy = mapY(row.cy);
    return row;
  }

  function recolorPrimitives(partId, primitives, color) {
    const normalized = normalizeColor(color, (PART_BY_ID[partId] || {}).color);
    const rows = clone(primitives, []);
    const nonDark = rows.filter((row) => !isDarkColor(primitiveColor(row, normalized)));
    const targets = partId === "mouth" && nonDark.length ? new Set(nonDark) : new Set(rows);
    rows.forEach((row) => {
      if (!targets.has(row)) return;
      row.color = normalized;
      delete row.c;
    });
    return rows;
  }

  function midpoint(primitive) {
    const box = primitiveBounds(primitive);
    return { x: (box.left + box.right) / 2, y: (box.top + box.bottom) / 2 };
  }

  function splitExtraParts(extra) {
    const rows = clone(Array.isArray(extra) ? extra : [], []);
    const used = new Set();
    const groups = { cheek_l: [], cheek_r: [], brow: [], tear: [], sweat: [] };

    rows.forEach((row, index) => {
      const role = String(row && (row.editor_role || row._editor_role) || "").trim().toLowerCase();
      if (groups[role]) {
        groups[role].push(clone(row, {}));
        used.add(index);
      }
    });

    const cheekCandidates = rows.map((row, index) => ({ row, index, point: midpoint(row) })).filter(({ row, index, point }) => {
      if (used.has(index) || normalizeShape(row.shape) !== "ellipse_fill") return false;
      const rw = num(row.rw != null ? row.rw : row.w, 0);
      const rh = num(row.rh != null ? row.rh : row.h, 0);
      return point.y >= 112 && point.y <= 178 && rw >= 4 && rw <= 28 && rh >= 2 && rh <= 18;
    });
    for (const side of ["cheek_l", "cheek_r"]) {
      if (groups[side].length) continue;
      const left = side === "cheek_l";
      const candidate = cheekCandidates.filter(({ point }) => left ? point.x < 132 : point.x > 152)
        .sort((a, b) => Math.abs(a.point.x - (left ? 58 : 226)) - Math.abs(b.point.x - (left ? 58 : 226)))[0];
      if (candidate) {
        groups[side].push(clone(candidate.row, {}));
        used.add(candidate.index);
      }
    }

    rows.forEach((row, index) => {
      if (used.has(index) || groups.brow.length) return;
      const point = midpoint(row);
      if (normalizeShape(row.shape) === "line" && point.y >= 48 && point.y <= 116 && point.x >= 44 && point.x <= 240) {
        groups.brow.push(clone(row, {}));
        used.add(index);
      }
    });
    if (groups.brow.length) {
      rows.forEach((row, index) => {
        if (used.has(index)) return;
        const point = midpoint(row);
        if (normalizeShape(row.shape) === "line" && point.y >= 48 && point.y <= 116 && point.x >= 44 && point.x <= 240) {
          groups.brow.push(clone(row, {}));
          used.add(index);
        }
      });
    }

    if (!groups.tear.length) {
      const tearCandidates = rows.map((row, index) => ({ row, index, point: midpoint(row) })).filter(({ row, index, point }) => {
        if (used.has(index) || !["circle", "circle_outline", "ellipse_fill"].includes(normalizeShape(row.shape))) return false;
        return point.x >= 54 && point.x <= 230 && point.y >= 104 && point.y <= 190;
      });
      const mirrored = tearCandidates.some((left, leftIndex) => tearCandidates.some((right, rightIndex) => (
        rightIndex !== leftIndex && Math.abs(left.point.y - right.point.y) <= 3 && Math.abs((left.point.x + right.point.x) - 284) <= 52
      )));
      if (mirrored) {
        tearCandidates.forEach(({ row, index }) => {
          groups.tear.push(clone(row, {}));
          used.add(index);
        });
      }
    }

    if (!groups.sweat.length) {
      rows.forEach((row, index) => {
        if (used.has(index) || normalizeShape(row.shape) !== "ellipse_fill") return;
        const point = midpoint(row);
        const rw = num(row.rw != null ? row.rw : row.w, 0);
        const rh = num(row.rh != null ? row.rh : row.h, 0);
        if (point.x >= 218 && point.y >= 42 && point.y <= 124 && rh > rw * 1.2 && rw <= 10 && rh <= 18) {
          groups.sweat.push(clone(row, {}));
          used.add(index);
        }
      });
    }

    return {
      groups,
      remaining: rows.filter((_, index) => !used.has(index)),
    };
  }

  function initialEditorState(partId, primitives, visible) {
    const part = PART_BY_ID[partId];
    const source = clone(Array.isArray(primitives) && primitives.length ? primitives : CANONICAL[partId], []);
    const bounds = groupBounds(source);
    const colored = source.find((row) => !isDarkColor(primitiveColor(row, part.color))) || source[0];
    return {
      visible: Boolean(visible),
      shape: "original",
      width: Math.max(1, round(bounds.width)),
      height: Math.max(1, round(bounds.height)),
      size: 100,
      x: round(bounds.x),
      y: round(bounds.y),
      color: primitiveColor(colored, part.color),
      source,
    };
  }

  function normalizeEditorState(partId, raw, fallbackPrimitives, fallbackVisible) {
    const base = initialEditorState(partId, fallbackPrimitives, fallbackVisible);
    if (!raw || typeof raw !== "object") return base;
    const source = Array.isArray(raw.source) && raw.source.length ? clone(raw.source, base.source) : base.source;
    return {
      visible: raw.visible == null ? base.visible : Boolean(raw.visible),
      shape: normalizeEditorShape(raw.shape),
      width: clamp(raw.width, 1, CANVAS_WIDTH),
      height: clamp(raw.height, 1, CANVAS_HEIGHT),
      size: clamp(raw.size, 25, 200),
      x: clamp(raw.x, 0, CANVAS_WIDTH),
      y: clamp(raw.y, 0, CANVAS_HEIGHT),
      color: normalizeColor(raw.color, base.color),
      source,
    };
  }

  function initializeEditorParts(frame, composedElements) {
    const elements = composedElements && typeof composedElements === "object" ? composedElements : {};
    const existing = frame && frame[EDITOR_META_KEY] && typeof frame[EDITOR_META_KEY] === "object" ? frame[EDITOR_META_KEY] : {};
    const split = splitExtraParts(elements.extra);
    const metadata = {
      _extra_remaining: clone(Array.isArray(existing._extra_remaining) ? existing._extra_remaining : split.remaining, []),
      [BRUSH_STROKES_KEY]: clone(Array.isArray(existing[BRUSH_STROKES_KEY]) ? existing[BRUSH_STROKES_KEY] : [], []),
    };
    while (metadata[BRUSH_STROKES_KEY].length > MAX_BRUSH_UNDO_STEPS) {
      metadata._extra_remaining.push(...clone(metadata[BRUSH_STROKES_KEY].shift(), []));
    }
    PARTS.forEach((part) => {
      const primitives = part.layer === "extra" ? split.groups[part.id] : clone(elements[part.layer], []);
      const hasSavedState = existing[part.id] && typeof existing[part.id] === "object";
      const visible = (Array.isArray(primitives) && primitives.length > 0) || (part.id === "nose" && !hasSavedState);
      metadata[part.id] = normalizeEditorState(part.id, existing[part.id], primitives, visible);
    });
    return metadata;
  }

  function renderPart(partId, state) {
    if (!state || !state.visible) return [];
    const selectedShape = normalizeEditorShape(state.shape);
    const source = selectedShape === "original"
      ? (Array.isArray(state.source) && state.source.length ? state.source : CANONICAL[partId])
      : shapeSource(partId, selectedShape);
    const bounds = groupBounds(source);
    return recolorPrimitives(partId, source, state.color).map((primitive) => transformPrimitive(primitive, bounds, state));
  }

  function composeElements(composedElements, metadata) {
    const elements = clone(composedElements && typeof composedElements === "object" ? composedElements : {}, {});
    for (const partId of ["eye_l", "eye_r", "mouth", "nose"]) elements[partId] = renderPart(partId, metadata[partId]);
    const extra = clone(metadata._extra_remaining, []);
    EXTRA_PART_IDS.forEach((partId) => extra.push(...renderPart(partId, metadata[partId])));
    const strokes = Array.isArray(metadata[BRUSH_STROKES_KEY]) ? metadata[BRUSH_STROKES_KEY] : [];
    strokes.forEach((stroke) => extra.push(...clone(stroke, [])));
    elements.extra = extra;
    if (!Array.isArray(elements.bg)) elements.bg = [];
    return elements;
  }

  function partBounds(metadata, partId) {
    if (!PART_BY_ID[partId] || !metadata || !metadata[partId] || !metadata[partId].visible) return null;
    const primitives = renderPart(partId, metadata[partId]);
    if (!primitives.length) return null;
    return groupBounds(primitives);
  }

  function hitTestParts(metadata, x, y, tolerance) {
    const px = num(x, -1);
    const py = num(y, -1);
    const pad = Math.max(0, num(tolerance, 6));
    const order = ["sweat", "tear", "brow", "cheek_r", "cheek_l", "eye_r", "eye_l", "mouth", "nose"];
    return order.filter((partId) => {
      const bounds = partBounds(metadata, partId);
      return bounds && px >= bounds.left - pad && px <= bounds.right + pad && py >= bounds.top - pad && py <= bounds.bottom + pad;
    });
  }

  function normalizeBrushStroke(primitives) {
    return clone(Array.isArray(primitives) ? primitives : [], []).filter((primitive) => (
      primitive && normalizeShape(primitive.shape) === "line"
    )).map((primitive) => ({
      shape: "line",
      x1: clamp(primitive.x1, 0, CANVAS_WIDTH),
      y1: clamp(primitive.y1, 0, CANVAS_HEIGHT),
      x2: clamp(primitive.x2, 0, CANVAS_WIDTH),
      y2: clamp(primitive.y2, 0, CANVAS_HEIGHT),
      sw: clamp(primitive.sw != null ? primitive.sw : primitive.stroke_width, 1, 12),
      color: normalizeColor(primitive.color, "#f4f4ef"),
    }));
  }

  function brushSegmentCount(metadata) {
    if (!metadata || typeof metadata !== "object") return 0;
    let count = (Array.isArray(metadata._extra_remaining) ? metadata._extra_remaining : []).filter((primitive) => (
      primitive && normalizeShape(primitive.shape) === "line"
    )).length;
    const strokes = Array.isArray(metadata[BRUSH_STROKES_KEY]) ? metadata[BRUSH_STROKES_KEY] : [];
    strokes.forEach((stroke) => {
      count += (Array.isArray(stroke) ? stroke : []).filter((primitive) => (
        primitive && normalizeShape(primitive.shape) === "line"
      )).length;
    });
    return count;
  }

  function nonBrushExtraCount(metadata) {
    if (!metadata || typeof metadata !== "object") return 0;
    let count = (Array.isArray(metadata._extra_remaining) ? metadata._extra_remaining : []).filter((primitive) => (
      !primitive || normalizeShape(primitive.shape) !== "line"
    )).length;
    EXTRA_PART_IDS.forEach((partId) => {
      count += renderPart(partId, metadata[partId]).length;
    });
    return count;
  }

  function brushRemainingSegments(metadata) {
    return Math.max(0, MAX_PRIMITIVES_PER_LAYER - nonBrushExtraCount(metadata) - brushSegmentCount(metadata));
  }

  function addBrushStroke(metadata, primitives) {
    const stroke = normalizeBrushStroke(primitives).slice(0, brushRemainingSegments(metadata));
    if (!stroke.length) return clone(metadata, {});
    const next = clone(metadata, {});
    if (!Array.isArray(next._extra_remaining)) next._extra_remaining = [];
    if (!Array.isArray(next[BRUSH_STROKES_KEY])) next[BRUSH_STROKES_KEY] = [];
    while (next[BRUSH_STROKES_KEY].length >= MAX_BRUSH_UNDO_STEPS) {
      next._extra_remaining.push(...clone(next[BRUSH_STROKES_KEY].shift(), []));
    }
    next[BRUSH_STROKES_KEY].push(stroke);
    return next;
  }

  function undoBrushStroke(metadata) {
    const next = clone(metadata, {});
    if (!Array.isArray(next[BRUSH_STROKES_KEY]) || !next[BRUSH_STROKES_KEY].length) return next;
    next[BRUSH_STROKES_KEY].pop();
    return next;
  }

  function brushUndoDepth(metadata) {
    return Math.min(MAX_BRUSH_UNDO_STEPS, Array.isArray(metadata && metadata[BRUSH_STROKES_KEY]) ? metadata[BRUSH_STROKES_KEY].length : 0);
  }

  function updateEditorPart(metadata, partId, patch) {
    if (!PART_BY_ID[partId]) return metadata;
    const next = clone(metadata, {});
    const current = normalizeEditorState(partId, next[partId], CANONICAL[partId], PART_BY_ID[partId].defaultVisible);
    const change = patch && typeof patch === "object" ? patch : {};
    if (Object.prototype.hasOwnProperty.call(change, "visible")) current.visible = Boolean(change.visible);
    if (Object.prototype.hasOwnProperty.call(change, "shape")) {
      const nextShape = normalizeEditorShape(change.shape);
      if (current.shape !== nextShape && ["eye_l", "eye_r", "mouth", "nose", "cheek_l", "cheek_r"].includes(partId)) {
        if (["circle_fill", "circle_outline", "square_fill", "square_outline", "diamond_fill", "diamond_outline", "drop_fill", "heart_fill", "cross"].includes(nextShape)) {
          const side = clamp(Math.sqrt(current.width * current.height), 2, Math.min(CANVAS_WIDTH, CANVAS_HEIGHT));
          current.width = round(side);
          current.height = round(side);
        } else if (nextShape === "hline") {
          current.height = Math.max(2, Math.min(8, current.height));
        } else if (nextShape === "vline") {
          current.width = Math.max(2, Math.min(8, current.width));
        }
      }
      current.shape = nextShape;
    }
    if (Object.prototype.hasOwnProperty.call(change, "width")) current.width = clamp(change.width, 1, CANVAS_WIDTH);
    if (Object.prototype.hasOwnProperty.call(change, "height")) current.height = clamp(change.height, 1, CANVAS_HEIGHT);
    if (Object.prototype.hasOwnProperty.call(change, "size")) current.size = clamp(change.size, 25, 200);
    if (Object.prototype.hasOwnProperty.call(change, "x")) current.x = clamp(change.x, 0, CANVAS_WIDTH);
    if (Object.prototype.hasOwnProperty.call(change, "y")) current.y = clamp(change.y, 0, CANVAS_HEIGHT);
    if (Object.prototype.hasOwnProperty.call(change, "color")) current.color = normalizeColor(change.color, current.color);
    next[partId] = current;
    return next;
  }

  function defaultScene(title) {
    const elements = {
      bg: [],
      nose: clone(CANONICAL.nose, []),
      eye_l: clone(CANONICAL.eye_l, []),
      eye_r: clone(CANONICAL.eye_r, []),
      mouth: clone(CANONICAL.mouth, []),
      extra: [],
    };
    const frame = { ms: 900, elements };
    frame[EDITOR_META_KEY] = initializeEditorParts(frame, elements);
    return { title: String(title || "我的表情"), frames: [frame] };
  }

  global.DeskbotFaceParts = Object.freeze({
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    EDITOR_META_KEY,
    BRUSH_STROKES_KEY,
    MAX_BRUSH_UNDO_STEPS,
    MAX_BRUSH_SEGMENTS,
    MAX_PRIMITIVES_PER_LAYER,
    parts: PARTS,
    partById: PART_BY_ID,
    shapeOptions: SHAPE_OPTIONS,
    canonical: CANONICAL,
    clone,
    normalizeColor,
    rgb565ToHex,
    groupBounds,
    partBounds,
    hitTestParts,
    splitExtraParts,
    initializeEditorParts,
    normalizeEditorShape,
    updateEditorPart,
    addBrushStroke,
    undoBrushStroke,
    brushUndoDepth,
    brushSegmentCount,
    brushRemainingSegments,
    composeElements,
    defaultScene,
  });
})(typeof window !== "undefined" ? window : globalThis);
