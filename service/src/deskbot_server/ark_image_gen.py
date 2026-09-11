"""卡通表情生图：Ark Seedream 组图（同一角色连贯 3 帧）→ 240×240 JPEG。

与 ``ark_face_svg``（矢量图元表情）不同，这里生成的是位图帧，作为表情库场景的
``assets`` 随 PB 二进制下发，帧里用 ``image`` 图元引用。风格通过预设 prompt 切换。
"""
from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from deskbot_server.ark_face_svg import _resolve_api_key
from deskbot_server.pb.llm_display import _canonical_jpeg_bytes
from deskbot_server.safe_fetch import safe_provider_urlopen

ARK_IMAGES_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
# 表情页「生图 API 配置」写进 .env 的三个键：Key 留空复用 LLM_API_KEY，地址/模型留空用默认。
IMAGE_GEN_API_KEY_ENV = "ARK_IMAGE_GEN_API_KEY"
IMAGE_GEN_URL_ENV = "ARK_IMAGE_GEN_URL"
IMAGE_GEN_MODEL_ENV = "ARK_IMAGE_GEN_MODEL"
# 设备只有 240×240，生成分辨率越小越快：Seedream 4.0 允许 1024²（单张实测 6s），
# Seedream 5.0 最小 1920²（单张 38s，2048² 组图 ~100s），所以默认用 4.0；
# 用 ARK_IMAGE_GEN_MODEL 换成 5.0 时自动用它的最小尺寸。
DEFAULT_IMAGE_GEN_MODEL = "doubao-seedream-4-0-250828"
# 表情页「生图 API 配置」的模型下拉：只列实测过的两代 Seedream，用户不用自己填 ID。
IMAGE_GEN_MODEL_CHOICES: tuple[dict[str, str], ...] = (
    {"id": DEFAULT_IMAGE_GEN_MODEL, "title": "Seedream 4.0（默认，1024²，单张约 6 秒）"},
    {"id": "doubao-seedream-5-0-260128", "title": "Seedream 5.0（1920²，单张约 40 秒，更精细）"},
)
IMAGE_GEN_CONSOLE_URL = "https://console.volcengine.com/ark/region:cn-beijing/model/detail?Id=doubao-seedream-5-0"
GENERATION_SIZE = "1024x1024"
SEEDREAM5_MIN_SIZE = "1920x1920"
FRAME_W = 240
FRAME_H = 240
FRAME_COUNT = 3
MAX_UPLOAD_FRAMES = 3

ArkTransport = Callable[[str, dict[str, Any], str, int], dict[str, Any]]

_COMMON_RULES = (
    "硬性规则（必须全部遵守）："
    "1）整张画面只有一张脸的五官，只画眼睛、眉毛、腮红、嘴（以及该风格允许的少量情绪符号）；"
    "绝对不能出现第二张脸、多个表情、对比图、分格、九宫格、表情包合集。"
    "2）五官直接悬浮在背景上：不画头、脸型、脸的皮肤或肤色区域、下巴轮廓、耳朵、头发、身体，"
    "不画任何脸盘/底板/圆形或椭圆的脸，除了眼白以外没有大块的浅色面。不要写实、不要真人感。"
    "角色特点（比如小狗、小猫）只能通过眼睛、鼻子、嘴的形状和颜色来体现，仍然不画头、耳朵、毛发和轮廓。"
    "3）背景：{background}，从中心一直铺满到四条边和四个角；不要边框、相框、四角的取景角标或括号标记、阴影、渐变。"
    "4）五官居中，整体占画面约 65%，左右对称摆放（表情本身可以不对称）。"
    "5）扁平矢量插画，线条干净；画面里不能出现任何文字、字母、数字、颜色代码、水印。"
    "背景色应占画面的大部分面积（约 75%），五官之间露出背景。"
    "{n_rule}"
)
_SINGLE_RULE = "6）只输出 1 张图。"
_MULTI_RULE = "6）输出 {n} 张独立的图片，每张是同一段动画的一帧，同一个角色、同一构图、同一大小；不要把多帧拼进同一张图。"
_REFERENCE_HINT = "以参考图为准：角色、五官画法、颜色、线条粗细都和参考图完全一致，只按下面的描述改变表情。"
_SPLIT_FRAMES_HINT = "（务必分成 {n} 张独立的图片分别输出，每张只画一帧，绝不要拼图或多格排版。）"

