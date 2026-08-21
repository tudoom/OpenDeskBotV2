from __future__ import annotations

import os
import re
import threading
from typing import Iterable

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.paths import ENV_FILE

_env_lock = threading.RLock()
_last_signature: tuple[int, int] | None = None
_file_managed_values: dict[str, str] = {}


def _parse_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    path = ENV_FILE
    if not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key] = val
    except OSError:
        return {}
    return out


def load_dotenv(*, force_reload: bool = False) -> bool:
    """Load `.env`, and hot-reload values previously sourced from that file.

    Real process environment variables remain authoritative.  A value is only
    replaced on reload when it still equals the last value read from `.env`.
    Returns whether a new file revision was observed.
    """
    global _last_signature, _file_managed_values
    try:
        stat = ENV_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (0, 0)
    with _env_lock:
        if not force_reload and signature == _last_signature:
            return False
        incoming = _parse_env_file()
        previous = dict(_file_managed_values)
        for key, value in incoming.items():
            current = os.environ.get(key)
            if current is None or (key in previous and current == previous[key]):
                os.environ[key] = value
        for key, old_value in previous.items():
            if key not in incoming and os.environ.get(key) == old_value:
                os.environ.pop(key, None)
        _file_managed_values = incoming
        _last_signature = signature
        return True


# ---------------------------------------------------------------------------
# 通用 .env 读写器（原 tts.env_store，供 tts/llm/asr 配置面共用）。
# ---------------------------------------------------------------------------


def looks_masked(value: str) -> bool:
    """判断一个提交值是否是掩码占位（不应覆盖已保存的密钥）。"""

    v = str(value or "").strip()
    return not v or "*" in v or "•" in v or "…" in v


def _quote_env_value(value: str) -> str:
    raw = value or ""
    if not raw:
        return ""
    if re.search(r'[\s#="\']', raw):
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return raw


def read_env_file() -> dict[str, str]:
    """读取 ``.env`` 全部键值（不动 ``os.environ``）。"""

    out: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if "=" not in body:
            continue
        key, val = body.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def update_env_keys(
    updates: dict[str, str],
    *,
    keys: Iterable[str],
    comment: str = "# 配置",
    write_empty_keys: Iterable[str] | None = None,
) -> None:
    """更新 .env 中指定键，保留其它行与注释。

    值为空时默认不覆盖已有行；``write_empty_keys`` 中的键会显式写成
    ``KEY=``，用于表达“清除覆盖、改用协议默认值”等有效配置。
    新增缺失键时，会在其上方写入 ``comment`` 作为分节标题。
    """
    allowed = set(keys)
    explicit_empty = set(write_empty_keys or ()) & allowed
    filtered = {k: (updates.get(k) or "").strip() for k in allowed if k in updates}
    if not filtered:
        return

    with file_lock(ENV_FILE):
        lines: list[str] = []
        if ENV_FILE.is_file():
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

        new_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            body = stripped[7:].strip() if stripped.startswith("export ") else stripped
            if "=" not in body:
                new_lines.append(line)
                continue
            key = body.split("=", 1)[0].strip()
            if key in filtered:
                val = filtered[key]
                if val or key in explicit_empty:
                    new_lines.append(f"{key}={_quote_env_value(val)}")
                else:
                    new_lines.append(line)
                seen.add(key)
            else:
                new_lines.append(line)

        missing = [
            k
            for k in filtered
            if k not in seen and (filtered[k] or k in explicit_empty)
        ]
        if missing:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append(comment)
            for key in missing:
                new_lines.append(f"{key}={_quote_env_value(filtered[key])}")

        atomic_write_text(ENV_FILE, "\n".join(new_lines).rstrip() + "\n", mode=0o600)

    for key, val in filtered.items():
        if val or key in explicit_empty:
            os.environ[key] = val
    load_dotenv(force_reload=True)
