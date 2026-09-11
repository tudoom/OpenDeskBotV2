"""Transient camera images for OpenAI-compatible multimodal requests.

Camera frames are deliberately represented as bytes until the provider request
is built.  The data URL must never be copied into a textual tool result,
conversation history, a pipeline event, or a durable tool ledger.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import warnings
from typing import Any

# PIL 惰性导入（见 _validate_jpeg_impl）：保持 deskbot_server.main 的导入闭包
# 不含 Pillow，Core 冷启动不为视觉栈买单。

JPEG_MIME_TYPE = "image/jpeg"
MAX_VISION_IMAGE_BYTES = 2 * 1024 * 1024
MAX_VISION_IMAGE_PIXELS = 4096 * 4096
TRANSIENT_VISION_IMAGE_KEY = "_llm_vision_image"


class VisionImageValidationError(ValueError):
    """A camera image is unsafe or malformed and must not reach a provider."""


class LlmVisionUnsupportedError(RuntimeError):
    """The configured provider/model cannot accept the requested image."""


class ValidatedJpeg:
    """Immutable proof that a JPEG passed byte, MIME, pixel, and decode checks.

    Instances are intentionally created only by :func:`validate_jpeg`. Trusted
    camera internals can pass this proof between the frame store and broker
    without decoding the same image repeatedly.
    """

    __slots__ = ("_data", "_height", "_pixel_count", "_width")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ValidatedJpeg instances must be created by validate_jpeg()")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ValidatedJpeg is immutable")

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def pixel_count(self) -> int:
        return self._pixel_count

    def __bytes__(self) -> bytes:
        return self._data

    def __len__(self) -> int:
        return len(self._data)


def _new_validated_jpeg(
    data: bytes,
    *,
    width: int,
    height: int,
) -> ValidatedJpeg:
    validated = object.__new__(ValidatedJpeg)
    object.__setattr__(validated, "_data", data)
    object.__setattr__(validated, "_width", width)
    object.__setattr__(validated, "_height", height)
    object.__setattr__(validated, "_pixel_count", width * height)
    return validated


def _validate_jpeg_impl(
    data: bytes | bytearray | memoryview,
    *,
    want_rgb: bool,
) -> tuple[ValidatedJpeg, Any]:
    # 惰性导入：相机首帧校验经 asyncio.to_thread 进入本函数，Pillow 的首次
    # 重导入发生在 worker 线程；模块导入链保持无 PIL。
    from PIL import Image, UnidentifiedImageError  # type: ignore

    try:
        image = bytes(data)
    except (TypeError, ValueError) as exc:
        raise VisionImageValidationError("相机帧不是有效的二进制 JPEG") from exc
    if not image:
        raise VisionImageValidationError("相机帧为空")
    if len(image) > MAX_VISION_IMAGE_BYTES:
        raise VisionImageValidationError(
            "相机 JPEG 超过视觉输入上限 "
            f"({len(image)} > {MAX_VISION_IMAGE_BYTES} bytes)"
        )
    if len(image) < 4 or not image.startswith(b"\xff\xd8\xff"):
        raise VisionImageValidationError(
            "相机帧 MIME 校验失败：仅允许 image/jpeg"
        )
    rgb: Any = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image)) as decoded:
                if str(decoded.format or "").upper() != "JPEG":
                    raise VisionImageValidationError(
                        "相机帧 MIME 校验失败：仅允许 image/jpeg"
                    )
                width, height = (int(value) for value in decoded.size)
                pixel_count = width * height
                if (
                    width <= 0
                    or height <= 0
                    or pixel_count > MAX_VISION_IMAGE_PIXELS
                ):
                    raise VisionImageValidationError(
                        "Camera JPEG exceeds the decoded pixel limit "
                        f"({width}x{height} > {MAX_VISION_IMAGE_PIXELS} pixels)"
                    )
                if want_rgb:
                    # ``convert`` forces the same full decode ``load`` would
                    # perform, so the truncated-scan rejection is preserved
                    # while the pixels are kept for the caller.
                    import numpy as np

                    rgb = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
                else:
                    # ``Image.open`` is lazy.  ``load`` is required to reject
                    # truncated scan data before the frame reaches any cache
                    # or provider request.
                    decoded.load()
    except VisionImageValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise VisionImageValidationError("相机 JPEG 无法完整解码") from exc
    return _new_validated_jpeg(image, width=width, height=height), rgb


def validate_jpeg(
    data: bytes | bytearray | memoryview,
) -> ValidatedJpeg:
    """Fully decode one bounded JPEG and return an immutable validation proof."""

    validated, _rgb = _validate_jpeg_impl(data, want_rgb=False)
    return validated


def validate_jpeg_with_rgb(
    data: bytes | bytearray | memoryview,
) -> tuple[ValidatedJpeg, Any]:
    """Validate one JPEG and also return its decoded RGB pixel array.

    Identical checks to :func:`validate_jpeg`, but the single mandatory decode
    is reused to hand trusted camera internals a ready ``numpy`` RGB array so
    face inference does not decode the same frame a second time.  The decoded
    array is deliberately not stored on :class:`ValidatedJpeg`: the proof
    object is cached (frame store, broker snapshots) and must stay small.
    """

    validated, rgb = _validate_jpeg_impl(data, want_rgb=True)
    return validated, rgb


def validate_jpeg_bytes(data: bytes | bytearray | memoryview) -> bytes:
    """Return immutable JPEG bytes after complete bounded validation."""

    return validate_jpeg(data).data


def make_transient_vision_image(
    jpeg_bytes: bytes | ValidatedJpeg,
) -> dict[str, Any]:
    """Build the private in-memory representation used by the tool loop."""

    validated = (
        jpeg_bytes
        if isinstance(jpeg_bytes, ValidatedJpeg)
        else validate_jpeg(jpeg_bytes)
    )
    return {
        "mime_type": JPEG_MIME_TYPE,
        "bytes": validated.data,
    }


def _data_url_from_image(image: dict[str, Any]) -> str:
    mime_type = str(image.get("mime_type") or "").strip().lower()
    if mime_type != JPEG_MIME_TYPE:
        raise VisionImageValidationError(
            f"视觉输入 MIME 不受支持：{mime_type or 'missing'}；仅允许 image/jpeg"
        )
    jpeg = validate_jpeg_bytes(image.get("bytes") or b"")
    encoded = base64.b64encode(jpeg).decode("ascii")
    return f"data:{JPEG_MIME_TYPE};base64,{encoded}"


def build_openai_vision_content(
    text: str,
    image: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build canonical Chat Completions content blocks for one JPEG."""

    return [
        {"type": "text", "text": str(text or "")},
        {
            "type": "image_url",
            "image_url": {"url": _data_url_from_image(image)},
        },
    ]