# 风格预设：prompt 只讲"怎么画"；acting 按状态/情绪单独取用（只把当前要的那一条写进 prompt——
# 一次把所有情绪都列出来，模型会把它们全画进同一张图，出现"好几个头"）；variants 用于"3 张预览"
# 让三张彼此不同、也和默认套图拉开差异。
CARTOON_STYLES: dict[str, dict[str, Any]] = {
    "kawaii": {
        "title": "可爱颜文字",
        "background": "纯黑色 #000000",
        "black_bg": True,
        "prompt": (
            "日系可爱颜文字（kawaii）风格：圆润的线条、粗描边、软萌。眼睛是主角（大而圆或弯成弧线），"
            "两侧各一个粉色腮红，嘴很小很简洁，眉毛细而短（可省略）。允许用白色小星星✦、汗珠、爱心等可爱符号点缀。"
        ),
        "acting": {
            "idle": "放松待机：眼睛平视、自然浅笑。",
            "listening": "认真倾听：眼睛睁得更大、高光变成星形，眉毛高高扬起，嘴闭成一个小小的 o。",
            "thinking": "思考中：眼珠一起飘向左上角，嘴抿成小波浪，额头旁一滴汗珠，右上角几个白色小圆点。",
            "speaking": "开心地说话：眼睛弯成向下的开心弧线，嘴张成圆润的猫嘴「ω」或红色小 D 形。",
            "happy": "开心：眼睛弯成「^ ^」，嘴大大地笑，旁边几颗白色小星星。",
            "sad": "难过：眼睛下垂、眼角挂一颗泪珠，嘴变成波浪线。",
            "angry": "生气：眉毛倒竖成「＼ ／」，眼睛眯成横线，嘴抿紧。",
            "surprised": "惊讶：眼睛睁成两个大圆、嘴张成 O，旁边一个白色感叹号。",
            "sleepy": "困了：眼睛变成两条弧线，嘴微张，角落一个白色小「z」。",
        },
        "variants": [
            "眼睛：超大的圆眼，眼珠占满眼白，两点高光；腮红是两个圆点；嘴是小小的猫嘴 ω。",
            "眼睛：细长上挑的杏仁眼，眼珠偏小、一点高光；腮红是三道短斜线；嘴是一条向上弯的细弧。",
            "眼睛：两个纯黑的圆点眼（没有眼白）配大大的白色星形高光；腮红是椭圆；嘴是张开的小 D 形露出红色口腔。",
        ],
    },
    "lineart": {
        "title": "线稿漫画",
        "background": "纯黑色 #000000",
        "black_bg": True,
        "prompt": (
            "日式漫画白色线稿风：只有眼睛、眉毛、嘴三组线条悬浮在黑底上，用粗细有变化的白色墨线勾勒，"
            "只用极少的色块（眼珠一种颜色、腮红粉色、嘴内红色）。两条有力的粗眉毛、两只带瞳孔和高光的大眼睛、"
            "一张轮廓分明的嘴。线条干净、有力，像漫画里贴着镜头的五官特写，但绝不画出脸和头："
            "眼白之外不允许出现白色的脸、面具形状或任何浅色的大块面，眼睛和嘴之间必须是黑色背景。"
        ),
        "acting": {
            "idle": "放松待机：眉毛平放，眼睛平视，嘴角微微上扬。",
            "listening": "认真倾听：眉毛上抬，眼睛睁大，眼周画几道短的白色集中线，嘴微微张开。",
            "thinking": "思考中：眉毛拧成八字、一只眼半闭，嘴角撇下，额头旁一滴大汗珠。",
            "speaking": "说话中：嘴大开露出一排白牙，眉毛随语气挑起。",
            "happy": "开心：眉毛高挑、眼睛眯成弧线，嘴大张露出一排白牙。",
            "sad": "难过：眉毛外侧下垂，眼睛半闭含泪，嘴向下弯。",
            "angry": "生气：眉毛倒竖，眼睛变成两道竖线，额头画漫画青筋「💢」。",
            "surprised": "惊讶：眼睛变成两个空心圆，嘴张成 O，周围几道放射线。",
            "sleepy": "困了：眼睛闭成两条横线，嘴微张，旁边一个「z」。",
        },
        "variants": [
            "眼睛：细长上挑的眼睛，眼珠是竖长的椭圆，一点高光；眉毛又粗又直；嘴是一条利落的线。",
            "眼睛：圆圆的大眼，上眼线粗、几根睫毛，眼珠里两层高光；眉毛细而弯；嘴小而圆润。",
            "眼睛：半圆形的眼睛（上眼皮是一条直线），眼珠是小圆点；眉毛短粗上挑；嘴角带一颗小獠牙。",
        ],
    },
    "doodle": {
        "title": "手绘涂鸦",
        "background": "米白色的纸张底色（像画在便签纸上，纯色、没有纹理）",
        "black_bg": False,
        "prompt": (
            "随性的手绘涂鸦风：用黑色细马克笔线条画在米白纸上，明显的手抖感和不闭合的线头，故意不对称、有点乱，"
            "少量粉色或红色点缀。允许在角落画小小的涂鸦符号（?、!、…、zzz、音符）。"
        ),
        "acting": {
            "idle": "放松待机：眼睛是两个随手画的圆圈，嘴是一条歪歪的微笑线。",
            "listening": "认真倾听：一只眼画成大圆、另一只画成小圆，旁边涂一个「?」。",
            "thinking": "思考中：眼睛变成两个螺旋圈，嘴是一条歪曲线，角落三个小圆点「…」。",
            "speaking": "说话中：嘴画成一团张开的乱线，周围几条放射状短线。",
            "happy": "开心：眼睛画成「>」「<」，嘴是大大的锯齿笑。",
            "sad": "难过：眼睛是两个「x」，嘴是波浪线，旁边几条落下的雨线。",
            "angry": "生气：眉毛是两道粗重的斜线，眼睛是两个实心点，嘴是一条抖动的直线，头顶几条乱线。",
            "surprised": "惊讶：眼睛是两个大圆里各一个小点，嘴是一个大 O，角落一个「!」。",
            "sleepy": "困了：眼睛是两条横线，嘴微张，角落写歪歪的「zzz」。",
        },
        "variants": [
            "眼睛：两个圆圈里各一个小黑点；嘴是一条随手的弧线；线条很细很抖。",
            "眼睛：两个实心的黑点；嘴是锯齿线；周围一圈随手画的短线像光芒。",
            "眼睛：一只圆一只方（故意不对称）；嘴是一个小三角；角落一个小音符。",
        ],
    },
    "flat": {
        "title": "扁平几何",
        "background": "深靛蓝色的纯色底",
        "black_bg": False,
        "prompt": (
            "极简扁平几何风：只用三种颜色——青色（#00FFFF）、亮黄色、白色。两只青色几何眼睛（眼中一个白色圆点高光），"
            "两眼中间偏下一个小小的亮黄色圆鼻子，鼻子下方一张白色简洁的嘴。纯色填充、无描边、无眉毛、不允许任何符号或文字，"
            "所有形状都是干净的几何图形、边缘锐利。"
        ),
        "acting": {
            "idle": "放松待机：眼睛是两个青色圆形，嘴是一段短横条。",
            "listening": "认真倾听：两只眼睛变大成竖长的椭圆并微微靠拢，嘴变成一个小圆。",
            "thinking": "思考中：眼睛缩小成两个小圆并一起移到画面一角，嘴变成斜的短横条。",
            "speaking": "说话中：嘴变成一个张开的白色圆角矩形。",
            "happy": "开心：眼睛变成向下的半圆（像倒过来的碗），嘴变成向上弯的粗弧。",
            "sad": "难过：眼睛变成上半部被切掉的半圆，嘴变成向下的弧。",
            "angry": "生气：眼睛变成两个向内倾斜的三角形，嘴是一条粗直线。",
            "surprised": "惊讶：眼睛变成两个大圆，嘴变成一个竖长的椭圆。",
            "sleepy": "困了：眼睛变成两条细横条，嘴是一个小圆。",
        },
        "variants": [
            "眼睛：两个大圆；鼻子小圆；嘴短横条。",
            "眼睛：两个圆角正方形；鼻子是小三角；嘴是一段细弧。",
            "眼睛：两个竖长椭圆，高光偏上；鼻子略大；嘴是一个小圆角矩形。",
        ],
    },
}
DEFAULT_STYLE = "kawaii"

