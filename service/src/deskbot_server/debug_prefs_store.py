"""Runtime and persisted debug preference for ASR automatic replies.

存储位置：``data/local/debug_prefs.json``（运行时回写只落这里）。
``config.yaml`` 的 ``debug.asr_auto_reply`` 键只作为一次性迁移源与默认值：
本地文件缺失该键时读取一次并落盘，此后 config.yaml 不再被本模块写入，
消除"出厂配置 + 运行时状态"双身份问题。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from deskbot_server.auto_reply import (
    get_asr_voice_auto_reply_enabled,
    set_asr_voice_auto_reply_enabled,
)
from deskbot_server.config import load_config

logger = logging.getLogger("deskbot-server")


def _prefs_path() -> Path:
    from deskbot_server.device_data import local_data_dir

    return local_data_dir() / "debug_prefs.json"


def _store():
    from deskbot_server.core.json_store import JsonDocumentStore

    return JsonDocumentStore(_prefs_path, sort_keys=True)


def _read_local_prefs(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except (ValueError, UnicodeDecodeError):
        logger.warning("[debug_prefs] corrupt %s ignored", path)
        return None
    return raw if isinstance(raw, dict) else None


def debug_prefs_snapshot() -> dict[str, Any]:
    return {"asr_auto_reply": get_asr_voice_auto_reply_enabled()}


def apply_debug_prefs_from_config(cfg: dict[str, Any] | None = None) -> None:
    """Load the only live debug switch into process memory.

    读取顺序：``data/local/debug_prefs.json`` 优先；缺失时读
    ``config.yaml`` 的 ``debug.asr_auto_reply`` 作为一次性迁移源
    （读到即落盘到本地文件），两者都没有则维持内存默认值。
    """

    path = _prefs_path()
    local = _read_local_prefs(path)
    if local is not None and "asr_auto_reply" in local:
        set_asr_voice_auto_reply_enabled(bool(local["asr_auto_reply"]))
        return

    doc = cfg if isinstance(cfg, dict) else load_config()
    debug = doc.get("debug")
    if isinstance(debug, dict) and "asr_auto_reply" in debug:
        migrated = bool(debug.get("asr_auto_reply"))
        set_asr_voice_auto_reply_enabled(migrated)
        store = _store()
        try:
            with store.lock():
                current = _read_local_prefs(path) or {}
                if "asr_auto_reply" not in current:
                    current["asr_auto_reply"] = migrated
                    store.save_unlocked(current)
                    logger.info(
                        "[debug_prefs] migrated asr_auto_reply=%s from "
                        "config.yaml to %s",
                        migrated,
                        path,
                    )
        except OSError:
            # Migration is best-effort; the in-memory switch is already set
            # and the next persist_asr_auto_reply writes the local file.
            logger.warning("[debug_prefs] could not write %s", path)


def persist_asr_auto_reply(enabled: bool) -> None:
    normalized = bool(enabled)
    set_asr_voice_auto_reply_enabled(normalized)
    path = _prefs_path()
    store = _store()
    with store.lock():
        current = _read_local_prefs(path) or {}
        current["asr_auto_reply"] = normalized
        store.save_unlocked(current)
    logger.info(
        "[debug_prefs] updated %s asr_auto_reply=%s", path.name, normalized
    )
