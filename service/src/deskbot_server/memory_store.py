"""PC-local long-term memory stored in ``data/local/memories.json``.

Hardware IDs are transport addresses only. Every robot connected to this PC
reads and edits the same memory library.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from deskbot_server import memory_md
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


def _ensure_migrated() -> None:
    """Move the retired memories.json into MEMORY.md the first time we look."""
    if memory_md.master_path().exists():
        return
    try:
        legacy = _STORE.load()
    except Exception:  # noqa: BLE001 - 旧库损坏不能挡住新库启用
        return
    if legacy:
        memory_md.migrate_from_json(legacy)


def load_memory_entries() -> list[dict[str, Any]]:
    """All memories, master core first then recent days."""
    _ensure_migrated()
    return memory_md.all_entries()


def list_memories(*, limit: int = _MAX_PROMPT_ENTRIES) -> list[dict[str, Any]]:
    """Return memories for prompt construction, master core first."""
    cap = max(1, min(int(limit), _MAX_ENTRIES))
    return load_memory_entries()[:cap]


def list_memory_entries(*, limit: int = _MAX_ENTRIES) -> list[dict[str, Any]]:
    """Return memories for management UIs."""
    cap = max(1, min(int(limit), _MAX_ENTRIES))
    return load_memory_entries()[:cap]


def add_memory(text: str) -> dict[str, Any]:
    """Record one memory in today's daily file."""
    _ensure_migrated()
    return memory_md.append_today(text)


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
    _ensure_migrated()
    return memory_md.update_entry(target, new_text)


def delete_memory(entry_id: str) -> bool:
    _ensure_migrated()
    return memory_md.delete_entry(entry_id)


def memory_prompt_text() -> str:
    """Master core plus a short recent window, ready for the system prompt."""
    _ensure_migrated()
    return memory_md.prompt_sections()


def memory_stats() -> dict[str, Any]:
    _ensure_migrated()
    return memory_md.stats()