# 三帧的默认分工：用户不写具体帧时，让动画天然有"眨眼/变化"
_DEFAULT_FRAME_PLAN = "第一帧：睁眼的基础表情；第二帧：眨眼（眼睛变成向下弯的弧线），其余不变；第三帧：表情更强烈的版本。"


def list_styles() -> list[dict[str, str]]:
    return [{"id": key, "title": val["title"]} for key, val in CARTOON_STYLES.items()]


def _resolve_model(model: str | None = None) -> str:
    return (
        str(model or os.environ.get("ARK_IMAGE_GEN_MODEL") or DEFAULT_IMAGE_GEN_MODEL).strip()
        or DEFAULT_IMAGE_GEN_MODEL
    )


def _resolve_image_api_key(api_key: str | None = None) -> str:
    """显式 Key → 生图自己的 ARK_IMAGE_GEN_API_KEY → 复用 LLM_API_KEY（历史行为）。"""
    key = str(api_key or "").strip() or str(os.environ.get("ARK_IMAGE_GEN_API_KEY") or "").strip()
    if key:
        return key
    try:
        return _resolve_api_key(None)
    except ValueError as exc:
        raise ValueError("生图 API Key 未配置：在表情页「生图 API 配置」填写火山方舟 Key，或在「模型配置」页填写 LLM_API_KEY。") from exc


