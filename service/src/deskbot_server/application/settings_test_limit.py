"""Daily guardrail for model test buttons in the one local console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select, text

from deskbot_server.db.engine import get_session
from deskbot_server.db.models import SettingsTestDaily

SETTINGS_TEST_DAILY_LIMIT = 50


class SettingsTestLimitExceeded(Exception):
    def __init__(self, *, limit: int = SETTINGS_TEST_DAILY_LIMIT):
        self.limit = limit
        super().__init__(f"本机今日测试次数已达上限（{limit} 次/天），请明天再试")


@dataclass(frozen=True)
class SettingsTestQuotaSnapshot:
    count: int
    limit: int = SETTINGS_TEST_DAILY_LIMIT

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _get_row(session, usage_date: date) -> SettingsTestDaily | None:
    return session.scalar(
        select(SettingsTestDaily).where(SettingsTestDaily.usage_date == usage_date)
    )


def get_settings_test_quota(*, usage_date: date | None = None, **_ignored) -> SettingsTestQuotaSnapshot:
    today = usage_date or _utc_today()
    session = get_session()
    try:
        row = _get_row(session, today)
        return SettingsTestQuotaSnapshot(count=int(row.count if row else 0))
    finally:
        session.close()


def check_and_consume_settings_test(**_ignored) -> SettingsTestQuotaSnapshot:
    today = _utc_today()
    session = get_session()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        row = _get_row(session, today)
        count = int(row.count if row else 0)
        if count >= SETTINGS_TEST_DAILY_LIMIT:
            raise SettingsTestLimitExceeded()
        if row is None:
            row = SettingsTestDaily(usage_date=today, count=1)
            session.add(row)
        else:
            row.count = count + 1
        session.commit()
        return SettingsTestQuotaSnapshot(count=count + 1)
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()
