"""Small, dependency-free helpers for safely updating shared configuration files.

The web console and the websocket service run in different processes.  A plain
``open(..., "w")`` can therefore expose a truncated JSON/.env file to the other
process.  Writers use a sibling lock file and atomically replace the target.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Windows 下 ``os.replace`` 在有未加锁读者持有目标文件句柄时会抛
# PermissionError（共享冲突）。读者通常在毫秒级内放开句柄，做一次有界退避
# 重试即可；总预算约 1 秒，重试耗尽后按原语义抛出。
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_BACKOFF_SCHEDULE_SEC = (0.05, 0.1, 0.2, 0.4)


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= _REPLACE_MAX_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SCHEDULE_SEC[attempt])


def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``os.replace`` with the Windows sharing-violation backoff applied."""

    _replace_with_retry(str(src), Path(dst))


class _ProcessLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users = 0


_process_locks: dict[str, _ProcessLockEntry] = {}
_process_locks_guard = threading.Lock()


@contextmanager
def _process_lock(
    path: Path,
    *,
    deadline: float | None = None,
) -> Iterator[None]:
    """Hold the unique in-process lock for *path* without retaining it forever.

    A caller is counted before it waits on the per-path lock.  The entry can
    therefore only be removed after the final holder or waiter has released
    its reference.  Entry lookup, reference changes and eviction all happen
    under the table guard, preventing a second lock from being created while
    the old one is still usable.
    """

    key = str(path.resolve())
    with _process_locks_guard:
        entry = _process_locks.get(key)
        if entry is None:
            entry = _ProcessLockEntry()
            _process_locks[key] = entry
        entry.users += 1
    acquired = False
    try:
        if deadline is None:
            entry.lock.acquire()
        else:
            remaining = max(0.0, deadline - time.monotonic())
            if not entry.lock.acquire(timeout=remaining):
                raise TimeoutError(f"file lock timed out: {path}")
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _process_locks_guard:
            entry.users -= 1
            if entry.users == 0 and _process_locks.get(key) is entry:
                del _process_locks[key]


def _lock_os_file(lock_file, *, deadline: float | None, lock_path: Path) -> None:
    """Acquire the OS-level byte/flock; poll非阻塞原语直到 deadline。"""

    if os.name == "nt":
        import msvcrt

        if deadline is None:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"file lock timed out: {lock_path}") from None
                time.sleep(0.1)
    else:
        import fcntl

        if deadline is None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"file lock timed out: {lock_path}") from None
                time.sleep(0.1)


@contextmanager
def file_lock(
    path: str | os.PathLike[str],
    *,
    blocking: bool = True,
    timeout: float | None = None,
) -> Iterator[None]:
    """Take a cross-process exclusive lock associated with *path*.

    - ``blocking=True, timeout=None``（默认）：无限期等待（Windows 底层
      ``LK_LOCK`` 保持既有约 10 秒重试语义）。
    - ``timeout=秒``：以 0.1s 步长轮询非阻塞原语，超时抛 ``TimeoutError``。
    - ``blocking=False``：只尝试一次（等价 ``timeout=0``）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    if not blocking:
        timeout = 0.0
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with _process_lock(lock_path, deadline=deadline):
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                # ``a+b`` forces writes to EOF regardless of seek position.
                # Check the actual size first; checking ``tell()`` immediately
                # after ``seek(0)`` used to append one byte on every lock.
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
            _lock_os_file(lock_file, deadline=deadline, lock_path=lock_path)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *, mode: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        if mode is not None:
            try:
                os.chmod(tmp_name, mode)
            except OSError:
                pass
        _replace_with_retry(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    mode: int | None = None,
) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        mode=mode,
    )
