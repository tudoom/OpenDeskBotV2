"""设备连上时把 PC 端「参数设置」推给设备（2026-09-14）。

静音以 PC 端偏好为准、默认关闭：设备自己 NVS 里存的值只是断开 PC 时的兜底，连上后
一律被 PC 的值覆盖。这样重装电脑端就回到默认（关），不会再出现「新装的电脑，静音却是开的」。
"""

from __future__ import annotations

import logging
from typing import Any

from deskbot_server.device_preferences import load_preferences

logger = logging.getLogger("deskbot-server")


def pc_mic_muted() -> bool:
    try:
        return bool((load_preferences().get("power") or {}).get("mic_muted", False))
    except Exception:  # noqa: BLE001 - 偏好读不到就按默认关
        return False


async def apply_pc_power_settings(session: Any, *, device_id: str = "") -> dict[str, Any] | None:
    """握手后调用：按 PC 端偏好下发静音。固件太旧（不回执）就跳过，返回 None。"""
    muted = pc_mic_muted()
    try:
        ack = await session.mic_mute_request(muted)
    except Exception:  # noqa: BLE001 - 推送失败不影响会话
        logger.warning("[power] 推送静音设置失败 device_id=%s", device_id, exc_info=True)
        return None
    if ack is None:
        logger.info("[power] 设备固件不支持静音开关，跳过推送 device_id=%s", device_id)
        return None
    if bool(ack.get("muted")) != muted:
        logger.warning("[power] 设备未按 PC 端设置生效 muted=%s ack=%s device_id=%s", muted, ack, device_id)
    else:
        logger.info("[power] 已按 PC 端设置推送 静音=%s device_id=%s", "开" if muted else "关", device_id)
    return dict(ack)
