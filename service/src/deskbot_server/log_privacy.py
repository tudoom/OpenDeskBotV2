from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def content_logging_enabled() -> bool:
    return (os.environ.get("DESKBOT_LOG_CONTENT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def safe_log_content(value: Any, *, limit: int = 160) -> str:
    """Return a non-reversible content descriptor unless explicitly opted in."""

    if isinstance(value, bytes):
        raw = value
        text = f"<{len(raw)} bytes>"
    elif isinstance(value, str):
        text = value
        raw = text.encode("utf-8", errors="replace")
    else:
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            text = str(value)
        raw = text.encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    if not content_logging_enabled():
        return f"<redacted len={len(raw)} sha256={digest}>"
    compact = text.replace("\r", "\\r").replace("\n", "\\n")
    cap = max(16, int(limit))
    if len(compact) > cap:
        compact = f"{compact[:cap]}…"
    return f"{compact} <len={len(raw)} sha256={digest}>"
