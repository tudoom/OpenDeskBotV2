"""Ark Responses image-to-face SVG integration."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Callable
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps, UnidentifiedImageError

from deskbot_server.face_expr_scenes_store import normalize_face_expr_scenes
from deskbot_server.safe_fetch import safe_provider_urlopen

ARK_RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
DEFAULT_IMAGE_MODEL = "doubao-seed-2-1-pro-260628"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MIN_ANIMATION_FRAMES = 4
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_IMAGE_UPLOAD_REQUEST_BYTES = MAX_IMAGE_BYTES + 512 * 1024
ALLOWED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ARK_IMAGE_PROCESSING_NOTICE = (
    "上传图片会发送至火山引擎 Ark 第三方服务，用于生成机器人表情。"
)
SCENE_LAYERS = ("eye_l", "eye_r", "nose", "mouth", "extra")

ArkTransport = Callable[[str, dict[str, Any], str, int], dict[str, Any]]

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)
_NAME_RE = re.compile(r"[^a-z0-9_]+", re.I)
_VALID_SCENE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$", re.I)
_SAFE_COLOR_RE = re.compile(
    r"^(none|currentColor|black|white|#[0-9a-fA-F]{3,8}|rgb\([0-9,\s.%-]+\)|rgba\([0-9,\s.%-]+\))$"
)
_IMAGE_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_IMAGE_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}
_ALLOWED_SVG_TAGS = {
    "svg",
    "g",
    "path",
    "circle",
    "ellipse",
    "rect",
    "line",
    "polyline",
    "polygon",
}
_ALLOWED_SVG_ATTRS = {
    "svg": {"viewBox", "width", "height", "fill", "stroke", "stroke-width"},
    "g": {"fill", "stroke", "stroke-width", "opacity", "transform"},
    "path": {
        "d",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "transform",
    },
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width", "opacity", "transform"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width", "opacity", "transform"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke", "stroke-width", "opacity", "transform"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width", "stroke-linecap", "opacity", "transform"},
    "polyline": {"points", "fill", "stroke", "stroke-width", "stroke-linejoin", "opacity", "transform"},
    "polygon": {"points", "fill", "stroke", "stroke-width", "stroke-linejoin", "opacity", "transform"},
}


_ARK_HOST_SUFFIX = "volces.com"
ARK_KEY_MISSING_MESSAGE = (
    "画表情 / 生图走的是火山方舟接口，需要方舟的 API Key：大模型不是火山方舟时，"
    "请在表情页「生图 API 配置」填一个方舟 Key，或在模型配置页保存过豆包（火山方舟）的 Key。"
)


def _llm_base_is_ark_or_unset() -> bool:
    base = str(os.environ.get("LLM_BASE_URL") or "").strip()
    if not base:
        return True  # 没配 Base URL 的老配置：历史上就是方舟，沿用 LLM_API_KEY
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    return host == _ARK_HOST_SUFFIX or host.endswith("." + _ARK_HOST_SUFFIX)


def _env_or_file(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        from deskbot_server.env import read_env_file

        return str(read_env_file().get(name) or "").strip()
    except Exception:  # noqa: BLE001 —— 读不到 .env 就当没有
        return ""


def _resolve_api_key(api_key: str | None = None) -> str:
    """画表情 / 生图都是火山方舟的接口，Key 必须是方舟的。

    顺序：显式实参 → 表情页「生图 API 配置」的 ARK_IMAGE_GEN_API_KEY → 大模型本身就是方舟
    （或没配 Base URL）时复用 LLM_API_KEY → 模型配置页保存过的豆包 Key（LLM_API_KEY_DOUBAO）
    → 联网检索单独填的方舟 Key。2026-09-14：默认大模型换成 DeepSeek 后这里仍直接拿
    LLM_API_KEY 去调方舟，被 401（"The API key format is incorrect"）。
    """
    key = str(api_key or "").strip()
    if key:
        return key
    key = str(os.environ.get("ARK_IMAGE_GEN_API_KEY") or "").strip()
    if key:
        return key
    llm_key = str(os.environ.get("LLM_API_KEY") or "").strip()
    if llm_key and "请替换" not in llm_key and _llm_base_is_ark_or_unset():
        return llm_key
    for name in ("LLM_API_KEY_DOUBAO", "ARK_WEB_SEARCH_API_KEY"):
        key = _env_or_file(name)
        if key:
            return key
    raise ValueError(ARK_KEY_MISSING_MESSAGE)


def _resolve_model(model: str | None = None) -> str:
    return (
        str(
            model
            or os.environ.get("ARK_IMAGE_TO_SVG_MODEL")
            or os.environ.get("ARK_VISION_MODEL")
            or DEFAULT_IMAGE_MODEL
        ).strip()
        or DEFAULT_IMAGE_MODEL
    )


def _resolve_max_output_tokens(value: int | None = None) -> int:
    raw = value if value is not None else os.environ.get("ARK_IMAGE_TO_SVG_MAX_OUTPUT_TOKENS")
    if raw is None or raw == "":
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        tokens = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT_TOKENS
    return max(512, min(tokens, 16000))


def _resolve_thinking() -> dict[str, str] | None:
    value = str(os.environ.get("ARK_IMAGE_TO_SVG_THINKING") or "disabled").strip().lower()
    if value in {"", "default", "auto"}:
        return None
    if value not in {"disabled", "enabled"}:
        value = "disabled"
    return {"type": value}


def _normalize_image_mime(mime_type: str) -> str:
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    return _IMAGE_MIME_ALIASES.get(mime, mime)


def validate_image_upload(image_bytes: bytes, mime_type: str = "") -> str:
    """Validate bytes, decoded format, full first frame, and pixel count."""

    if not image_bytes:
        raise ValueError("请上传图片文件")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("图片不能超过 6MB")
    declared_mime = _normalize_image_mime(mime_type)
    if declared_mime and declared_mime not in ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("只支持 PNG、JPG、WebP 或 GIF 图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as probe:
                image_format = str(probe.format or "").upper()
                actual_mime = _IMAGE_FORMAT_MIME_TYPES.get(image_format)
                if actual_mime not in ALLOWED_IMAGE_MIME_TYPES:
                    raise ValueError("只支持 PNG、JPG、WebP 或 GIF 图片")
                if declared_mime and declared_mime != actual_mime:
                    raise ValueError("图片声明格式与实际解码格式不一致")
                width, height = (int(value) for value in probe.size)
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片超过解压像素上限")
                probe.verify()

            # ``verify`` does not decode pixels. Reopen and load the exact
            # frame that preprocessing will use so truncated scan data cannot
            # reach Ark through a filename/MIME-only check.
            with Image.open(io.BytesIO(image_bytes)) as decoded:
                if str(decoded.format or "").upper() != image_format:
                    raise ValueError("图片格式验证不一致")
                if getattr(decoded, "is_animated", False):
                    decoded.seek(0)
                decoded.load()
    except ValueError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as exc:
        raise ValueError("图片无法完整解码") from exc
    return actual_mime


def _validate_image_upload(image_bytes: bytes, mime_type: str) -> str:
    """Compatibility wrapper for existing internal callers and tests."""

    return validate_image_upload(image_bytes, mime_type)


def _image_data_url(image_bytes: bytes, mime_type: str) -> str:
    mime = _validate_image_upload(image_bytes, mime_type)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _otsu_threshold(gray: Any) -> int:
    histogram = gray.histogram()
    total = sum(histogram)
    if total <= 0:
        return 160

    sum_total = sum(level * count for level, count in enumerate(histogram))
    sum_back = 0
    weight_back = 0
    best_threshold = 160
    best_variance = -1.0
    for level, count in enumerate(histogram):
        weight_back += count
        if weight_back <= 0:
            continue
        weight_fore = total - weight_back
        if weight_fore <= 0:
            break
        sum_back += level * count
        mean_back = sum_back / weight_back
        mean_fore = (sum_total - sum_back) / weight_fore
        variance = weight_back * weight_fore * (mean_back - mean_fore) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = level
    return max(40, min(220, int(best_threshold)))


def _preprocess_image_for_face_svg(image_bytes: bytes, mime_type: str) -> tuple[bytes, str, dict[str, Any]]:
    source_mime = validate_image_upload(image_bytes, mime_type)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as source:
                if getattr(source, "is_animated", False):
                    source.seek(0)
                source.load()
                image = ImageOps.exif_transpose(source)
                gray = ImageOps.grayscale(image.convert("RGB"))
            gray = ImageOps.autocontrast(gray)
            threshold = _otsu_threshold(gray)
            black_white = gray.point(lambda pixel: 255 if pixel > threshold else 0, mode="L")
            output = io.BytesIO()
            black_white.save(output, format="PNG", optimize=True)
            processed = output.getvalue()
        # Treat preprocessing as a security boundary: if its output cannot be
        # fully decoded, fail closed instead of silently uploading the original.
        validate_image_upload(processed, "image/png")
        return (
            processed,
            "image/png",
            {
                "applied": True,
                "mode": "binary_bw",
                "mime_type": "image/png",
                "source_mime_type": source_mime,
                "threshold": threshold,
            },
        )
    except ValueError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as exc:
        raise ValueError("图片预处理失败，未发送至 Ark") from exc


def _build_prompt(user_prompt: str) -> str:
    extra = str(user_prompt or "").strip()
    prompt = (
        "你是 Deskbot 小歪的图片表情包转译器。输入图会先被服务端转成高对比黑白图，"
        "请定位并提取面部表情，只保留眉眼、眼神、鼻子、嘴型和脸颊这些表达情绪的五官造型，"
        "忽略背景、文字、水印、边框、装饰、身体和手势，生成适合 284x240 OLED 显示的矢量表情。"
        "只输出 JSON，不要 Markdown，不要解释。"
        "JSON schema: {name, title, svg, scene}。"
        "svg 必须是单个 <svg viewBox=\"0 0 284 240\">，只使用 path/circle/ellipse/rect/line/polyline/polygon，"
        "不要 script、style、foreignObject、image 或外链资源。"
        "scene 必须是 Deskbot emotion scene：{name,title,frames:[{ms,elements}]}，"
        "frames 必须是 4 到 6 帧动画，每帧 80 到 900ms，且每帧 elements 必须是 object。"
        "elements 只能包含 eye_l/eye_r/nose/mouth/extra 数组；scene 图元 shape 只使用 "
        "ellipse_fill, ellipse, circle, line, rect, round_rect, round_rect_outline。"
        "scene 图元必须使用 PB 坐标字段：圆/椭圆用 x/y/r 或 x/y/rw/rh，矩形用 x/y/w/h/radius，"
        "线用 x1/y1/x2/y2；不要在 scene 里使用 cx/cy/rx/ry/path/svg。"
        "scene.name 必须匹配 ^[a-z][a-z0-9_]*$，用英文小写 snake_case，坐标范围基于 284x240。"
    )
    if extra:
        prompt += f"\n用户补充要求：{extra}"
    return prompt


def _build_text_prompt(user_prompt: str) -> str:
    """文生表情专用提示词：没有输入图，靠情绪描述从"手绘库画法"出发设计。

    模型对纯坐标摆放很不擅长；给它几个真实手绘表情当范例（few-shot）并讲清这套图元
    语言里的画法——弧形嘴靠"白圆角矩形上叠黑色小圆角矩形抠出来"、眯眼靠压 rh、眉毛靠
    斜线对、脸颊靠粉色小椭圆、颜色走 c(RGB565)——生成质量比只给 schema 高一个档次。
    """
    extra = str(user_prompt or "").strip() or "自然、可爱的默认表情"
    return (
        "你是桌面机器人「小歪」的表情设计师。请为下面的情绪设计一个显示在 284×240 屏幕上的表情，"
        "严格沿用【手绘范例】的画法与坐标习惯，只输出一个 JSON 对象，不要 Markdown、不要解释。"
        "JSON schema: {name, title, svg, scene}。\n"
        "【画法要点（必须照做）】"
        "1) 脸型固定：eye_l 椭圆中心 (105, 96~100)、eye_r 椭圆中心 (181, 96~100)，正常 rw=rh=11；"
        "nose 固定 circle (142,124) r=11 c=65159；两眼关于 x=143 对称。"
        "2) 弧形嘴的唯一画法：mouth 里先放一个白色 round_rect（c=2047），再叠一个黑色 round_rect（c=0）"
        "把中间抠掉——黑矩形比白矩形左右各缩进 5、向下错开 6，黑矩形越高（h 越大）笑弧越深："
        "微笑 h=3、大笑 h=13、张嘴 O 型用高瘦白矩形+高瘦黑矩形（见范例）。不要用 line 画嘴。"
        "3) 眯眼/闭眼：把该眼 rh 压到 2~4（rw 不变）；瞪眼：rw=rh=15。"
        "4) 眉毛/皱眉：放在 extra，用 line 成对出现，生气是每侧 3 条平行斜线（内低外高）。"
        "5) 脸颊：extra 里两个粉色椭圆 c=64064（害羞用红 c=63488），rw=12 rh=6，位于 (55,131) 与 (218,131)。"
        "6) 颜色只用 c（RGB565 整数）：2047=青色脸部色，65159=黄，64064=粉，63488=红，65535=白，0=黑。"
        "7) 坏笑/得意/狡黠/撇嘴这类【不对称】神态，嘴不能再用对称胶囊，必须用带角度的矩形画歪嘴："
        "mouth 放一个白色 rotated_rect_fill（x/y 是中心，x≈152 y≈158 w≈56 h≈12），angle 取 -10~-15 让右嘴角翘起"
        "（想让左边翘就取 +10~+15），可再叠一个同角度、更小的黑色 rotated_rect_fill（w≈44 h≈4，y 再 +2，c=0）当嘴缝；"
        "翘起那一侧的眼睛 rh 压到 3~5（眯眼），另一侧眼睛正常并在其上方 extra 加一条粗眉 line（sw=4，"
        "y 在眼睛中心上方 14~18，从眼睛左侧 16 到右侧 14，靠外那头更高）；翘起那一侧再加一个粉色腮红椭圆。"
        "9) 眉毛统一用 line 且 sw=3~4，必须紧贴眼睛上方（距离 12~18），不要飘远；线条一律带 sw。"
        "8) 每帧图元 4~9 个，坐标不得超出 0~284 / 0~240。\n"
        "【手绘范例（真实库数据）——只学它们的画法与坐标习惯；具体数值（眯哪只眼、嘴的角度/宽度/位置、"
        "眉毛角度、腮红有无）必须按当前情绪自行决定，不要把范例原样照抄】"
        "开心：{\"eye_l\":[{\"shape\":\"ellipse_fill\",\"x\":105,\"y\":99,\"rw\":11,\"rh\":11,\"c\":2047}],"
        "\"eye_r\":[{\"shape\":\"ellipse_fill\",\"x\":181,\"y\":99,\"rw\":11,\"rh\":11,\"c\":2047}],"
        "\"nose\":[{\"shape\":\"circle\",\"x\":142,\"y\":124,\"r\":11,\"c\":65159}],"
        "\"mouth\":[{\"shape\":\"round_rect\",\"x\":141,\"y\":148,\"w\":72,\"h\":28,\"radius\":14,\"c\":2047},"
        "{\"shape\":\"round_rect\",\"x\":146,\"y\":154,\"w\":62,\"h\":13,\"radius\":6,\"c\":0}],"
        "\"extra\":[{\"shape\":\"ellipse_fill\",\"x\":55,\"y\":131,\"rw\":12,\"rh\":6,\"c\":64064},"
        "{\"shape\":\"ellipse_fill\",\"x\":218,\"y\":131,\"rw\":12,\"rh\":6,\"c\":64064}]}\n"
        "生气：{\"eye_l\":[{\"shape\":\"ellipse_fill\",\"x\":105,\"y\":99,\"rw\":11,\"rh\":11,\"c\":2047}],"
        "\"eye_r\":[{\"shape\":\"ellipse_fill\",\"x\":181,\"y\":99,\"rw\":11,\"rh\":11,\"c\":2047}],"
        "\"nose\":[{\"shape\":\"circle\",\"x\":142,\"y\":124,\"r\":11,\"c\":65159}],"
        "\"mouth\":[{\"shape\":\"round_rect\",\"x\":145,\"y\":157,\"w\":64,\"h\":18,\"radius\":9,\"c\":2047},"
        "{\"shape\":\"round_rect\",\"x\":150,\"y\":163,\"w\":54,\"h\":3,\"radius\":1,\"c\":0}],"
        "\"extra\":[{\"shape\":\"line\",\"x1\":79,\"y1\":83,\"x2\":129,\"y2\":93,\"c\":2047},"
        "{\"shape\":\"line\",\"x1\":79,\"y1\":85,\"x2\":129,\"y2\":95,\"c\":2047},"
        "{\"shape\":\"line\",\"x1\":79,\"y1\":87,\"x2\":129,\"y2\":97,\"c\":2047},"
        "{\"shape\":\"line\",\"x1\":207,\"y1\":83,\"x2\":157,\"y2\":93,\"c\":2047},"
        "{\"shape\":\"line\",\"x1\":207,\"y1\":85,\"x2\":157,\"y2\":95,\"c\":2047},"
        "{\"shape\":\"line\",\"x1\":207,\"y1\":87,\"x2\":157,\"y2\":97,\"c\":2047}]}\n"
        "惊讶：{\"eye_l\":[{\"shape\":\"ellipse_fill\",\"x\":105,\"y\":96,\"rw\":15,\"rh\":15,\"c\":2047}],"
        "\"eye_r\":[{\"shape\":\"ellipse_fill\",\"x\":181,\"y\":97,\"rw\":15,\"rh\":15,\"c\":2047}],"
        "\"nose\":[{\"shape\":\"circle\",\"x\":142,\"y\":124,\"r\":11,\"c\":65159}],"
        "\"mouth\":[{\"shape\":\"round_rect\",\"x\":163,\"y\":146,\"w\":28,\"h\":32,\"radius\":16,\"c\":2047},"
        "{\"shape\":\"round_rect\",\"x\":168,\"y\":152,\"w\":18,\"h\":17,\"radius\":8,\"c\":0}]}\n"
        "眯眼（苏醒）：eye_l/eye_r 各为 {\"shape\":\"ellipse_fill\",\"x\":105 或 181,\"y\":100,\"rw\":11,\"rh\":3,\"c\":2047}。\n"
        "坏笑（不对称范例，右嘴角翘）：{\"eye_l\":[{\"shape\":\"ellipse_fill\",\"x\":105,\"y\":99,\"rw\":11,\"rh\":11,\"c\":2047}],"
        "\"eye_r\":[{\"shape\":\"ellipse_fill\",\"x\":181,\"y\":101,\"rw\":11,\"rh\":4,\"c\":2047}],"
        "\"nose\":[{\"shape\":\"circle\",\"x\":142,\"y\":124,\"r\":11,\"c\":65159}],"
        "\"mouth\":[{\"shape\":\"rotated_rect_fill\",\"x\":152,\"y\":158,\"w\":56,\"h\":12,\"angle\":-12,\"c\":2047},"
        "{\"shape\":\"rotated_rect_fill\",\"x\":152,\"y\":160,\"w\":44,\"h\":4,\"angle\":-12,\"c\":0}],"
        "\"extra\":[{\"shape\":\"line\",\"x1\":89,\"y1\":84,\"x2\":119,\"y2\":80,\"sw\":4,\"c\":2047},"
        "{\"shape\":\"ellipse_fill\",\"x\":218,\"y\":131,\"rw\":12,\"rh\":6,\"c\":64064}]}\n"
        "【技术约束】"
        "svg 必须是单个 <svg viewBox=\"0 0 284 240\">，只用 path/circle/ellipse/rect/line/polyline/polygon，"
        "不要 script、style、foreignObject、image 或外链资源。"
        "scene 必须是 {name,title,frames:[{ms,elements}]}，frames 必须是 4 到 6 帧动画，每帧 80 到 900ms，"
        "每帧 elements 必须是 object，只含 eye_l/eye_r/nose/mouth/extra 五个键，每个键的值必须是数组（哪怕只有一个图元）。"
        "让表情有 2~3 帧自然变化（眨眼、嘴弧微变、眉毛微动），不要每帧完全相同。"
        "scene 图元 shape 只用 ellipse_fill, ellipse, circle, line, round_rect, round_rect_outline, "
        "rotated_rect_fill, rotated_rect_outline, triangle_fill；"
        "圆/椭圆用 x/y/r 或 x/y/rw/rh（中心坐标），round_rect 用 x/y/w/h/radius（x/y 是左上角），"
        "rotated_rect 用 x/y/w/h/angle（x/y 是中心，angle 为度数，负值右端翘起），triangle 用 x1/y1/x2/y2/x3/y3，"
        "线用 x1/y1/x2/y2/sw；"
        "不要在 scene 里使用 cx/cy/rx/ry/path/svg。"
        "scene.name 必须匹配 ^[a-z][a-z0-9_]*$。\n"
        f"情绪描述：{extra}"
    )


def _post_ark_responses(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deskbot-server/0.1",
        },
        method="POST",
    )
    try:
        with safe_provider_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace").strip()
        preview = err_body[:1000] if err_body else str(exc)
        raise RuntimeError(f"Ark Responses 请求失败 HTTP {exc.code}: {preview}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ark Responses 请求失败: {exc.reason}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw[:1000].decode("utf-8", "replace")
        raise RuntimeError(f"Ark Responses 返回不是合法 JSON: {preview}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Ark Responses 返回格式异常：顶层不是 JSON object")
    return parsed


def _extract_response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            content = item.get("content")
            if isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, dict):
                        if isinstance(chunk.get("text"), str):
                            parts.append(chunk["text"])
                        elif isinstance(chunk.get("content"), str):
                            parts.append(chunk["content"])
            elif isinstance(content, str):
                parts.append(content)

    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                parts.append(message["content"])

    return "\n".join(p for p in parts if p).strip()


def _json_object_from_text(text: str) -> dict[str, Any]:
    cleaned = _JSON_FENCE_RE.sub("", str(text or "").strip()).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        obj = json.loads(cleaned[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("模型输出不是 JSON object")
    return obj


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _safe_attr(name: str, value: str) -> str | None:
    if name.lower().startswith("on"):
        return None
    clean = str(value or "").strip()
    if not clean:
        return None
    if name in {"fill", "stroke"} and not _SAFE_COLOR_RE.match(clean):
        return None
    if name in {"href", "xlink:href", "style"}:
        return None
    return clean[:2000]


def _clean_svg_node(node: ET.Element) -> ET.Element | None:
    tag = _strip_namespace(str(node.tag))
    if tag not in _ALLOWED_SVG_TAGS:
        return None
    clean = ET.Element(tag)
    allowed = _ALLOWED_SVG_ATTRS.get(tag, set())
    for raw_name, raw_value in node.attrib.items():
        name = _strip_namespace(str(raw_name))
        if name not in allowed:
            continue
        value = _safe_attr(name, raw_value)
        if value is not None:
            clean.set(name, value)
    for child in list(node):
        child_clean = _clean_svg_node(child)
        if child_clean is not None:
            clean.append(child_clean)
    return clean


def sanitize_svg(svg: str) -> str:
    try:
        root = ET.fromstring(str(svg or "").strip())
    except ET.ParseError as exc:
        raise ValueError(f"SVG 解析失败: {exc}") from exc
    if _strip_namespace(str(root.tag)) != "svg":
        raise ValueError("模型输出的 svg 必须以 <svg> 为根节点")
    clean = _clean_svg_node(root)
    if clean is None:
        raise ValueError("SVG 内容为空")
    if not clean.get("viewBox"):
        clean.set("viewBox", "0 0 284 240")
    return ET.tostring(clean, encoding="unicode", short_empty_elements=True)


def _hashed_scene_name(*parts: Any) -> str:
    digest = hashlib.sha1()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(str(part or "").encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return f"image_expr_{digest.hexdigest()[:12]}"


def _slug_name(value: str, fallback: str = "image_expression") -> str:
    raw = str(value or fallback).strip().lower()
    raw = _NAME_RE.sub("_", raw).strip("_")[:64]
    if not raw or not _VALID_SCENE_NAME_RE.match(raw):
        raw = fallback
    return raw


def _num(value: Any, fallback: float = 0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    return n if n == n else fallback


def _is_background_like(params: dict[str, Any]) -> bool:
    fill = str(params.get("fill") or params.get("color") or "").strip().lower()
    rw = _num(params.get("rx", params.get("rw", params.get("r"))), 0)
    rh = _num(params.get("ry", params.get("rh", params.get("r"))), 0)
    w = _num(params.get("width", params.get("w")), 0)
    h = _num(params.get("height", params.get("h")), 0)
    large = (rw >= 90 and rh >= 70) or (w >= 180 and h >= 140)
    if fill in {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgb(255, 255, 255)"}:
        return large
    return False


def _model_element_to_primitive(item: object) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    params = item.get("params") if isinstance(item.get("params"), dict) else item
    shape = str(item.get("shape") or item.get("type") or params.get("shape") or "").strip().lower()
    if not shape:
        return None
    if _is_background_like(params):
        return None
    # 颜色透传：手绘库靠 c=0（黑）叠在白色圆角矩形上"抠"出弧形嘴、c=64064 画粉脸颊，
    # 早先转换只保留几何、把颜色全丢，黑色抠嘴一律变回图层缺省色，弧形直接消失。
    # c（RGB565 整数）原样透传；color/fill/stroke（css）作为 color 交给归一化校验。
    color_fields: dict[str, Any] = {}
    raw_c = params.get("c")
    if isinstance(raw_c, int) and not isinstance(raw_c, bool) and 0 <= raw_c <= 0xFFFF:
        color_fields["c"] = raw_c
    else:
        css = params.get("color") or params.get("fill") or params.get("stroke")
        if isinstance(css, str) and css.strip() and css.strip().lower() != "none":
            color_fields["color"] = css.strip()
    prim = _model_element_geometry(shape, params)
    if prim is None:
        return None
    prim.update(color_fields)
    return prim


def _model_element_geometry(shape: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if shape in {"ellipse", "ellipse_fill", "ellipse_outline"}:
        return {
            "shape": "ellipse" if "outline" in shape else "ellipse_fill",
            "x": round(_num(params.get("x", params.get("cx")), 0)),
            "y": round(_num(params.get("y", params.get("cy")), 0)),
            "rw": round(_num(params.get("rw", params.get("rx", params.get("r"))), 1)),
            "rh": round(_num(params.get("rh", params.get("ry", params.get("r"))), 1)),
            **({"sw": round(_num(params.get("stroke_width", params.get("sw")), 2), 2)} if "outline" in shape or shape == "ellipse" else {}),
        }
    if shape in {"circle", "circle_fill", "circle_outline"}:
        return {
            "shape": "circle_outline" if "outline" in shape else "circle",
            "x": round(_num(params.get("x", params.get("cx")), 0)),
            "y": round(_num(params.get("y", params.get("cy")), 0)),
            "r": round(_num(params.get("r"), 1)),
        }
    if shape == "line":
        return {
            "shape": "line",
            "x1": round(_num(params.get("x1"), 0)),
            "y1": round(_num(params.get("y1"), 0)),
            "x2": round(_num(params.get("x2"), 0)),
            "y2": round(_num(params.get("y2"), 0)),
            "sw": round(_num(params.get("stroke_width", params.get("sw")), 2), 2),
        }
    if shape in {"rotated_rect", "rotated_rect_fill", "rotated_rect_outline", "fill_rotated_rect", "draw_rotated_rect"}:
        # 带角度的矩形（x/y 为中心）：库里没有，但坏笑/撇嘴这类"一边翘"的嘴只有它能画。
        outline = "outline" in shape or shape == "draw_rotated_rect"
        out = {
            "shape": "rotated_rect_outline" if outline else "rotated_rect_fill",
            "x": round(_num(params.get("x", params.get("cx")), 0)),
            "y": round(_num(params.get("y", params.get("cy")), 0)),
            "w": round(_num(params.get("w", params.get("width")), 1)),
            "h": round(_num(params.get("h", params.get("height")), 1)),
            "angle": round(_num(params.get("angle", params.get("rotation")), 0), 1),
        }
        if outline:
            out["sw"] = round(_num(params.get("stroke_width", params.get("sw")), 2), 2)
        return out
    if shape in {"triangle", "triangle_fill", "triangle_outline", "fill_triangle", "draw_triangle"}:
        outline = shape in {"triangle", "triangle_outline", "draw_triangle"}
        out = {
            "shape": "triangle" if outline else "triangle_fill",
            "x1": round(_num(params.get("x1"), 0)), "y1": round(_num(params.get("y1"), 0)),
            "x2": round(_num(params.get("x2"), 0)), "y2": round(_num(params.get("y2"), 0)),
            "x3": round(_num(params.get("x3"), 0)), "y3": round(_num(params.get("y3"), 0)),
        }
        if outline:
            out["sw"] = round(_num(params.get("stroke_width", params.get("sw")), 2), 2)
        return out
    if shape in {"rect", "rect_fill", "round_rect_fill", "round_rect", "round_rect_outline", "rect_outline"}:
        w = _num(params.get("w", params.get("width")), 1)
        h = _num(params.get("h", params.get("height")), 1)
        x = _num(params.get("x"), _num(params.get("cx"), 0) - w / 2)
        y = _num(params.get("y"), _num(params.get("cy"), 0) - h / 2)
        outline = "outline" in shape
        round_rect = "round" in shape or params.get("rx") is not None or params.get("radius") is not None
        return {
            "shape": ("round_rect_outline" if outline else "round_rect") if round_rect else ("rect_outline" if outline else "rect"),
            "x": round(x),
            "y": round(y),
            "w": round(w),
            "h": round(h),
            "radius": round(_num(params.get("radius", params.get("rx", params.get("r"))), min(w, h) / 2)),
        }
    return None


def _primitive_center(item: dict[str, Any]) -> tuple[float, float]:
    if item.get("shape") == "line":
        return (
            (_num(item.get("x1"), 0) + _num(item.get("x2"), 0)) / 2,
            (_num(item.get("y1"), 0) + _num(item.get("y2"), 0)) / 2,
        )
    if "w" in item and "h" in item:
        return (_num(item.get("x"), 0) + _num(item.get("w"), 0) / 2, _num(item.get("y"), 0) + _num(item.get("h"), 0) / 2)
    return (_num(item.get("x"), 0), _num(item.get("y"), 0))


def _group_flat_model_elements(items: list[object]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {layer: [] for layer in SCENE_LAYERS}
    for raw in items:
        primitive = _model_element_to_primitive(raw)
        if not primitive:
            continue
        cx, cy = _primitive_center(primitive)
        if cy < 122:
            layer = "eye_l" if cx < 142 else "eye_r"
        elif cy < 140 and abs(cx - 142) < 28:
            layer = "nose"
        elif cy >= 130:
            layer = "mouth"
        else:
            layer = "extra"
        grouped[layer].append(primitive)
    return grouped


def _coerce_grouped_model_elements(elements: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {layer: [] for layer in SCENE_LAYERS}
    for layer in grouped:
        rows = elements.get(layer)
        # 模型常把单个图元的层写成对象而非数组（eye_l:{...} 而非 eye_l:[{...}]），
        # 早先直接 continue 丢弃，导致眼/鼻/嘴整层消失、脸残缺。单对象在此包成单元素数组。
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        for row in rows:
            primitive = _model_element_to_primitive(row)
            if primitive:
                grouped[layer].append(primitive)
    return grouped


def _shift_primitive(item: dict[str, Any], *, dx: int = 0, dy: int = 0) -> dict[str, Any]:
    out = copy.deepcopy(item)
    shape = str(out.get("shape") or "").strip().lower()
    if shape == "line":
        for key, delta in (("x1", dx), ("x2", dx), ("y1", dy), ("y2", dy)):
            if key in out:
                out[key] = round(_num(out.get(key), 0) + delta)
    else:
        if "x" in out:
            out["x"] = round(_num(out.get("x"), 0) + dx)
        if "y" in out:
            out["y"] = round(_num(out.get("y"), 0) + dy)
    return out


def _animate_primitive(item: dict[str, Any], *, layer: str, variant: int) -> dict[str, Any]:
    out = copy.deepcopy(item)
    shape = str(out.get("shape") or "").strip().lower()
    if layer in {"eye_l", "eye_r"} and shape in {"ellipse", "ellipse_fill"}:
        rh = max(1, round(_num(out.get("rh", out.get("r")), 4)))
        rw = max(1, round(_num(out.get("rw", out.get("r")), 4)))
        if variant == 1:
            out["rh"] = max(1, round(rh * 0.55))
            out["rw"] = max(1, round(rw * 1.04))
            out["y"] = round(_num(out.get("y"), 0) + 1)
        elif variant == 2:
            out["rh"] = max(1, rh + 2)
        else:
            out["y"] = round(_num(out.get("y"), 0) - 1)
        return out
    if layer == "mouth":
        if shape in {"ellipse", "ellipse_fill"}:
            rh = max(1, round(_num(out.get("rh", out.get("r")), 4)))
            out["rh"] = max(1, rh + (3 if variant == 2 else -2 if variant == 1 else 1))
            out["y"] = round(_num(out.get("y"), 0) + (1 if variant == 2 else 0))
        elif shape in {"round_rect", "round_rect_outline", "rect", "rect_outline"}:
            h = max(1, round(_num(out.get("h"), 4)))
            out["h"] = max(1, h + (4 if variant == 2 else -2 if variant == 1 else 1))
            out["radius"] = min(round(_num(out.get("radius", out.get("r")), out["h"] / 2)), max(1, out["h"] // 2))
        elif shape == "line":
            out = _shift_primitive(out, dy=(1 if variant == 2 else -1 if variant == 1 else 0))
        return out
    if layer == "extra":
        return _shift_primitive(out, dy=(-1 if variant == 1 else 1 if variant == 2 else 0))
    return out


def _animated_frame_from(frame: dict[str, Any], *, variant: int, ms: int) -> dict[str, Any]:
    elements = frame.get("elements") if isinstance(frame.get("elements"), dict) else {}
    next_elements: dict[str, list[dict[str, Any]]] = {layer: [] for layer in SCENE_LAYERS}
    for layer in SCENE_LAYERS:
        rows = elements.get(layer)
        if not isinstance(rows, list):
            continue
        next_elements[layer] = [
            _animate_primitive(row, layer=layer, variant=variant)
            for row in rows
            if isinstance(row, dict)
        ]
    return {"ms": ms, "elements": next_elements}


def _ensure_animation_frames(scene: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(scene)
    frames = [f for f in out.get("frames", []) if isinstance(f, dict)]
    if not frames:
        frames = _fallback_scene(fallback_name=out.get("name") or "image_expression", fallback_title=out.get("title") or "表情")["frames"]
    normalized_frames = []
    for frame in frames:
        next_frame = copy.deepcopy(frame)
        elements = next_frame.get("elements")
        if isinstance(elements, dict):
            next_frame["elements"] = _coerce_grouped_model_elements(elements)
        else:
            next_frame["elements"] = {layer: [] for layer in SCENE_LAYERS}
        normalized_frames.append(next_frame)
    while len(normalized_frames) < MIN_ANIMATION_FRAMES:
        base = normalized_frames[(len(normalized_frames) - 1) % len(normalized_frames)]
        variant = len(normalized_frames) % 3
        ms = 140 if variant == 1 else 220 if variant == 2 else 360
        normalized_frames.append(_animated_frame_from(base, variant=variant, ms=ms))
    out["frames"] = normalized_frames
    return out


def _coerce_model_scene(raw: dict[str, Any]) -> dict[str, Any]:
    scene = dict(raw)
    frames = scene.get("frames")
    if not isinstance(frames, list):
        return scene
    next_frames = []
    for frame in frames:
        if not isinstance(frame, dict):
            next_frames.append(frame)
            continue
        next_frame = dict(frame)
        elements = next_frame.get("elements")
        if isinstance(elements, list):
            next_frame["elements"] = _group_flat_model_elements(elements)
        elif isinstance(elements, dict):
            next_frame["elements"] = _coerce_grouped_model_elements(elements)
        next_frames.append(next_frame)
    scene["frames"] = next_frames
    return scene


def _fallback_scene(*, fallback_name: str, fallback_title: str) -> dict[str, Any]:
    return {
        "name": fallback_name,
        "title": fallback_title,
        "frames": [
            {
                "ms": 360,
                "elements": {
                    "eye_l": [
                        {"shape": "ellipse_fill", "x": 90, "y": 88, "rw": 18, "rh": 20},
                        {"shape": "line", "x1": 66, "y1": 62, "x2": 110, "y2": 74, "sw": 4},
                    ],
                    "eye_r": [
                        {"shape": "ellipse_fill", "x": 196, "y": 88, "rw": 18, "rh": 20},
                        {"shape": "line", "x1": 174, "y1": 74, "x2": 218, "y2": 62, "sw": 4},
                    ],
                    "nose": [{"shape": "circle", "x": 142, "y": 122, "r": 4}],
                    "mouth": [{"shape": "round_rect_outline", "x": 112, "y": 146, "w": 60, "h": 36, "radius": 18}],
                    "extra": [],
                },
            }
        ],
    }


def _normalize_scene(raw: object, *, fallback_name: str, fallback_title: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        scene = _fallback_scene(fallback_name=fallback_name, fallback_title=fallback_title)
    else:
        scene = _coerce_model_scene(raw)
    scene["name"] = _slug_name(str(scene.get("name") or fallback_name), fallback_name)
    scene["title"] = str(scene.get("title") or fallback_title or scene["name"]).strip()[:80]
    try:
        normalized = normalize_face_expr_scenes([scene])[0]
    except ValueError:
        fallback = _fallback_scene(fallback_name=scene["name"], fallback_title=scene["title"])
        normalized = normalize_face_expr_scenes([fallback])[0]
    animated = _ensure_animation_frames(normalized)
    return normalize_face_expr_scenes([animated])[0]


def _response_payload(model: str, image_url: str | None, prompt: str, *, max_output_tokens: int | None = None) -> dict[str, Any]:
    # 图生与文生共用同一条 Ark 通路：有图带 input_image，纯文本就只发描述。
    content: list[dict[str, Any]] = []
    if image_url:
        content.append({"type": "input_image", "image_url": image_url})
    prompt_text = _build_prompt(prompt) if image_url else _build_text_prompt(prompt)
    content.append({"type": "input_text", "text": prompt_text})
    payload: dict[str, Any] = {
        "model": model,
        "max_output_tokens": _resolve_max_output_tokens(max_output_tokens),
        "input": [{"role": "user", "content": content}],
    }
    thinking = _resolve_thinking()
    if thinking:
        payload["thinking"] = thinking
    return payload


def generate_face_svg_from_text(
    prompt: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    responses_url: str | None = None,
    timeout: int = 90,
    max_output_tokens: int | None = None,
    transport: ArkTransport | None = None,
) -> dict[str, Any]:
    """文生表情：与图生同一条流水线，只是没有输入图片。"""
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("请先输入一句描述")
    resolved_key = _resolve_api_key(api_key)
    resolved_model = _resolve_model(model)
    payload = _response_payload(resolved_model, None, clean_prompt, max_output_tokens=max_output_tokens)
    call = transport or _post_ark_responses
    response = call(str(responses_url or ARK_RESPONSES_URL), payload, resolved_key, timeout)
    raw_text = _extract_response_text(response)
    if not raw_text:
        raise RuntimeError("Ark Responses 没有返回文本内容")
    obj = _json_object_from_text(raw_text)
    fallback_name = _hashed_scene_name(
        clean_prompt.encode("utf-8"), clean_prompt, obj.get("name"), obj.get("title"), obj.get("svg")
    )
    name = _slug_name(str(obj.get("name") or fallback_name), fallback=fallback_name)
    title = str(obj.get("title") or name).strip()[:80]
    svg = sanitize_svg(str(obj.get("svg") or ""))
    scene = _normalize_scene(obj.get("scene"), fallback_name=name, fallback_title=title)
    return {
        "ok": True,
        "name": scene["name"],
        "title": scene.get("title") or title,
        "svg": svg,
        "scene": scene,
        "raw": response,
        "model": resolved_model,
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else None,
    }


def generate_face_svg_from_image(
    image_bytes: bytes,
    mime_type: str,
    *,
    prompt: str = "",
    api_key: str | None = None,
    model: str | None = None,
    responses_url: str | None = None,
    timeout: int = 90,
    max_output_tokens: int | None = None,
    transport: ArkTransport | None = None,
) -> dict[str, Any]:
    resolved_key = _resolve_api_key(api_key)
    resolved_model = _resolve_model(model)
    model_image_bytes, model_mime_type, preprocess_meta = _preprocess_image_for_face_svg(image_bytes, mime_type)
    image_url = _image_data_url(model_image_bytes, model_mime_type)
    payload = _response_payload(resolved_model, image_url, prompt, max_output_tokens=max_output_tokens)
    call = transport or _post_ark_responses
    response = call(str(responses_url or ARK_RESPONSES_URL), payload, resolved_key, timeout)
    raw_text = _extract_response_text(response)
    if not raw_text:
        raise RuntimeError("Ark Responses 没有返回文本内容")
    obj = _json_object_from_text(raw_text)
    fallback_name = _hashed_scene_name(image_bytes, prompt, obj.get("name"), obj.get("title"), obj.get("svg"))
    name = _slug_name(str(obj.get("name") or fallback_name), fallback=fallback_name)
    title = str(obj.get("title") or name).strip()[:80]
    svg = sanitize_svg(str(obj.get("svg") or ""))
    scene = _normalize_scene(obj.get("scene"), fallback_name=name, fallback_title=title)
    return {
        "ok": True,
        "name": scene["name"],
        "title": scene.get("title") or title,
        "svg": svg,
        "scene": scene,
        "raw": response,
        "model": resolved_model,
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else None,
        "image_preprocess": preprocess_meta,
        "external_processor": {
            "provider": "火山引擎 Ark",
            "purpose": "图片转机器人表情",
            "notice": ARK_IMAGE_PROCESSING_NOTICE,
        },
    }
