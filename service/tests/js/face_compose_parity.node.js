"use strict";

// 对拍驱动：stdin 读 {scene, frameIndex}，stdout 写
// {composed, svg}。composed 为 face_preview 的 frameElements 逐层合成结果
// （缺层继承 / 显式 [] 清层），svg 为同帧渲染（含默认色语义）。
// 由 tests/test_face_compose_parity.py 调用。

const fs = require("node:fs");
const path = require("node:path");

global.window = globalThis;
const staticDir = path.resolve(__dirname, "../../src/deskbot_server/web/static");
// eslint-disable-next-line no-eval
eval(fs.readFileSync(path.join(staticDir, "generated/primitive_spec.js"), "utf8"));
// eslint-disable-next-line no-eval
eval(fs.readFileSync(path.join(staticDir, "face_preview_2c.js"), "utf8"));

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const preview = globalThis.DeskbotFacePreview;
const frameIndex = input.frameIndex | 0;
process.stdout.write(
  JSON.stringify({
    composed: preview.frameElements(input.scene, frameIndex),
    svg: preview.sceneToSvg(input.scene, frameIndex),
  }),
);