def _image_url_from_block(block: dict[str, Any]) -> str | None:
    block_type = str(block.get("type") or "").strip()
    if block_type == "input_image":
        value = block.get("image_url")
        return value if isinstance(value, str) else None
    if block_type != "image_url":
        return None
    value = block.get("image_url")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        url = value.get("url")
        return url if isinstance(url, str) else None
    return None


def validate_image_data_url(url: str) -> str:
    """Validate and canonicalize an inline JPEG data URL."""

    prefix = f"data:{JPEG_MIME_TYPE};base64,"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise VisionImageValidationError(
            "视觉输入仅允许内联 data:image/jpeg;base64 图片"
        )
    encoded = url[len(prefix) :]
    if not encoded:
        raise VisionImageValidationError("视觉输入 JPEG 为空")
    # Bound the encoded form before allocating decoded bytes.
    max_encoded = ((MAX_VISION_IMAGE_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded:
        raise VisionImageValidationError(
            f"视觉输入超过 {MAX_VISION_IMAGE_BYTES} bytes 上限"
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise VisionImageValidationError("视觉输入 base64 非法") from exc
    validate_jpeg_bytes(decoded)
    return url


def messages_contain_images(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and _image_url_from_block(block) is not None:
                return True
    return False


def to_openai_chat_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize internal blocks to OpenAI Chat Completions format."""

    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        content = message.get("content")
        if not isinstance(content, list):
            out.append({"role": role, "content": content})
            continue
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type in {"text", "input_text", "output_text"}:
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text:
                    parts.append({"type": "text", "text": text})
                continue
            image_url = _image_url_from_block(block)
            if image_url is not None:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": validate_image_data_url(image_url)},
                    }
                )
        if parts:
            out.append({"role": role, "content": parts})
    return out


def to_ark_responses_input(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize internal blocks to Ark/OpenAI Responses ``input`` format."""

    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        content = message.get("content")
        if not isinstance(content, list):
            if isinstance(content, str):
                text = content.strip()
            elif content is None:
                text = ""
            else:
                try:
                    text = json.dumps(content, ensure_ascii=False).strip()
                except (TypeError, ValueError):
                    text = str(content).strip()
            if text:
                out.append(
                    {
                        "role": role,
                        "content": [{"type": "input_text", "text": text}],
                    }
                )
            continue
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type in {"text", "input_text", "output_text"}:
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text:
                    parts.append({"type": "input_text", "text": text})
                continue
            image_url = _image_url_from_block(block)
            if image_url is not None:
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": validate_image_data_url(image_url),
                    }
                )
        if parts:
            out.append({"role": role, "content": parts})
    return out


def redact_images_from_messages(messages: list[dict[str, Any]]) -> None:
    """Remove image blocks in place immediately after their provider call."""

    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if _image_url_from_block(block) is not None:
                continue
            if str(block.get("type") or "") in {"text", "input_text", "output_text"}:
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        message["content"] = "\n".join(text_parts)


__all__ = [
    "JPEG_MIME_TYPE",
    "LlmVisionUnsupportedError",
    "MAX_VISION_IMAGE_BYTES",
    "MAX_VISION_IMAGE_PIXELS",
    "TRANSIENT_VISION_IMAGE_KEY",
    "ValidatedJpeg",
    "VisionImageValidationError",
    "build_openai_vision_content",
    "make_transient_vision_image",
    "messages_contain_images",
    "redact_images_from_messages",
    "to_ark_responses_input",
    "to_openai_chat_messages",
    "validate_image_data_url",
    "validate_jpeg",
    "validate_jpeg_bytes",
    "validate_jpeg_with_rgb",
]
