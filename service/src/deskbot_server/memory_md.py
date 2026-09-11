"""Markdown-backed memory: one curated master file plus one file per day.

``data/local/memory/MEMORY.md`` holds durable facts about the user; each
``data/local/memory/daily/YYYY-MM-DD.md`` holds what was worth remembering
from that day. New memories land in today's file, and consolidation later
promotes what has proven durable into the master file — so the prompt can
carry a small curated core plus a short recent window instead of an
ever-growing flat list.

Both files are plain Markdown bullet lists so a person can read and edit
them by hand::

    # 长期记忆

    - [a1b2c3d4] 主人喜欢喝美式，不加糖
    - [e5f6a7b8] 家里有一只叫团子的橘猫

The bracketed id keeps ``memory_delete`` addressable across both files, and
survives hand-editing as long as the bracket is left alone.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.device_data import local_data_dir

logger = logging.getLogger("deskbot-server")

MEMORY_DIRNAME = "memory"
# 与 Agent 页的三份文档统一命名：Agent.md / Memory.md / User.md。
MASTER_FILENAME = "Memory.md"
DAILY_DIRNAME = "daily"

MASTER_TITLE = "# 长期记忆"
DAILY_TITLE_FMT = "# {date} 的记忆"

# 提示词里带的每日文件天数。够覆盖"昨天说过的事"，又不会让近期琐事
# 淹没长期事实。
PROMPT_DAILY_DAYS = 7

MAX_ENTRY_BYTES = 4096
MAX_MASTER_BYTES = 64 * 1024
MAX_DAILY_BYTES = 32 * 1024
MAX_DAILY_FILES = 180

_BULLET_RE = re.compile(r"^\s*-\s*\[([0-9a-zA-Z_-]{1,32})\]\s*(.+?)\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def memory_dir() -> Path:
    return local_data_dir() / MEMORY_DIRNAME


def master_path() -> Path:
    return memory_dir() / MASTER_FILENAME


def daily_dir() -> Path:
    return memory_dir() / DAILY_DIRNAME


def daily_path(date_str: str) -> Path:
    return daily_dir() / f"{date_str}.md"


def today_str() -> str:
    """Local calendar date; a day boundary the user would recognise."""
    return datetime.now().strftime("%Y-%m-%d")


def new_entry_id() -> str:
    return uuid.uuid4().hex[:8]


def _ensure_dirs() -> None:
    daily_dir().mkdir(parents=True, exist_ok=True)


def parse_bullets(text: str) -> list[dict[str, Any]]:
    """Read ``- [id] text`` bullets, ignoring headings and prose."""
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        body = match.group(2).strip()
        if body:
            rows.append({"id": match.group(1), "text": body})
    return rows


def _render(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [title, ""]
    lines.extend(f"- [{row['id']}] {row['text']}" for row in rows)
    return "\n".join(lines).rstrip() + "\n"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_master() -> list[dict[str, Any]]:
    return parse_bullets(_read(master_path()))


def read_daily(date_str: str) -> list[dict[str, Any]]:
    return parse_bullets(_read(daily_path(date_str)))


def list_daily_dates(*, limit: int | None = None) -> list[str]:
    """Newest first."""
    try:
        names = [p.stem for p in daily_dir().glob("*.md")]
    except OSError:
        return []
    dates = sorted((n for n in names if _DATE_RE.match(n)), reverse=True)
    return dates[:limit] if limit else dates


def write_master(rows: list[dict[str, Any]]) -> None:
    _ensure_dirs()
    body = _render(MASTER_TITLE, rows)
    if len(body.encode("utf-8")) > MAX_MASTER_BYTES:
        raise ValueError(f"master memory exceeds {MAX_MASTER_BYTES} bytes")
    atomic_write_text(master_path(), body)


def write_daily(date_str: str, rows: list[dict[str, Any]]) -> None:
    _ensure_dirs()
    body = _render(DAILY_TITLE_FMT.format(date=date_str), rows)
    if len(body.encode("utf-8")) > MAX_DAILY_BYTES:
        raise ValueError(f"daily memory exceeds {MAX_DAILY_BYTES} bytes")
    atomic_write_text(daily_path(date_str), body)


def append_today(text: str) -> dict[str, Any]:
    """Record one memory in today's file and return the stored entry."""
    body = str(text or "").strip()
    if not body:
        raise ValueError("text required")
    if len(body.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise ValueError(f"memory text exceeds {MAX_ENTRY_BYTES} UTF-8 bytes")
    _ensure_dirs()
    date_str = today_str()
    with file_lock(str(memory_dir())):
        rows = read_daily(date_str)
        entry = {"id": new_entry_id(), "text": body}
        rows.append(entry)
        write_daily(date_str, rows)
        # 值得记的事立刻进长期记忆，而不是悬到每晚整理才出现——整理只负责
        # 之后的去重与提炼。同一个 id 同时写进两处，删除与整理都能对上。
        master_rows = read_master()
        master_rows.append(dict(entry))
        try:
            write_master(master_rows)
        except ValueError:
            # 长期记忆满了就只留每日副本，等整理压缩后自然补入。
            logger.warning("[memory] 长期记忆已满，%s 暂留每日文件", entry["id"])
    prune_daily_files()
    return {
        "id": entry["id"],
        "text": entry["text"],
        "created_at": time.time(),
        "date": date_str,
        "scope": "daily",
    }


def delete_entry(entry_id: str) -> bool:
    """Remove one memory from the master file or from any daily file."""
    target = str(entry_id or "").strip()
    if not target:
        return False
    removed = False
    with file_lock(str(memory_dir())):
        rows = read_master()
        kept = [row for row in rows if row["id"] != target]
        if len(kept) != len(rows):
            write_master(kept)
            removed = True
        # 实时写入后同一条记忆存在于长期与每日两处，删除必须两处同时清，
        # 否则每晚整理会把每日残留的那份重新提炼回长期记忆。
        for date_str in list_daily_dates():
            rows = read_daily(date_str)
            kept = [row for row in rows if row["id"] != target]
            if len(kept) != len(rows):
                write_daily(date_str, kept)
                removed = True
    return removed


def update_entry(entry_id: str, text: str) -> dict[str, Any] | None:
    body = str(text or "").strip()
    if not body:
        raise ValueError("text required")
    if len(body.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise ValueError(f"memory text exceeds {MAX_ENTRY_BYTES} UTF-8 bytes")
    target = str(entry_id or "").strip()
    if not target:
        return None
    updated: dict[str, Any] | None = None
    with file_lock(str(memory_dir())):
        rows = read_master()
        for row in rows:
            if row["id"] == target:
                row["text"] = body
                write_master(rows)
                updated = {"id": target, "text": body, "scope": "master"}
                break
        for date_str in list_daily_dates():
            rows = read_daily(date_str)
            for row in rows:
                if row["id"] == target:
                    row["text"] = body
                    write_daily(date_str, rows)
                    if updated is None:
                        updated = {
                            "id": target,
                            "text": body,
                            "scope": "daily",
                            "date": date_str,
                        }
                    break
    return updated


def prune_daily_files(*, keep: int = MAX_DAILY_FILES) -> int:
    """Drop the oldest daily files once the window is full."""
    dates = list_daily_dates()
    stale = dates[keep:]
    removed = 0
    for date_str in stale:
        try:
            daily_path(date_str).unlink()
            removed += 1
        except OSError:
            continue
    return removed


def all_entries() -> list[dict[str, Any]]:
    """Master first, then daily newest-first — the order the prompt uses."""
    master = read_master()
    master_ids = {row["id"] for row in master}
    rows: list[dict[str, Any]] = [{**row, "scope": "master"} for row in master]
    for date_str in list_daily_dates():
        rows.extend(
            {**row, "scope": "daily", "date": date_str}
            for row in read_daily(date_str)
            if row["id"] not in master_ids
        )
    return rows


def prompt_sections(*, daily_days: int = PROMPT_DAILY_DAYS) -> str:
    """Render the master core plus a short recent window for the prompt."""
    parts: list[str] = []
    master = read_master()
    if master:
        parts.append(
            "长期记忆（可用 memory_delete 删除，id 见方括号）：\n"
            + "\n".join(f"  - [{r['id']}] {r['text']}" for r in master)
        )
    master_ids = {row["id"] for row in master}
    recent: list[str] = []
    for date_str in list_daily_dates(limit=max(0, int(daily_days))):
        # 实时写入后每日文件只是流水账，长期记忆里已有的条目不再重复出现。
        rows = [r for r in read_daily(date_str) if r["id"] not in master_ids]
        if not rows:
            continue
        recent.append(
            f"  {date_str}：\n"
            + "\n".join(f"    - [{r['id']}] {r['text']}" for r in rows)
        )
    if recent:
        parts.append("最近几天的记忆：\n" + "\n".join(recent))
    if not parts:
        return "长期记忆：暂无。"
    return "\n\n".join(parts)


def migrate_from_json(entries: list[dict[str, Any]]) -> int:
    """Seed the master file from the retired memories.json, once."""
    if master_path().exists() or not entries:
        return 0
    rows = []
    for raw in entries:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        rows.append({"id": str(raw.get("id") or new_entry_id())[:32], "text": text})
    if not rows:
        return 0
    _ensure_dirs()
    write_master(rows)
    return len(rows)


def stats() -> dict[str, Any]:
    dates = list_daily_dates()
    return {
        "master_entries": len(read_master()),
        "daily_files": len(dates),
        "latest_daily": dates[0] if dates else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
