"""配置加载：YAML + LLM system prompt 外置文件。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

import yaml

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.paths import DEFAULT_CONFIG_PATH


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = Path(path or DEFAULT_CONFIG_PATH).resolve()
    with config_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    _resolve_llm_system_prompt(cfg, config_path.parent)
    return cfg


def resolve_llm_system_prompt(llm_cfg: dict[str, Any], config_dir: str | Path) -> str:
    """从 llm 配置段解析 system prompt（文件优先于内联）。"""
    if not isinstance(llm_cfg, dict):
        return ""
    file_ref = llm_cfg.get("system_prompt_file")
    if file_ref:
        p = Path(file_ref)
        if not p.is_absolute():
            p = Path(config_dir) / p
        return p.read_text(encoding="utf-8").strip()
    raw = llm_cfg.get("system_prompt")
    return str(raw).strip() if raw else ""


def _resolve_llm_system_prompt(cfg: dict[str, Any], config_dir: Path) -> None:
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        return
    if llm.get("system_prompt_file"):
        llm["system_prompt"] = resolve_llm_system_prompt(llm, config_dir)


def _config_for_storage(cfg: dict[str, Any]) -> dict[str, Any]:
    """Remove resolved duplicates before persisting configuration.

    ``load_config`` injects the contents of ``system_prompt_file`` for runtime
    consumers.  Writing that derived value back into YAML creates a second,
    silently drifting prompt (and previously retained a side-effecting volume
    example), so the external file remains the sole stored source of truth.
    """

    stored = copy.deepcopy(cfg)
    llm = stored.get("llm")
    if isinstance(llm, dict) and llm.get("system_prompt_file"):
        llm.pop("system_prompt", None)
    return stored


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> None:
    """写回 ``config.yaml``（会丢失 YAML 注释，与调试页其它保存行为一致）。"""
    config_path = Path(path or DEFAULT_CONFIG_PATH).resolve()
    rendered = yaml.safe_dump(
        _config_for_storage(cfg),
        allow_unicode=True,
        sort_keys=False,
    )
    with file_lock(config_path):
        atomic_write_text(config_path, rendered)


def update_config(
    update: Callable[[dict[str, Any]], dict[str, Any] | None],
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply a complete YAML read/modify/write transaction under one lock."""

    config_path = Path(path or DEFAULT_CONFIG_PATH).resolve()
    with file_lock(config_path):
        if config_path.is_file():
            with config_path.open(encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        else:
            current = {}
        if not isinstance(current, dict):
            raise ValueError("config root must be a mapping")
        replacement = update(current)
        result = replacement if replacement is not None else current
        if not isinstance(result, dict):
            raise ValueError("updated config root must be a mapping")
        result = _config_for_storage(result)
        rendered = yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
        atomic_write_text(config_path, rendered)
        return result
