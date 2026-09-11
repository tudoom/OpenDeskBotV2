"""Single-PC persistent data rooted at ``data/local``.

Hardware identifiers are transport addresses only. They must never select a
different persistent profile: every robot plugged into this PC uses the same
configuration, memories and expression library.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from deskbot_server.atomic_store import (
    atomic_write_bytes,
    atomic_write_text,
    file_lock,
)
from deskbot_server.paths import DATA_DIR

logger = logging.getLogger("deskbot-server")

LOCAL_DATA_ROOT = DATA_DIR / "local"
LLM_SYSTEM_FILENAME = "llm_system.txt"

# ``data/global`` is a read-only seed/template area. Editable runtime files
# always live in ``data/local``.
LOCAL_EDITABLE_SEED_NAMES: frozenset[str] = frozenset(
    {
        "camera_face.json",
        "device_volume.json",
        "scene_playbooks.json",
        "servo.json",
    }
)


def global_config_dir() -> Path:
    return DATA_DIR / "global"
def local_data_dir() -> Path:
    return LOCAL_DATA_ROOT


def ensure_local_data_initialized() -> bool:
    """Create ``data/local`` and copy missing shipped templates into it."""

    LOCAL_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in list_data_seed_files():
        dst = LOCAL_DATA_ROOT / src.name
        with file_lock(dst):
            if dst.exists():
                continue
            try:
                mode = src.stat().st_mode & 0o777
            except OSError:
                mode = None
            atomic_write_bytes(dst, src.read_bytes(), mode=mode)
            copied += 1
    if copied:
        logger.info(
            "[device_data] seeded local profile dir=%s copied=%d",
            LOCAL_DATA_ROOT,
            copied,
        )
    return copied > 0


def global_llm_system_path() -> Path:
    return global_config_dir() / LLM_SYSTEM_FILENAME


def list_data_json_files() -> list[Path]:
    """Editable JSON templates copied from ``data/global`` to ``data/local``."""
    return sorted(
        global_config_dir() / name
        for name in LOCAL_EDITABLE_SEED_NAMES
        if (global_config_dir() / name).is_file()
    )


def list_data_seed_files() -> list[Path]:
    """Templates for the one editable PC-local profile."""
    return list_data_json_files()


def resolve_json_path(global_path: str) -> str:
    """Resolve system files globally and all editable files under ``data/local``."""
    base = os.path.basename(global_path)
    return str(local_data_dir() / base)


def load_llm_system_prompt() -> str:
    """Read the PC-local prompt, falling back to the system template."""
    local_path = local_data_dir() / LLM_SYSTEM_FILENAME
    if local_path.is_file():
        return local_path.read_text(encoding="utf-8").strip()
    global_path = global_llm_system_path()
    if global_path.is_file():
        return global_path.read_text(encoding="utf-8").strip()
    from deskbot_server.config import load_config

    cfg = load_config()
    return str((cfg.get("llm") or {}).get("system_prompt") or "").strip()


def save_llm_system_prompt(content: str) -> Path:
    """Save the editable prompt in the one PC-local profile."""
    local_data_dir().mkdir(parents=True, exist_ok=True)
    path = local_data_dir() / LLM_SYSTEM_FILENAME
    text = (content or "").strip()
    with file_lock(path):
        atomic_write_text(path, text + ("\n" if text else ""))
    return path
