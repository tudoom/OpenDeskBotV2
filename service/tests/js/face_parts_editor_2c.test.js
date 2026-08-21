"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

require(path.resolve(__dirname, "../../src/deskbot_server/web/static/generated/primitive_spec.js"));
require(path.resolve(__dirname, "../../src/deskbot_server/web/static/face_parts_editor_2c.js"));

const editor = globalThis.DeskbotFaceParts;
assert.ok(editor, "face parts editor must be exported");

// 编辑器与预览页共用 spec 色域：命名色 / rgb() / hex8 不再窄于预览页。
assert.equal(editor.normalizeColor("red"), "#ff0000");
assert.equal(editor.normalizeColor("rgb(255, 103, 0)"), "#ff6700");
assert.equal(editor.normalizeColor("#ffd23fcc"), "#ffd23f");
assert.equal(editor.normalizeColor("url(javascript:alert(1))", "#f4f4ef"), "#f4f4ef");
assert.equal(editor.normalizeColor(0xfb20), "#ff6500");
assert.deepEqual(editor.parts.map((part) => part.id), [
  "eye_l",
  "eye_r",
  "mouth",
  "nose",
  "cheek_l",
  "cheek_r",
  "brow",
  "tear",
  "sweat",
]);

const scene = editor.defaultScene("测试表情");
const frame = scene.frames[0];
let metadata = editor.initializeEditorParts(frame, frame.elements);
assert.equal(metadata.eye_l.visible, true);
assert.equal(metadata.eye_r.visible, true);
assert.equal(metadata.mouth.visible, true);
assert.equal(metadata.nose.visible, true);
assert.equal(metadata.nose.color, "#ffd23f");
for (const id of ["cheek_l", "cheek_r", "brow", "tear", "sweat"]) {
  assert.equal(metadata[id].visible, false, `${id} must default to hidden`);
}

const supportedDeviceShapes = new Set([
  "rect",
  "rect_outline",
  "circle",
  "circle_outline",
  "line",
  "ellipse",
  "ellipse_fill",
  "triangle",
  "triangle_fill",
  "round_rect",
  "round_rect_outline",
]);

assert.equal(
  editor.shapeOptions.find((item) => item.value === "hline").label,
  "横线（实心）",
);

for (const part of editor.parts) {
  for (const option of editor.shapeOptions.filter((item) => item.value !== "original")) {
    let next = metadata;
    for (const otherPart of editor.parts) {
      next = editor.updateEditorPart(next, otherPart.id, { visible: otherPart.id === part.id });
    }
    next = editor.updateEditorPart(next, part.id, {
      shape: option.value,
      color: "#ff3366",
    });
    const elements = editor.composeElements(frame.elements, next);
    const primitives = part.layer === "extra" ? elements.extra : elements[part.layer];
    assert.ok(primitives.length > 0, `${part.id}/${option.value} must render at least one primitive`);
    assert.ok(
      primitives.every((primitive) => supportedDeviceShapes.has(primitive.shape)),
      `${part.id}/${option.value} emitted an unsupported device shape`,
    );
  }
}

metadata = editor.updateEditorPart(metadata, "cheek_l", { visible: true, shape: "heart_fill" });
metadata = editor.updateEditorPart(metadata, "cheek_r", { visible: false, shape: "drop_fill" });
const elements = editor.composeElements(frame.elements, metadata);
assert.ok(elements.extra.length > 0, "visible left cheek must render");
assert.equal(metadata.cheek_r.visible, false, "right cheek visibility must remain independent");

let brushMetadata = editor.initializeEditorParts(frame, frame.elements);
for (let index = 0; index < 6; index += 1) {
  brushMetadata = editor.addBrushStroke(brushMetadata, [{
    shape: "line",
    x1: 10 + index,
    y1: 20,
    x2: 20 + index,
    y2: 30,
    sw: 3,
    color: "#12abef",
  }]);
}
assert.equal(editor.brushUndoDepth(brushMetadata), 5, "only the latest five strokes remain undoable");
assert.equal(editor.composeElements(frame.elements, brushMetadata).extra.length, 6, "older strokes stay rendered after leaving the undo window");
for (let index = 0; index < 5; index += 1) brushMetadata = editor.undoBrushStroke(brushMetadata);
assert.equal(editor.brushUndoDepth(brushMetadata), 0);
assert.equal(editor.composeElements(frame.elements, brushMetadata).extra.length, 1, "five undo operations remove the latest five strokes");

const oversizedStroke = Array.from({ length: editor.MAX_BRUSH_SEGMENTS + 20 }, (_, index) => ({
  shape: "line",
  x1: index,
  y1: 10,
  x2: index + 1,
  y2: 11,
  sw: 2,
  color: "#ffffff",
}));
let boundedBrushMetadata = editor.initializeEditorParts(frame, frame.elements);
boundedBrushMetadata = editor.addBrushStroke(boundedBrushMetadata, oversizedStroke);
assert.equal(editor.brushSegmentCount(boundedBrushMetadata), editor.MAX_BRUSH_SEGMENTS, "brush segments are capped per frame");
assert.equal(editor.brushRemainingSegments(boundedBrushMetadata), 0);
boundedBrushMetadata = editor.addBrushStroke(boundedBrushMetadata, oversizedStroke);
assert.equal(editor.brushSegmentCount(boundedBrushMetadata), editor.MAX_BRUSH_SEGMENTS, "additional strokes cannot grow a full frame");

let sharedExtraMetadata = editor.initializeEditorParts(frame, frame.elements);
sharedExtraMetadata = editor.updateEditorPart(sharedExtraMetadata, "cheek_l", { visible: true });
sharedExtraMetadata = editor.updateEditorPart(sharedExtraMetadata, "brow", { visible: true });
const nonBrushExtra = editor.composeElements(frame.elements, sharedExtraMetadata).extra.length;
assert.equal(
  editor.brushRemainingSegments(sharedExtraMetadata),
  editor.MAX_PRIMITIVES_PER_LAYER - nonBrushExtra,
  "visible extra-layer face parts reduce the remaining brush budget",
);

const noseBounds = editor.partBounds(metadata, "nose");
assert.ok(noseBounds && noseBounds.width === 22 && noseBounds.height === 22, "default nose must use the fake-smile yellow circle");
assert.ok(editor.hitTestParts(metadata, 142, 124, 0).includes("nose"), "nose must be selectable in the preview");

const legacyFrame = { elements: { bg: [], eye_l: [], eye_r: [], mouth: [], nose: [], extra: [] } };
const legacyMetadata = editor.initializeEditorParts(legacyFrame, legacyFrame.elements);
assert.equal(legacyMetadata.nose.visible, true, "legacy frames without nose editor state receive the default nose");
const explicitlyHiddenNose = editor.initializeEditorParts({
  elements: legacyFrame.elements,
  editor_parts: { nose: { ...legacyMetadata.nose, visible: false } },
}, legacyFrame.elements);
assert.equal(explicitlyHiddenNose.nose.visible, false, "an explicitly hidden nose stays hidden");
