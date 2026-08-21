(function (global) {
  "use strict";

  // Single protocol source: generated/primitive_spec.js (built from
  // service/src/deskbot_server/pb/primitive_spec.py) must load first.
  const SPEC = global.DeskbotPrimitiveSpec;
  if (!SPEC) {
    throw new Error("generated/primitive_spec.js must be loaded before face_preview_2c.js");
  }

  // Firmware draw order. A missing key inherits the previous frame while an
  // explicit [] replaces (and therefore clears) that layer.
  const LAYERS = SPEC.LAYER_ORDER;
  const LAYER_COLORS = SPEC.LAYER_DEFAULT_COLORS;

  // Same shape vocabulary as the PB device renderer (spec lockstep-tested
  // against the firmware strcmp table).
  const SHAPE_ALIASES = SPEC.SHAPE_ALIASES;

  // The offline fallback face mirrors the firmware builtin standby face
  // (display.cpp pb_build_builtin_face_layers), copied through the spec.
  const FALLBACK_SCENE = {
    name: "fallback",
    title: "待机",
    frames: [{
      elements: JSON.parse(JSON.stringify(SPEC.BUILTIN_FACE_ELEMENTS)),
    }],
  };

  function num(value, fallback) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  const normalizeShape = SPEC.normalizeShape;

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isOutline(shape) {
    return [
      "rect_outline",
      "circle_outline",
      "ellipse",
      "triangle",
      "round_rect_outline",
      "rotated_rect_outline",
    ].includes(normalizeShape(shape));
  }

  function strokeWidth(p) {
    return Math.max(1, Math.min(12, num(p.stroke_width != null ? p.stroke_width : p.sw, 1)));
  }

  function primitiveCenter(p, shape) {
    if (shape === "line") {
      return {
        x: (num(p.x1, 0) + num(p.x2, 0)) / 2,
        y: (num(p.y1, 0) + num(p.y2, 0)) / 2,
      };
    }
    if (shape === "rotated_rect_outline" || shape === "rotated_rect_fill") {
      return { x: num(p.x, 0), y: num(p.y, 0) };
    }
    if (shape === "hline") {
      return { x: num(p.x, 0) + (Math.max(1, num(p.w, 1)) - 1) / 2, y: num(p.y, 0) };
    }
    if (shape === "vline") {
      return { x: num(p.x, 0), y: num(p.y, 0) + (Math.max(1, num(p.h, 1)) - 1) / 2 };
    }
    if (shape === "rect" || shape === "rect_fill" || shape === "rect_outline" ||
        shape === "round_rect" || shape === "round_rect_fill" || shape === "round_rect_outline") {
      return { x: num(p.x, 0) + num(p.w, 1) / 2, y: num(p.y, 0) + num(p.h, 1) / 2 };
    }
    if (shape === "triangle" || shape === "triangle_fill" || shape === "triangle_outline") {
      return {
        x: (num(p.x0 != null ? p.x0 : p.x, 0) + num(p.x1, 0) + num(p.x2, 0)) / 3,
        y: (num(p.y0 != null ? p.y0 : p.y, 0) + num(p.y1, 0) + num(p.y2, 0)) / 3,
      };
    }
    return { x: num(p.x, 0), y: num(p.y, 0) };
  }

  function rotateTransform(p, shape) {
    const angle = num(p.rotation != null ? p.rotation : p.angle, 0);
    if (!angle) return "";
    const center = primitiveCenter(p, shape);
    const cx = num(p.rot_cx != null ? p.rot_cx : p.cx, center.x);
    const cy = num(p.rot_cy != null ? p.rot_cy : p.cy, center.y);
    return ` transform="rotate(${angle} ${cx} ${cy})"`;
  }

  const rgb565ToCss = SPEC.rgb565ToCss;
  const cssColorToRgb565 = SPEC.cssColorToRgb565;

  // Structural whitelist (shared spec rule): only the named palette,
  // #rgb / #rrggbb / #rrggbbaa, numeric rgb()/rgba() and RGB565/RGB888
  // integers survive. The scene document is untrusted (importable JSON) and
  // the output lands inside v-html rendered SVG markup, so anything else is
  // rejected — callers then fall back to the per-layer default color.
  const normalizeCssColor = SPEC.normalizeCssColor;

  function primitiveColor(p, layer) {
    if (p && p.c != null) return rgb565ToCss(p.c);
    const source = normalizeCssColor(p && p.color) || LAYER_COLORS[layer] || "#f4f4ef";
    const quantized = cssColorToRgb565(source, LAYER_COLORS[layer]);
    return quantized == null ? source : rgb565ToCss(quantized);
  }

  function shapeToSvg(p, layer) {
    if (!p || typeof p !== "object") return "";
    const shape = normalizeShape(p.shape);
    const color = primitiveColor(p, layer);
    const sw = strokeWidth(p);
    const fill = isOutline(shape) ? "none" : color;
    const stroke = isOutline(shape) || shape === "line" ? color : "none";
    const tr = rotateTransform(p, shape);

    if (shape === "circle" || shape === "circle_fill" || shape === "circle_outline") {
      return `<circle cx="${num(p.x, 0)}" cy="${num(p.y, 0)}" r="${num(p.r, 1)}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${tr}/>`;
    }
    if (shape === "ellipse" || shape === "ellipse_fill" || shape === "ellipse_outline") {
      const rw = p.rw != null ? p.rw : (p.w != null ? p.w : p.r);
      const rh = p.rh != null ? p.rh : (p.h != null ? p.h : p.r);
      return `<ellipse cx="${num(p.x, 0)}" cy="${num(p.y, 0)}" rx="${num(rw, 1)}" ry="${num(rh, 1)}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${tr}/>`;
    }
    if (shape === "rect" || shape === "rect_fill" || shape === "rect_outline") {
      return `<rect x="${num(p.x, 0)}" y="${num(p.y, 0)}" width="${num(p.w, 1)}" height="${num(p.h, 1)}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${tr}/>`;
    }
    if (shape === "round_rect" || shape === "round_rect_fill" || shape === "round_rect_outline") {
      return `<rect x="${num(p.x, 0)}" y="${num(p.y, 0)}" width="${num(p.w, 1)}" height="${num(p.h, 1)}" rx="${num(p.radius != null ? p.radius : p.r, 0)}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${tr}/>`;
    }
    if (shape === "line") {
      return `<line x1="${num(p.x1, 0)}" y1="${num(p.y1, 0)}" x2="${num(p.x2, 0)}" y2="${num(p.y2, 0)}" stroke="${color}" stroke-width="${sw}" stroke-linecap="round"${tr}/>`;
    }
    if (shape === "hline") {
      const x = num(p.x, 0);
      const y = num(p.y, 0);
      return `<line x1="${x}" y1="${y}" x2="${x + Math.max(1, num(p.w, 1)) - 1}" y2="${y}" stroke="${color}" stroke-width="${sw}" stroke-linecap="round"${tr}/>`;
    }
    if (shape === "vline") {
      const x = num(p.x, 0);
      const y = num(p.y, 0);
      return `<line x1="${x}" y1="${y}" x2="${x}" y2="${y + Math.max(1, num(p.h, 1)) - 1}" stroke="${color}" stroke-width="${sw}" stroke-linecap="round"${tr}/>`;
    }
    if (shape === "pixel") {
      return `<rect x="${num(p.x, 0)}" y="${num(p.y, 0)}" width="1" height="1" fill="${color}"${tr}/>`;
    }
    if (shape === "triangle" || shape === "triangle_fill" || shape === "triangle_outline") {
      const points = [
        `${num(p.x0 != null ? p.x0 : p.x, 0)},${num(p.y0 != null ? p.y0 : p.y, 0)}`,
        `${num(p.x1, 0)},${num(p.y1, 0)}`,
        `${num(p.x2, 0)},${num(p.y2, 0)}`,
      ].join(" ");
      return `<polygon points="${points}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${tr}/>`;
    }
    if (shape === "rotated_rect_outline" || shape === "rotated_rect_fill") {
      const cx = num(p.x, 0);
      const cy = num(p.y, 0);
      const w = Math.max(1, num(p.w, 1));
      const h = Math.max(1, num(p.h, 1));
      const angle = num(p.angle != null ? p.angle : p.rotation, 0);
      const rotatedFill = shape === "rotated_rect_fill" ? color : "none";
      const rotatedStroke = shape === "rotated_rect_outline" ? color : "none";
      const transform = angle ? ` transform="rotate(${angle} ${cx} ${cy})"` : "";
      return `<rect x="${cx - w / 2}" y="${cy - h / 2}" width="${w}" height="${h}" fill="${rotatedFill}" stroke="${rotatedStroke}" stroke-width="${sw}"${transform}/>`;
    }
    if (shape === "text") {
      const size = Math.max(1, Math.min(3, Math.trunc(num(p.size != null ? p.size : p.text_size, 1))));
      return `<text x="${num(p.x, 0)}" y="${num(p.y, 0)}" fill="${color}" font-size="${size * 8}" text-anchor="start" dominant-baseline="text-before-edge"${tr}>${esc(p.text || "")}</text>`;
    }
    return "";
  }

  function lerp(a, b, progress) {
    return num(a, 0) + (num(b, 0) - num(a, 0)) * progress;
  }

  function interpolatePrimitive(previous, current, progress) {
    if (!previous || !current || normalizeShape(previous.shape) !== normalizeShape(current.shape)) {
      return Object.assign({}, current);
    }
    const shape = normalizeShape(current.shape);
    if (shape === "image" || (shape === "text" && (previous.text !== current.text || previous.size !== current.size))) {
      return Object.assign({}, current);
    }
    const out = Object.assign({}, current);
    ["x", "y", "w", "h", "r", "rw", "rh", "x0", "y0", "x1", "y1", "x2", "y2",
      "rot_cx", "rot_cy", "cx", "cy", "angle", "rotation", "stroke_width", "sw"].forEach((key) => {
      if (current[key] != null || previous[key] != null) out[key] = lerp(previous[key], current[key], progress);
    });
    return out;
  }

  function interpolatedElements(scene, frameIndex, progress) {
    const current = frameElements(scene, frameIndex);
    if (frameIndex <= 0) return current;
    const previous = frameElements(scene, frameIndex - 1);
    const t = Math.max(0, Math.min(1, num(progress, 1)));
    const out = {};
    for (const layer of LAYERS) {
      const rows = Array.isArray(current[layer]) ? current[layer] : [];
      const prior = Array.isArray(previous[layer]) ? previous[layer] : [];
      out[layer] = rows.map((primitive, index) => interpolatePrimitive(prior[index], primitive, t));
    }
    return out;
  }

  function frameElements(scene, frameIndex) {
    const s = scene && typeof scene === "object" ? scene : FALLBACK_SCENE;
    const frames = Array.isArray(s.frames) && s.frames.length ? s.frames : FALLBACK_SCENE.frames;
    const idx = Math.max(0, Math.min(Math.floor(num(frameIndex, 0)), frames.length - 1));
    const composed = {};
    for (let frameIndex = 0; frameIndex <= idx; frameIndex += 1) {
      const frame = frames[frameIndex] || null;
      const elements = frame && (frame.elements || (frame.anim && frame.anim.elements));
      if (!elements || typeof elements !== "object") continue;
      for (const layer of LAYERS) {
        if (Object.prototype.hasOwnProperty.call(elements, layer) && Array.isArray(elements[layer])) {
          composed[layer] = elements[layer].slice();
        }
      }
    }
    return composed;
  }

  function sceneToSvg(scene, frameIndex, progress) {
    const elements = progress == null
      ? frameElements(scene, frameIndex)
      : interpolatedElements(scene, frameIndex, progress);
    let out = "";
    for (const layer of LAYERS) {
      const rows = Array.isArray(elements[layer]) ? elements[layer] : [];
      for (const p of rows) out += shapeToSvg(p, layer);
    }
    return out;
  }

  function frameCount(scene) {
    const frames = scene && Array.isArray(scene.frames) ? scene.frames : [];
    return Math.max(1, frames.length || FALLBACK_SCENE.frames.length);
  }

  function frameMs(scene, frameIndex) {
    const s = scene && typeof scene === "object" ? scene : FALLBACK_SCENE;
    const frames = Array.isArray(s.frames) && s.frames.length ? s.frames : FALLBACK_SCENE.frames;
    const idx = Math.max(0, Math.min(Math.floor(num(frameIndex, 0)), frames.length - 1));
    return Math.max(40, num((frames[idx] || {}).ms, 500));
  }

  function findScene(scenes, name) {
    const want = String(name || "").trim().toLowerCase();
    if (!want || !Array.isArray(scenes)) return null;
    return scenes.find((s) => String((s && s.name) || "").trim().toLowerCase() === want) || null;
  }

  function pickScene(scenes, map, mood) {
    const rows = Array.isArray(scenes) ? scenes : [];
    const mapping = map && typeof map === "object" ? map : {};
    return (
      findScene(rows, mapping[mood || "idle"]) ||
      findScene(rows, "idle") ||
      findScene(rows, "default") ||
      rows[0] ||
      FALLBACK_SCENE
    );
  }

  global.DeskbotFacePreview = {
    sceneToSvg,
    frameElements,
    interpolatedElements,
    frameCount,
    frameMs,
    rgb565ToCss,
    cssColorToRgb565,
    findScene,
    pickScene,
    fallbackScene: FALLBACK_SCENE,
  };
})(window);
