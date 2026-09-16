"""两个「主动」来源的统一门禁：主动陪伴（含定时提醒）> 空闲待机行为。

各来源仍各管各的逻辑，这里只回答一个问题：现在这个来源能不能主动？
- 勿扰时段：主动陪伴、待机行为都静默（定时提醒在勿扰过后的宽限内补提，见 quest_proactive）
- 设备 lane 上有别人在说 / 在演：待机行为不张望
"""

from __future__ import annotations

import logging
from typing import Any

from deskbot_server.application.turn_arbiter import device_turn_arbiter
from deskbot_server.device_preferences import quiet_hours_active

logger = logging.getLogger("deskbot-server")

SOURCE_QUEST = "quest"
SOURCE_LIVE = "live"


def can_be_proactive(source: str, device_id: str | None = None) -> tuple[bool, str]:
    """(能不能, 不能的原因)。原因是短 key，供各来源写进自己的 skip reason。"""
    try:
        if quiet_hours_active():
            return False, "quiet_hours"
    except Exception:  # noqa: BLE001
        pass
    if source == SOURCE_LIVE and device_id:
        try:
            snap: dict[str, Any] = device_turn_arbiter.snapshot(device_id)
        except Exception:  # noqa: BLE001
            snap = {}
        if snap.get("active"):
            return False, "lane_busy"
    return True, ""


__all__ = ["SOURCE_LIVE", "SOURCE_QUEST", "can_be_proactive"]
