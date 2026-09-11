"""剧本任务引擎：任务实例状态机 + 分数流转 + LLM 工具函数 + 提示词附录。

设计契约（与控制台 /quest 模块连线编辑器一一对应）：
- 剧本 = JSON 定义文件（``quest_playbooks_store``），只存"定义"
- 实例 = 每设备每任务的运行态（DB ``quest_instances`` 表）
- 状态机：not_started --(current_score ≥ activation_score)--> running --(AI 判定)--> success/failed
- 分数流转：任务成功 → on_success 各目标加分；失败 → on_failure 各目标加分；
  目标当前分数 ≥ 激活分数 时自动激活（not_started → running，写入 started_at）
- 起点任务：定义里 initial_status=running 的任务在分配剧本时直接进入 running
- 工具函数（供 LLM tool runner 与控制台沙箱共用）：
  update_task_result(device, playbook, task_id, status, result)  置成功/失败并传播
  update_task_strategy(device, playbook, task_id, strategy)      AI 按用户反馈更新策略
- 终态（success/failed）不可再变

单机单用户：真实运行态统一记在 PC 本地档案（``LOCAL_PROFILE_DEVICE``），换机器人
即插即用（与定时提醒同口径）；控制台沙箱模拟用 ``DESIGN_SANDBOX_DEVICE``。
剧本绑定与开关在行为偏好 ``quest`` 段（``device_preferences``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from deskbot_server import agent_docs
from deskbot_server.application.speech_turn import (
    address_confirmation_hint,
    extract_address_name,
    name_is_suspicious,
)
from deskbot_server.core.clock import utcnow
from deskbot_server.db.engine import get_session
from deskbot_server.db.models import QuestInstance, _new_id
from deskbot_server.device_preferences import load_preferences, update_preferences
from deskbot_server.quest_playbooks_store import (
    CARE_DEFAULT_MAX_MISSES,
    CARE_PAUSE_SEC,
    CARE_PLAYBOOK_NAME,
    DEFAULT_CHECK_INTERVAL_HOURS,
    DEFAULT_NEXT_DELAY_SEC,
    DEFAULT_PLAYBOOK_NAME,
    KIND_CARE,
    RESULT_STATUS,
    SETTLED_STATUS,
    STATUS_FAILED,
    STATUS_NOT_STARTED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_UNMET,
    TASK_ID_RE,
    TERMINAL_STATUS,
    QuestError,
    _int_or,
    clamp_check_interval_hours,
    delete_playbook_file,
    ensure_care_playbook,
    is_linear,
    linearize,
    list_playbooks,
    load_playbook,
    normalize_playbook,
    normalize_pos,
    normalize_refs,
    normalize_task,
    playbook_path,
    playbooks_dir,
    story_sequence,
    write_playbook,
)

logger = logging.getLogger("deskbot-server")

RESULT_SUCCESS = STATUS_SUCCESS
RESULT_UNMET = STATUS_UNMET
RESULT_SKIPPED = STATUS_SKIPPED
RESULT_FAILED = STATUS_FAILED  # 旧名（2026-09-10 前的工具契约 / 控制台）：进来一律当 unmet
ALL_RESULTS = RESULT_STATUS
# 标了结果之后主人一直在说话超过这么久：「接下一个」的标记作废，回到冷场规则
CHAIN_MAX_AGE_SEC = 1800.0

# 真实运行态的档案键（PC 本地档案，所有接入的机器人共用）
LOCAL_PROFILE_DEVICE = "local"
# 控制台沙箱模拟用的固定设备（不涉及真实运行态）
DESIGN_SANDBOX_DEVICE = "__design__"

# 提示词里最多列出的进行中主线任务数 / 日常关心条数
PROMPT_MAX_TASKS = 3
PROMPT_MAX_CARE = 8
# 小歪提的小目标主人一直没处理 → 这么久后自动作废（不然它再也提不了新的）
PROPOSAL_TTL_SEC = 7 * 86400

QUEST_TOOL_NAMES = frozenset({"update_task_result", "update_task_strategy", "propose_goal", "skip_goal"})
SKIP_RESULT = "主人说不用了{reason}"
CARE_PAUSED_RESULT = "连续 {n} 次没回应，先歇一天"


def is_care(defn: dict | None) -> bool:
    return str((defn or {}).get("kind") or "") == KIND_CARE

# 小歪自己提的小目标：同一时间最多挂一个待同意
MAX_PENDING_PROPOSALS = 1


def profile_device_id(device_id: str | None = None) -> str:
    """把调用方的 device_id 折到运行态档案键：沙箱原样，其余一律本地档案。"""
    dev = str(device_id or "").strip()
    return DESIGN_SANDBOX_DEVICE if dev == DESIGN_SANDBOX_DEVICE else LOCAL_PROFILE_DEVICE


# ── 剧本管理 ──────────────────────────────────────────────────


def get_playbook(name: str) -> dict | None:
    return load_playbook(name)


def require_playbook(name: str) -> dict:
    pb = load_playbook(name)
    if pb is None:
        raise QuestError(f"剧本不存在: {name}")
    return pb


def create_playbook(name: str) -> dict:
    name = str(name or "").strip()
    # playbook_path 负责名称校验
    if playbook_path(name).is_file():
        raise QuestError(f"剧本已存在: {name}")
    data = {"name": name, "tasks": []}
    write_playbook(name, data)
    return data


def save_playbook(name: str, data: dict) -> dict:
    """整体保存（导入用）：校验通过后原子写盘。"""
    normalized = normalize_playbook(name, data)
    write_playbook(name, normalized)
    return normalized


def delete_playbook(name: str) -> None:
    if name == CARE_PLAYBOOK_NAME:
        raise QuestError("「日常关心」是随包场景，不能删除")
    _delete_instances_by_playbook(name)
    delete_playbook_file(name)
    prefs = load_preferences()
    if str(prefs.get("quest", {}).get("playbook") or "") == name:
        set_bound_playbook("")


def get_task_definition(playbook_name: str, task_id: str) -> dict | None:
    pb = require_playbook(playbook_name)
    return next((t for t in pb.get("tasks") or [] if t.get("id") == task_id), None)


# ── 任务 CRUD（控制台编辑器用）──────────────────────────────


def add_task(name: str, raw: dict) -> dict:
    pb = require_playbook(name)
    tasks = pb.setdefault("tasks", [])
    task = normalize_task(raw, existing=tasks)
    if not task["id"]:
        raise QuestError("任务缺少 id")
    if any(t.get("id") == task["id"] for t in tasks):
        raise QuestError(f"任务 id 已存在: {task['id']}")
    tasks.append(task)
    save_playbook(name, pb)
    return task


def update_task(name: str, task_id: str, patch: dict) -> dict:
    """更新任务字段；支持改名（id 变了会同步其他任务的连线和运行实例）。"""
    pb = require_playbook(name)
    tasks = pb.get("tasks") or []
    target = next((t for t in tasks if t.get("id") == task_id), None)
    if target is None:
        raise QuestError(f"任务不存在: {task_id}")
    raw_id = patch.get("id")
    new_id = str(raw_id).strip() if raw_id is not None else task_id
    renamed = new_id != task_id
    if renamed:
        if not TASK_ID_RE.match(new_id):
            raise QuestError(f"任务 id 非法（{new_id!r}，需匹配 {TASK_ID_RE.pattern}）")
        if any(t.get("id") == new_id for t in tasks):
            raise QuestError(f"任务 id 已存在: {new_id}")
    merged = dict(target)
    for key, val in patch.items():
        if key == "id":
            continue
        if key == "pos":
            merged[key] = normalize_pos(val, tasks)
        elif key in ("on_success", "on_failure"):
            merged[key] = normalize_refs(val)
        else:
            merged[key] = val
    merged = normalize_task(merged, existing=tasks)
    merged["id"] = new_id
    tasks[tasks.index(target)] = merged
    if renamed:
        for t in tasks:
            for port in ("on_success", "on_failure"):
                seen: set[str] = set()
                out: list[dict] = []
                for ref in t.get(port) or []:
                    if ref["id"] == task_id:
                        ref["id"] = new_id
                    if ref["id"] not in seen:
                        seen.add(ref["id"])
                        out.append(ref)
                t[port] = out
    save_playbook(name, pb)
    if renamed:
        _rename_instances(name, task_id, new_id)
    return merged


def delete_task(name: str, task_id: str) -> None:
    pb = require_playbook(name)
    tasks = pb.get("tasks") or []
    if not any(t.get("id") == task_id for t in tasks):
        raise QuestError(f"任务不存在: {task_id}")
    pb["tasks"] = [t for t in tasks if t.get("id") != task_id]
    for t in pb["tasks"]:
        for port in ("on_success", "on_failure"):
            t[port] = [r for r in t.get(port) or [] if r["id"] != task_id]
    save_playbook(name, pb)
    _delete_task_instances(name, task_id)


# ── DB 访问 ───────────────────────────────────────────────────


def _dt_str(val: datetime | None) -> str | None:
    if val is None:
        return None
    return val.isoformat(timespec="seconds")


def _row_to_dict(row: QuestInstance) -> dict[str, Any]:
    return {
        "device_id": row.device_id,
        "playbook": row.playbook,
        "task_id": row.task_id,
        "status": row.status,
        "current_score": int(row.current_score or 0),
        "started_at": _dt_str(row.started_at),
        "finished_at": _dt_str(row.finished_at),
        "result": row.result,
        "strategy_override": row.strategy_override,
        "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
        "paused_until": _dt_str(getattr(row, "paused_until", None)),
    }


def _list_rows(device_id: str, playbook: str) -> list[QuestInstance]:
    session = get_session()
    try:
        stmt = (
            select(QuestInstance)
            .where(QuestInstance.device_id == device_id, QuestInstance.playbook == playbook)
            .order_by(QuestInstance.task_id)
        )
        return list(session.execute(stmt).scalars().all())
    finally:
        session.close()


def _get_row(device_id: str, playbook: str, task_id: str) -> QuestInstance | None:
    session = get_session()
    try:
        stmt = select(QuestInstance).where(
            QuestInstance.device_id == device_id,
            QuestInstance.playbook == playbook,
            QuestInstance.task_id == task_id,
        )
        return session.execute(stmt).scalars().first()
    finally:
        session.close()


def _require_row(device_id: str, playbook: str, task_id: str) -> QuestInstance:
    row = _get_row(device_id, playbook, task_id)
    if row is None:
        raise QuestError(f"任务实例不存在（剧本 {playbook}/{task_id}）——请先创建/分配实例")
    return row


def _update_row(row_id: str, **fields: Any) -> None:
    session = get_session()
    try:
        row = session.get(QuestInstance, row_id)
        if row is None:
            raise QuestError("任务实例已被删除")
        for key, val in fields.items():
            setattr(row, key, val)
        row.updated_at = utcnow()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _delete_instances_by_playbook(playbook: str) -> int:
    session = get_session()
    try:
        res = session.execute(delete(QuestInstance).where(QuestInstance.playbook == playbook))
        session.commit()
        return int(res.rowcount or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _delete_task_instances(playbook: str, task_id: str) -> int:
    session = get_session()
    try:
        res = session.execute(
            delete(QuestInstance).where(
                QuestInstance.playbook == playbook, QuestInstance.task_id == task_id
            )
        )
        session.commit()
        return int(res.rowcount or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _rename_instances(playbook: str, old_task_id: str, new_task_id: str) -> None:
    session = get_session()
    try:
        stmt = select(QuestInstance).where(
            QuestInstance.playbook == playbook, QuestInstance.task_id == old_task_id
        )
        for row in session.execute(stmt).scalars().all():
            row.task_id = new_task_id
            row.updated_at = utcnow()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── 实例管理（分配/查询/重置）────────────────────────────────


def ensure_instances(device_id: str, playbook_name: str) -> dict:
    """为设备创建剧本下缺失的任务实例（幂等）。

    定义里 initial_status=running 的任务（剧情起点）直接进入 running。
    """
    pb = require_playbook(playbook_name)
    existing = {r.task_id for r in _list_rows(device_id, playbook_name)}
    now = utcnow()
    created = activated = 0
    session = get_session()
    try:
        for t in pb.get("tasks") or []:
            tid = t["id"]
            if tid in existing:
                continue
            # 日常关心没有前置，一建就在“进行中”
            running = t.get("initial_status") == STATUS_RUNNING or is_care(t)
            session.add(
                QuestInstance(
                    id=_new_id(),
                    device_id=device_id,
                    playbook=playbook_name,
                    task_id=tid,
                    status=STATUS_RUNNING if running else STATUS_NOT_STARTED,
                    current_score=0,
                    started_at=now if running else None,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
            if running:
                activated += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return {"created": created, "activated": activated, "total": len(pb.get("tasks") or [])}


def reset_instances(device_id: str, playbook_name: str) -> dict:
    """清空并重建设备在剧本下的全部实例。"""
    session = get_session()
    try:
        session.execute(
            delete(QuestInstance).where(
                QuestInstance.device_id == device_id, QuestInstance.playbook == playbook_name
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return ensure_instances(device_id, playbook_name)


def get_instances(device_id: str, playbook_name: str) -> list[dict]:
    return [_row_to_dict(r) for r in _list_rows(device_id, playbook_name)]


def get_effective_strategy(device_id: str, playbook_name: str, task_id: str) -> str:
    """生效策略 = 实例级覆盖（update_task_strategy 写入）优先，否则取定义 strategy。"""
    row = _get_row(device_id, playbook_name, task_id)
    if row is not None and row.strategy_override:
        return row.strategy_override
    defn = get_task_definition(playbook_name, task_id)
    return str((defn or {}).get("strategy") or "").strip()


# ── 绑定与开关（行为偏好 quest 段）────────────────────────────


def quest_preferences() -> dict[str, Any]:
    prefs = load_preferences()
    block = prefs.get("quest")
    return dict(block) if isinstance(block, dict) else {}


def bound_playbook() -> str | None:
    """本机绑定的剧本名。

    偏好 ``quest.playbook``：None（从未设置）→ 随包默认剧本（须已由 Core 启动 /
    控制台 ``ensure_default_playbook`` 落盘，这里只读不写）；
    ""（用户明确解绑）→ None；其它 → 该剧本（文件缺失/损坏 → None）。
    """
    raw = quest_preferences().get("playbook")
    if raw is None:
        name = DEFAULT_PLAYBOOK_NAME
    else:
        name = str(raw or "").strip()
        if not name:
            return None
    try:
        return name if load_playbook(name) is not None else None
    except QuestError:
        return None


def set_bound_playbook(name: str | None) -> dict[str, Any]:
    """绑定剧本（""/None = 解绑）；剧本文件必须存在。返回保存后的偏好。"""
    value = str(name or "").strip()
    if value and load_playbook(value) is None:
        raise QuestError(f"剧本不存在: {value}")
    return update_preferences({"quest": {"playbook": value}})


def proactive_enabled() -> bool:
    return bool(quest_preferences().get("proactive_enabled", True))


def proactive_idle_sec() -> float:
    """冷场多久才主动开口（秒），偏好 ``quest.idle_sec``。"""
    try:
        return max(30.0, float(quest_preferences().get("idle_sec") or 120))
    except (TypeError, ValueError):
        return 120.0


def care_daily_limit() -> int:
    """日常关心每天最多占几次主动开口，偏好 ``quest.care_daily_limit``。"""
    try:
        return max(0, int(quest_preferences().get("care_daily_limit", 10)))
    except (TypeError, ValueError):
        return 2


def _pref_number(key: str, default: float, *, lo: float) -> float:
    try:
        return max(float(lo), float(quest_preferences().get(key, default)))
    except (TypeError, ValueError):
        return float(default)


def task_retry_sec() -> float:
    """同一个小目标两次主动提之间至少隔多久（秒），偏好 ``quest.task_retry_sec``。"""
    return _pref_number("task_retry_sec", 120, lo=0)


def reminder_soon_sec() -> float:
    """提醒还有多少秒到点时主动陪伴让路，偏好 ``quest.reminder_soon_sec``。"""
    return _pref_number("reminder_soon_sec", 90, lo=0)


def care_pause_sec() -> float:
    """日常关心连续没回应后歇多久（秒），偏好 ``quest.care_pause_sec``。"""
    return _pref_number("care_pause_sec", CARE_PAUSE_SEC, lo=600)


def proactive_daily_limit() -> int:
    """每天最多主动开口次数（0 = 不限），偏好 ``quest.daily_limit``。"""
    try:
        return max(0, int(quest_preferences().get("daily_limit", 6)))
    except (TypeError, ValueError):
        return 6


# ── 运行视角查询（提示词 / 工具接线用）────────────────────────


def is_care_scene(name: str | None) -> bool:
    return str(name or "") == CARE_PLAYBOOK_NAME


def care_playbook() -> str | None:
    """「日常关心」场景名（缺失时从随包模板补上；补不上 → None）。"""
    try:
        if load_playbook(CARE_PLAYBOOK_NAME) is None:
            ensure_care_playbook()
        return CARE_PLAYBOOK_NAME if load_playbook(CARE_PLAYBOOK_NAME) is not None else None
    except QuestError:
        return None


def active_playbooks() -> list[str]:
    """运行时同时生效的场景：开启的那个主线场景（最多一个）+ 日常关心。"""
    out: list[str] = []
    story = bound_playbook()
    if story and story != CARE_PLAYBOOK_NAME:
        out.append(story)
    care = care_playbook()
    if care:
        out.append(care)
    return out


def resolve_task_playbook(task_id: str) -> str | None:
    """task_id 属于哪个生效中的场景（主线优先）；找不到 → None。"""
    tid = str(task_id or "").strip()
    if not tid:
        return None
    for name in active_playbooks():
        if get_task_definition(name, tid) is not None:
            return name
    return None


_care_migrated_names: set[str] = set()
_linearized_names: set[str] = set()


def ensure_linear_playbook(name: str) -> bool:
    """列表顺序就是编排：已标记 sequence 的场景只按顺序重算边；没标记的老文件（分支编排）按边走一遍
    压成线并打上标记。每次都检查（纯函数，很便宜），只有真的要改时才写文件。返回是否改了文件。"""
    if not name or is_care_scene(name):
        return False
    try:
        pb = require_playbook(name)
    except QuestError:
        return False
    if pb.get("sequence") and is_linear(pb):
        return False
    legacy = not pb.get("sequence")
    linearize(pb)
    save_playbook(name, pb)
    if legacy:
        logger.info("[quest] %s: 分支编排已压成一条线 order=%s", name, [t.get("id") for t in story_sequence(pb)])
    return True


def migrate_care_tasks_into_care_scene() -> int:
    """把散在各场景里的日常关心搬进「日常关心」场景（定义 + 进度），每个进程只扫一遍。返回搬动条数。"""
    care = care_playbook()
    if care is None:
        return 0
    moved = 0
    for name in list_playbooks():
        if name == care or name in _care_migrated_names:
            continue
        _care_migrated_names.add(name)
        try:
            pb = require_playbook(name)
        except QuestError:
            continue
        stray_ids = [str(t.get("id")) for t in pb.get("tasks") or [] if is_care(t)]
        if not stray_ids:
            continue
        moved += move_tasks_to_care_scene(name, stray_ids)
        logger.info("[quest] moved %d care goal(s) from %s into care scene", len(stray_ids), name)
    return moved


def move_tasks_to_care_scene(name: str, task_ids: list[str]) -> int:
    """把 name 场景里的这些小目标搬进「日常关心」场景：定义改为日常关心、去掉连线、进度一起搬。返回搬动条数。"""
    care = care_playbook()
    if care is None or name == care:
        return 0
    pb = require_playbook(name)
    wanted = {str(t) for t in task_ids}
    strays = [t for t in pb.get("tasks") or [] if str(t.get("id")) in wanted]
    if not strays:
        return 0
    care_pb = require_playbook(care)
    have = {str(t.get("id")) for t in care_pb.get("tasks") or []}
    for t in strays:
        tid = str(t.get("id"))
        if tid not in have:
            t = dict(t)
            t["kind"], t["on_success"], t["on_failure"], t["proposed_after"] = KIND_CARE, [], [], ""
            care_pb.setdefault("tasks", []).append(t)
            have.add(tid)
            _move_instances(name, care, tid)
        else:
            _delete_task_instances(name, tid)
    stray_ids = {str(t.get("id")) for t in strays}
    pb["tasks"] = [t for t in pb.get("tasks") or [] if str(t.get("id")) not in stray_ids]
    for t in pb["tasks"]:
        for key in ("on_success", "on_failure"):
            t[key] = [r for r in t.get(key) or [] if r["id"] not in stray_ids]
    save_playbook(care, care_pb)
    save_playbook(name, pb)
    return len(strays)


def move_task_to_story(src: str, task_id: str, story: str) -> dict:
    """把 src 场景（通常是日常关心）里的一条小目标搬进主线场景 story，作为不连线的主线小目标；进度一起搬。"""
    if src == story:
        raise QuestError("已经在这个场景里")
    if is_care_scene(story):
        raise QuestError("目标场景不能是「日常关心」")
    pb = require_playbook(src)
    task = next((t for t in pb.get("tasks") or [] if str(t.get("id")) == task_id), None)
    if task is None:
        raise QuestError(f"任务不存在: {task_id}")
    story_pb = require_playbook(story)
    if any(str(t.get("id")) == task_id for t in story_pb.get("tasks") or []):
        raise QuestError(f"主线里已有同名小目标 {task_id}")
    moved = dict(task)
    moved.update(
        kind="story", repeatable=False, max_attempts=0, next_delay_sec=DEFAULT_NEXT_DELAY_SEC, schedule_time="", schedule_days=[],
        proposed=False, proposed_after="", on_success=[], on_failure=[],
    )
    story_pb.setdefault("tasks", []).append(moved)
    pb["tasks"] = [t for t in pb.get("tasks") or [] if str(t.get("id")) != task_id]
    for t in pb["tasks"]:
        for key in ("on_success", "on_failure"):
            t[key] = [r for r in t.get(key) or [] if r["id"] != task_id]
    save_playbook(story, story_pb)
    save_playbook(src, pb)
    _move_instances(src, story, task_id)
    return moved


_PROACTIVE_STATE_FILE = ".proactive_state.json"


def load_proactive_state() -> dict[str, Any]:
    """主动陪伴的运行态（定时叫过 / 今天开口次数 / 各小目标上次提的时间）；Core 重启后接着用。"""
    try:
        path = playbooks_dir() / _PROACTIVE_STATE_FILE
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_proactive_state(**fields: Any) -> None:
    """读-改-写（小文件，同进程内串行）。"""
    try:
        state = load_proactive_state()
        state.update(fields)
        path = playbooks_dir() / _PROACTIVE_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("[quest] save_proactive_state failed", exc_info=True)


def load_scheduled_fired() -> dict[str, str]:
    """定时日常关心「今天已经叫过」的记录（task_id → YYYY-MM-DD）。"""
    fired = load_proactive_state().get("scheduled_fired")
    return {str(k): str(v) for k, v in fired.items() if isinstance(v, str)} if isinstance(fired, dict) else {}


def save_scheduled_fired(fired: dict[str, str]) -> None:
    save_proactive_state(scheduled_fired=dict(fired))


def state_fingerprint() -> str:
    """主动陪伴状态的指纹：实例表最近改动 + 生效场景文件改动时间 + 选中的主线。
    变了就说明语音 Agent 手里的任务列表可能旧了。便宜：一条聚合查询 + 两次 stat。"""
    parts = [str(bound_playbook() or "")]
    session = get_session()
    try:
        row = session.execute(
            select(func.max(QuestInstance.updated_at), func.count()).where(QuestInstance.device_id == LOCAL_PROFILE_DEVICE)
        ).one()
        parts.append(f"{row[0]}|{row[1]}")
    except Exception:  # noqa: BLE001
        parts.append("db?")
    finally:
        session.close()
    for name in active_playbooks():
        try:
            parts.append(f"{name}:{playbook_path(name).stat().st_mtime_ns}")
        except (OSError, QuestError):
            parts.append(f"{name}:-")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _move_instances(src_playbook: str, dst_playbook: str, task_id: str) -> None:
    session = get_session()
    try:
        rows = session.execute(
            select(QuestInstance).where(QuestInstance.playbook == src_playbook, QuestInstance.task_id == task_id)
        ).scalars().all()
        for row in rows:
            exists = session.execute(
                select(QuestInstance).where(
                    QuestInstance.device_id == row.device_id,
                    QuestInstance.playbook == dst_playbook,
                    QuestInstance.task_id == task_id,
                )
            ).scalars().first()
            if exists is None:
                row.playbook = dst_playbook
                row.updated_at = utcnow()
            else:
                session.delete(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_status_migrated_keys: set[str] = set()


def migrate_legacy_statuses(device_id: str, playbook_name: str) -> int:
    """2026-09-10 前的 failed：主线 → unmet（定时检测会再提），日常关心 → paused。每进程每场景只查一次。"""
    key = f"{device_id}/{playbook_name}"
    if key in _status_migrated_keys:
        return 0
    _status_migrated_keys.add(key)
    care = is_care_scene(playbook_name)
    n = 0
    for row in _list_rows(device_id, playbook_name):
        if row.status == STATUS_FAILED:
            _update_row(row.id, status=STATUS_PAUSED if care else STATUS_UNMET)
            n += 1
    if n:
        logger.info("[quest] %s/%s 旧状态 failed 迁移 %d 条", device_id, playbook_name, n)
    return n


def _epoch(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def get_current_tasks(device_id: str | None = None) -> list[dict]:
    """当前进行中（running）的任务列表：开启的主线场景在前，日常关心在后。

    - 沙箱设备：无绑定概念，跨全部剧本查（控制台模拟用）不在这里；沙箱只通过
      ``current_tasks_for_playbook`` 显式指定剧本。
    - 其余：无实例 → ensure_instances 幂等初始化后再查。
    """
    dev = profile_device_id(device_id)
    if dev == DESIGN_SANDBOX_DEVICE:
        return []
    try:
        migrate_care_tasks_into_care_scene()
        expire_stale_proposals()
    except Exception:  # noqa: BLE001
        logger.debug("[quest] care migration / proposal expiry failed", exc_info=True)
    out: list[dict] = []
    for playbook in active_playbooks():
        try:
            have = {r.task_id for r in _list_rows(dev, playbook)}
            defined = {str(t.get("id")) for t in (load_playbook(playbook) or {}).get("tasks") or []}
            if defined - have:
                ensure_instances(dev, playbook)  # 新加的小目标（含日常关心）立刻有实例
        except QuestError:
            continue
        try:
            migrate_legacy_statuses(dev, playbook)
            if is_care_scene(playbook):
                reactivate_due_repeats(dev, playbook)
            else:
                ensure_linear_playbook(playbook)
                sync_sequence_cursor(dev, playbook)
        except Exception:  # noqa: BLE001 —— 同步失败不能影响正常对话
            logger.debug("[quest] sequence sync failed", exc_info=True)
        out.extend(current_tasks_for_playbook(dev, playbook))
    out.sort(key=lambda x: x["kind"] == KIND_CARE)  # 稳定排序：主线在前，各自保持场景里的先后
    return out


def current_tasks_for_playbook(device_id: str, playbook: str) -> list[dict]:
    """单个剧本下 running 任务的活跃目标集，按场景里定义的先后顺序（早的步骤在前）。"""
    try:
        pb = require_playbook(playbook)
    except QuestError:
        return []
    rows = {r.task_id: r for r in _list_rows(device_id, playbook)}
    seq = story_sequence(pb)
    next_title = {
        str(t.get("id")): str(seq[i + 1].get("title") or seq[i + 1].get("id")) if i + 1 < len(seq) else ""
        for i, t in enumerate(seq)
    }
    out: list[dict] = []
    for defn in pb.get("tasks") or []:
        inst = rows.get(str(defn.get("id")))
        if inst is None or inst.status != STATUS_RUNNING:
            continue
        if defn.get("proposed"):
            continue
        activation = max(int(defn.get("activation_score") or 1), 1)
        score = int(inst.current_score or 0)
        strategy = inst.strategy_override or str(defn.get("strategy") or "").strip()
        out.append(
            {
                "playbook": playbook,
                "task_id": inst.task_id,
                "title": defn.get("title", "notitle"),
                "goal": defn.get("goal", ""),
                "strategy": strategy,
                "success_condition": defn.get("success_condition", ""),
                "current_score": score,
                "activation_score": activation,
                "ratio": round(score / activation, 3),
                "trigger_hint": str(defn.get("trigger_hint") or "").strip(),
                "perform": dict(defn.get("perform") or {}),
                "attempt_count": int(getattr(inst, "attempt_count", 0) or 0),
                "max_attempts": int(defn.get("max_attempts") or 0),
                # 这轮开始（重开 / 定时检测重新计时）的时间：主动陪伴据此判断「这轮提过没有」
                "started_at_ts": _epoch(inst.started_at),
                "next_delay_sec": int(defn.get("next_delay_sec", DEFAULT_NEXT_DELAY_SEC) or 0),
                "kind": KIND_CARE if is_care(defn) else "story",
                "repeat_interval_sec": int(defn.get("repeat_interval_sec") or 86400),
                "schedule_time": str(defn.get("schedule_time") or ""),
                "schedule_days": list(defn.get("schedule_days") or []),
                "next_title": next_title.get(str(defn.get("id")), ""),
            }
        )
    out.sort(key=lambda x: x["kind"] == KIND_CARE)
    return out


def get_tool_calls(device_id: str | None = None) -> list[dict]:
    """当前可用的剧情工具契约（供 system prompt 附录），附可操作的 running 任务 id。"""
    task_ids = [t["task_id"] for t in get_current_tasks(device_id)]
    return [
        {
            "name": "update_task_result",
            "description": (
                "聊完这一步就调用：按达成条件和主人的回复判断，达成置 success、没达成置 unmet；"
                "标了结果、大家安静几秒后自动进入顺序里的下一步。result 必填"
            ),
            "parameters": {
                "task_id": "string（目标任务 id）",
                "status": 'string（"success" 或 "unmet"）',
                "result": "string（达成了什么 / 为什么没达成）",
            },
            "available_task_ids": task_ids,
        },
        {
            "name": "update_task_strategy",
            "description": "根据用户反馈更新某任务的处理策略（实例级覆盖定义 strategy，后续对话按新策略执行）",
            "parameters": {
                "task_id": "string（目标任务 id）",
                "strategy": "string（新的处理策略）",
            },
            "available_task_ids": task_ids,
        },
        {
            "name": "skip_goal",
            "description": "主人明确说不想要这个小目标时调用：记为跳过、不再提，进入下一步（日常关心则暂停）",
            "parameters": {"task_id": "string（目标任务 id）", "reason": "string（主人的原话，可选）"},
            "available_task_ids": task_ids,
        },
        {
            "name": "propose_goal",
            "description": "主人口头同意后，提一个新的长期小目标（等主人在控制台同意后才开始；一次只提一个）",
            "parameters": {
                "title": "string（小目标名）",
                "goal": "string（要达成什么）",
                "strategy": "string（怎么开口，可选）",
                "success_condition": "string（怎样算达成，可选）",
                "schedule_time": "string（每天几点提，HH:MM，可选）",
            },
        },
    ]


def _instance_result(device_id: str, playbook: str, task_id: str, *, activated: bool = False) -> dict:
    row = _get_row(device_id, playbook, task_id)
    out = _row_to_dict(row) if row else {"task_id": task_id}
    out["activated"] = activated
    return out


# ── 工具函数（LLM tool runner 与控制台沙箱共用）────────────────


def update_task_result(
    device_id: str, playbook_name: str, task_id: str, status: str, result: str
) -> dict:
    """工具函数①：小歪按「达成条件」和主人的回复判断后调用（控制台手动记结果也走这里）。

    - status ∈ {success, unmet, skipped}（旧的 failed 当 unmet），result 为结果 / 原因（必填）
    - 任务须处于 running（未激活 / 已有结果会抛错）
    - 主线：记下结果，沿 on_success / on_failure 传播；并写一个「接下一个」标记——主动陪伴循环
      等大家安静 next_delay_sec 后触发顺序里的下一个（未达成的留着，定时检测按顺序再提）
    - 日常关心：unmet / skipped = 主人说不用 → 无限期暂停，等手动重新打开
    """
    status = str(status or "").strip()
    if status == RESULT_FAILED:
        status = RESULT_UNMET
    if status not in ALL_RESULTS:
        raise QuestError(f"status 必须是 {'/'.join(ALL_RESULTS)}（{status!r}）")
    result = str(result or "").strip()
    if not result:
        raise QuestError("缺少结果描述（达成了什么 / 为什么没达成）")
    row = _require_row(device_id, playbook_name, task_id)
    if row.status == STATUS_NOT_STARTED:
        raise QuestError(f"任务未激活（{task_id}），还不能判定结果")
    if row.status in TERMINAL_STATUS:
        raise QuestError(f"任务已有结果（{row.status}）")
    now = utcnow()
    defn = get_task_definition(playbook_name, task_id)
    if is_care(defn) and status != RESULT_SUCCESS:
        # 日常关心没有失败边：主人说不用 → 无限期暂停，等手动重新打开
        task = pause_task(device_id, playbook_name, task_id, pause_sec=None, result=result)
        logger.info("[quest] care %s/%s paused by user: %s", playbook_name, task_id, result[:60])
        return {"task": task, "propagated": [], "activated": []}
    _update_row(row.id, status=status, finished_at=now, result=result)
    if status == RESULT_SUCCESS and device_id == LOCAL_PROFILE_DEVICE and not is_care(defn) and not (defn or {}).get("repeatable"):
        # 只有一次性的主线里程碑才写进 User.md；日常关心 / 可重复的每次达成都写会把它撑爆
        _note_success_in_user_doc(playbook_name, task_id, result, now=now)
    propagated, activated = _propagate(device_id, playbook_name, task_id, status, now=now)
    if device_id == LOCAL_PROFILE_DEVICE and not is_care(defn):
        note_chain(playbook_name, task_id, defn)
    logger.info(
        "[quest] task %s/%s -> %s propagated=%d activated=%d",
        playbook_name,
        task_id,
        status,
        len(propagated),
        len(activated),
    )
    return {
        "task": _instance_result(device_id, playbook_name, task_id),
        "propagated": propagated,
        "activated": activated,
    }


def restart_task(device_id: str, playbook_name: str, task_id: str) -> dict:
    """把一个已完成/已放弃/未开始的任务重新置为进行中（清掉结果，保留备注）。"""
    row = _require_row(device_id, playbook_name, task_id)
    if row.status == STATUS_RUNNING:
        return _instance_result(device_id, playbook_name, task_id)
    _update_row(
        row.id,
        status=STATUS_RUNNING,
        started_at=utcnow(),
        finished_at=None,
        result=None,
        attempt_count=0,
        paused_until=None,
    )
    return _instance_result(device_id, playbook_name, task_id, activated=True)


def pause_task(
    device_id: str, playbook_name: str, task_id: str, *, pause_sec: float | None, result: str
) -> dict:
    """日常关心：暂停。pause_sec=None 表示无限期（主人说不用，等手动重新打开）。"""
    row = _require_row(device_id, playbook_name, task_id)
    now = utcnow()
    until = (now + timedelta(seconds=float(pause_sec))) if pause_sec else None
    _update_row(
        row.id, status=STATUS_PAUSED, finished_at=now, result=str(result or "").strip() or None,
        attempt_count=0, paused_until=until,
    )
    return _instance_result(device_id, playbook_name, task_id)


def record_attempt(device_id: str, playbook_name: str, task_id: str) -> int:
    """主动提了一次；返回累计次数。"""
    row = _require_row(device_id, playbook_name, task_id)
    count = int(getattr(row, "attempt_count", 0) or 0) + 1
    _update_row(row.id, attempt_count=count)
    return count


def reset_attempts(device_id: str, playbook_name: str, task_id: str) -> None:
    """主人在上次主动开口后回应过 → 之前那些次数不算「没回应」，清零重数。"""
    row = _get_row(device_id, playbook_name, task_id)
    if row is not None and int(getattr(row, "attempt_count", 0) or 0):
        _update_row(row.id, attempt_count=0)


def pause_care_if_missed(device_id: str, playbook_name: str, task_id: str) -> dict | None:
    """日常关心：连续没回应到了上限还在进行中 → 歇一天；否则 None。
    主线小目标不数次数：达成没达成只由小歪（或主人在控制台）明确标，定时检测再按顺序提。"""
    row = _get_row(device_id, playbook_name, task_id)
    if row is None or row.status != STATUS_RUNNING:
        return None
    defn = get_task_definition(playbook_name, task_id) or {}
    if not is_care(defn):
        return None
    limit = int(defn.get("max_attempts", CARE_DEFAULT_MAX_MISSES) or 0)
    count = int(getattr(row, "attempt_count", 0) or 0)
    if limit <= 0 or count < limit:
        return None
    logger.info("[quest] care %s/%s missed %d times -> paused for a day", playbook_name, task_id, count)
    return {"task": pause_task(device_id, playbook_name, task_id, pause_sec=care_pause_sec(),
                               result=CARE_PAUSED_RESULT.format(n=count)), "propagated": [], "activated": []}


auto_fail_if_exhausted = pause_care_if_missed  # 旧名


def reactivate_due_repeats(device_id: str, playbook_name: str, *, now: datetime | None = None) -> list[str]:
    """可重复的小目标：终态后过了 repeat_interval_sec → 重新进入进行中。返回重开的 id。"""
    try:
        pb = require_playbook(playbook_name)
    except QuestError:
        return []
    now = now or utcnow()
    reopened: list[str] = []
    defs = {t.get("id"): t for t in pb.get("tasks") or [] if t.get("repeatable") or is_care(t)}
    if not defs:
        return []
    base = now.replace(tzinfo=None) if now.tzinfo is not None else now
    for inst in _list_rows(device_id, playbook_name):
        defn = defs.get(inst.task_id)
        if defn is None or inst.status not in TERMINAL_STATUS:
            continue
        paused_until = getattr(inst, "paused_until", None)
        if inst.status in (STATUS_PAUSED, STATUS_FAILED) and is_care(defn):
            # 日常关心的“未达成”= 暂停：有期限的到期重开，无期限的等主人
            if paused_until is None:
                continue
            until = paused_until.replace(tzinfo=None) if paused_until.tzinfo is not None else paused_until
            if base >= until:
                restart_task(device_id, playbook_name, inst.task_id)
                reopened.append(inst.task_id)
            continue
        if inst.finished_at is None:
            continue
        interval = max(int(defn.get("repeat_interval_sec") or 0), 60)
        finished = inst.finished_at
        if finished.tzinfo is not None:
            finished = finished.replace(tzinfo=None)
        if (base - finished).total_seconds() >= interval:
            restart_task(device_id, playbook_name, inst.task_id)
            reopened.append(inst.task_id)
    return reopened


SKIPPED_AHEAD_RESULT = "主人在控制台跳到了后面"


def set_waiting(device_id: str, playbook_name: str, task_id: str) -> None:
    """把一个小目标退回「等待中」（一条线上同时只有一个在进行中）。"""
    row = _get_row(device_id, playbook_name, task_id)
    if row is None or row.status == STATUS_NOT_STARTED:
        return
    _update_row(row.id, status=STATUS_NOT_STARTED, started_at=None, finished_at=None, result=None, attempt_count=0, paused_until=None)


def sync_sequence_cursor(device_id: str, playbook_name: str, *, now: datetime | None = None) -> str | None:
    """一条线的唯一规则：当前小目标 = 顺序里第一个还没有结果的。

    它若还在等待中就现在开始；顺序里其它「进行中」的退回等待中（拖动排序、跳过、重开之后都按这条重算）。
    未达成 / 跳过 / 达成都算「有结果」，不会在这里被重开——未达成的由定时检测（``begin_pass``）按顺序重开。
    返回当前小目标 id；全都有结果 → None。"""
    if is_care_scene(playbook_name):
        return None
    try:
        pb = require_playbook(playbook_name)
    except QuestError:
        return None
    seq = story_sequence(pb)
    if not seq:
        return None
    have = {r.task_id for r in _list_rows(device_id, playbook_name)}
    if any(str(t.get("id")) not in have for t in seq):
        ensure_instances(device_id, playbook_name)
    migrate_legacy_statuses(device_id, playbook_name)
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    cursor: str | None = None
    for t in seq:
        tid = str(t.get("id"))
        row = rows.get(tid)
        if row is None or row.status not in TERMINAL_STATUS:
            cursor = tid
            break
    if cursor is not None:
        row = rows.get(cursor)
        if row is None or row.status != STATUS_RUNNING:
            restart_task(device_id, playbook_name, cursor)
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    for t in seq:
        tid = str(t.get("id"))
        row = rows.get(tid)
        if tid != cursor and row is not None and row.status == STATUS_RUNNING:
            set_waiting(device_id, playbook_name, tid)
    return cursor


def skip_until(device_id: str, playbook_name: str, task_id: str) -> list[str]:
    """「从这个开始」：它前面还没有结果的都记为未达成（主人跳过），然后它成为当前。返回被跳过的 id。"""
    pb = require_playbook(playbook_name)
    seq = story_sequence(pb)
    ids = [str(t.get("id")) for t in seq]
    if task_id not in ids:
        raise QuestError(f"小目标不在主线里: {task_id}")
    ensure_instances(device_id, playbook_name)
    now = utcnow()
    skipped: list[str] = []
    for tid in ids[: ids.index(task_id)]:
        row = _get_row(device_id, playbook_name, tid)
        if row is not None and row.status not in TERMINAL_STATUS:
            _update_row(row.id, status=STATUS_SKIPPED, finished_at=now, result=SKIPPED_AHEAD_RESULT, attempt_count=0)
            skipped.append(tid)
    sync_sequence_cursor(device_id, playbook_name, now=now)
    return skipped


def playbook_finished(device_id: str, playbook_name: str) -> bool:
    """场景是否已走完：一条线上每个主线小目标都有结果了。"""
    if is_care_scene(playbook_name):
        return False
    try:
        pb = require_playbook(playbook_name)
    except QuestError:
        return False
    seq = story_sequence(pb)
    if not seq:
        return False
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    return all((rows.get(str(t.get("id"))) is not None and rows[str(t.get("id"))].status in TERMINAL_STATUS) for t in seq)


def playbook_settled(device_id: str, playbook_name: str) -> bool:
    """场景是否彻底结束：每个主线小目标都达成或跳过了（没有未达成的留给定时检测）。"""
    if is_care_scene(playbook_name):
        return False
    try:
        pb = require_playbook(playbook_name)
    except QuestError:
        return False
    seq = story_sequence(pb)
    if not seq:
        return False
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    return all((rows.get(str(t.get("id"))) is not None and rows[str(t.get("id"))].status in SETTLED_STATUS) for t in seq)


def unmet_task_ids(device_id: str, playbook_name: str) -> list[str]:
    """主线里未达成的小目标（按顺序）。"""
    if is_care_scene(playbook_name):
        return []
    try:
        pb = require_playbook(playbook_name)
    except QuestError:
        return []
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    out: list[str] = []
    for t in story_sequence(pb):
        row = rows.get(str(t.get("id")))
        if row is not None and row.status in (STATUS_UNMET, STATUS_FAILED):
            out.append(str(t.get("id")))
    return out


# ── 接下一个（标了结果 → 安静几秒 → 下一个）与定时检测（每 N 小时按顺序再提未达成的）──


def note_chain(playbook_name: str, task_id: str, defn: dict | None = None) -> dict[str, Any]:
    """主线小目标有了结果 → 写「接下一个」标记。控制台与 Core 是两个进程，标记落在共用的状态文件里，
    主动陪伴循环每拍轮询：大家安静 delay_sec 后触发顺序里的下一个。"""
    defn = defn or get_task_definition(playbook_name, task_id) or {}
    delay = max(int(defn.get("next_delay_sec", DEFAULT_NEXT_DELAY_SEC) or 0), 0)
    marker = {"playbook": playbook_name, "after": task_id, "at": time.time(), "delay_sec": delay}
    save_proactive_state(chain=marker)
    return marker


def pending_chain(*, now: float | None = None) -> dict[str, Any] | None:
    """待处理的「接下一个」标记；过期（主人一直在说话超过 CHAIN_MAX_AGE_SEC）就清掉。"""
    raw = load_proactive_state().get("chain")
    if not isinstance(raw, dict) or not raw.get("playbook") or not raw.get("after"):
        return None
    try:
        at = float(raw.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    now = time.time() if now is None else now
    if at <= 0 or now - at > CHAIN_MAX_AGE_SEC:
        clear_chain()
        return None
    return {
        "playbook": str(raw["playbook"]),
        "after": str(raw["after"]),
        "at": at,
        "delay_sec": max(_int_or(raw.get("delay_sec"), DEFAULT_NEXT_DELAY_SEC), 0),
    }


def clear_chain() -> None:
    save_proactive_state(chain=None)


def chain_next_task(device_id: str, playbook_name: str, after_id: str) -> str | None:
    """顺序里 after_id 之后第一个还没达成的（进行中 / 等待中 / 未达成）→ 让它进行中并返回 id；
    没有 → None（这一轮到头了，剩下的未达成等定时检测）。达成 / 跳过的不再碰。"""
    if is_care_scene(playbook_name):
        return None
    pb = require_playbook(playbook_name)
    ids = [str(t.get("id")) for t in story_sequence(pb)]
    if not ids:
        return None
    have = {r.task_id for r in _list_rows(device_id, playbook_name)}
    if any(tid not in have for tid in ids):
        ensure_instances(device_id, playbook_name)
    migrate_legacy_statuses(device_id, playbook_name)
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    start = ids.index(after_id) + 1 if after_id in ids else 0
    for tid in ids[start:]:
        row = rows.get(tid)
        status = row.status if row is not None else STATUS_NOT_STARTED
        if status == STATUS_RUNNING:
            return tid
        if status in (STATUS_NOT_STARTED, STATUS_UNMET, STATUS_FAILED):
            restart_task(device_id, playbook_name, tid)
            for other in ids:
                orow = rows.get(other)
                if other != tid and orow is not None and orow.status == STATUS_RUNNING:
                    set_waiting(device_id, playbook_name, other)
            return tid
    return None


def begin_pass(device_id: str, playbook_name: str) -> dict[str, Any]:
    """定时检测：检测目标完成情况，按顺序重新触发未达成的小目标。

    - 有进行中的小目标：重新计时（started_at = 现在）——这轮还没提过，冷场时就会再提
    - 没有进行中的：顺序里第一个未达成 / 等待中的重开（进行中），后面的靠「接下一个」串起来
    返回 {"touched": id|None, "reopened": id|None}。"""
    if is_care_scene(playbook_name):
        return {"touched": None, "reopened": None}
    pb = require_playbook(playbook_name)
    ids = [str(t.get("id")) for t in story_sequence(pb)]
    if not ids:
        return {"touched": None, "reopened": None}
    have = {r.task_id for r in _list_rows(device_id, playbook_name)}
    if any(tid not in have for tid in ids):
        ensure_instances(device_id, playbook_name)
    migrate_legacy_statuses(device_id, playbook_name)
    rows = {r.task_id: r for r in _list_rows(device_id, playbook_name)}
    for tid in ids:
        row = rows.get(tid)
        if row is not None and row.status == STATUS_RUNNING:
            _update_row(row.id, started_at=utcnow())
            return {"touched": tid, "reopened": None}
    for tid in ids:
        row = rows.get(tid)
        status = row.status if row is not None else STATUS_NOT_STARTED
        if status in (STATUS_NOT_STARTED, STATUS_UNMET, STATUS_FAILED):
            restart_task(device_id, playbook_name, tid)
            return {"touched": None, "reopened": tid}
    return {"touched": None, "reopened": None}


def check_interval_hours(playbook_name: str | None = None) -> int:
    """场景的目标检测间隔（小时，1–24；默认 1）。"""
    name = playbook_name or bound_playbook()
    pb = get_playbook(name) if name else None
    return clamp_check_interval_hours((pb or {}).get("check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS))


def scheduled_care_candidates(device_id: str | None = None) -> list[dict[str, Any]]:
    """当前场景里带定时（schedule_time）的日常关心，连同实例状态；给主动陪伴循环按钟表触发用。"""
    dev = profile_device_id(device_id)
    playbook = care_playbook()
    if not playbook:
        return []
    try:
        pb = require_playbook(playbook)
    except QuestError:
        return []
    rows = {r.task_id: r for r in _list_rows(dev, playbook)}
    out: list[dict[str, Any]] = []
    for t in pb.get("tasks") or []:
        if not is_care(t) or t.get("proposed") or not t.get("schedule_time"):
            continue
        row = rows.get(t["id"])
        out.append(
            {
                "playbook": playbook,
                "task_id": t["id"],
                "title": t.get("title") or t["id"],
                "schedule_time": t["schedule_time"],
                "schedule_days": list(t.get("schedule_days") or []),
                "status": row.status if row else STATUS_RUNNING,
                "paused_until": _dt_str(getattr(row, "paused_until", None)) if row else None,
            }
        )
    return out


def prepare_scheduled_task(device_id: str | None, playbook_name: str, task_id: str) -> bool:
    """定时到点：冷却中的（上次达成）先重开；无限期暂停 / 歇一天中的不动。返回是否可提。"""
    dev = profile_device_id(device_id)
    if not _list_rows(dev, playbook_name):
        ensure_instances(dev, playbook_name)
    row = _get_row(dev, playbook_name, task_id)
    if row is None:
        ensure_instances(dev, playbook_name)
        row = _get_row(dev, playbook_name, task_id)
        if row is None:
            return False
    if row.status == STATUS_RUNNING:
        return True
    if row.status == STATUS_SUCCESS or row.status == STATUS_NOT_STARTED:
        restart_task(dev, playbook_name, task_id)
        return True
    return False  # failed：主人说不用 / 歇一天中


def propose_task(playbook_name: str, raw: dict[str, Any]) -> dict:
    """小歪自己提一个小目标：标记 proposed、不连线、不进入运行，等主人同意。"""
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        raise QuestError("propose_goal 需要 goal（要达成什么）")
    pb = require_playbook(playbook_name)
    tasks = pb.setdefault("tasks", [])
    pending = [t for t in tasks if t.get("proposed")]
    if len(pending) >= MAX_PENDING_PROPOSALS:
        raise QuestError("已经有一个待主人同意的小目标了，先等主人回复")
    title = str(raw.get("title") or "").strip() or goal[:20]
    used = {str(t.get("id") or "") for t in tasks}
    n = 1
    while f"p{n}" in used:
        n += 1
    after = str(raw.get("after") or "").strip()
    if after and not any(t.get("id") == after for t in tasks):
        # 提议默认挂在「日常关心」场景，「接在谁后面」指的是主线里的小目标
        story = bound_playbook()
        story_tasks = (get_playbook(story) or {}).get("tasks") or [] if story and story != playbook_name else []
        if not any(t.get("id") == after for t in story_tasks):
            after = ""
    task = normalize_task(
        {
            "id": f"p{n}",
            "title": title[:40],
            "goal": goal[:400],
            "strategy": str(raw.get("strategy") or "").strip()[:400],
            "success_condition": str(raw.get("success_condition") or "").strip()[:400],
            "trigger_hint": str(raw.get("trigger_hint") or "").strip()[:200],
            "perform": raw.get("perform") if isinstance(raw.get("perform"), dict) else {"mode": str(raw.get("perform") or "speak")},
            "initial_status": STATUS_NOT_STARTED,
            "proposed": True,
            # 小歪提的默认是「日常关心」：条件 + 频率，不进主线
            "kind": KIND_CARE,
            "repeatable": True,
            "repeat_interval_sec": _int_or(raw.get("repeat_interval_sec"), 86400) if raw.get("repeat_interval_sec") is not None else 86400,
            "schedule_time": str(raw.get("schedule_time") or "").strip(),
        },
        existing=tasks,
    )
    task["proposed_after"] = after
    task["proposed_at"] = utcnow().replace(microsecond=0).isoformat()
    tasks.append(task)
    save_playbook(playbook_name, pb)
    logger.info("[quest] proposed goal %s/%s: %s", playbook_name, task["id"], safe_title(title))
    return task


def expire_stale_proposals(*, now: datetime | None = None, ttl_sec: float = PROPOSAL_TTL_SEC) -> list[str]:
    """小歪提的小目标主人 PROPOSAL_TTL_SEC 内没处理 → 自动作废（否则它一直提不了新的）。返回作废的 id。"""
    care = care_playbook()
    if care is None:
        return []
    try:
        pb = require_playbook(care)
    except QuestError:
        return []
    base = now or utcnow()
    if base.tzinfo is not None:
        base = base.replace(tzinfo=None)
    expired: list[str] = []
    for t in list(pb.get("tasks") or []):
        if not t.get("proposed"):
            continue
        raw = str(t.get("proposed_at") or "")
        try:
            at = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            at = None
        if at is None:
            # 老数据没有时间：从现在开始计时
            t["proposed_at"] = base.replace(microsecond=0).isoformat()
            save_playbook(care, pb)
            continue
        if at.tzinfo is not None:
            at = at.replace(tzinfo=None)
        if (base - at).total_seconds() >= ttl_sec:
            expired.append(str(t.get("id")))
    for tid in expired:
        try:
            delete_task(care, tid)
            logger.info("[quest] proposal %s expired after %.0f days without an answer", tid, ttl_sec / 86400)
        except QuestError:
            pass
    return expired


def safe_title(text: str) -> str:
    return str(text or "")[:40]


def update_task_strategy(device_id: str, playbook_name: str, task_id: str, strategy: str) -> dict:
    """工具函数②：AI 根据用户反馈更新任务的处理策略（实例级覆盖定义 strategy）。"""
    strategy = str(strategy or "").strip()
    if not strategy:
        raise QuestError("strategy 不能为空")
    row = _require_row(device_id, playbook_name, task_id)
    _update_row(row.id, strategy_override=strategy)
    return {"task_id": task_id, "strategy": strategy}


def _note_success_in_user_doc(playbook_name: str, task_id: str, result: str, *, now: datetime) -> None:
    """小目标达成后把结果写进 User.md（Agent 对主人的了解），失败只记日志。"""
    try:
        defn = get_task_definition(playbook_name, task_id) or {}
        title = str(defn.get("title") or task_id)
        line = f"- {now.strftime('%Y-%m-%d')} 陪伴小目标「{title}」达成：{result[:200]}"
        current = agent_docs.read_doc(agent_docs.USER_FILENAME)
        body = (current.rstrip("\n") + "\n" + line + "\n") if current.strip() else (line + "\n")
        agent_docs.write_doc(agent_docs.USER_FILENAME, body)
    except Exception:  # noqa: BLE001
        logger.debug("[quest] User.md write-back failed", exc_info=True)


def _propagate(
    device_id: str, playbook_name: str, task_id: str, status: str, *, now: datetime
) -> tuple[list[dict], list[dict]]:
    defn = get_task_definition(playbook_name, task_id)
    port = "on_success" if status == RESULT_SUCCESS else "on_failure"
    propagated: list[dict] = []
    activated: list[dict] = []
    for ref in (defn or {}).get(port) or []:
        target_id = ref["id"]
        tgt = _get_row(device_id, playbook_name, target_id)
        if tgt is None or tgt.status in TERMINAL_STATUS:
            continue
        tgt_defn = get_task_definition(playbook_name, target_id)
        activation = int((tgt_defn or {}).get("activation_score") or 1)
        new_score = int(tgt.current_score or 0) + int(ref.get("score") or 0)
        is_activated = tgt.status == STATUS_NOT_STARTED and new_score >= activation
        fields: dict[str, Any] = {"current_score": new_score}
        if is_activated:
            fields["status"] = STATUS_RUNNING
            fields["started_at"] = now
        _update_row(tgt.id, **fields)
        info = {
            "task_id": target_id,
            "current_score": new_score,
            "status": STATUS_RUNNING if is_activated else tgt.status,
        }
        propagated.append(info)
        if is_activated:
            activated.append(info)
    return propagated, activated


# ── LLM 工具入口（llm_tool_runner 分发到这里）─────────────────


def execute_quest_tool(raw: dict[str, Any], *, device_id: str | None = None) -> dict[str, Any]:
    """执行 update_task_result / update_task_strategy；错误以 QuestError 抛出。"""
    tool = str(raw.get("tool") or raw.get("name") or "").strip()
    task_id = str(raw.get("task_id") or "").strip()
    if not task_id and tool != "propose_goal":
        raise QuestError("缺少 task_id")
    if tool == "update_task_result" and str(raw.get("status") or "").strip() == "success":
        # 称呼只有一个字/像截断的半句：先反问确认，别把「小」当成名字结题（2026-09-09）。
        result_text = str(raw.get("result") or "")
        if task_id == "g_learn_name" or "称呼" in result_text:
            name = extract_address_name(result_text)
            if name is not None and name_is_suspicious(name):
                raise QuestError(address_confirmation_hint(name) + " 确认后再把这个小目标标为达成。")
    dev = profile_device_id(device_id)
    playbook = str(raw.get("playbook") or "").strip() if dev == DESIGN_SANDBOX_DEVICE else ""
    if tool == "propose_goal" and not playbook:
        playbook = care_playbook() or ""
        if not playbook:
            raise QuestError("「日常关心」场景不可用")
    if not playbook:
        playbook = resolve_task_playbook(task_id) or bound_playbook() or ""
    if not playbook:
        raise QuestError("本机未绑定剧本，剧情工具不可用")
    if not _list_rows(dev, playbook):
        ensure_instances(dev, playbook)
    if tool == "update_task_result":
        out = update_task_result(
            dev, playbook, task_id, str(raw.get("status") or "").strip(), str(raw.get("result") or "")
        )
        return {"tool": tool, "ok": True, **out}
    if tool == "update_task_strategy":
        out = update_task_strategy(dev, playbook, task_id, str(raw.get("strategy") or ""))
        return {"tool": tool, "ok": True, **out}
    if tool == "skip_goal":
        reason = str(raw.get("reason") or "").strip()
        out = update_task_result(
            dev, playbook, task_id, RESULT_SKIPPED, SKIP_RESULT.format(reason=f"：{reason}" if reason else "")
        )
        return {"tool": tool, "ok": True, **out}
    if tool == "propose_goal":
        task = propose_task(playbook, raw)
        return {
            "tool": tool,
            "ok": True,
            "task_id": task["id"],
            "title": task["title"],
            "note": "已记下，等主人在控制台同意后才会开始；先口头问问主人愿不愿意",
        }
    raise QuestError(f"未知剧情工具: {tool}")


# ── 系统提示附录 ──────────────────────────────────────────────


def _running_tasks_safe() -> list[dict]:
    try:
        return get_current_tasks()
    except Exception:  # noqa: BLE001 —— 剧情附录缺失不能让对话失败
        logger.debug("[quest] prompt appendix failed", exc_info=True)
        return []


def _care_limit_text() -> str:
    try:
        n = int(care_daily_limit())
    except Exception:  # noqa: BLE001
        n = 2
    return f"一天合计最多 {n} 次" if n > 0 else "一天不限次数但别反复念"


def _task_lines(tasks: list[dict]) -> list[str]:
    story = [t for t in tasks if t.get("kind") != KIND_CARE][:PROMPT_MAX_TASKS]
    care = [t for t in tasks if t.get("kind") == KIND_CARE][:PROMPT_MAX_CARE]
    lines: list[str] = []
    if story:
        lines.append("当前剧情任务（主线一条线，同时只有这一步在进行中；对话中自然推进，不要生硬念任务）：")
        for t in story:
            lines.append(f"  - [{t['task_id']}] {t['title']}")
            lines.append(f"    目标：{t['goal']}")
            if t.get("strategy"):
                lines.append(f"    策略：{t['strategy']}")
            if t.get("trigger_hint"):
                lines.append(f"    额外触发时机：{t['trigger_hint']}（满足时再推进，不满足就先不提）")
            lines.append(f"    达成条件：{t.get('success_condition') or '主人回应了就算'}")
            lines.append(
                "    聊完这一步必须标结果：按达成条件和主人的回复判断，达成就 update_task_result(success)，"
                "没达成就 update_task_result(unmet)，主人明确说不想要就 skip_goal；不标结果就不会进入下一步。"
            )
            delay = int(t.get("next_delay_sec") or 0)
            if t.get("next_title"):
                lines.append(
                    f"    标了结果后，大家安静 {delay} 秒就会进入下一步「{t['next_title']}」；"
                    "未达成的过后会按顺序再提，这一步上不要反复纠缠。"
                )
    if care:
        lines.append(f"可以顺带关心的事（日常关心：只在条件满足时提，每条按自己的频率，{_care_limit_text()}）：")
        for t in care:
            cond = t.get("trigger_hint") or "看情况"
            if t.get("schedule_time"):
                cond = f"每天 {t['schedule_time']} 由系统按时叫你提" + (f"；另外：{t['trigger_hint']}" if t.get("trigger_hint") else "")
            lines.append(f"  - [{t['task_id']}] {t['title']}：{t['goal']}｜条件：{cond}")
            if t.get("strategy"):
                lines.append(f"    做法：{t['strategy']}")
            lines.append(f"    做到了：{t['success_condition'] or '主人回应了'}｜主人说不用：调用 skip_goal 暂停")
    return lines


_PROPOSE_GUIDE = (
    "从记忆或对主人的了解里发现值得长期关心的事（作息、健康、正在忙的事），可以先口头问主人"
    "「我想以后多关心你的 X，可以吗」，主人同意后调用 propose_goal 记下一个新小目标；"
    "一次只提一个，主人在控制台同意后才会开始。"
)


def quest_tasks_appendix() -> str:
    """只列任务、不写工具契约：给原生函数工具（RTC 语音）链路用，工具 schema 已单独注册，
    不再重复一份文本契约；只补一句可用任务 id 与调用时机。"""
    tasks = _running_tasks_safe()
    if not tasks:
        return ""
    ids = ", ".join(t["task_id"] for t in tasks)
    lines = _task_lines(tasks)
    lines.append(
        f"聊完一个主线小目标必须调用 update_task_result 标 success 或 unmet——按达成条件和主人的回复判断（可用任务 id：{ids}）；"
        "主人明确说不想要这个时调用 skip_goal；主人对做法给出反馈时调用 update_task_strategy。"
    )
    lines.append(_PROPOSE_GUIDE)
    return "\n".join(lines)


def quest_prompt_appendix() -> str:
    """「当前剧情任务 + 剧情工具」附录：无绑定 / 无 running 任务 → 空串（不注入）。

    只列优先级最高的 PROMPT_MAX_TASKS 个进行中任务，不展示分数进度（避免模型以分数为目标）。
    工具按本项目的 ``tools`` JSON 数组约定书写（文字/传统链路）。
    """
    tasks = _running_tasks_safe()
    if not tasks:
        return ""
    ids = ", ".join(t["task_id"] for t in tasks)
    lines = _task_lines(tasks)
    lines.append(f"剧情任务工具（可用任务 id：{ids}）：")
    lines.append(
        '  - update_task_result: {"tool":"update_task_result","task_id":"任务id","status":"success|unmet","result":"达成了什么/为什么没达成"}'
    )
    lines.append(
        "    聊完这一步就当轮调用：按达成条件和主人的回复判断，达成 success、没达成 unmet；"
        "标了结果、大家安静几秒后才会进入下一步，不标就不会往下走。result 写主人原意。"
    )
    lines.append(
        '  - update_task_strategy: {"tool":"update_task_strategy","task_id":"任务id","strategy":"新的处理策略"}'
    )
    lines.append("    主人对你的做法给出反馈（太啰嗦/别再问）时调用，后续按新策略执行。")
    lines.append('  - skip_goal: {"tool":"skip_goal","task_id":"任务id","reason":"主人的原话"}')
    lines.append("    主人明确说不想要这个（别问了、不用管）时当轮调用：记为跳过、进入下一步，以后不再提。")
    lines.append(
        '  - propose_goal: {"tool":"propose_goal","title":"小目标名","goal":"要达成什么",'
        '"strategy":"怎么开口","success_condition":"怎样算达成",'
        '"schedule_time":"每天几点提，HH:MM，可选","after":"接在哪个任务id之后(可选)"}'
    )
    lines.append("    " + _PROPOSE_GUIDE)
    return "\n".join(lines)


def quest_tool_schemas() -> list[dict[str, Any]]:
    """原生函数工具形态（RTC 语音链路注册用）。"""
    return [
        {
            "name": "update_task_result",
            "description": (
                "Mark the running quest task as success or unmet once you have talked it through: "
                "judge by its success condition and what the user replied. Call it every time you finish "
                "a step, even when unmet; the scene only moves on to the next step after a result is "
                "recorded (a few quiet seconds later). Only use task ids listed in the current quest tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "status": {"type": "string", "enum": ["success", "unmet"]},
                    "result": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                "required": ["task_id", "status", "result"],
                "additionalProperties": False,
            },
        },
        {
            "name": "update_task_strategy",
            "description": (
                "Update how a running quest task should be handled after the user gives "
                "feedback (for example: stop asking, keep it short)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "strategy": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                "required": ["task_id", "strategy"],
                "additionalProperties": False,
            },
        },
        {
            "name": "skip_goal",
            "description": (
                "Skip the current running quest goal when the user says they do not want it; "
                "it is marked skipped, will not be asked again, and the playbook moves on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "reason": {"type": "string", "maxLength": 200},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "propose_goal",
            "description": (
                "Propose one new long-term companion goal after the user verbally agrees "
                "(for example caring about their sleep). It waits for the user's approval in "
                "the console before it starts. Propose at most one at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 40},
                    "goal": {"type": "string", "minLength": 1, "maxLength": 400},
                    "strategy": {"type": "string", "maxLength": 400},
                    "success_condition": {"type": "string", "maxLength": 400},
                    "schedule_time": {"type": "string", "maxLength": 5},
                    "after": {"type": "string", "maxLength": 64},
                },
                "required": ["title", "goal"],
                "additionalProperties": False,
            },
        },
    ]


__all__ = [
    "ALL_RESULTS",
    "CARE_PAUSED_RESULT",
    "DESIGN_SANDBOX_DEVICE",
    "LOCAL_PROFILE_DEVICE",
    "QUEST_TOOL_NAMES",
    "QuestError",
    "RESULT_FAILED",
    "RESULT_SKIPPED",
    "RESULT_SUCCESS",
    "RESULT_UNMET",
    "active_playbooks",
    "add_task",
    "auto_fail_if_exhausted",
    "begin_pass",
    "chain_next_task",
    "check_interval_hours",
    "clear_chain",
    "migrate_legacy_statuses",
    "note_chain",
    "pause_care_if_missed",
    "pending_chain",
    "playbook_settled",
    "unmet_task_ids",
    "bound_playbook",
    "care_playbook",
    "care_daily_limit",
    "care_pause_sec",
    "create_playbook",
    "current_tasks_for_playbook",
    "delete_playbook",
    "delete_task",
    "ensure_instances",
    "ensure_linear_playbook",
    "execute_quest_tool",
    "expire_stale_proposals",
    "get_current_tasks",
    "get_effective_strategy",
    "get_instances",
    "get_playbook",
    "get_task_definition",
    "get_tool_calls",
    "is_care",
    "is_care_scene",
    "list_playbooks",
    "load_proactive_state",
    "load_scheduled_fired",
    "migrate_care_tasks_into_care_scene",
    "move_task_to_story",
    "save_scheduled_fired",
    "pause_task",
    "playbook_finished",
    "proactive_enabled",
    "profile_device_id",
    "quest_preferences",
    "quest_prompt_appendix",
    "quest_tasks_appendix",
    "quest_tool_schemas",
    "reactivate_due_repeats",
    "record_attempt",
    "reset_attempts",
    "proactive_daily_limit",
    "proactive_idle_sec",
    "reminder_soon_sec",
    "require_playbook",
    "reset_instances",
    "resolve_task_playbook",
    "prepare_scheduled_task",
    "restart_task",
    "scheduled_care_candidates",
    "save_playbook",
    "save_proactive_state",
    "set_bound_playbook",
    "set_waiting",
    "skip_until",
    "state_fingerprint",
    "sync_sequence_cursor",
    "task_retry_sec",
    "update_task",
    "update_task_result",
    "update_task_strategy",
]
