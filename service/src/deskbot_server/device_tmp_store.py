"""Sandboxed reads and writes below the PC-local ``data/local/tmp`` directory."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.device_data import local_data_dir

_TMP_DIRNAME = "tmp"
_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000
_MAX_TOTAL_BYTES = 4_000_000
_MAX_FILES = 128
_TTL_SECONDS = 24 * 60 * 60


def local_tmp_root() -> Path:
    root = local_data_dir() / _TMP_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_local_tmp_path(rel_path: str) -> Path:
    """解析相对路径，禁止越界到 tmp 之外。"""
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("path 不能为空")
    parts = Path(rel).parts
    if ".." in parts:
        raise ValueError("path 不能包含 ..")
    root = local_tmp_root().resolve()
    target = (root / rel).resolve()
    root_s = str(root)
    target_s = str(target)
    if target_s != root_s and not target_s.startswith(root_s + os.sep):
        raise ValueError("path 越界")
    return target


def _quota_lock_path() -> Path:
    return local_tmp_root() / ".quota"


def _iter_tmp_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name == ".quota.lock" or name.endswith(".lock"):
            continue
        yield path


def cleanup_local_tmp_files(*, now: float | None = None) -> dict[str, int]:
    """Remove expired sandbox files and report the remaining quota usage."""
    root = local_tmp_root()
    cutoff = float(now if now is not None else time.time()) - _TTL_SECONDS
    removed = 0
    for path in list(_iter_tmp_files(root)):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    # Best-effort removal of now-empty subdirectories. The local root and
    # lock sentinel are deliberately retained.
    dirs = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in dirs:
        try:
            directory.rmdir()
        except OSError:
            pass
    files = list(_iter_tmp_files(root))
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except FileNotFoundError:
            pass
    return {
        "removed": removed,
        "files": len(files),
        "total_bytes": total_bytes,
    }


def read_local_tmp_file(path: str) -> dict[str, Any]:
    target = resolve_local_tmp_path(path)
    with file_lock(_quota_lock_path()):
        cleanup_local_tmp_files()
        if not target.is_file():
            raise ValueError(f"文件不存在或已过期: {path}")
        data = target.read_bytes()
    if len(data) > _MAX_READ_BYTES:
        raise ValueError(f"文件过大（>{_MAX_READ_BYTES} 字节）")
    try:
        text = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        encoding = "utf-8-replace"
    return {
        "path": path,
        "size": len(data),
        "encoding": encoding,
        "content": text,
    }


def write_local_tmp_file(path: str, content: str) -> dict[str, Any]:
    target = resolve_local_tmp_path(path)
    text = str(content if content is not None else "")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_WRITE_BYTES:
        raise ValueError(f"内容过大（>{_MAX_WRITE_BYTES} 字节）")
    with file_lock(_quota_lock_path()):
        usage = cleanup_local_tmp_files()
        old_size = target.stat().st_size if target.is_file() else 0
        if not target.is_file() and usage["files"] >= _MAX_FILES:
            raise ValueError(f"临时文件数量已达上限（{_MAX_FILES} 个）")
        projected = usage["total_bytes"] - old_size + len(encoded)
        if projected > _MAX_TOTAL_BYTES:
            raise ValueError(
                f"临时目录总容量将超过上限（{_MAX_TOTAL_BYTES} 字节）"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, text)
    return {"path": path, "size": len(encoded), "written": True}


def list_local_tmp_files(*, subpath: str = "") -> list[dict[str, Any]]:
    with file_lock(_quota_lock_path()):
        cleanup_local_tmp_files()
        base = local_tmp_root()
        if subpath:
            base = resolve_local_tmp_path(subpath)
        if not base.exists():
            return []
        now = time.time()
        if base.is_file():
            stat = base.stat()
            return [
                {
                    "path": subpath or base.name,
                    "type": "file",
                    "size": stat.st_size,
                    "age_seconds": max(0, int(now - stat.st_mtime)),
                }
            ]
        root = local_tmp_root()
        out: list[dict[str, Any]] = []
        for p in sorted(_iter_tmp_files(base)):
            stat = p.stat()
            rel = p.relative_to(root).as_posix()
            out.append(
                {
                    "path": rel,
                    "type": "file",
                    "size": stat.st_size,
                    "age_seconds": max(0, int(now - stat.st_mtime)),
                }
            )
        return out
