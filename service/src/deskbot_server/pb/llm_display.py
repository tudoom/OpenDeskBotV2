"""LLM ``images`` → pb ``anim`` + ``assets``。"""

from __future__ import annotations

import base64
import binascii
import io
import re
import warnings
from typing import TYPE_CHECKING, Any

from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH

# PIL 惰性导入（各使用函数内）：保持 deskbot_server.main 的导入闭包不含
# Pillow，Core 冷启动不为图片下发链路买单。
if TYPE_CHECKING:
    from PIL import Image

MAX_LLM_DISPLAY_IMAGE_BYTES = 2 * 1024 * 1024
MAX_LLM_DISPLAY_IMAGE_PIXELS = 4096 * 4096
MAX_LLM_DISPLAY_IMAGES = 4
MAX_LLM_DISPLAY_TOTAL_ASSET_BYTES = 2 * 1024 * 1024
# A silent image still needs a real PB timeline row.  Three seconds is long
# enough to inspect a requested photo while keeping the display lease bounded.
LLM_DISPLAY_IMAGE_HOLD_MS = 3000

_DATA_URL_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9+.-]+);base64,",
    re.I,
)
_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}


def _normalize_image_mime(value: str | None) -> str:
    mime = str(value or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(mime, mime)


def _max_base64_chars() -> int:
    return ((MAX_LLM_DISPLAY_IMAGE_BYTES + 2) // 3) * 4


def _decode_base64_image(value: Any) -> tuple[bytes, str | None]:
    if not isinstance(value, str):
        raise ValueError("图片 base64 必须是字符串")
    # Check before ``strip`` so an attacker cannot force a second unbounded
    # allocation merely by surrounding the payload with whitespace.
    if len(value) > _max_base64_chars() + 96:
        raise ValueError("图片 base64 超过大小上限")
    encoded = value.strip()
    if not encoded:
        raise ValueError("图片 base64 为空")

    declared_mime: str | None = None
    match = _DATA_URL_RE.match(encoded)
    if match:
        declared_mime = _normalize_image_mime(match.group(1))
        encoded = encoded[match.end() :]
    elif encoded.lower().startswith("data:"):
        raise ValueError("图片 data URL 格式无效")
    if not encoded or len(encoded) > _max_base64_chars():
        raise ValueError("图片 base64 超过大小上限")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("图片 base64 非法") from exc
    if not decoded:
        raise ValueError("图片为空")
    if len(decoded) > MAX_LLM_DISPLAY_IMAGE_BYTES:
        raise ValueError("图片超过解码字节上限")
    return decoded, declared_mime


def _open_verified_image(data: bytes, declared_mime: str | None = None) -> Image.Image:
    """Fully verify and decode one bounded raster image."""

    from PIL import Image, ImageOps, UnidentifiedImageError  # type: ignore

    if not data or len(data) > MAX_LLM_DISPLAY_IMAGE_BYTES:
        raise ValueError("图片超过解码字节上限")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                image_format = str(probe.format or "").upper()
                actual_mime = _FORMAT_MIME_TYPES.get(image_format)
                if actual_mime is None:
                    raise ValueError("图片格式不受支持")
                if declared_mime and declared_mime != actual_mime:
                    raise ValueError("图片声明格式与实际解码格式不一致")
                width, height = (int(value) for value in probe.size)
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_LLM_DISPLAY_IMAGE_PIXELS
                ):
                    raise ValueError("图片超过解压像素上限")
                # ``verify`` checks container integrity without trusting a
                # filename, Content-Type, or magic prefix alone.
                probe.verify()

            with Image.open(io.BytesIO(data)) as decoded:
                if str(decoded.format or "").upper() != image_format:
                    raise ValueError("图片格式验证不一致")
                if getattr(decoded, "is_animated", False):
                    decoded.seek(0)
                # Pillow is lazy; ``load`` rejects truncated pixel data.
                decoded.load()
                return ImageOps.exif_transpose(decoded).copy()
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


def _to_rgb(image: Image.Image) -> Image.Image:
    from PIL import Image  # type: ignore

    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _canonical_jpeg_bytes(
    image_bytes: bytes,
    w: int,
    h: int,
    *,
    declared_mime: str | None = None,
) -> bytes:
    from PIL import Image  # type: ignore

    image = _open_verified_image(image_bytes, declared_mime)
    try:
        rgb = _to_rgb(image)
        if rgb.size != (w, h):
            rgb = rgb.resize((w, h), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        # Always encode, even for a same-sized JPEG. This guarantees every PB
        # asset is a fully decoded, canonical JPEG and prevents PNG/WebP/GIF
        # bytes from being mislabeled as ``fmt: jpeg`` by the wire layer.
        rgb.save(output, format="JPEG", quality=85, optimize=True)
        result = output.getvalue()
    finally:
        image.close()
    if not result or len(result) > MAX_LLM_DISPLAY_TOTAL_ASSET_BYTES:
        raise ValueError("JPEG 资产超过总字节上限")
    return result


def jpeg_blob_dimensions(jpeg_bytes: bytes) -> tuple[int, int]:
    """读取 JPEG 像素尺寸；失败时回退 LCD 逻辑分辨率。"""
    if not jpeg_bytes or len(jpeg_bytes) > MAX_LLM_DISPLAY_IMAGE_BYTES:
        return FACE_LCD_WIDTH, FACE_LCD_HEIGHT
    from PIL import Image  # type: ignore

    try:
        with Image.open(io.BytesIO(jpeg_bytes)) as im:
            if str(im.format or "").upper() != "JPEG":
                return FACE_LCD_WIDTH, FACE_LCD_HEIGHT
            w, h = im.size
            if w > 0 and h > 0 and int(w) * int(h) <= MAX_LLM_DISPLAY_IMAGE_PIXELS:
                im.load()
                return int(w), int(h)
    except Exception:
        pass
    return FACE_LCD_WIDTH, FACE_LCD_HEIGHT


def resize_jpeg_bytes_to(jpeg_bytes: bytes, w: int, h: int) -> bytes:
    """将 JPEG 缩放到目标 (w,h) 再编码，使固件解码尺寸与 ``shape:image`` 一致。"""
    w = max(1, int(w))
    h = max(1, int(h))
    return _canonical_jpeg_bytes(jpeg_bytes, w, h)


def decode_llm_image_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    b64 = raw.get("b64") or raw.get("base64") or raw.get("data")
    if b64 is None:
        return None
    try:
        x = int(raw.get("x", 0))
        y = int(raw.get("y", 0))
        w = int(raw.get("w", FACE_LCD_WIDTH))
        h = int(raw.get("h", FACE_LCD_HEIGHT))
    except (TypeError, ValueError):
        x, y, w, h = 0, 0, FACE_LCD_WIDTH, FACE_LCD_HEIGHT
    w = max(1, min(FACE_LCD_WIDTH, w))
    h = max(1, min(FACE_LCD_HEIGHT, h))
    x = max(0, min(FACE_LCD_WIDTH - 1, x))
    y = max(0, min(FACE_LCD_HEIGHT - 1, y))
    # 相机常为 320×240，屏为 284×240；下发前缩放到绘制区域，避免固件 stride 不一致花屏
    try:
        data, declared_mime = _decode_base64_image(b64)
        data = _canonical_jpeg_bytes(
            data,
            w,
            h,
            declared_mime=declared_mime,
        )
    except (TypeError, ValueError):
        return None
    return {"bytes": data, "x": x, "y": y, "w": w, "h": h}


def parse_llm_images(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return out
    total_asset_bytes = 0
    for item in raw[:MAX_LLM_DISPLAY_IMAGES]:
        dec = decode_llm_image_item(item)
        if dec:
            asset_size = len(dec["bytes"])
            if total_asset_bytes + asset_size > MAX_LLM_DISPLAY_TOTAL_ASSET_BYTES:
                break
            out.append(dec)
            total_asset_bytes += asset_size
    return out


def _attach_images_to_anim_item(
    item: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    asset_base: int,
) -> None:
    if not images:
        return
    els = item.setdefault("elements", {})
    if not isinstance(els, dict):
        return
    layer = els.setdefault("extra", [])
    if not isinstance(layer, list):
        layer = []
        els["extra"] = layer
    for i, img in enumerate(images):
        layer.append(
            {
                "shape": "image",
                "asset": asset_base + i,
                "x": img["x"],
                "y": img["y"],
                "w": img["w"],
                "h": img["h"],
            }
        )


def apply_llm_display_to_rows(
    rows: list[dict[str, Any]],
    *,
    images: list[dict[str, Any]] | None = None,
) -> None:
    """将 LLM 图片叠加到 pb 行（就地修改）。"""
    from PIL import Image  # type: ignore

    imgs: list[dict[str, Any]] = []
    total_asset_bytes = 0
    for image in list(images or [])[:MAX_LLM_DISPLAY_IMAGES]:
        if not isinstance(image, dict):
            continue
        data = image.get("bytes")
        if not isinstance(data, bytes) or not data.startswith(b"\xff\xd8\xff"):
            continue
        if len(data) > MAX_LLM_DISPLAY_IMAGE_BYTES:
            continue
        if jpeg_blob_dimensions(data) == (FACE_LCD_WIDTH, FACE_LCD_HEIGHT):
            # The fallback and a legitimate full-screen asset are ambiguous;
            # validate the container explicitly before accepting either.
            try:
                with Image.open(io.BytesIO(data)) as decoded:
                    if str(decoded.format or "").upper() != "JPEG":
                        continue
                    decoded.load()
            except Exception:
                continue
        if total_asset_bytes + len(data) > MAX_LLM_DISPLAY_TOTAL_ASSET_BYTES:
            break
        imgs.append(image)
        total_asset_bytes += len(data)
    if not rows:
        if not imgs:
            return
        # Image-only replies used to produce no rows at all because there was
        # no PCM, servo or model-authored animation to attach the asset to.
        # Create one explicit display row; servo-only requests remain untouched
        # because this path is entered only when a verified image exists.
        rows.append({"chunk_ms": LLM_DISPLAY_IMAGE_HOLD_MS})
    asset_bytes = [img["bytes"] for img in imgs]

    for row in rows:
        anim = row.get("anim")
        if not isinstance(anim, list) or not anim:
            if not imgs:
                continue
            # A real image is a display payload in its own right.  Create a
            # frame for it explicitly instead of relying on the historical
            # empty/default-mouth animation that motor-only PBs used to gain.
            anim = [
                {
                    "elements": {"extra": []},
                    "ms": max(1, int(row.get("chunk_ms") or 1)),
                }
            ]
            row["anim"] = anim
        if not isinstance(anim, list):
            continue
        for item in anim:
            if not isinstance(item, dict):
                continue
            _attach_images_to_anim_item(item, imgs, asset_base=0)

        if asset_bytes:
            row["_assets"] = list(asset_bytes)
