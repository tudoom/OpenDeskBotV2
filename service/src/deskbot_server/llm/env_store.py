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
from deskbot_server.safe_fetch import validate_provider_http_url

# Web 写通用 LLM_* 键与图片表情独立的 ARK_API_KEY。ARK_API_KEY 不纳入
# 配置 revision（实时服务不依赖它），留空/掩码时不会覆盖已有值。
LLM_ENV_KEYS = (*LLM_WRITABLE_ENV_KEYS, "ARK_API_KEY")

_SECRET_ENV_KEYS = frozenset({"LLM_API_KEY", "ARK_API_KEY"})

_ENV_KEY_BY_FIELD = {
    "api_key": "LLM_API_KEY",
    "ark_api_key": "ARK_API_KEY",
    "protocol": "LLM_PROTOCOL",
    "model_name": "LLM_MODEL",
    "base_url": "LLM_BASE_URL",
}


def save_llm_env(payload: dict[str, Any]) -> dict[str, Any]:
    """把 payload 里的 LLM 字段写入 .env（并更新 os.environ）。

    api_key 为空或看起来是掩码时不覆盖已有值；其它字段按需写入。
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
    if not updates:
        return get_llm_config_status()

    # The state lock is the outer transaction lock shared with the realtime
    # watcher.  It prevents the watcher from acknowledging file content before
    # the corresponding desired revision has been recorded.
    with file_lock(LLM_CONFIG_STATE_FILE):
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
