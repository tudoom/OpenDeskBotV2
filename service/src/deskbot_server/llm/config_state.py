"""Cross-process revision tracking for the system LLM configuration.

The Flask console and the realtime websocket service are separate processes.
The console records a desired revision after atomically updating ``.env``;
only the realtime process acknowledges that revision after reloading the file.
No secret values are copied into the state file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from deskbot_server.atomic_store import atomic_write_json, file_lock
from deskbot_server.device_data import local_data_dir
from deskbot_server.env import load_dotenv
from deskbot_server.paths import ENV_FILE

logger = logging.getLogger("deskbot-server")

LLM_CONFIG_STATE_FILE = local_data_dir() / ".llm_config_state.json"
LLM_WRITABLE_ENV_KEYS = (
    "LLM_API_KEY",
    "LLM_PROTOCOL",
    "LLM_MODEL",
    "LLM_BASE_URL",
)
_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_llm_env_values(path: Path | None = None) -> dict[str, str]:
    """Read Web-managed LLM keys from an env file, preserving empty values."""
    target = path or ENV_FILE
    values: dict[str, str] = {}
    if not target.is_file():
        return values
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in LLM_WRITABLE_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def llm_config_digest(values: Mapping[str, str]) -> str:
    """Return a stable digest without persisting the underlying secrets."""
    canonical = {
        key: str(values[key])
        for key in sorted(LLM_WRITABLE_ENV_KEYS)
        if key in values
    }
    body = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "revision": 0,
        "digest": "",
        "applied_revision": 0,
        "applied_digest": "",
        "updated_at": None,
        "applied_at": None,
        "apply_error": "",
    }


def _read_state_unlocked() -> dict[str, Any]:
    state = _empty_state()
    try:
        raw = json.loads(LLM_CONFIG_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if not isinstance(raw, dict):
        return state
    try:
        state["revision"] = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError):
        pass
    try:
        state["applied_revision"] = max(0, int(raw.get("applied_revision") or 0))
    except (TypeError, ValueError):
        pass
    for key in (
        "digest",
        "applied_digest",
        "updated_at",
        "applied_at",
        "apply_error",
    ):
        value = raw.get(key)
        state[key] = value if value is None else str(value)
    return state


def _write_state_unlocked(state: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "revision": max(0, int(state.get("revision") or 0)),
        "digest": str(state.get("digest") or ""),
        "applied_revision": max(0, int(state.get("applied_revision") or 0)),
        "applied_digest": str(state.get("applied_digest") or ""),
        "updated_at": state.get("updated_at"),
        "applied_at": state.get("applied_at"),
        "apply_error": str(state.get("apply_error") or ""),
    }
    atomic_write_json(LLM_CONFIG_STATE_FILE, payload)


def _register_desired_unlocked(values: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
    state = _read_state_unlocked()
    digest = llm_config_digest(values)
    changed = False
    if state["revision"] <= 0 or state["digest"] != digest:
        state["revision"] = max(
            int(state["revision"]),
            int(state["applied_revision"]),
        ) + 1
        state["digest"] = digest
        state["updated_at"] = _utc_now()
        state["apply_error"] = ""
        changed = True
    return state, changed


def record_desired_llm_config_locked(
    values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Register desired content while the caller holds the state-file lock."""
    state, changed = _register_desired_unlocked(
        values if values is not None else read_llm_env_values()
    )
    if changed:
        _write_state_unlocked(state)
    return _public_status(state)


def get_llm_config_status(*, register_current: bool = True) -> dict[str, Any]:
    """Return desired/applied state without ever acknowledging an update."""
    with file_lock(LLM_CONFIG_STATE_FILE):
        if register_current:
            state, changed = _register_desired_unlocked(read_llm_env_values())
            if changed:
                _write_state_unlocked(state)
        else:
            state = _read_state_unlocked()
        return _public_status(state)


def _public_status(state: Mapping[str, Any]) -> dict[str, Any]:
    revision = max(0, int(state.get("revision") or 0))
    applied_revision = max(0, int(state.get("applied_revision") or 0))
    digest = str(state.get("digest") or "")
    applied_digest = str(state.get("applied_digest") or "")
    apply_error = str(state.get("apply_error") or "")
    applied = bool(
        revision > 0
        and applied_revision == revision
        and digest
        and applied_digest == digest
        and not apply_error
    )
    return {
        "revision": revision,
        "applied_revision": applied_revision,
        "pending": not applied,
        "applied": applied,
        "status": "applied" if applied else "pending",
        "updated_at": state.get("updated_at"),
        "applied_at": state.get("applied_at"),
        "apply_error": apply_error,
    }


def _environment_mismatches(values: Mapping[str, str]) -> list[str]:
    return sorted(
        key
        for key in LLM_WRITABLE_ENV_KEYS
        if key in values and os.environ.get(key) != values[key]
    )


def apply_pending_llm_config() -> dict[str, Any]:
    """Reload ``.env`` and acknowledge its current revision.

    This function is intentionally called only by the realtime service.  The
    lock order (state, then env) matches the writer and prevents a revision
    from being acknowledged against different file content.
    """
    with file_lock(LLM_CONFIG_STATE_FILE):
        with file_lock(ENV_FILE):
            values = read_llm_env_values()
            state, desired_changed = _register_desired_unlocked(values)
            digest = str(state["digest"])
            already_applied = (
                int(state["applied_revision"]) == int(state["revision"])
                and str(state["applied_digest"]) == digest
                and not str(state.get("apply_error") or "")
            )
            if already_applied and not _environment_mismatches(values):
                if desired_changed:
                    _write_state_unlocked(state)
                return _public_status(state)
            try:
                load_dotenv(force_reload=True)
            except Exception as exc:
                state["applied_digest"] = ""
                state["apply_error"] = f"{type(exc).__name__}: {exc}"
                _write_state_unlocked(state)
                raise
            mismatched = _environment_mismatches(values)
            if mismatched:
                names = ", ".join(mismatched)
                state["applied_digest"] = ""
                state["apply_error"] = (
                    "process environment overrides pending .env keys: "
                    f"{names}"
                )
                _write_state_unlocked(state)
                return _public_status(state)
            state["applied_revision"] = int(state["revision"])
            state["applied_digest"] = digest
            state["applied_at"] = _utc_now()
            state["apply_error"] = ""
            _write_state_unlocked(state)
            return _public_status(state)


async def watch_llm_config(
    *,
    interval_seconds: float | None = None,
) -> None:
    """Continuously apply pending system LLM configuration revisions."""
    if interval_seconds is None:
        raw = str(os.environ.get("DESKBOT_LLM_CONFIG_POLL_SECONDS") or "1").strip()
        try:
            interval_seconds = float(raw)
        except ValueError:
            interval_seconds = 1.0
    interval_seconds = max(0.2, float(interval_seconds))
    last_applied = -1
    last_pending: tuple[int, str] | None = None
    while True:
        try:
            status = apply_pending_llm_config()
            if status["applied"]:
                revision = int(status["applied_revision"])
                if revision != last_applied:
                    logger.info(
                        "[config] realtime LLM configuration applied revision=%d",
                        revision,
                    )
                    last_applied = revision
                last_pending = None
            else:
                pending = (
                    int(status["revision"]),
                    str(status.get("apply_error") or ""),
                )
                if pending != last_pending:
                    logger.warning(
                        "[config] realtime LLM configuration pending "
                        "revision=%d error=%s",
                        pending[0],
                        pending[1] or "reload not completed",
                    )
                    last_pending = pending
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[config] failed to apply pending LLM configuration")
        await asyncio.sleep(interval_seconds)