def _resolve_images_url(images_url: str | None = None) -> str:
    return str(images_url or os.environ.get("ARK_IMAGE_GEN_URL") or "").strip() or ARK_IMAGES_URL


def generation_size_for_model(model: str) -> str:
    """每个模型用它允许的最小尺寸（反正最后都缩到 240×240）。"""
    return SEEDREAM5_MIN_SIZE if "seedream-5" in str(model or "").lower() else GENERATION_SIZE


def build_cartoon_prompt(
    style: str,
    description: str,
    *,
    frames: int = FRAME_COUNT,
    state: str | None = None,
    variant: int | None = None,
) -> str:
    """拼 prompt：硬规则 → 画法 → （预览变体）→ 当前状态的演法 → 用户描述 → 帧分工。

    ``state`` 只取该状态那一条演法写进去（把所有情绪都列出来会被画成多张脸）；
    ``variant`` 0/1/2 给"3 张预览"各自一个不同的五官构造，让三张彼此不同、也不和默认套图撞脸。
    """
    preset = CARTOON_STYLES.get(str(style or "").strip().lower()) or CARTOON_STYLES[DEFAULT_STYLE]
    emotion = str(description or "").strip() or "开心的微笑"
    n_rule = _SINGLE_RULE if frames <= 1 else _MULTI_RULE.format(n=frames)
    background = str(preset.get("background") or "纯黑色 #000000")
    parts = [_COMMON_RULES.format(n_rule=n_rule, background=background), "画法：" + preset["prompt"]]
    variants = preset.get("variants") or []
    if variant is not None and variants:
        parts.append("这一张的五官构造：" + variants[int(variant) % len(variants)])
    acting = (preset.get("acting") or {}).get(str(state or "").strip().lower())
    if acting:
        parts.append("这个状态怎么演：" + acting)
    parts.append(f"要表达的内容：{emotion}。")
    # 描述里已经写了逐帧分工（"第二帧…"）就不再追加默认的眨眼分工，否则两套分工互相冲突。
    if frames > 1 and "帧" not in emotion:
        parts.append(_DEFAULT_FRAME_PLAN)
    return "".join(parts)


BACKGROUND_BLACK_THRESHOLD = 12
CORNER_FILL_TOLERANCE = 48


def blacken_background(image_bytes: bytes, *, threshold: int = BACKGROUND_BLACK_THRESHOLD) -> bytes:
    """把生图的底色压成纯黑，两种常见跑偏都处理：

    1. 深灰底（Seedream 5.0 常画成 #1a1a1a、4.0 是 2~5 的噪声）：阈值以下的像素直接归零。
       阈值不能高——kawaii 的细眉毛只有 (10,12,25) 这种深色，40 会把眉毛一起抹掉。
    2. 白底/浅色底上画一个黑色圆角底板（4.0 偶发）：四个角是亮的，从四角做泛洪填充把整块
       连通的亮底填黑；五官都在中间、不和角连通，不会被误填。
    """
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        w, h = rgb.size
        for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            r, g, b = rgb.getpixel(corner)
            if (r + g + b) / 3 > 100:
                ImageDraw.floodfill(rgb, corner, (0, 0, 0), thresh=CORNER_FILL_TOLERANCE)
        limit = max(0, min(255, int(threshold)))
        rgb = rgb.point(lambda v: 0 if v < limit else v)
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
    return buf.getvalue()


