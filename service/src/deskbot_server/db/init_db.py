from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.schema import CreateTable

from deskbot_server.atomic_store import file_lock, replace_file
from deskbot_server.db.engine import default_db_path, init_engine
from deskbot_server.db.models import (
    ApiKey,
    Base,
    ControlOperation,
    Device,
    ScheduledTask,
    SettingsTestDaily,
    ToolConfirmation,
    ToolOperation,
    VoiceCloneJob,
    VoiceClonePollThrottle,
)

logger = logging.getLogger("deskbot-server")
_seed_lock = threading.Lock()


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_text(offset: timedelta | None = None) -> str:
    value = datetime.now(timezone.utc) + (offset or timedelta())
    return value.isoformat(sep=" ")


def _columns(engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _database_path(engine) -> Path:
    engine_database = str(getattr(engine.url, "database", "") or "").strip()
    return (
        Path(engine_database).expanduser().resolve()
        if engine_database
        else default_db_path()
    )


@contextmanager
def _schema_migration_lock(engine):
    """Serialize 5050/9000 schema startup across Windows processes.

    复用 ``atomic_store.file_lock`` 的非阻塞轮询语义（0.1s 步长、60s 超时）；
    锁文件名与旧实现相同（``<db>.schema-migration.lock``）。
    """

    lock_target = _database_path(engine).with_suffix(".schema-migration")
    stack = ExitStack()
    try:
        stack.enter_context(file_lock(lock_target, timeout=60.0))
    except TimeoutError:
        raise TimeoutError(
            "database schema migration lock timed out: "
            f"{lock_target}.lock"
        ) from None
    with stack:
        yield


def _unique_column_sets(engine, table_name: str) -> set[tuple[str, ...]]:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    result = {
        tuple(str(name) for name in (constraint.get("column_names") or ()))
        for constraint in inspector.get_unique_constraints(table_name)
    }
    result.update(
        tuple(str(name) for name in (index.get("column_names") or ()))
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    return {columns for columns in result if columns}


def _needs_rebuild(
    engine,
    table,
    *,
    unique_columns: tuple[str, ...] | None = None,
) -> bool:
    existing = _columns(engine, table.name)
    if not existing:
        return False
    if existing != set(table.c.keys()):
        return True
    if unique_columns is None:
        return False
    return unique_columns not in _unique_column_sets(engine, table.name)


def _legacy_schema_present(engine) -> bool:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if table_names.intersection(
        {
            "users",
            "login_throttles",
            "device_pairing_challenges",
            "usage_daily_device",
        }
    ):
        return True
    obsolete_by_table = {
        "api_keys": {"user_id"},
        "devices": {
            "owner_user_id",
            "access_token_hash",
            "lifecycle_state",
            "claim_id",
            "claim_generation",
            "claimed_at",
            "revoking_at",
            "retired_data_path",
        },
        "scheduled_tasks": {"device_id", "run_at"},
        "tool_operations": {
            "device_id",
            "owner_user_id",
            "initiator_scope",
            "initiator_subject_id",
        },
        "tool_confirmations": {
            "device_id",
            "owner_user_id",
            "initiator_scope",
            "initiator_subject_id",
        },
        "control_operations": {"owner_user_id"},
        "voice_clone_jobs": {"owner_user_id", "device_id"},
        "voice_clone_poll_throttles": {"scope", "scope_key"},
        "settings_test_daily": {"scope", "scope_key"},
    }
    if any(
        table_name in table_names
        and bool(_columns(engine, table_name).intersection(obsolete_columns))
        for table_name, obsolete_columns in obsolete_by_table.items()
    ):
        return True
    from deskbot_server.auth.api_key_service import FREE_FILE_KEY_ID

    try:
        with engine.connect() as conn:
            if (
                "api_keys" in table_names
                and "id" in _columns(engine, "api_keys")
                and conn.execute(
                    text(
                        "SELECT 1 FROM api_keys "
                        "WHERE id IS NULL OR id <> :free_id LIMIT 1"
                    ),
                    {"free_id": FREE_FILE_KEY_ID},
                ).first()
                is not None
            ):
                return True
            if (
                "usage_daily" in table_names
                and "api_key_id" in _columns(engine, "usage_daily")
                and conn.execute(
                    text(
                        "SELECT 1 FROM usage_daily "
                        "WHERE api_key_id IS NULL "
                        "OR api_key_id <> :free_id LIMIT 1"
                    ),
                    {"free_id": FREE_FILE_KEY_ID},
                ).first()
                is not None
            ):
                return True
    except Exception:
        logger.exception("检测旧多 API-Key 数据失败，将保守创建迁移备份")
        return True
    # Also protect installations that already removed the old columns but
    # still carry device/owner-era uniqueness constraints.
    rebuild_specs = (
        (ApiKey.__table__, ("key_hash",)),
        (Device.__table__, ("device_id",)),
        (ScheduledTask.__table__, None),
        (ToolOperation.__table__, ("tool_name", "operation_id")),
        (ToolConfirmation.__table__, None),
        (ControlOperation.__table__, ("operation_id",)),
        (VoiceCloneJob.__table__, ("speaker_id",)),
        (VoiceClonePollThrottle.__table__, ("job_key",)),
        (SettingsTestDaily.__table__, ("usage_date",)),
    )
    return any(
        _needs_rebuild(engine, table, unique_columns=unique_columns)
        for table, unique_columns in rebuild_specs
    )


def _ensure_pre_local_scope_backup(engine) -> None:
    """Create one consistent, recoverable snapshot before destructive DDL."""

    if not _legacy_schema_present(engine):
        return
    _ensure_backup_snapshot(engine, suffix=".pre-local-scope.bak")


def _ensure_backup_snapshot(engine, *, suffix: str) -> None:
    """Create one consistent, recoverable snapshot named by *suffix*."""

    source_path = _database_path(engine)
    backup_path = source_path.with_suffix(source_path.suffix + suffix)

    def backup_is_valid(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        connection = sqlite3.connect(str(path))
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return bool(result and str(result[0]).lower() == "ok")
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()

    if backup_path.exists() and backup_is_valid(backup_path):
        return
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = backup_path.with_name(
        f".{backup_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    raw = None
    destination = None
    try:
        raw = engine.raw_connection()
        destination = sqlite3.connect(str(temporary_path))
        source = getattr(raw, "driver_connection", None)
        if source is None:
            source = raw.connection
        source.backup(destination)
        destination.commit()
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError("database backup integrity check failed")
        destination.close()
        destination = None
        raw.close()
        raw = None
        # atomic_store 的替换带 Windows 共享冲突（PermissionError）退避。
        replace_file(temporary_path, backup_path)
    except Exception:
        if destination is not None:
            destination.close()
        if raw is not None:
            raw.close()
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    logger.warning(
        "已在迁移前创建可恢复数据库快照：%s",
        backup_path,
    )


def _copy_common(row: dict[str, Any], table) -> dict[str, Any]:
    return {
        column.name: row.get(column.name)
        for column in table.columns
        if column.name in row
    }


def _insert_mapping(conn, table_name: str, payload: dict[str, Any]) -> None:
    columns = list(payload)
    if not columns:
        return
    quoted = ", ".join(f'"{column}"' for column in columns)
    values = ", ".join(f":{column}" for column in columns)
    conn.execute(
        text(f'INSERT INTO "{table_name}" ({quoted}) VALUES ({values})'),
        payload,
    )


def _rebuild_table(
    engine,
    table,
    transform: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]],
) -> None:
    """Atomically rebuild one SQLite table from an explicit row projection."""

    table_name = table.name
    temporary_name = f"__deskbot_local_{table_name}"
    with engine.connect() as conn:
        foreign_keys_enabled = bool(
            conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        )
        conn.commit()
        if foreign_keys_enabled:
            # SQLite cannot rebuild a referenced parent table while FK
            # enforcement is active.  The full migration runs under the
            # cross-process schema lock and validates all FKs after seeding.
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
        try:
            with conn.begin():
                conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{temporary_name}"')
                temporary_metadata = MetaData()
                temporary = table.to_metadata(
                    temporary_metadata,
                    name=temporary_name,
                )
                conn.execute(CreateTable(temporary))
                rows = [
                    dict(row)
                    for row in conn.execute(
                        text(f'SELECT * FROM "{table_name}"')
                    ).mappings()
                ]
                for payload in transform(rows):
                    _insert_mapping(conn, temporary_name, payload)
                conn.exec_driver_sql(f'DROP TABLE "{table_name}"')
                conn.exec_driver_sql(
                    f'ALTER TABLE "{temporary_name}" RENAME TO "{table_name}"'
                )
                for index in table.indexes:
                    index.create(bind=conn, checkfirst=True)
        finally:
            if foreign_keys_enabled:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.commit()


def _dedupe(
    value: str,
    used: set[str],
    *,
    seed: str,
    maximum: int,
) -> str:
    base = (value or "").strip() or _new_id()
    candidate = base[:maximum]
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix_seed = (seed or uuid.uuid4().hex).replace("-", "")[:12]
    counter = 1
    while True:
        suffix = f"-legacy-{suffix_seed}-{counter}"
        candidate = f"{base[: max(1, maximum - len(suffix))]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def _migrate_api_keys_schema(engine) -> None:
    table = ApiKey.__table__
    if not _needs_rebuild(engine, table, unique_columns=("key_hash",)):
        return

    from deskbot_server.auth.api_key_service import FREE_FILE_KEY_ID

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        for row in rows:
            # Managed/multi-key rows have no runtime entry point anymore.  Do
            # not copy them into the rebuilt unique-key table only to delete
            # them in the following data migration (which could also fail on
            # duplicate hashes in malformed legacy databases).
            if str(row.get("id") or "") != FREE_FILE_KEY_ID:
                continue
            payload = _copy_common(row, table)
            row_id = str(payload.get("id") or _new_id())
            payload.update(
                id=row_id,
                name=str(payload.get("name") or "default")[:128],
                key_hash=str(payload.get("key_hash") or f"legacy-{row_id}"),
                key_prefix=str(payload.get("key_prefix") or "legacy")[:16],
                is_free=bool(payload.get("is_free") or False),
                daily_quota_bytes=max(0, int(payload.get("daily_quota_bytes") or 0)),
                is_active=bool(
                    True if payload.get("is_active") is None else payload["is_active"]
                ),
                created_at=payload.get("created_at") or _utc_text(),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_single_api_key_data(engine) -> None:
    """Discard legacy managed keys and their usage; only the file key remains."""

    from deskbot_server.auth.api_key_service import FREE_FILE_KEY_ID

    table_names = set(inspect(engine).get_table_names())
    removed_usage = 0
    removed_keys = 0
    with engine.begin() as conn:
        if "usage_daily_device" in table_names:
            # This legacy per-device ledger is dropped later in the same
            # migration.  Empty it first so FK-enabled databases can safely
            # remove their managed parent keys.
            conn.exec_driver_sql('DELETE FROM "usage_daily_device"')
        if (
            "usage_daily" in table_names
            and "api_key_id" in _columns(engine, "usage_daily")
        ):
            result = conn.execute(
                text(
                    "DELETE FROM usage_daily "
                    "WHERE api_key_id IS NULL "
                    "OR api_key_id <> :free_id"
                ),
                {"free_id": FREE_FILE_KEY_ID},
            )
            removed_usage = max(0, int(result.rowcount or 0))
        if "api_keys" in table_names and "id" in _columns(engine, "api_keys"):
            result = conn.execute(
                text("DELETE FROM api_keys WHERE id <> :free_id"),
                {"free_id": FREE_FILE_KEY_ID},
            )
            removed_keys = max(0, int(result.rowcount or 0))
    if removed_usage or removed_keys:
        logger.warning(
            "已清理旧多 API-Key 数据：keys=%d usage_rows=%d",
            removed_keys,
            removed_usage,
        )


def _migrate_devices_schema(engine) -> None:
    """Reduce devices to a physical-connection history directory."""

    table = Device.__table__
    if not _needs_rebuild(engine, table, unique_columns=("device_id",)):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            device_id = str(row.get("device_id") or "").strip()
            if not device_id:
                continue
            payload = {
                "id": str(row.get("id") or _new_id()),
                "device_id": device_id[:128],
                "display_name": row.get("display_name"),
                "last_seen_at": row.get("last_seen_at"),
                "created_at": (
                    row.get("created_at")
                    or row.get("claimed_at")
                    or _utc_text()
                ),
            }
            previous = merged.get(payload["device_id"])
            if previous is None:
                merged[payload["device_id"]] = payload
                continue
            if not previous.get("display_name") and payload.get("display_name"):
                previous["display_name"] = payload["display_name"]
            if str(payload.get("last_seen_at") or "") > str(
                previous.get("last_seen_at") or ""
            ):
                previous["last_seen_at"] = payload["last_seen_at"]
            if str(payload["created_at"]) < str(previous["created_at"]):
                previous["created_at"] = payload["created_at"]
        return merged.values()

    _rebuild_table(engine, table, transform)


def _migrate_scheduled_tasks_schema(engine) -> None:
    """Move scheduled tasks from per-device scope to the local workspace."""

    table = ScheduledTask.__table__
    if not _needs_rebuild(engine, table):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        for row in rows:
            payload = _copy_common(row, table)
            payload.update(
                id=str(payload.get("id") or _new_id()),
                description=str(payload.get("description") or "") or "legacy task",
                cron_expr=str(payload.get("cron_expr") or "* * * * *"),
                task_kind=str(payload.get("task_kind") or "once"),
                enabled=bool(True if payload.get("enabled") is None else payload["enabled"]),
                next_run_at=(
                    payload.get("next_run_at")
                    or row.get("run_at")
                    or _utc_text()
                ),
                status=(
                    "active"
                    if str(payload.get("status") or "active") == "pending"
                    else str(payload.get("status") or "active")
                ),
                created_at=payload.get("created_at") or _utc_text(),
                attempt_count=max(0, int(payload.get("attempt_count") or 0)),
                offline_wait_count=max(0, int(payload.get("offline_wait_count") or 0)),
                occurrence_id=str(payload.get("occurrence_id") or _new_id()),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_tool_operations_schema(engine) -> None:
    table = ToolOperation.__table__
    if not _needs_rebuild(
        engine,
        table,
        unique_columns=("tool_name", "operation_id"),
    ):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        used: dict[str, set[str]] = {}
        for row in rows:
            payload = _copy_common(row, table)
            row_id = str(payload.get("id") or _new_id())
            tool_name = str(payload.get("tool_name") or "legacy")[:64]
            operation_id = _dedupe(
                str(payload.get("operation_id") or ""),
                used.setdefault(tool_name, set()),
                seed=row_id,
                maximum=128,
            )
            payload.update(
                id=row_id,
                tool_name=tool_name,
                operation_id=operation_id,
                status=str(payload.get("status") or "running")[:16],
                created_at=payload.get("created_at") or _utc_text(),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_tool_confirmations_schema(engine) -> None:
    table = ToolConfirmation.__table__
    if not _needs_rebuild(engine, table):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        for row in rows:
            payload = _copy_common(row, table)
            payload.update(
                id=str(payload.get("id") or _new_id()),
                tool_name=str(payload.get("tool_name") or "legacy")[:64],
                payload_hash=str(
                    payload.get("payload_hash") or uuid.uuid4().hex
                )[:64],
                created_at=payload.get("created_at") or _utc_text(),
                expires_at=payload.get("expires_at") or _utc_text(),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_tool_safety_schema(engine) -> None:
    """Remove tenant/actor bindings while retaining local safety ledgers."""

    _migrate_tool_operations_schema(engine)
    _migrate_tool_confirmations_schema(engine)


def _migrate_control_operations_schema(engine) -> None:
    table = ControlOperation.__table__
    if not _needs_rebuild(engine, table, unique_columns=("operation_id",)):
        return
    valid_statuses = {
        "accepted",
        "running",
        "completed",
        "failed",
        "cancelled",
        "timeout",
    }

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        used: set[str] = set()
        for row in rows:
            payload = _copy_common(row, table)
            row_id = str(payload.get("id") or _new_id())
            status = str(payload.get("status") or "accepted")
            if status == "pending":
                status = "accepted"
            elif status not in valid_statuses:
                status = "failed"
            payload.update(
                id=row_id,
                operation_id=_dedupe(
                    str(payload.get("operation_id") or ""),
                    used,
                    seed=row_id,
                    maximum=128,
                ),
                kind=str(payload.get("kind") or "legacy")[:64],
                payload_hash=str(
                    payload.get("payload_hash") or uuid.uuid4().hex
                )[:64],
                request_id=str(
                    payload.get("request_id") or uuid.uuid4().hex[:16]
                )[:64],
                status=status,
                deadline_at=(
                    payload.get("deadline_at")
                    or _utc_text(timedelta(minutes=15))
                ),
                created_at=payload.get("created_at") or _utc_text(),
                updated_at=(
                    payload.get("updated_at")
                    or payload.get("created_at")
                    or _utc_text()
                ),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_voice_clone_jobs_schema(engine) -> None:
    table = VoiceCloneJob.__table__
    if not _needs_rebuild(engine, table, unique_columns=("speaker_id",)):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        used: set[str] = set()
        for row in rows:
            payload = _copy_common(row, table)
            row_id = str(payload.get("id") or _new_id())
            speaker_id = _dedupe(
                str(payload.get("speaker_id") or f"legacy-{row_id}"),
                used,
                seed=row_id,
                maximum=128,
            )
            payload.update(
                id=row_id,
                speaker_id=speaker_id,
                display_name=str(
                    payload.get("display_name") or speaker_id
                )[:128],
                state=str(payload.get("state") or "submitted")[:24],
                ready=bool(payload.get("ready") or False),
                created_at=payload.get("created_at") or _utc_text(),
                updated_at=(
                    payload.get("updated_at")
                    or payload.get("created_at")
                    or _utc_text()
                ),
            )
            yield payload

    _rebuild_table(engine, table, transform)


def _migrate_voice_poll_throttles_schema(engine) -> None:
    table = VoiceClonePollThrottle.__table__
    if not _needs_rebuild(engine, table, unique_columns=("job_key",)):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if "job_key" in row:
                job_key = str(row.get("job_key") or "").strip()
            elif str(row.get("scope") or "") == "job":
                job_key = str(row.get("scope_key") or "").strip()
            else:
                continue
            if not job_key:
                continue
            payload = {
                "id": str(row.get("id") or _new_id()),
                "job_key": job_key[:128],
                "last_polled_at": row.get("last_polled_at") or _utc_text(),
            }
            previous = latest.get(payload["job_key"])
            if previous is None or str(payload["last_polled_at"]) > str(
                previous["last_polled_at"]
            ):
                latest[payload["job_key"]] = payload
        return latest.values()

    _rebuild_table(engine, table, transform)


def _migrate_settings_test_schema(engine) -> None:
    table = SettingsTestDaily.__table__
    if not _needs_rebuild(engine, table, unique_columns=("usage_date",)):
        return

    def transform(rows: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        # Old user/IP rows were incremented together; maximum avoids doubling.
        by_date: dict[str, dict[str, Any]] = {}
        for row in rows:
            usage_date = str(row.get("usage_date") or "").strip()
            if not usage_date:
                continue
            count = max(0, int(row.get("count") or 0))
            current = by_date.get(usage_date)
            if current is None or count > int(current["count"]):
                by_date[usage_date] = {
                    "id": str(row.get("id") or _new_id()),
                    "usage_date": usage_date,
                    "count": count,
                }
        return by_date.values()

    _rebuild_table(engine, table, transform)


def _drop_obsolete_identity_tables(engine) -> None:
    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table_name in (
            "usage_daily_device",
            "device_pairing_challenges",
            "login_throttles",
            "users",
        ):
            if table_name in existing:
                conn.exec_driver_sql(f'DROP TABLE "{table_name}"')


def _migrate_legacy_schema(engine) -> None:
    """One-way migration from legacy account/device scope to one local scope."""

    _ensure_pre_local_scope_backup(engine)
    _migrate_single_api_key_data(engine)
    _migrate_api_keys_schema(engine)
    _migrate_devices_schema(engine)
    _migrate_scheduled_tasks_schema(engine)
    _migrate_tool_safety_schema(engine)
    _migrate_control_operations_schema(engine)
    _migrate_voice_clone_jobs_schema(engine)
    _migrate_voice_poll_throttles_schema(engine)
    _migrate_settings_test_schema(engine)
    _drop_obsolete_identity_tables(engine)


def _migrate_scheduled_tasks_drop_legacy_run_at(engine) -> None:
    """Compatibility entry point retained for older migration tests."""

    _migrate_scheduled_tasks_schema(engine)


# ---------------------------------------------------------------------------
# 带版本号的一次性数据迁移（挂在 SQLite ``PRAGMA user_version`` 上）。
# 与上面的幂等 schema 重建不同：这些迁移改写行内数据、不能靠列差异检测，
# 因此只能各跑一次。执行始终处于 ``_schema_migration_lock`` 之内。
# ---------------------------------------------------------------------------

#: v1: scheduled_tasks 时间字段从 naive-CST 统一为 naive-UTC（core.clock 口径）。
_DB_USER_VERSION = 1

_SCHEDULED_TASK_CST_COLUMNS = (
    "next_run_at",
    "executed_at",
    "lease_expires_at",
    "first_due_at",
)


def _read_user_version(engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def _migrate_scheduled_tasks_naive_cst_to_utc(engine) -> None:
    """One-time shift: scheduled_tasks 混存修复（naive-CST 字段 -8h 转 UTC）。

    历史约定：``created_at`` 由 ``db.models._utcnow`` 写入（naive-UTC 墙钟），
    而 ``next_run_at/executed_at/lease_expires_at/first_due_at`` 由旧
    ``cst_now`` 写入（naive-CST 墙钟）。统一为 naive-UTC 后：

    - 无时区后缀的值按 CST 墙钟解释，减 8 小时（上海无夏令时，偏移恒定）；
    - 带显式 ``+HH:MM`` 后缀的值（旧 ``_utc_text`` 迁移兜底所写）本就无歧义，
      仅经 ``datetime()`` 规范化成 naive-UTC，不做偏移；
    - 无法解析的值保持原样（``COALESCE`` 兜底），不让迁移中断启动。
    """

    if "scheduled_tasks" not in set(inspect(engine).get_table_names()):
        return
    with engine.connect() as conn:
        has_rows = conn.exec_driver_sql(
            "SELECT 1 FROM scheduled_tasks LIMIT 1"
        ).first()
    if has_rows is None:
        return
    _ensure_backup_snapshot(engine, suffix=".pre-utc-clock.bak")
    with engine.begin() as conn:
        for column in _SCHEDULED_TASK_CST_COLUMNS:
            conn.exec_driver_sql(
                f'''
                UPDATE scheduled_tasks SET "{column}" = COALESCE(
                    CASE
                        WHEN "{column}" IS NULL THEN NULL
                        WHEN instr(substr("{column}", 12), '+') > 0
                            THEN datetime("{column}")
                        ELSE datetime("{column}", '-8 hours')
                    END,
                    "{column}"
                )
                '''
            )
        # created_at 已是 UTC；仅把带显式偏移后缀的旧值规范化成 naive-UTC，
        # 使全表字符串可作字典序比较。
        conn.exec_driver_sql(
            """
            UPDATE scheduled_tasks SET created_at = COALESCE(
                CASE
                    WHEN created_at IS NULL THEN NULL
                    WHEN instr(substr(created_at, 12), '+') > 0
                        THEN datetime(created_at)
                    ELSE created_at
                END,
                created_at
            )
            """
        )
    logger.warning("scheduled_tasks 时间口径已统一为 UTC（user_version=1）")


def _run_versioned_migrations(engine) -> None:
    version = _read_user_version(engine)
    if version >= _DB_USER_VERSION:
        return
    if version < 1:
        _migrate_scheduled_tasks_naive_cst_to_utc(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {_DB_USER_VERSION:d}")


def _ensure_model_indexes(engine) -> None:
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def init_database() -> None:
    engine = init_engine()
    with _schema_migration_lock(engine):
        _migrate_legacy_schema(engine)
        Base.metadata.create_all(bind=engine)
        _run_versioned_migrations(engine)
        _ensure_model_indexes(engine)
        # Keep first-run .free_api_key creation inside the same cross-process
        # lock used by the 5050/9000 schema startup paths.
        _seed_free_api_key()
        with engine.connect() as conn:
            violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError(
                f"database migration left foreign-key violations: {violations[:5]}"
            )

    from deskbot_server.application.control_operations import (
        maybe_purge_expired_control_operations,
        recover_stale_control_operations,
    )

    recover_stale_control_operations()
    maybe_purge_expired_control_operations(force=True)
    from deskbot_server.playback_receipts import (
        maybe_purge_expired_playback_receipts,
    )

    maybe_purge_expired_playback_receipts(force=True)


def _seed_free_api_key() -> None:
    from deskbot_server.auth.api_key_service import (
        ensure_free_api_key_file,
        ensure_free_usage_placeholder,
    )

    with _seed_lock:
        cfg, created = ensure_free_api_key_file()
        if created:
            logger.warning(
                "已创建免费 API Key（每日 1GB 配额），prefix=%s；完整 Key 见 data/.free_api_key",
                cfg.api_key[:12],
            )
        ensure_free_usage_placeholder()
