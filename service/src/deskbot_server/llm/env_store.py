"""读写 .env 中的大模型配置。

复用 deskbot_server.env 的通用 .env 读写（read_env_file / update_env_keys），
避免重复实现；写入后同步更新 os.environ，使运行中的进程立即生效。
"""

from __future__ import annotations

from typing import Any

from deskbot_server.atomic_store import file_lock
from deskbot_server.env import looks_masked as _looks_masked
from deskbot_server.env import read_env_file, update_env_keys
from deskbot_server.llm.config_state import (
    LLM_CONFIG_STATE_FILE,
    LLM_WRITABLE_ENV_KEYS,
    get_llm_config_status,
    read_llm_env_values,
    record_desired_llm_config_locked,
)
from deskbot_server.llm.provider_keys import (
    key_env_name,
    provider_label,
    same_provider,
)
from deskbot_server.safe_fetch import validate_provider_http_url

# Web 只写通用 LLM_* 键；凭证统一为 LLM_API_KEY（图片生成同用，无独立 Key）。
LLM_ENV_KEYS = LLM_WRITABLE_ENV_KEYS

_SECRET_ENV_KEYS = frozenset({"LLM_API_KEY"})

_ENV_KEY_BY_FIELD = {
    "api_key": "LLM_API_KEY",
    "protocol": "LLM_PROTOCOL",
    "model_name": "LLM_MODEL",
    "base_url": "LLM_BASE_URL",
}


def _provider_key_updates(
    env: dict[str, str],
    updates: dict[str, str],
    current_base_url: str | None,
) -> dict[str, str]:
    """按提供方分别保存 Key，并在切换提供方时把对应的 Key 带回 LLM_API_KEY（就地改 ``updates``）。

    - 现有的 LLM_API_KEY 若还没记在它所属提供方名下，先记一份（老安装只有一把 Key 的迁移）；
    - 这次填了 Key：记到目标提供方名下；
    - 这次没填 Key 且换了提供方：有存过就带出，没存过就报错，不再沿用别家的 Key。
    """
    old_base = str(current_base_url or env.get("LLM_BASE_URL") or "").strip()
    new_base = str(updates.get("LLM_BASE_URL", old_base) or "").strip() if "LLM_BASE_URL" in updates else old_base
    out: dict[str, str] = {}
    current_key = str(env.get("LLM_API_KEY") or "").strip()
    supplied = str(updates.get("LLM_API_KEY") or "").strip()
    switching = bool(new_base and old_base and not same_provider(new_base, old_base))
    slot_old = key_env_name(old_base)
    # 只在这次真的动了 Key 或换了提供方时才把现有 Key 记到它的提供方名下：
    # 原样重存（页面回填后直接保存）必须让 .env 一个字节都不变。
    if current_key and slot_old and (supplied or switching) and not str(env.get(slot_old) or "").strip():
        out[slot_old] = current_key
    slot_new = key_env_name(new_base)
    if supplied:
        if slot_new:
            out[slot_new] = supplied
        return out
    if switching:
        stored = str(env.get(slot_new) or out.get(slot_new) or "").strip() if slot_new else ""
        if not stored:
            raise ValueError(f"还没有保存过{provider_label(new_base)}的 API Key，请填写后再保存")
        updates["LLM_API_KEY"] = stored
    return out


def save_llm_env(payload: dict[str, Any], *, current_base_url: str | None = None) -> dict[str, Any]:
    """把 payload 里的 LLM 字段写入 .env（并更新 os.environ）。

    api_key 为空或看起来是掩码时不覆盖已有值；其它字段按需写入。``current_base_url`` 是改动前
    实际生效的接入点（.env 里可能留空走协议默认），用来判断有没有换提供方。
    """
    updates: dict[str, str] = {}
    for field, env_key in _ENV_KEY_BY_FIELD.items():
        if field not in payload:
            continue
        val = str(payload.get(field) or "").strip()
        if env_key in _SECRET_ENV_KEYS and _looks_masked(val):
            continue  # 保留已保存的 Key
        if env_key in {"LLM_PROTOCOL", "LLM_MODEL"} and not val:
            continue
        if env_key == "LLM_BASE_URL" and val:
            validate_provider_http_url(val)
        updates[env_key] = val
    provider_updates = _provider_key_updates(read_env_file(), updates, current_base_url)
    if not updates and not provider_updates:
        return get_llm_config_status()

    # The state lock is the outer transaction lock shared with the realtime
    # watcher.  It prevents the watcher from acknowledging file content before
    # the corresponding desired revision has been recorded.
    with file_lock(LLM_CONFIG_STATE_FILE):
        if provider_updates:
            update_env_keys(
                provider_updates,
                keys=provider_updates.keys(),
                comment="# 各提供方的大模型 Key（切换提供方时自动带出，由控制台维护）",
            )
        update_env_keys(
            updates,
            keys=LLM_ENV_KEYS,
            comment="# 大模型 LLM",
            write_empty_keys={"LLM_BASE_URL"},
        )
        return record_desired_llm_config_locked(read_llm_env_values())


def read_llm_env() -> dict[str, str]:
    """读取 .env 中当前的 LLM 配置（原始值，供内部使用）。"""
    env = read_env_file()
    return {k: env.get(k, "") for k in LLM_ENV_KEYS}
