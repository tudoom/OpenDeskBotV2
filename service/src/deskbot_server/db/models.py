from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from deskbot_server.core.clock import utcnow as _utcnow


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_quota_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usage_rows: Mapped[list[UsageDaily]] = relationship(back_populates="api_key")


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("api_key_id", "usage_date", name="uq_usage_api_key_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    api_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("api_keys.id"), nullable=False, index=True)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    asr_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    face_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    llm_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tts_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    api_key: Mapped[ApiKey] = relationship(back_populates="usage_rows")

    @property
    def total_bytes(self) -> int:
        return int(self.asr_bytes + self.face_bytes + self.llm_bytes + self.tts_bytes)


class Device(Base):
    """Physical hardware observed by this PC.

    ``device_id`` is a transport address only. It does not select product data;
    editable data belongs to one local workspace.
    """

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("device_id", name="uq_devices_device_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

class ScheduledTask(Base):
    """PC-local task; dispatch selects the currently connected hardware."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False, default="* * * * *")
    task_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="once")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 全表时间字段统一按 UTC 墙钟落库（core.clock 约定）；序列化边界经
    # scheduled_task_service.format_cst 转成北京时间展示。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offline_wait_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrence_id: Mapped[str] = mapped_column(
        String(36), default=_new_id, nullable=False, index=True
    )


class PlaybackReceipt(Base):
    """Durable proof that one device reached the terminal ``played`` ACK."""

    __tablename__ = "playback_receipts"
    __table_args__ = (
        UniqueConstraint(
            "device_id",
            "request_id",
            name="uq_playback_receipt_device_request",
        ),
        Index("ix_playback_receipts_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pb_ack"
    )
    played_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ToolOperation(Base):
    """PC-local at-most-once ledger for tools with external side effects."""

    __tablename__ = "tool_operations"
    __table_args__ = (
        UniqueConstraint(
            "tool_name",
            "operation_id",
            name="uq_tool_operation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolConfirmation(Base):
    """One-time local confirmation bound to the exact tool payload."""

    __tablename__ = "tool_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ControlOperation(Base):
    """Durable PC-local ledger for asynchronous hardware controls."""

    __tablename__ = "control_operations"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            name="uq_control_operation",
        ),
        CheckConstraint(
            "status IN "
            "('accepted','running','completed','failed','cancelled','timeout')",
            name="ck_control_operation_status",
        ),
        Index(
            "ix_control_operations_status_completed_at",
            "status",
            "completed_at",
        ),
        Index(
            "ix_control_operations_status_updated_at",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Required for new controls by the service layer; nullable only so legacy
    # rows that predate routing metadata remain readable after migration.
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="accepted", index=True
    )
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VoiceCloneJob(Base):
    """One provider voice-clone job in the PC-local voice library."""

    __tablename__ = "voice_clone_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    speaker_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="submitted", index=True
    )
    provider_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class VoiceClonePollThrottle(Base):
    """Cross-worker provider polling throttle for local job scopes."""

    __tablename__ = "voice_clone_poll_throttles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    job_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    last_polled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )


class SettingsTestDaily(Base):
    """PC-local daily counter for settings-page LLM/TTS tests."""

    __tablename__ = "settings_test_daily"
    __table_args__ = (UniqueConstraint("usage_date", name="uq_settings_test_daily"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
