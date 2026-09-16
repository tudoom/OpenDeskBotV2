"""大模型 Key 按提供方分别保存（2026-09-14 用户要求）。

``LLM_API_KEY`` 是当前生效的那把；每家提供方的 Key 另存一份在 ``LLM_API_KEY_<提供方>``
（DEEPSEEK / DOUBAO / MIMO，其它接入点按主机名记为 ``HOST_<主机名>``）。切换提供方时
自动把对应的那把带回 ``LLM_API_KEY``；没保存过的提供方必须先填 Key，不再沿用别家的 Key
（否则就是 401「Your api key is invalid」）。提供方按 Base URL 的主机识别。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

PREFIX = "LLM_API_KEY_"
_KNOWN_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("deepseek.com", "DEEPSEEK", "DeepSeek"),
    ("volces.com", "DOUBAO", "豆包（火山方舟）"),
    ("xiaomimimo.com", "MIMO", "Xiaomi MiMo"),
)


def _host(base_url: str | None) -> str:
    return (urlsplit(str(base_url or "").strip()).hostname or "").lower()


def provider_id(base_url: str | None) -> str:
    """Base URL → 提供方标识；没有主机名时为空串（没有专属槽位）。"""
    host = _host(base_url)
    if not host:
        return ""
    for suffix, ident, _label in _KNOWN_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return ident
    return "HOST_" + re.sub(r"[^A-Z0-9]+", "_", host.upper()).strip("_")


def provider_label(base_url: str | None) -> str:
    host = _host(base_url)
    for suffix, _ident, label in _KNOWN_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return label
    return host or "该提供方"


def key_env_name(base_url: str | None) -> str:
    ident = provider_id(base_url)
    return PREFIX + ident if ident else ""


def same_provider(a: str | None, b: str | None) -> bool:
    return bool(provider_id(a)) and provider_id(a) == provider_id(b)


def stored_key_for(env: dict[str, str], base_url: str | None) -> str:
    name = key_env_name(base_url)
    return str(env.get(name) or "").strip() if name else ""


def mask_secret(value: str | None) -> str:
    """给控制台展示用的掩码：保留前 3 位和后 4 位（短 Key 只留后 2 位），中间用 … 代替。

    后端保存接口遇到含 * / • / … 的值一律当作"沿用旧值"，所以即使用户把掩码原样提交也不会覆盖真 Key。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "•" * max(1, len(raw) - 2) + raw[-2:]
    return raw[:3] + "…" + raw[-4:]


def saved_key_masks(env: dict[str, str]) -> dict[str, str]:
    """{提供方标识: 掩码后的 Key}——页面切换提供方时在输入框里展示"已保存的是哪一个"。"""
    return {
        name[len(PREFIX):]: mask_secret(value)
        for name, value in env.items()
        if name.startswith(PREFIX) and str(value or "").strip()
    }


def saved_provider_ids(env: dict[str, str]) -> list[str]:
    """已经保存过 Key 的提供方标识（供控制台提示「留空沿用」还是「请填写」）。"""
    return sorted(
        name[len(PREFIX):]
        for name, value in env.items()
        if name.startswith(PREFIX) and str(value or "").strip()
    )
