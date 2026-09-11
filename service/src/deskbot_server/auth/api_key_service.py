"""本机进程间凭证（free_api_key 文件名保留兼容）。

Web 控制台等本机进程通过 ``data/.free_api_key`` 中的凭证访问 Core；
它不是商业化的"免费额度 Key"，每日字节配额只是本机资源保护。
文件名与 ``FREE_*`` 标识符为存量兼容保留。
"""
from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from deskbot_server.atomic_store import atomic_write_text, file_lock
from deskbot_server.core.clock import utcnow as _utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import ApiKey, UsageDaily

FREE_DAILY_QUOTA_BYTES = 1_073_741_824  # 1 GiB
FREE_FILE_KEY_ID = "00000000-0000-4000-8000-000000000001"
FREE_FILE_KEY_HASH_SENTINEL = "__file_based_free_key__"
USAGE_CATEGORIES = ("asr", "face", "llm", "tts")


class QuotaExceededError(Exception):
    pass


@dataclass(frozen=True)
class ApiKeyAuth:
    api_key_id: str
    is_free: bool
    daily_quota_bytes: int
    name: str


@dataclass(frozen=True)
class FreeApiKeyConfig:
    name: str
    api_key: str
    daily_quota_bytes: int


def _today() -> date:
    return _utcnow().date()


def _free_api_key_path() -> "Path":
    from deskbot_server.db.engine import default_db_path

    return default_db_path().parent / ".free_api_key"


def read_free_api_key_config() -> FreeApiKeyConfig | None:
    """读取 ``data/.free_api_key``（本机进程间凭证唯一来源，不查数据库）。"""
    path = _free_api_key_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    name = "本机进程间凭证"
    api_key = ""
    daily_quota_bytes = FREE_DAILY_QUOTA_BYTES
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "name":
            name = val or name
        elif key == "api_key":
            api_key = val
        elif key == "daily_quota_bytes":
            try:
                daily_quota_bytes = max(0, int(val))
            except ValueError:
                pass
    if not api_key:
        return None
    return FreeApiKeyConfig(name=name, api_key=api_key, daily_quota_bytes=daily_quota_bytes)


def read_free_api_key_raw() -> str | None:
    """读取 ``data/.free_api_key`` 中的本机凭证（供本机调试台订阅 WS）。"""
    cfg = read_free_api_key_config()
    return cfg.api_key if cfg else None


