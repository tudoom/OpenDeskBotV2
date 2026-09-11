"""卡通表情生图的本机日配额（与设置页"测试"按钮的 50 次/天分开）。

一次「AI 生成表情套图」= 3 张预览 + 4 个状态 = 7 次生图，共用设置测试的 50 次/天几轮就见底，
所以生图单独记账：``data/local/cartoon_gen_quota.json``，按 UTC 日期计数，
上限 ``DESKBOT_CARTOON_GEN_DAILY_LIMIT``（默认 400 张）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from deskbot_server.atomic_store import atomic_write_json, file_lock
from deskbot_server.device_data import LOCAL_DATA_ROOT

CARTOON_GEN_DAILY_LIMIT_DEFAULT = 400
QUOTA_FILENAME = "cartoon_gen_quota.json"


def cartoon_gen_daily_limit() -> int:
    raw = os.environ.get("DESKBOT_CARTOON_GEN_DAILY_LIMIT")
    try:
        value = int(raw) if raw else CARTOON_GEN_DAILY_LIMIT_DEFAULT
    except ValueError:
        value = CARTOON_GEN_DAILY_LIMIT_DEFAULT
    return max(1, value)


class CartoonGenLimitExceeded(Exception):
    def __init__(self, *, limit: int):
        self.limit = limit
        super().__init__(f"本机今日卡通表情生图已达上限（{limit} 张/天），请明天再试")


@dataclass(frozen=True)
class CartoonGenQuotaSnapshot:
    count: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)


def _quota_path(root: Path | None = None) -> Path:
    return (root or LOCAL_DATA_ROOT) / QUOTA_FILENAME


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _read(path: Path) -> tuple[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", 0
    if not isinstance(raw, dict):
        return "", 0
    try:
        return str(raw.get("date") or ""), max(0, int(raw.get("count") or 0))
    except (TypeError, ValueError):
        return "", 0


def check_and_consume_cartoon_gen(*, images: int = 1, root: Path | None = None) -> CartoonGenQuotaSnapshot:
    """记 ``images`` 张；超限抛 ``CartoonGenLimitExceeded``，不记账。"""
    limit = cartoon_gen_daily_limit()
    path = _quota_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = _utc_today()
    with file_lock(str(path)):
        day, count = _read(path)
        if day != today:
            count = 0
        if count + max(1, int(images)) > limit:
            raise CartoonGenLimitExceeded(limit=limit)
        count += max(1, int(images))
        atomic_write_json(str(path), {"date": today, "count": count})
    return CartoonGenQuotaSnapshot(count=count, limit=limit)
