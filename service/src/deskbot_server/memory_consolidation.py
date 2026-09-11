"""Nightly pass that distils daily memories into the curated master file.

Daily files accumulate whatever came up that day; without review the
master file never grows and the prompt's recent window keeps sliding
past things that turned out to matter. Once a night the model reads the
days since the last pass together with the current master file and
rewrites the master: promoting what has proven durable, merging repeats,
and dropping what was only ever true for an afternoon.

Runs quietly in the core process — it never speaks to the user, so a
failure costs nothing beyond a stale master file and is retried the next
night. The PC is often asleep at the configured hour, so the trigger is
"has today's pass happened yet", not "is it exactly 03:00": a machine
woken at noon still consolidates once.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from deskbot_server import memory_md

logger = logging.getLogger("deskbot-server")

STATE_FILENAME = "consolidation.json"
DEFAULT_HOUR = 3
MAX_DAYS_PER_PASS = 14
MAX_MASTER_ENTRIES = 120


def state_path() -> Path:
    return memory_md.memory_dir() / STATE_FILENAME


def _configured_hour() -> int:
    raw = os.environ.get("DESKBOT_MEMORY_CONSOLIDATION_HOUR")
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_HOUR


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    from deskbot_server.atomic_store import atomic_write_text

    memory_md.daily_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(), json.dumps(state, ensure_ascii=False, indent=1) + "\n"
    )


def should_run(
    state: dict[str, Any],
    *,
    now: datetime,
    hour: int | None = None,
) -> bool:
    """True when tonight's pass is still owed.

    Keyed on the calendar date rather than the clock so a machine that was
    off at the configured hour still consolidates once when it comes back.
    """
    target_hour = _configured_hour() if hour is None else hour
    today = now.strftime("%Y-%m-%d")
    if str(state.get("last_run_date") or "") == today:
        return False
    if now.hour < target_hour:
        # 还没到当天的整理时刻；昨天的那次由 last_run_date 判定，已经跑过。
        return str(state.get("last_run_date") or "") not in {
            today,
            _previous_date(today),
        }
    return True


def _previous_date(date_str: str) -> str:
    from datetime import timedelta

    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return ""
    return (day - timedelta(days=1)).strftime("%Y-%m-%d")


def pending_dates(state: dict[str, Any], *, limit: int = MAX_DAYS_PER_PASS) -> list[str]:
    """Daily files newer than the last consolidated day, oldest first."""
    done = str(state.get("last_consolidated_date") or "")
    dates = [d for d in memory_md.list_daily_dates() if d > done]
    return sorted(dates)[-limit:]


def build_prompt(master: list[dict[str, Any]], days: list[tuple[str, list[dict]]]) -> str:
    lines = [
        "你在整理一台桌面机器人的长期记忆。",
        "",
        "下面是当前的长期记忆，以及最近若干天的每日记忆。请输出整理后的新版长期记忆。",
        "",
        "规则：",
        "- 只保留对以后仍然成立、值得长期记住的事：偏好、习惯、关系、重要事实。",
        "- 合并重复或同义的条目，用更准确的一条覆盖旧的说法。",
        "- 丢掉一次性的、当天就失效的琐事（今天吃了什么、当时的天气）。",
        "- 保留原有条目的方括号 id；新增的条目 id 写成 new。",
        f"- 总条数不超过 {MAX_MASTER_ENTRIES} 条。",
        "- 只输出条目行，每行形如：- [id] 内容。不要输出标题或解释。",
        "",
        "当前长期记忆：",
    ]
    if master:
        lines.extend(f"- [{row['id']}] {row['text']}" for row in master)
    else:
        lines.append("（暂无）")
    for date_str, rows in days:
        lines.append("")
        lines.append(f"{date_str} 的记忆：")
        lines.extend(f"- [{row['id']}] {row['text']}" for row in rows)
    return "\n".join(lines)


def parse_master_reply(text: str) -> list[dict[str, Any]]:
    """Read the model's bullet list, minting ids for new entries."""
    rows = []
    seen: set[str] = set()
    for row in memory_md.parse_bullets(text or ""):
        entry_id = row["id"]
        if entry_id in {"new", "NEW"} or entry_id in seen:
            entry_id = memory_md.new_entry_id()
        seen.add(entry_id)
        rows.append({"id": entry_id, "text": row["text"]})
    return rows[:MAX_MASTER_ENTRIES]


async def consolidate_once(*, now: datetime | None = None) -> dict[str, Any]:
    """Run one pass. Returns a summary; never raises for provider errors."""
    from deskbot_server.llm.runtime import chat_acompletion

    moment = now or datetime.now()
    state = load_state()
    days = pending_dates(state)
    if not days:
        state["last_run_date"] = moment.strftime("%Y-%m-%d")
        save_state(state)
        return {"ok": True, "skipped": "no new daily memories", "days": 0}

    master = memory_md.read_master()
    payload = [(date, memory_md.read_daily(date)) for date in days]
    payload = [(date, rows) for date, rows in payload if rows]
    if not payload:
        state["last_run_date"] = moment.strftime("%Y-%m-%d")
        state["last_consolidated_date"] = days[-1]
        save_state(state)
        return {"ok": True, "skipped": "daily files were empty", "days": 0}

    try:
        # json_mode 默认开着，而这里要的是纯文本条目行，必须显式关掉。
        reply, _usage = await chat_acompletion(
            [{"role": "user", "content": build_prompt(master, payload)}],
            temperature=0.3,
            json_mode=False,
        )
    except Exception as exc:  # noqa: BLE001 - 整理失败不影响任何用户可见行为
        logger.warning("[memory] 整理失败，留待明晚重试: %s", exc)
        return {"ok": False, "error": str(exc), "days": len(payload)}

    rows = parse_master_reply(reply)
    if not rows:
        logger.warning("[memory] 整理结果为空，保留原长期记忆")
        return {"ok": False, "error": "empty consolidation", "days": len(payload)}

    memory_md.write_master(rows)
    state["last_run_date"] = moment.strftime("%Y-%m-%d")
    state["last_consolidated_date"] = days[-1]
    state["master_entries"] = len(rows)
    save_state(state)
    logger.info(
        "[memory] 长期记忆已整理 days=%d before=%d after=%d",
        len(payload),
        len(master),
        len(rows),
    )
    return {
        "ok": True,
        "days": len(payload),
        "before": len(master),
        "after": len(rows),
    }
