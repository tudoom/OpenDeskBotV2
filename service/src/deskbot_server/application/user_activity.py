"""主人最近一次真的在和小歪说话的时间（只记主人侧，按设备）。

记：文字 / ASR 对话轮（``chat_flow.run_chat_turn`` 的非系统轮）、语音 Agent 回传的带用户话的轮次。
不记：小歪自己主动开口、提醒、表演、张望——设备的 THINKING/SPEAKING 状态那些都会动，
所以 ``registry.interaction_state_ts`` 分不清是主人在说还是小歪在说。
主动陪伴用它判断「上次主动开口之后主人回应了没」：回应过的那次不算没回应。
"""

from __future__ import annotations

import time

_last: dict[str, float] = {}


def note(device_id: str, ts: float | None = None) -> None:
    dev = str(device_id or "").strip()
    if dev:
        _last[dev] = float(time.time() if ts is None else ts)


def last_ts(device_id: str) -> float:
    return float(_last.get(str(device_id or "").strip(), 0.0))


def _reset_for_tests() -> None:
    _last.clear()


__all__ = ["last_ts", "note"]