def _post_ark_images(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with safe_provider_urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        preview = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Ark 生图请求失败 HTTP {exc.code}: {preview}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ark 生图请求失败: {exc.reason}") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise RuntimeError("Ark 生图返回不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Ark 生图返回格式异常")
    return data


def generate_cartoon_frames(
    description: str,
    *,
    style: str = DEFAULT_STYLE,
    frames: int = FRAME_COUNT,
    api_key: str | None = None,
    model: str | None = None,
    images_url: str | None = None,
    timeout: int = 300,
    transport: ArkTransport | None = None,
    reference_b64: str | None = None,
    state: str | None = None,
    variant: int | None = None,
) -> dict[str, Any]:
    """生成 ``frames`` 张连贯帧，返回 ``{frames:[b64 jpeg 240x240...], model, prompt}``。

    ``reference_b64``（JPEG base64）作为参考图随请求下发（Ark ``image`` 字段），用于"先出 3 张
    预览让用户挑一张，再以它为准生成整套"——整套四个状态才会是同一个角色、同一画风。
    """
    frames = max(1, min(FRAME_COUNT, int(frames or FRAME_COUNT)))
    resolved_key = _resolve_image_api_key(api_key)
    resolved_model = _resolve_model(model)
    prompt = build_cartoon_prompt(style, description, frames=frames, state=state, variant=variant)
    preset = CARTOON_STYLES.get(str(style or "").strip().lower()) or CARTOON_STYLES[DEFAULT_STYLE]
    style_black_bg = bool(preset.get("black_bg", True))
    reference = str(reference_b64 or "").strip()
    if reference:
        prompt = _REFERENCE_HINT + prompt
    payload: dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt,
        "size": generation_size_for_model(resolved_model),
        "response_format": "b64_json",
        "watermark": False,
    }
    if reference:
        payload["image"] = "data:image/jpeg;base64," + reference
    if frames > 1:
        payload["sequential_image_generation"] = "auto"
        payload["sequential_image_generation_options"] = {"max_images": frames}
    call = transport or _post_ark_images

    def _run(prompt_text: str) -> tuple[list[str], dict[str, Any]]:
        data = call(_resolve_images_url(images_url), dict(payload, prompt=prompt_text), resolved_key, timeout)
        items = data.get("data") if isinstance(data.get("data"), list) else []
        frames_b64: list[str] = []
        for item in items:
            b64 = item.get("b64_json") if isinstance(item, dict) else None
            if not b64:
                continue
            try:
                raw = base64.b64decode(b64)
                # 只有黑底风格才压黑（米白纸底/纯色底的风格压黑会把整张底色吃掉）
                if style_black_bg:
                    raw = blacken_background(raw)
                jpeg = _canonical_jpeg_bytes(raw, FRAME_W, FRAME_H)
            except (ValueError, TypeError):
                continue
            frames_b64.append(base64.b64encode(jpeg).decode("ascii"))
        return frames_b64, data

    out, data = _run(prompt)
    if frames > 1 and 0 < len(out) < frames:
        # 组图是 "auto"：模型偶尔自作主张把三帧拼进一张图、只回一张。再要一次并把
        # "分开输出"说重，通常第二次就分开了；仍不够就照单返回，页面按实际帧数显示。
        retry_out, retry_data = _run(prompt + _SPLIT_FRAMES_HINT.format(n=frames))
        if len(retry_out) > len(out):
            out, data = retry_out, retry_data
    if not out:
        raise RuntimeError("Ark 没有返回可用的图片，请换个描述再试")
    return {"frames": out[:frames], "model": resolved_model, "prompt": prompt, "usage": data.get("usage")}


def canonicalize_upload_frame(image_bytes: bytes, declared_mime: str | None = None) -> str:
    """上传图片 → 240×240 JPEG base64（居中拉伸到方形）。"""
    jpeg = _canonical_jpeg_bytes(image_bytes, FRAME_W, FRAME_H, declared_mime=declared_mime)
    return base64.b64encode(jpeg).decode("ascii")


def frames_to_scene(
    assets_b64: list[str],
    *,
    name: str,
    title: str,
    frame_ms: int = 450,
) -> dict[str, Any]:
    """位图帧 → 表情库场景：每帧一个 image 图元（居中 240×240），场景级 assets。"""
    x = max(0, (284 - FRAME_W) // 2)
    frames = [
        {
            "ms": int(frame_ms),
            "elements": {
                "eye_l": [], "eye_r": [], "nose": [], "mouth": [],
                "extra": [{"shape": "image", "asset": i, "x": x, "y": 0, "w": FRAME_W, "h": FRAME_H}],
            },
        }
        for i in range(len(assets_b64))
    ]
    return {"name": name, "title": title, "frames": frames, "assets": list(assets_b64)}
