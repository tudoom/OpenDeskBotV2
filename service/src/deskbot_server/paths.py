"""项目根目录与静态资源路径（src 布局下统一由此解析）。"""
from __future__ import annotations

import os
from pathlib import Path


def _environment_path(name: str, fallback: Path) -> Path:
    """Resolve a launcher-provided path without changing source checkouts.

    The desktop client keeps mutable state under LocalAppData while loading
    Python modules and models from its versioned, extracted runtime.  Source
    and normal developer launches continue to use the repository layout.
    """

    raw = str(os.environ.get(name) or "").strip()
    return Path(raw).expanduser().resolve() if raw else fallback.resolve()


# service/ 项目根目录（含 config.yaml、data/、models/）
PROJECT_ROOT = _environment_path(
    "DESKBOT_PROJECT_ROOT",
    Path(__file__).resolve().parents[2],
)

DATA_DIR = _environment_path("DESKBOT_DATA_DIR", PROJECT_ROOT / "data")
MODELS_DIR = _environment_path("DESKBOT_MODELS_DIR", PROJECT_ROOT / "models")
DEFAULT_CONFIG_PATH = _environment_path(
    "DESKBOT_SERVER_CONFIG",
    PROJECT_ROOT / "config.yaml",
)
ENV_FILE = _environment_path("DESKBOT_ENV_FILE", PROJECT_ROOT / ".env")
