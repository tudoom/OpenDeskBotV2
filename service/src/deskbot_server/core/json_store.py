"""Generic JSON document store skeleton for PC-local configuration files.

统一约 10 个 ``*_store`` 模块重复的样板：path 解析、load（missing/corrupt
策略 + normalize 钩子）、save（``file_lock`` + 原子替换）、update（锁内
读-改-写事务）。各 store 的领域校验保留在自己的 normalize 钩子里，对外
函数签名不变。

注意：与既有各 store 一致，``load`` 不加锁 —— 写入方通过原子替换保证
读者永远看到完整文件；需要读-改-写一致性的路径走 ``update``/``lock``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from deskbot_server.atomic_store import atomic_write_text, file_lock


class JsonDocumentStore:
    """One JSON document on disk, with normalize/migrate hooks.

    参数：
    - ``path_factory``：每次访问时解析目标路径（data 目录可能随环境/测试
      变化，不能在构造时固化）。
    - ``normalize``：load 后与 save 前统一调用；``normalize_save`` 提供时
      save 前改用它（例如 memory_store 读取宽松、写入严格）。
    - ``migrate``：接收解析出的路径、返回实际生效路径（例如旧文件名迁移）。
    - ``default``：文件缺失时 ``load`` 的返回值工厂。
    - ``corrupt_to_default``：读取/解析失败时返回 ``default()`` 而不是抛出。
    - ``mode``：save 时的文件权限（如密钥文件 0o600）。
    - ``sort_keys``：save 序列化时是否排序键。
    """

    def __init__(
        self,
        path_factory: Callable[[], str | Path],
        *,
        normalize: Callable[[Any], Any] | None = None,
        normalize_save: Callable[[Any], Any] | None = None,
        migrate: Callable[[Path], Path] | None = None,
        default: Callable[[], Any] | None = None,
        corrupt_to_default: bool = False,
        mode: int | None = None,
        sort_keys: bool = False,
    ) -> None:
        self._path_factory = path_factory
        self._normalize = normalize
        self._normalize_save = normalize_save or normalize
        self._migrate = migrate
        self._default = default
        self._corrupt_to_default = corrupt_to_default
        self._mode = mode
        self._sort_keys = sort_keys

    # -- path ---------------------------------------------------------------

    def path(self) -> Path:
        resolved = Path(self._path_factory())
        if self._migrate is not None:
            resolved = Path(self._migrate(resolved))
        return resolved

    def lock(self):
        """Cross-process exclusive lock context for read-modify-write flows."""

        return file_lock(self.path())

    # -- load ---------------------------------------------------------------

    def _default_value(self) -> Any:
        return self._default() if self._default is not None else None

    def load_raw(self, path: Path | None = None) -> Any:
        """Read and parse the raw JSON document (no normalize hook)."""

        target = path if path is not None else self.path()
        if not target.is_file():
            return self._default_value()
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            if self._corrupt_to_default:
                return self._default_value()
            raise

    def load(self, path: Path | None = None) -> Any:
        """Load the normalized document (missing → ``default()``)."""

        target = path if path is not None else self.path()
        if not target.is_file():
            return self._default_value()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            return self._normalize(raw) if self._normalize is not None else raw
        except (OSError, ValueError, UnicodeDecodeError):
            if self._corrupt_to_default:
                return self._default_value()
            raise

    # -- save ---------------------------------------------------------------

    def _serialize(self, doc: Any) -> str:
        return (
            json.dumps(
                doc,
                ensure_ascii=False,
                indent=2,
                sort_keys=self._sort_keys,
            )
            + "\n"
        )

    def save_unlocked(self, doc: Any, *, path: Path | None = None) -> Any:
        """Normalize and atomically write; caller already holds the lock."""

        normalized = (
            self._normalize_save(doc) if self._normalize_save is not None else doc
        )
        target = path if path is not None else self.path()
        atomic_write_text(target, self._serialize(normalized), mode=self._mode)
        return normalized

    def save(self, doc: Any) -> Any:
        """Normalize and atomically write the document under the file lock."""

        path = self.path()
        with file_lock(path):
            return self.save_unlocked(doc, path=path)

    # -- read-modify-write ---------------------------------------------------

    def update(self, mutate: Callable[[Any], Any]) -> Any:
        """Atomically apply ``mutate(document) -> document`` under the lock."""

        path = self.path()
        with file_lock(path):
            doc = self.load(path)
            result = mutate(doc)
            return self.save_unlocked(result, path=path)