def _write_free_api_key_file_unlocked(
    path: Path,
    raw_key: str,
    *,
    daily_quota_bytes: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        f"name=本机进程间凭证\napi_key={raw_key}\n"
        f"daily_quota_bytes={daily_quota_bytes}\n",
        mode=0o600,
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_free_api_key_file(
    raw_key: str,
    *,
    daily_quota_bytes: int = FREE_DAILY_QUOTA_BYTES,
) -> None:
    """写入 ``data/.free_api_key``（首次启动或手动重置时使用）。"""
    path = _free_api_key_path()
    with file_lock(path):
        _write_free_api_key_file_unlocked(
            path,
            raw_key,
            daily_quota_bytes=daily_quota_bytes,
        )


def ensure_free_api_key_file() -> tuple[FreeApiKeyConfig, bool]:
    """Return the one PC-local credential, creating its file when absent."""

    path = _free_api_key_path()
    with file_lock(path):
        # Re-read only after taking the cross-process lock: 5050 and 9000 may
        # reach first-run initialization at the same time.
        existing = read_free_api_key_config()
        if existing is not None:
            return existing, False
        raw_key = f"odk_free_{secrets.token_urlsafe(24)}"
        _write_free_api_key_file_unlocked(
            path,
            raw_key,
            daily_quota_bytes=FREE_DAILY_QUOTA_BYTES,
        )
        return FreeApiKeyConfig(
            name="本机进程间凭证",
            api_key=raw_key,
            daily_quota_bytes=FREE_DAILY_QUOTA_BYTES,
        ), True


def ensure_free_usage_placeholder() -> None:
    """确保用量统计占位行存在（Key 本身仅存于文件，不入库）。"""
    cfg = read_free_api_key_config()
    quota = cfg.daily_quota_bytes if cfg else FREE_DAILY_QUOTA_BYTES
    session = get_session()
    try:
        row = session.get(ApiKey, FREE_FILE_KEY_ID)
        if row is not None:
            row.name = "本机进程间凭证"
            row.key_hash = FREE_FILE_KEY_HASH_SENTINEL
            row.key_prefix = "odk_free_"
            row.is_free = True
            row.daily_quota_bytes = quota
            row.is_active = True
            session.commit()
            return
        row = ApiKey(
            id=FREE_FILE_KEY_ID,
            name="本机进程间凭证",
            key_hash=FREE_FILE_KEY_HASH_SENTINEL,
            key_prefix="odk_free_",
            is_free=True,
            daily_quota_bytes=quota,
            is_active=True,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            # Only suppress the expected same-ID insert race.  A different
            # uniqueness/integrity failure must remain visible to callers.
            concurrent = session.get(ApiKey, FREE_FILE_KEY_ID)
            if concurrent is None:
                raise
    finally:
        session.close()


def _authenticate_free_file_key(token: str) -> ApiKeyAuth | None:
    cfg = read_free_api_key_config()
    if cfg is None or not hmac.compare_digest(token, cfg.api_key):
        return None
    ensure_free_usage_placeholder()
    session = get_session()
    try:
        row = session.get(ApiKey, FREE_FILE_KEY_ID)
        if row is not None:
            row.last_used_at = _utcnow()
            session.commit()
        return ApiKeyAuth(
            api_key_id=FREE_FILE_KEY_ID,
            is_free=True,
            daily_quota_bytes=cfg.daily_quota_bytes,
            name=cfg.name,
        )
    finally:
        session.close()


def authenticate_api_key(raw_key: str) -> ApiKeyAuth | None:
    token = (raw_key or "").strip()
    if not token:
        return None
    return _authenticate_free_file_key(token)


def api_key_auth_is_current(raw_key: str, expected: ApiKeyAuth) -> bool:
    """Read-only revalidation for long-lived WebSocket sessions.

    Unlike ``authenticate_api_key`` this does not update ``last_used_at`` on
    every heartbeat, avoiding a continuous SQLite write workload.
    """

    token = (raw_key or "").strip()
    if not token:
        return False
    if expected.api_key_id != FREE_FILE_KEY_ID:
        return False
    cfg = read_free_api_key_config()
    return bool(
        cfg is not None
        and hmac.compare_digest(token, cfg.api_key)
    )


def _free_key_quota_bytes() -> int:
    cfg = read_free_api_key_config()
    return cfg.daily_quota_bytes if cfg else FREE_DAILY_QUOTA_BYTES


def check_quota(api_key_id: str, additional_bytes: int = 0) -> None:
    if api_key_id != FREE_FILE_KEY_ID:
        raise PermissionError("invalid_api_key")
    quota = _free_key_quota_bytes()
    if quota <= 0:
        return
    session = get_session()
    try:
        usage = session.scalar(
            select(UsageDaily).where(
                UsageDaily.api_key_id == api_key_id,
                UsageDaily.usage_date == _today(),
            )
        )
        used = usage.total_bytes if usage else 0
        if used + max(0, additional_bytes) > quota:
            raise QuotaExceededError("quota_exhausted")
    finally:
        session.close()


def record_usage_checked(
    api_key_id: str,
    category: str,
    byte_count: int,
) -> None:
    if byte_count <= 0:
        return
    if category not in USAGE_CATEGORIES:
        return
    _record_usage_batch_atomic(
        api_key_id,
        {category: int(byte_count)},
        enforce_quota=True,
    )


def record_usage_batch_checked(
    api_key_id: str,
    *,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> None:
    """Atomically consume one turn from the PC-local API-key quota."""

    _record_usage_batch_atomic(
        api_key_id,
        {
            "asr": max(0, int(asr_bytes)),
            "face": max(0, int(face_bytes)),
            "llm": max(0, int(llm_bytes)),
            "tts": max(0, int(tts_bytes)),
        },
        enforce_quota=True,
    )


def adjust_usage_batch_checked(
    api_key_id: str,
    *,
    asr_bytes: int = 0,
    face_bytes: int = 0,
    llm_bytes: int = 0,
    tts_bytes: int = 0,
) -> None:
    """Atomically settle a prior reservation with signed category deltas."""

    _record_usage_batch_atomic(
        api_key_id,
        {
            "asr": int(asr_bytes),
            "face": int(face_bytes),
            "llm": int(llm_bytes),
            "tts": int(tts_bytes),
        },
        enforce_quota=True,
        allow_negative=True,
    )


def _record_usage_batch_atomic(
    api_key_id: str,
    deltas: dict[str, int],
    *,
    enforce_quota: bool,
    allow_negative: bool = False,
) -> None:
    """Reserve and update the PC-local daily quota under one SQLite lock."""

    if api_key_id != FREE_FILE_KEY_ID:
        raise PermissionError("invalid_api_key")
    clean = {
        category: (
            int(deltas.get(category) or 0)
            if allow_negative
            else max(0, int(deltas.get(category) or 0))
        )
        for category in USAGE_CATEGORIES
    }
    if not any(clean.values()):
        return

    quota = _free_key_quota_bytes()
    from deskbot_server.db.engine import init_engine

    raw = init_engine().raw_connection()
    cursor = raw.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT is_active FROM api_keys WHERE id = ?",
            (api_key_id,),
        )
        key_row = cursor.fetchone()
        if key_row is None:
            raise PermissionError("invalid_api_key")
        usage_date = _today().isoformat()
        cursor.execute(
            "SELECT asr_bytes, face_bytes, llm_bytes, tts_bytes "
            "FROM usage_daily WHERE api_key_id = ? AND usage_date = ?",
            (api_key_id, usage_date),
        )
        current_row = cursor.fetchone()
        current = [
            int(value or 0) for value in current_row
        ] if current_row else [0, 0, 0, 0]
        proposed = [
            max(0, current[index] + clean[category])
            for index, category in enumerate(("asr", "face", "llm", "tts"))
        ]
        used = sum(current)
        proposed_total = sum(proposed)
        if not bool(key_row[0]) and proposed_total > used:
            raise PermissionError("invalid_api_key")
        if enforce_quota and quota > 0 and proposed_total > quota:
            raise QuotaExceededError("quota_exhausted")

        cursor.execute(
            "INSERT OR IGNORE INTO usage_daily "
            "(id, api_key_id, usage_date, asr_bytes, face_bytes, llm_bytes, tts_bytes) "
            "VALUES (?, ?, ?, 0, 0, 0, 0)",
            (str(uuid.uuid4()), api_key_id, usage_date),
        )
        cursor.execute(
            "UPDATE usage_daily SET "
            "asr_bytes = MAX(0, asr_bytes + ?), "
            "face_bytes = MAX(0, face_bytes + ?), "
            "llm_bytes = MAX(0, llm_bytes + ?), "
            "tts_bytes = MAX(0, tts_bytes + ?) "
            "WHERE api_key_id = ? AND usage_date = ?",
            (
                clean["asr"],
                clean["face"],
                clean["llm"],
                clean["tts"],
                api_key_id,
                usage_date,
            ),
        )

        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        cursor.close()
        raw.close()


def _aggregate_usage_rows(rows) -> dict:
    totals = _empty_totals()
    for usage in rows:
        totals["asr_bytes"] += int(usage.asr_bytes)
        totals["face_bytes"] += int(usage.face_bytes)
        totals["llm_bytes"] += int(usage.llm_bytes)
        totals["tts_bytes"] += int(usage.tts_bytes)
        totals["total_bytes"] += usage.total_bytes
    return totals


def get_local_usage_today() -> dict:
    """Aggregate today's provider usage for this PC."""
    session = get_session()
    try:
        rows = session.scalars(
            select(UsageDaily).where(
                UsageDaily.api_key_id == FREE_FILE_KEY_ID,
                UsageDaily.usage_date == _today(),
            )
        ).all()
        for row in rows:
            session.expunge(row)
    finally:
        session.close()
    return _aggregate_usage_rows(rows)


def get_local_usage_summary(*, days: int = 7) -> dict:
    from datetime import timedelta

    start = _today() - timedelta(days=max(1, days) - 1)
    session = get_session()
    try:
        key_rows = session.scalars(
            select(UsageDaily)
            .where(
                UsageDaily.api_key_id == FREE_FILE_KEY_ID,
                UsageDaily.usage_date >= start,
            )
            .order_by(UsageDaily.usage_date.desc())
        ).all()
    finally:
        session.close()

    stats = {
        "api_key_id": FREE_FILE_KEY_ID,
        "days": [],
        "asr_bytes": 0,
        "face_bytes": 0,
        "llm_bytes": 0,
        "tts_bytes": 0,
        "total_bytes": 0,
    }
    for usage in key_rows:
        day_row = {
            "date": usage.usage_date.isoformat(),
            "asr_bytes": int(usage.asr_bytes),
            "face_bytes": int(usage.face_bytes),
            "llm_bytes": int(usage.llm_bytes),
            "tts_bytes": int(usage.tts_bytes),
            "total_bytes": usage.total_bytes,
        }
        stats["days"].append(day_row)
        for field in (
            "asr_bytes",
            "face_bytes",
            "llm_bytes",
            "tts_bytes",
            "total_bytes",
        ):
            stats[field] += day_row[field]

    totals = _aggregate_usage_rows(key_rows)
    cfg = read_free_api_key_config()
    key_stats = (
        [
            {
                "name": cfg.name,
                "key_prefix": cfg.api_key[:12],
                **stats,
            }
        ]
        if cfg is not None and key_rows
        else []
    )
    return {"key_stats": key_stats, "totals": totals}


def _empty_totals() -> dict:
    return {"asr_bytes": 0, "face_bytes": 0, "llm_bytes": 0, "tts_bytes": 0, "total_bytes": 0}


def extract_api_key_from_query(qargs: dict) -> str | None:
    for key in ("api_key", "apikey", "key"):
        val = (qargs.get(key) or "").strip()
        if val:
            return val
    return None


def extract_api_key_from_headers(headers) -> str | None:
    if headers is None:
        return None
    raw = (headers.get("X-API-Key") or headers.get("x-api-key") or "").strip()
    if raw:
        return raw
    auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
