"""读写 .env 中的卡通表情生图（火山方舟 Seedream）配置。

只是配置入口：生图逻辑本身在 ``ark_image_gen``，这里负责三件事——
读出当前生效的 Key 来源 / 接口地址 / 模型给页面显示，把页面提交的值写回 .env
（复用 ``deskbot_server.env`` 的读写器，写入后同步 ``os.environ`` 立即生效），
以及在写入前校验接口地址（和 LLM Base URL 一样必须是公网 HTTPS 供应商地址）。

Key 留空表示复用大模型的 ``LLM_API_KEY``（历史行为不变）；接口地址、模型留空用默认值。
"""

from __future__ import annotations

import os
from typing import Any

from deskbot_server.ark_image_gen import (
    ARK_IMAGES_URL,
    DEFAULT_IMAGE_GEN_MODEL,
    IMAGE_GEN_API_KEY_ENV,
    IMAGE_GEN_CONSOLE_URL,
    IMAGE_GEN_MODEL_CHOICES,
    IMAGE_GEN_MODEL_ENV,
    IMAGE_GEN_URL_ENV,
)
from deskbot_server.env import load_dotenv, looks_masked, update_env_keys
from deskbot_server.safe_fetch import validate_provider_http_url

IMAGE_GEN_ENV_KEYS = (IMAGE_GEN_API_KEY_ENV, IMAGE_GEN_URL_ENV, IMAGE_GEN_MODEL_ENV)
_ENV_COMMENT = "# 卡通表情生图（火山方舟 Seedream）"


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def image_gen_config_status() -> dict[str, Any]:
    """页面展示用：Key 来源、生效的接口地址与模型（不返回 Key 本身）。"""
    load_dotenv()
    own_key = _env(IMAGE_GEN_API_KEY_ENV)
    llm_key = _env("LLM_API_KEY")
    url = _env(IMAGE_GEN_URL_ENV)
    model = _env(IMAGE_GEN_MODEL_ENV)
    if own_key:
        source = "own"
    elif llm_key:
        source = "llm"
    else:
        source = "none"
    models = [dict(choice) for choice in IMAGE_GEN_MODEL_CHOICES]
    if model and all(choice["id"] != model for choice in models):
        # .env 里手填了不在清单里的模型：照样列出来，下拉才能显示当前值。
        models.append({"id": model, "title": f"{model}（.env 手动指定）"})
    return {
        "api_key_set": source != "none",
        "api_key_source": source,
        "url": url or ARK_IMAGES_URL,
        "url_overridden": bool(url),
        "default_url": ARK_IMAGES_URL,
        "model": model or DEFAULT_IMAGE_GEN_MODEL,
        "model_overridden": bool(model),
        "default_model": DEFAULT_IMAGE_GEN_MODEL,
        "models": models,
        "console_url": IMAGE_GEN_CONSOLE_URL,
    }


def save_image_gen_env(payload: dict[str, Any]) -> dict[str, Any]:
    """把页面提交的 api_key / url / model 写入 .env，返回写入后的状态。

    - ``api_key`` 为空或是掩码时保留原值；``clear_api_key`` 为真时清空（改回复用 LLM Key）。
    - ``url`` / ``model`` 只要字段出现就按提交值写入，空值显式写成 ``KEY=`` 表示用默认。
    - ``url`` 非空时先过供应商地址校验，非法地址抛 ``ValueError``。
    """
    updates: dict[str, str] = {}
    explicit_empty: set[str] = set()
    if payload.get("clear_api_key"):
        updates[IMAGE_GEN_API_KEY_ENV] = ""
        explicit_empty.add(IMAGE_GEN_API_KEY_ENV)
    elif "api_key" in payload:
        key = str(payload.get("api_key") or "").strip()
        if key and not looks_masked(key):
            updates[IMAGE_GEN_API_KEY_ENV] = key
    if "url" in payload:
        url = str(payload.get("url") or "").strip()
        if url:
            validate_provider_http_url(url)
        updates[IMAGE_GEN_URL_ENV] = url
        if not url:
            explicit_empty.add(IMAGE_GEN_URL_ENV)
    if "model" in payload:
        model = str(payload.get("model") or "").strip()
        updates[IMAGE_GEN_MODEL_ENV] = model
        if not model:
            explicit_empty.add(IMAGE_GEN_MODEL_ENV)
    if updates:
        update_env_keys(
            updates,
            keys=IMAGE_GEN_ENV_KEYS,
            comment=_ENV_COMMENT,
            write_empty_keys=explicit_empty,
        )
    return image_gen_config_status()


__all__ = ["IMAGE_GEN_ENV_KEYS", "image_gen_config_status", "save_image_gen_env"]
