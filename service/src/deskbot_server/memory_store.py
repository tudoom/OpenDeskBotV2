"""PC-local long-term memory stored in ``data/local/memories.json``.

Hardware IDs are transport addresses only. Every robot connected to this PC
reads and edits the same memory library.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from deskbot_server.atomic_store import file_lock
from deskbot_server.constants import MEMORIES_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path

_LEGACY_MEMORY_FILENAME = "user_memory.json"
_MAX_ENTRIES = 200
_MAX_PROMPT_ENTRIES = 30
_MAX_ENTRY_BYTES = 4096
_MAX_TOTAL_TEXT_BYTES = 256 * 1024
_MAX_PROMPT_TEXT_BYTES = 16 * 1024


def _text_bytes(entry: dict[str, Any]) -> int:
    return len(str(entry.get("text") or "").encode("utf-8"))


def _total_text_bytes(entries: list[dict[str, Any]]) -> int:
    return sum(_text_bytes(entry) for entry in entries)


def _cap_prompt_entries(
    entries: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    total = 0
    for entry in entries[:limit]:
        size = _text_bytes(entry)
        if total + size > _MAX_PROMPT_TEXT_BYTES:
            break
        selected.append(entry)
        total += size
    return selected


def _normalize_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("entry must be object")
    text = str(raw.get("text") or raw.get("value") or "").strip()
    if not text:
        raise ValueError("text required")
    text_size = len(text.encode("utf-8"))
    if text_size > _MAX_ENTRY_BYTES:
        raise ValueError(
            f"memory text exceeds {_MAX_ENTRY_BYTES} UTF-8 bytes"
        )
    entry_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    created = raw.get("created_at")
    try:
        created_at = float(created) if created is not None else time.time()
    except (TypeError, ValueError):
        created_at = time.time()
    return {
        "id": entry_id,
        "text": text,
        "created_at": created_at,
    }


def _migrate_legacy_memory_file(current: Path) -> Path:
    """Normalize the legacy file into the new name without overwriting it."""

    legacy = current.with_name(_LEGACY_MEMORY_FILENAME)
    if current.is_file() or not legacy.is_file():
        return current
    with file_lock(current):
        if current.is_file() or not legacy.is_file():
            return current
        with file_lock(legacy):
            if current.is_file() or not legacy.is_file():
                return current
            try:
                entries = _STORE.load(legacy)
                _STORE.save_unlocked(entries, path=current)
            except OSError:
                # Keep the existing library usable and retry migration on the
                # next access if this process cannot create the new file.
                return legacy
            try:
                legacy.unlink()
            except OSError:
                # The new file is already authoritative; an undeletable legacy
                # copy is harmless and must never overwrite it later.
                pass
    return current


def _normalize_entries_lenient(raw: object) -> list[dict[str, Any]]:
    """读取口径：跳过坏条目（写入口径见 ``_normalize_entries_strict``）。"""
    items = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    output: list[dict[str, Any]] = []
    for item in items:
        try:
            output.append(_normalize_entry(item))
        except ValueError:
            continue
    return output


def _normalize_entries_strict(entries: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [_normalize_entry(entry) for entry in entries]
    if _total_text_bytes(normalized) > _MAX_TOTAL_TEXT_BYTES:
        raise ValueError(
            f"memory store exceeds {_MAX_TOTAL_TEXT_BYTES} UTF-8 text bytes"
        )
    return {"entries": normalized}


_STORE = JsonDocumentStore(
    lambda: _migrate_legacy_memory_file(Path(resolve_json_path(MEMORIES_FILE))),
    normalize=_normalize_entries_lenient,
    normalize_save=_normalize_entries_strict,
    default=list,
)


def load_memory_entries() -> list[dict[str, Any]]:
    return _STORE.load()


def _locked_entries():
    """迁移钩子只在进锁前跑一次：锁内统一用固定 path 读写。"""
    path = _STORE.path()
    return path, file_lock(path)


def list_memories(*, limit: int = _MAX_PROMPT_ENTRIES) -> list[dict[str, Any]]:
    """Return the newest memories for prompt construction."""

    cap = max(1, min(int(limit), _MAX_ENTRIES))
    rows = load_memory_entries()
    rows.sort(key=lambda entry: float(entry.get("created_at") or 0), reverse=True)
    return _cap_prompt_entries(rows, limit=cap)


def list_memory_entries(*, limit: int = _MAX_ENTRIES) -> list[dict[str, Any]]:
    """Return PC-local memories, newest first, for management UIs."""

    rows = load_memory_entries()
    rows.sort(key=lambda entry: float(entry.get("created_at") or 0), reverse=True)
    cap = max(1, min(int(limit), _MAX_ENTRIES))
    return rows[:cap]


def add_memory(text: str) -> dict[str, Any]:
    """Add one memory to this PC's local library."""

    path, lock = _locked_entries()
    with lock:
        entries = _STORE.load(path)
        entry = _normalize_entry({"text": text})
        entries.append(entry)
        if len(entries) > _MAX_ENTRIES:
            entries = sorted(
                entries,
                key=lambda item: float(item.get("created_at") or 0),
            )[-_MAX_ENTRIES:]
        while entries and _total_text_bytes(entries) > _MAX_TOTAL_TEXT_BYTES:
            entries.pop(0)
        _STORE.save_unlocked(entries, path=path)
    return entry


def get_memory(entry_id: str) -> dict[str, Any] | None:
    target = str(entry_id or "").strip()
    if not target:
        return None
    for entry in load_memory_entries():
        if str(entry.get("id") or "") == target:
            return dict(entry)
    return None


def update_memory(entry_id: str, text: str) -> dict[str, Any] | None:
    target = str(entry_id or "").strip()
    new_text = str(text or "").strip()
    if not target or not new_text:
        raise ValueError("id and text are required")
    path, lock = _locked_entries()
    with lock:
        entries = _STORE.load(path)
        updated: dict[str, Any] | None = None
        for entry in entries:
            if str(entry.get("id") or "") == target:
                entry["text"] = new_text
                updated = dict(entry)
                break
        if updated is None:
            return None
        _STORE.save_unlocked(entries, path=path)
        return updated


def delete_memory(entry_id: str) -> bool:
    target = str(entry_id or "").strip()
    if not target:
        return False
    path, lock = _locked_entries()
    with lock:
        entries = _STORE.load(path)
        kept = [entry for entry in entries if str(entry.get("id") or "") != target]
        if len(kept) == len(entries):
            return False
        _STORE.save_unlocked(kept, path=path)
        return True
