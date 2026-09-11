"""三个「主动」来源的统一门禁：定时提醒 > 主动陪伴 > 空闲待机行为。

各来源仍各管各的逻辑，这里只回答一个问题：现在这个来源能不能主动？
- 勿扰时段：主动陪伴、待机行为都静默（提醒自己会推迟到勿扰结束）
- 提醒快到点（``REMINDER_SOON_SEC`` 内）：主动陪伴让路，免得刚开口就被提醒打断
- 设备 lane 上有别人在说 / 在演：待机行为不张望
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select

from deskbot_server.application import quest_service
from deskbot_server.application.turn_arbiter import device_turn_arbiter
from deskbot_server.core.clock import utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import ScheduledTask
from deskbot_server.device_preferences import quiet_hours_active

logger = logging.getLogger("deskbot-server")

SOURCE_QUEST = "quest"
SOURCE_LIVE = "live"
REMINDER_SOON_SEC = 90.0


def seconds_until_next_reminder() -> float | None:
    """最近一条待发提醒还有多久到点；没有 → None。查库失败按 None。"""
    session = get_session()
    try:
        stmt = select(func.min(ScheduledTask.next_run_at)).where(
            ScheduledTask.status == "active", ScheduledTask.enabled.is_(True)
        )
        nxt = session.execute(stmt).scalar()
    except Exception:  # noqa: BLE001
        logger.debug("[proactive_gate] next reminder query failed", exc_info=True)
        return None
    finally:
        session.close()
    if nxt is None:
        return None
    now = utcnow()
    if nxt.tzinfo is not None:
        nxt = nxt.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return max(0.0, (nxt - now).total_seconds())


def can_be_proactive(source: str, device_id: str | None = None) -> tuple[bool, str]:
    """(能不能, 不能的原因)。原因是短 key，供各来源写进自己的 skip reason。"""
    try:
        if quiet_hours_active():
            return False, "quiet_hours"
    except Exception:  # noqa: BLE001
        pass
    if source == SOURCE_QUEST:
        secs = seconds_until_next_reminder()
        try:
            soon = float(quest_service.reminder_soon_sec())
        except Exception:  # noqa: BLE001
            soon = REMINDER_SOON_SEC
        if secs is not None and soon > 0 and secs <= soon:
            return False, "reminder_soon"
    if source == SOURCE_LIVE and device_id:
        try:
            snap: dict[str, Any] = device_turn_arbiter.snapshot(device_id)
        except Exception:  # noqa: BLE001
            snap = {}
        if snap.get("active"):
            return False, "lane_busy"
    return True, ""


__all__ = ["REMINDER_SOON_SEC", "SOURCE_LIVE", "SOURCE_QUEST", "can_be_proactive", "seconds_until_next_reminder"]
