"""Full-text search over stored conversations.

Sessions live as JSON files under ``data/local/session``. Recalling
something said last week meant the model paging through them one by one,
which costs a tool round per session and quietly gives up long before it
reaches ninety days of history. This keeps an FTS5 index beside them so a
single query reaches the whole archive.

The index is rebuilt lazily at search time by comparing each file's mtime
and size against what was indexed. Search is model- or user-initiated and
never sits on the realtime speech path, so the sync cost lands where it
can be afforded rather than on every turn.

Chinese needs the ``trigram`` tokenizer — ``unicode61`` treats a whole
run of Han characters as one token, so nothing short of the exact phrase
would match. Trigram in turn cannot match queries shorter than three
characters, so those fall back to a LIKE scan, which the archive is small
enough to absorb.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from deskbot_server.device_data import local_data_dir

INDEX_FILENAME = "session_index.db"
TRIGRAM_MIN_CHARS = 3
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
SNIPPET_TOKENS = 12


def session_root() -> Path:
    return local_data_dir() / "session"


def index_path() -> Path:
    return local_data_dir() / INDEX_FILENAME


def _connect() -> sqlite3.Connection:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5("
        "  session_id UNINDEXED,"
        "  role UNINDEXED,"
        "  ts UNINDEXED,"
        "  title UNINDEXED,"
        "  text,"
        "  tokenize='trigram'"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS indexed_files ("
        "  session_id TEXT PRIMARY KEY,"
        "  mtime REAL NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  indexed_at REAL NOT NULL"
        ")"
    )
    return conn


def _session_files() -> dict[str, Path]:
    try:
        return {p.stem: p for p in session_root().glob("*.json")}
    except OSError:
        return {}


def _read_session(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _index_one(conn: sqlite3.Connection, session_id: str, path: Path) -> int:
    session = _read_session(path)
    if session is None:
        return 0
    title = str(session.get("title") or "")
    rows = []
    for item in list(session.get("messages") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("message") or item.get("content") or "").strip()
        if not text:
            continue
        rows.append(
            (
                session_id,
                str(item.get("role") or ""),
                float(item.get("ts") or 0.0),
                title,
                text,
            )
        )
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    if rows:
        conn.executemany(
            "INSERT INTO messages(session_id, role, ts, title, text)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def sync(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Bring the index in line with the session files on disk."""
    own = conn is None
    conn = conn or _connect()
    try:
        files = _session_files()
        known = {
            row["session_id"]: (row["mtime"], row["size"])
            for row in conn.execute(
                "SELECT session_id, mtime, size FROM indexed_files"
            )
        }
        indexed = 0
        messages = 0
        for session_id, path in files.items():
            try:
                stat = path.stat()
            except OSError:
                continue
            previous = known.get(session_id)
            if previous and previous[0] == stat.st_mtime and previous[1] == stat.st_size:
                continue
            messages += _index_one(conn, session_id, path)
            conn.execute(
                "INSERT INTO indexed_files(session_id, mtime, size, indexed_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "  mtime=excluded.mtime, size=excluded.size,"
                "  indexed_at=excluded.indexed_at",
                (session_id, stat.st_mtime, stat.st_size, time.time()),
            )
            indexed += 1
        dropped = 0
        for session_id in set(known) - set(files):
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                "DELETE FROM indexed_files WHERE session_id = ?", (session_id,)
            )
            dropped += 1
        conn.commit()
        return {"sessions": indexed, "messages": messages, "dropped": dropped}
    finally:
        if own:
            conn.close()


def _escape_fts(query: str) -> str:
    """Quote the query so punctuation cannot become FTS5 syntax."""
    return '"' + str(query).replace('"', '""') + '"'


def search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """Return matching turns, newest first."""
    text = str(query or "").strip()
    if not text:
        return []
    cap = max(1, min(int(limit), MAX_LIMIT))
    conn = _connect()
    try:
        sync(conn=conn)
        params: list[Any] = []
        if len(text) >= TRIGRAM_MIN_CHARS:
            sql = (
                "SELECT session_id, role, ts, title,"
                " snippet(messages, 4, '[', ']', '…', ?) AS snippet, text"
                " FROM messages WHERE messages MATCH ?"
            )
            params.extend([SNIPPET_TOKENS, _escape_fts(text)])
        else:
            # trigram 索引不响应两字以内的查询，退回扫描；会话库最多
            # 100 个、每个 200 条，这点量扫得起。
            sql = (
                "SELECT session_id, role, ts, title,"
                " text AS snippet, text"
                " FROM messages WHERE text LIKE ?"
            )
            params.append(f"%{text}%")
        if role:
            sql += " AND role = ?"
            params.append(str(role).strip().lower())
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(cap)
        return [
            {
                "session_id": row["session_id"],
                "role": row["role"],
                "ts": row["ts"],
                "title": row["title"],
                "snippet": row["snippet"],
                "text": row["text"],
            }
            for row in conn.execute(sql, params)
        ]
    finally:
        conn.close()


def stats() -> dict[str, Any]:
    conn = _connect()
    try:
        sync(conn=conn)
        messages = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
        sessions = conn.execute(
            "SELECT count(*) AS n FROM indexed_files"
        ).fetchone()["n"]
        return {
            "sessions": int(sessions),
            "messages": int(messages),
            "index_path": str(index_path()),
        }
    finally:
        conn.close()


def rebuild() -> dict[str, int]:
    """Drop everything and index from scratch."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM indexed_files")
        conn.commit()
        return sync(conn=conn)
    finally:
        conn.close()
