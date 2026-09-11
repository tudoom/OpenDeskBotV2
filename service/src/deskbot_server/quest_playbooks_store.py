"""剧本（Quest playbook）定义文件存储：``data/local/quest/<name>.json``。

只存"定义"：每个任务的 goal / strategy / success_condition（达成条件，一段）/
activation_score / initial_status / on_success / on_failure（带分数的后继边）/ pos（画布位置）。
运行态（每设备每任务的状态与分数）在 DB ``quest_instances`` 表，见
``application/quest_service.py``。

校验（``validate_playbook``）是纯函数：字段合法性、后继引用存在、连边不成环（DAG）。
随包默认剧本 ``quest_default_playbook.json``（``DEFAULT_PLAYBOOK_NAME``）在
``ensure_default_playbook`` 里按需落到剧本目录，用户可在控制台改或删。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from deskbot_server.device_data import local_data_dir

STATUS_NOT_STARTED = "not_started"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_UNMET = "unmet"  # 主线：小歪按达成条件判断没达成（定时检测会按顺序再提）
STATUS_SKIPPED = "skipped"  # 主线：主人说不用 / 控制台跳过（不再提）
STATUS_PAUSED = "paused"  # 日常关心：主人说不用 / 连续没回应 → 暂停
STATUS_FAILED = "failed"  # 2026-09-10 之前的旧数据：主线 = unmet、日常关心 = paused；读到就迁移
ALL_STATUS = (STATUS_NOT_STARTED, STATUS_RUNNING, STATUS_SUCCESS, STATUS_UNMET, STATUS_SKIPPED, STATUS_PAUSED, STATUS_FAILED)
TERMINAL_STATUS = (STATUS_SUCCESS, STATUS_UNMET, STATUS_SKIPPED, STATUS_PAUSED, STATUS_FAILED)
SETTLED_STATUS = (STATUS_SUCCESS, STATUS_SKIPPED)  # 主线：不会再提的结果
RESULT_STATUS = (STATUS_SUCCESS, STATUS_UNMET, STATUS_SKIPPED)  # 小歪 / 控制台能标的结果

PORT_SUCCESS = "success"
PORT_FAILED = "failed"
PORT_KEYS = {PORT_SUCCESS: "on_success", PORT_FAILED: "on_failure"}

# 执行方式：只说 / 先做表情再说 / 先演一段表演（场景编排）再说
PERFORM_SPEAK = "speak"
PERFORM_EXPRESSION = "expression"
PERFORM_SCENE = "scene"
PERFORM_MODES = (PERFORM_SPEAK, PERFORM_EXPRESSION, PERFORM_SCENE)

# 主线小目标：小歪标了结果（达成 / 未达成 / 跳过）后，大家安静这么久就触发顺序里的下一个
DEFAULT_NEXT_DELAY_SEC = 10
NEXT_DELAY_MAX_SEC = 600
# 主线场景：每隔这么久检测一次目标完成情况，按顺序重新触发未达成的小目标
DEFAULT_CHECK_INTERVAL_HOURS = 1
CHECK_INTERVAL_MAX_HOURS = 24

# 小目标类型：主线（链条里的一步）/ 日常关心（长期反复做的小事，不进链）
KIND_STORY = "story"
KIND_CARE = "care"
KINDS = (KIND_STORY, KIND_CARE)
CARE_DEFAULT_MAX_MISSES = 2  # 日常关心：连续这么多次没回应 → 暂停一天
CARE_PAUSE_SEC = 86400
SCHEDULE_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # 日常关心的定时：每天 HH:MM
DEFAULT_REPEAT_INTERVAL_SEC = 86400  # 可重复的小目标：达成后过多久重新开始

PLAYBOOK_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")

DEFAULT_PLAYBOOK_NAME = "xiaoy"
_DEFAULT_PLAYBOOK_FILE = Path(__file__).resolve().parent / "quest_default_playbook.json"
# 「日常关心」是一个特殊场景：只放日常关心，不可删，主动陪伴开着它就生效
CARE_PLAYBOOK_NAME = "care"
_CARE_PLAYBOOK_FILE = Path(__file__).resolve().parent / "quest_care_playbook.json"

_DEFAULT_NODE_W = 210
_DEFAULT_NODE_H = 120

_playbooks_dir_override: Path | None = None


class QuestError(Exception):
    """剧本任务错误：定义校验失败 / 状态机非法操作，抛给调用方（工具/API）。"""


def configure_playbooks_dir(path: str | Path | None) -> None:
    """测试用：把剧本目录指到临时目录（None 恢复默认）。"""
    global _playbooks_dir_override
    _playbooks_dir_override = Path(path) if path else None


def playbooks_dir() -> Path:
    if _playbooks_dir_override is not None:
        return _playbooks_dir_override
    return local_data_dir() / "quest"


# ── 定义校验（纯函数）──────────────────────────────────────────


def _errs_for_task(task: dict) -> list[str]:
    errs: list[str] = []
    tid = str(task.get("id") or "")
    label = f"任务 {tid or '<无id>'}"
    if not tid or not TASK_ID_RE.match(tid):
        errs.append(f"{label}: id 非法（{tid!r}，需匹配 {TASK_ID_RE.pattern}）")
    if not str(task.get("goal") or "").strip():
        errs.append(f"{label}: goal 不能为空")
    act = task.get("activation_score")
    if act is None or isinstance(act, bool) or not isinstance(act, (int, float)) or act < 0:
        errs.append(f"{label}: activation_score 必须是非负数字（{act!r}）")
    init = task.get("initial_status") or STATUS_NOT_STARTED
    if init not in (STATUS_NOT_STARTED, STATUS_RUNNING):
        errs.append(f"{label}: initial_status 只能是 not_started/running（{init!r}）")
    perform = task.get("perform")
    if perform is not None:
        if not isinstance(perform, dict):
            errs.append(f"{label}: perform 必须是对象")
        else:
            mode = str(perform.get("mode") or PERFORM_SPEAK)
            if mode not in PERFORM_MODES:
                errs.append(f"{label}: perform.mode 只能是 {'/'.join(PERFORM_MODES)}（{mode!r}）")
    for key in ("max_attempts", "repeat_interval_sec", "next_delay_sec"):
        val = task.get(key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0):
            errs.append(f"{label}: {key} 必须是非负数字（{val!r}）")
    kind = task.get("kind")
    if kind is not None and kind not in KINDS:
        errs.append(f"{label}: kind 只能是 {'/'.join(KINDS)}（{kind!r}）")
    st = task.get("schedule_time")
    if st not in (None, "") and not SCHEDULE_TIME_RE.match(str(st)):
        errs.append(f"{label}: schedule_time 必须是 HH:MM（{st!r}）")
    days = task.get("schedule_days")
    if days is not None and (not isinstance(days, list) or any(not isinstance(d, int) or isinstance(d, bool) or d < 0 or d > 6 for d in days)):
        errs.append(f"{label}: schedule_days 必须是 0-6（周一=0）的列表")
    for port in ("on_success", "on_failure"):
        refs = task.get(port)
        if refs is None:
            continue
        if not isinstance(refs, list):
            errs.append(f"{label}: {port} 必须是列表")
            continue
        seen: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict) or not str(ref.get("id") or "").strip():
                errs.append(f"{label}: {port} 里的后继必须含 id")
                continue
            rid = str(ref["id"]).strip()
            if rid in seen:
                errs.append(f"{label}: {port} 后继 {rid} 重复")
            seen.add(rid)
            score = ref.get("score", 0)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or score < 0:
                errs.append(f"{label}: {port} 后继 {rid} 的 score 必须是非负数字")
    return errs


def validate_playbook(data: Any) -> list[str]:
    """校验整个剧本定义，返回错误列表（空 = 通过）。

    检查：name 合法性、tasks 是列表、任务字段、后继引用存在、连边成环。
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["剧本必须是 JSON 对象"]
    name = str(data.get("name") or "")
    if not PLAYBOOK_NAME_RE.match(name):
        errs.append(f"name 非法（{name!r}，需匹配 {PLAYBOOK_NAME_RE.pattern}）")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return errs + ["tasks 必须是列表"]
    ids: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            errs.append("tasks 里存在非对象元素")
            continue
        errs += _errs_for_task(t)
        tid = str(t.get("id") or "")
        if tid:
            if tid in ids:
                errs.append(f"任务 id 重复：{tid}")
            ids.add(tid)
    edges: list[tuple[str, str]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        for port in ("on_success", "on_failure"):
            refs = t.get(port)
            if not isinstance(refs, list):
                continue
            for ref in refs:
                rid = str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""
                if rid and rid not in ids:
                    errs.append(f"任务 {tid}: {port} 引用了不存在的任务 {rid}")
                if rid and tid:
                    edges.append((tid, rid))
    if has_cycle(edges, ids):
        errs.append("后继关系成环（剧本必须是有向无环图）")
    return errs


def has_cycle(edges: list[tuple[str, str]], nodes: set[str]) -> bool:
    """有向图环检测（含自环）。"""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    visiting: set[str] = set()
    done: set[str] = set()

    def dfs(n: str) -> bool:
        if n in done:
            return False
        if n in visiting:
            return True
        visiting.add(n)
        for nxt in adj.get(n, []):
            if dfs(nxt):
                return True
        visiting.discard(n)
        done.add(n)
        return False

    return any(dfs(n) for n in list(adj))


# ── 规格化 ────────────────────────────────────────────────────


def default_pos(existing: list[dict]) -> dict:
    """新任务默认摆放：按已有任务数网格排布。"""
    idx = len(existing)
    return {
        "x": 120 + (idx % 4) * 280,
        "y": 120 + (idx // 4) * 220,
        "width": _DEFAULT_NODE_W,
        "height": _DEFAULT_NODE_H,
    }


def normalize_refs(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for ref in raw:
        if isinstance(ref, dict) and str(ref.get("id") or "").strip():
            try:
                score = int(ref.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            out.append({"id": str(ref["id"]).strip(), "score": score})
    return out


def normalize_pos(raw: Any, existing: list[dict]) -> dict:
    base = default_pos(existing)
    if not isinstance(raw, dict):
        return base
    for key in ("x", "y", "width", "height"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            base[key] = int(val)
    return base


def _int_or(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def normalize_perform(raw: Any) -> dict:
    """执行方式：{mode, expression, scene}；缺省只说话。"""
    out = {"mode": PERFORM_SPEAK, "expression": "", "scene": ""}
    if not isinstance(raw, dict):
        return out
    mode = str(raw.get("mode") or PERFORM_SPEAK).strip()
    out["mode"] = mode if mode in PERFORM_MODES else PERFORM_SPEAK
    out["expression"] = str(raw.get("expression") or "").strip()[:80]
    out["scene"] = str(raw.get("scene") or "").strip()[:80]
    if out["mode"] == PERFORM_EXPRESSION and not out["expression"]:
        out["mode"] = PERFORM_SPEAK
    if out["mode"] == PERFORM_SCENE and not out["scene"]:
        out["mode"] = PERFORM_SPEAK
    return out


def normalize_task(raw: dict, *, existing: list[dict] | None = None) -> dict:
    """补全任务默认字段（不校验，校验交给 validate_playbook）。"""
    existing = existing or []
    act_raw = raw.get("activation_score")
    max_raw = raw.get("max_attempts")
    rep_raw = raw.get("repeat_interval_sec")
    delay_raw = raw.get("next_delay_sec")
    kind = str(raw.get("kind") or "").strip()
    if kind not in KINDS:
        # 老数据：只有小歪提议的任务带 proposed_after 键，它们一律归日常关心
        kind = KIND_CARE if "proposed_after" in raw else KIND_STORY
    care = kind == KIND_CARE
    return {
        "kind": kind,
        "id": str(raw.get("id") or "").strip(),
        "title": str(raw.get("title") or "notitle").strip(),
        "goal": str(raw.get("goal") or "").strip(),
        "strategy": str(raw.get("strategy") or "").strip(),
        "activation_score": _int_or(act_raw, 1) if act_raw is not None else 1,
        "initial_status": str(raw.get("initial_status") or STATUS_NOT_STARTED).strip(),
        # 达成条件只有一段：达成还是未达成由小歪按它和主人的回复判断（旧的 failure_condition 并进来）
        "success_condition": _merge_conditions(raw.get("success_condition"), raw.get("failure_condition")),
        "on_success": normalize_refs(raw.get("on_success")),
        "on_failure": normalize_refs(raw.get("on_failure")),
        "pos": normalize_pos(raw.get("pos"), existing),
        # 触发时机：默认「上一个小目标完成」写死；这里只存用户补充的其它条件（自由文本，给模型判断）
        "trigger_hint": str(raw.get("trigger_hint") or "").strip()[:200],
        "perform": normalize_perform(raw.get("perform")),
        # 只对日常关心有意义：连续这么多次没回应就先歇一天（0 = 不歇）；主线不数次数
        "max_attempts": (max(_int_or(max_raw, CARE_DEFAULT_MAX_MISSES), 0) if max_raw is not None else CARE_DEFAULT_MAX_MISSES) if care else 0,
        # 主线：标了结果后大家安静这么久就触发下一个小目标；日常关心不用
        "next_delay_sec": (
            min(max(_int_or(delay_raw, DEFAULT_NEXT_DELAY_SEC), 0), NEXT_DELAY_MAX_SEC)
            if delay_raw is not None
            else DEFAULT_NEXT_DELAY_SEC
        ) if not care else 0,
        "repeatable": bool(raw.get("repeatable")) or care,
        "repeat_interval_sec": (
            max(_int_or(rep_raw, DEFAULT_REPEAT_INTERVAL_SEC), 60)
            if rep_raw is not None
            else DEFAULT_REPEAT_INTERVAL_SEC
        ),
        # 小歪自己提的、还没经主人同意的小目标（不进入运行，不连线）
        "proposed": bool(raw.get("proposed")),
        "proposed_after": str(raw.get("proposed_after") or "").strip()[:64],
        "proposed_at": str(raw.get("proposed_at") or "").strip()[:40],  # 提议时间（ISO），过期自动作废
        # 日常关心的定时：每天 HH:MM（空 = 看情况由小歪判断）；schedule_days 空 = 每天
        "schedule_time": _schedule_time(raw.get("schedule_time")),
        "schedule_days": sorted({int(d) for d in (raw.get("schedule_days") or []) if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}),
    }


def _schedule_time(raw: Any) -> str:
    val = str(raw or "").strip()
    return val if SCHEDULE_TIME_RE.match(val) else ""


def _merge_conditions(success: Any, failure: Any) -> str:
    """2026-09-10 起只有一段「达成条件」；老数据里单独的「未达成条件」并成一句，别丢主人写过的话。"""
    ok = str(success or "").strip()
    bad = str(failure or "").strip()
    if not bad or bad in ok:
        return ok
    return f"{ok}（未达成的情况：{bad}）" if ok else f"未达成的情况：{bad}"


def clamp_check_interval_hours(raw: Any) -> int:
    return min(max(_int_or(raw, DEFAULT_CHECK_INTERVAL_HOURS), 1), CHECK_INTERVAL_MAX_HOURS)


# ── 顺序编排（主线场景 = 一条线：不论达成与否都进入顺序里的下一个）──────


def story_sequence(pb: dict) -> list[dict]:
    """主线小目标按场景里的先后顺序（不含日常关心、不含小歪还没被同意的提议）。"""
    return [
        t for t in pb.get("tasks") or []
        if isinstance(t, dict) and str(t.get("kind") or KIND_STORY) != KIND_CARE and not t.get("proposed")
    ]


def _ref_ids(refs: Any) -> list[str]:
    return [str(r.get("id")) for r in refs or [] if isinstance(r, dict) and r.get("id")]


def is_linear(pb: dict) -> bool:
    """边是否已经和顺序一致：每一步的达成边、未达成边都只指向下一步，最后一步没有下一步。"""
    seq = story_sequence(pb)
    for i, t in enumerate(seq):
        want = [str(seq[i + 1].get("id"))] if i + 1 < len(seq) else []
        if _ref_ids(t.get("on_success")) != want or _ref_ids(t.get("on_failure")) != want:
            return False
        if str(t.get("initial_status") or STATUS_NOT_STARTED) != (STATUS_RUNNING if i == 0 else STATUS_NOT_STARTED):
            return False
    return True


def linear_order(pb: dict) -> list[str]:
    """把老的分支编排压成一条线：从起点沿达成边、再沿未达成边走一遍，走不到的按原来的位置补在后面。"""
    seq = story_sequence(pb)
    by = {str(t.get("id")): t for t in seq}
    incoming: set[str] = set()
    for t in seq:
        for key in ("on_success", "on_failure"):
            incoming.update(_ref_ids(t.get(key)))
    starts = [str(t.get("id")) for t in seq if str(t.get("id")) not in incoming] or ([str(seq[0].get("id"))] if seq else [])
    order: list[str] = []
    seen: set[str] = set()

    def walk(tid: str) -> None:
        if tid in seen or tid not in by:
            return
        seen.add(tid)
        order.append(tid)
        for key in ("on_success", "on_failure"):
            for nxt in _ref_ids(by[tid].get(key)):
                walk(nxt)

    for s in starts:
        walk(s)
    for t in seq:
        if str(t.get("id")) not in seen:
            walk(str(t.get("id")))
    return order


def relink_sequence(pb: dict) -> bool:
    """按当前顺序重算主线的边和起点：每一步 → 下一步（达成、未达成都一样）。返回是否改了东西。"""
    seq = story_sequence(pb)
    dirty = False
    for i, t in enumerate(seq):
        if i + 1 < len(seq):
            nxt_task = seq[i + 1]
            score = max(_int_or(nxt_task.get("activation_score"), 1), 1)  # 一步到位：分数 = 下一步的激活线
            nxt = [{"id": str(nxt_task.get("id")), "score": score}]
        else:
            nxt = []
        want_init = STATUS_RUNNING if i == 0 else STATUS_NOT_STARTED
        for key in ("on_success", "on_failure"):
            cur = [(str(r.get("id")), int(r.get("score") or 0)) for r in t.get(key) or [] if isinstance(r, dict)]
            if cur != [(r["id"], r["score"]) for r in nxt]:
                t[key] = [dict(r) for r in nxt]
                dirty = True
        if str(t.get("initial_status") or "") != want_init:
            t["initial_status"] = want_init
            dirty = True
        if t.get("repeatable"):
            t["repeatable"] = False  # 主线不回头；反复做的事放日常关心
            dirty = True
    return dirty


def linearize(pb: dict) -> bool:
    """老编排 → 一条线：先按走一遍的顺序重排主线小目标（其它照旧放在后面），再重算边，打上 sequence 标记。
    返回是否改了东西。"""
    if pb.get("sequence"):
        return relink_sequence(pb)
    pb["sequence"] = True
    if is_linear(pb):
        return True
    order = linear_order(pb)
    tasks = list(pb.get("tasks") or [])
    by = {str(t.get("id")): t for t in tasks}
    story_ids = {str(t.get("id")) for t in story_sequence(pb)}
    rest = [t for t in tasks if str(t.get("id")) not in story_ids]
    pb["tasks"] = [by[tid] for tid in order if tid in by] + rest
    relink_sequence(pb)
    return True


def normalize_playbook(name: str, data: dict) -> dict:
    """整体规格化 + 校验（导入/保存共用）；失败抛 QuestError。"""
    data = dict(data or {})
    data["name"] = name
    errs = validate_playbook(data)
    if errs:
        raise QuestError("剧本校验失败：" + "；".join(errs))
    tasks: list[dict] = []
    for raw in data.get("tasks") or []:
        tasks.append(normalize_task(raw, existing=tasks))
    out = {"name": name, "tasks": tasks}
    title = str(data.get("title") or "").strip()
    if title:
        out["title"] = title
    desc = str(data.get("description") or "").strip()
    if desc:
        out["description"] = desc
    version = _int_or(data.get("template_version"), 0)
    if version > 0:
        out["template_version"] = version
    if data.get("is_care_scene") or name == CARE_PLAYBOOK_NAME:
        out["is_care_scene"] = True
    else:
        # 目标检测间隔：每隔这么多小时检测一次，按顺序重新触发未达成的小目标
        out["check_interval_hours"] = clamp_check_interval_hours(data.get("check_interval_hours"))
    if data.get("sequence"):
        out["sequence"] = True  # 已经是一条线：列表顺序就是编排，边由顺序生成
    return out


# ── 文件读写 ──────────────────────────────────────────────────


def playbook_path(name: str) -> Path:
    if not PLAYBOOK_NAME_RE.match(str(name or "")):
        raise QuestError(f"非法剧本名: {name!r}（需匹配 {PLAYBOOK_NAME_RE.pattern}）")
    return playbooks_dir() / f"{name}.json"


def list_playbooks() -> list[str]:
    d = playbooks_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file() and PLAYBOOK_NAME_RE.match(p.stem))


def load_playbook(name: str) -> dict | None:
    path = playbook_path(name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuestError(f"剧本 {name} 读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise QuestError(f"剧本 {name} 内容不是 JSON 对象")
    data.setdefault("name", name)
    data.setdefault("tasks", [])
    return data


def write_playbook(name: str, data: dict) -> None:
    """原子写盘（tmp + os.replace）。"""
    path = playbook_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def delete_playbook_file(name: str) -> bool:
    path = playbook_path(name)
    if path.is_file():
        path.unlink()
        return True
    return False


def load_default_playbook_template() -> dict | None:
    """随包默认剧本模板（不落盘）；缺文件/损坏 → None。"""
    return _load_template(_DEFAULT_PLAYBOOK_FILE)


def load_care_playbook_template() -> dict | None:
    return _load_template(_CARE_PLAYBOOK_FILE)


def _load_template(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _ensure_template(name: str, template: dict | None) -> bool:
    """文件缺失、或比随包模板旧（template_version 更小）时从模板落盘。
    升级只换模板自带的小目标；主人自己加的小目标保留并接回原位，进度也保留。"""
    if template is None:
        return False
    try:
        path = playbook_path(name)
    except QuestError:
        return False
    current: dict = {}
    if path.is_file():
        try:
            current = load_playbook(name) or {}
        except QuestError:
            current = {}
        if _int_or(current.get("template_version"), 0) >= _int_or(template.get("template_version"), 0):
            return False
    try:
        write_playbook(name, merge_template_upgrade(name, current, template))
    except (QuestError, OSError):
        return False
    return True


def _bundled_task_ids() -> set[str]:
    out: set[str] = set()
    for tmpl in (load_default_playbook_template(), load_care_playbook_template()):
        for t in (tmpl or {}).get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                out.add(str(t["id"]))
    return out


def merge_template_upgrade(name: str, current: dict, template: dict) -> dict:
    """模板升级的合并规则：模板里的小目标按新模板换；不属于任何随包模板的（主人加的、小歪提的）保留，
    并接回原来的位置（A→自定义→B 里 A、B 换新后仍然是 A→自定义→B）。"""
    merged = normalize_playbook(name, template)
    tmpl_by = {str(t["id"]): t for t in merged.get("tasks") or []}
    # 模板明确退役的小目标（比如并进别的小目标里的备用分支）：升级时直接丢掉，不当成主人加的
    retired = {str(x) for x in (template.get("retired_task_ids") or []) if x}
    bundled = _bundled_task_ids() | set(tmpl_by) | retired
    old_tasks = [t for t in (current.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
    tmpl_titles = {str(t.get("title") or "").strip() for t in tmpl_by.values()}
    kept = [
        dict(t) for t in old_tasks
        if str(t["id"]) not in bundled and str(t.get("title") or "").strip() not in tmpl_titles
    ]
    # 场景级设置（目标检测间隔）是主人的，升级不动
    if current.get("check_interval_hours") is not None and not merged.get("is_care_scene"):
        merged["check_interval_hours"] = clamp_check_interval_hours(current.get("check_interval_hours"))
    if not kept:
        return merged
    kept_by = {str(t["id"]): t for t in kept}
    valid = set(tmpl_by) | set(kept_by)
    for t in kept:
        for key in ("on_success", "on_failure"):
            t[key] = [dict(r) for r in t.get(key) or [] if str(r.get("id")) in valid]
    after_of: dict[str, str] = {}  # 自定义小目标 → 它原来接在哪个模板小目标之后
    for old in old_tasks:
        new = tmpl_by.get(str(old["id"]))
        if new is None:
            continue
        for key in ("on_success", "on_failure"):
            refs = [r for r in old.get(key) or [] if str(r.get("id")) in kept_by]
            if not refs:
                continue
            tmpl_refs = list(new.get(key) or [])
            new[key] = [dict(refs[0])]
            custom = kept_by[str(refs[0]["id"])]
            after_of.setdefault(str(refs[0]["id"]), str(old["id"]))
            if not custom.get(key) and tmpl_refs:
                custom[key] = [dict(tmpl_refs[0])]
    tasks = list(merged.get("tasks") or [])
    for t in kept:
        anchor = after_of.get(str(t["id"]))
        pos = next((i for i, x in enumerate(tasks) if str(x.get("id")) == anchor), None) if anchor else None
        if pos is None:
            tasks.append(t)
        else:
            tasks.insert(pos + 1, t)
    merged["tasks"] = tasks
    return normalize_playbook(name, merged)


def ensure_care_playbook() -> bool:
    """「日常关心」场景缺失或过旧时落盘。"""
    return _ensure_template(CARE_PLAYBOOK_NAME, load_care_playbook_template())


def ensure_default_playbook() -> bool:
    """两个随包场景（初识、日常关心）缺失或过旧时落盘（幂等）；返回是否写入了任意一个。"""
    wrote_default = _ensure_template(DEFAULT_PLAYBOOK_NAME, load_default_playbook_template())
    wrote_care = ensure_care_playbook()
    return wrote_default or wrote_care


__all__ = [
    "ALL_STATUS",
    "CARE_DEFAULT_MAX_MISSES",
    "CARE_PAUSE_SEC",
    "CARE_PLAYBOOK_NAME",
    "CHECK_INTERVAL_MAX_HOURS",
    "DEFAULT_CHECK_INTERVAL_HOURS",
    "DEFAULT_NEXT_DELAY_SEC",
    "NEXT_DELAY_MAX_SEC",
    "RESULT_STATUS",
    "SETTLED_STATUS",
    "STATUS_PAUSED",
    "STATUS_SKIPPED",
    "STATUS_UNMET",
    "clamp_check_interval_hours",
    "KINDS",
    "KIND_CARE",
    "KIND_STORY",
    "DEFAULT_PLAYBOOK_NAME",
    "DEFAULT_REPEAT_INTERVAL_SEC",
    "PERFORM_EXPRESSION",
    "PERFORM_MODES",
    "PERFORM_SCENE",
    "PERFORM_SPEAK",
    "PLAYBOOK_NAME_RE",
    "PORT_FAILED",
    "PORT_KEYS",
    "PORT_SUCCESS",
    "SCHEDULE_TIME_RE",
    "QuestError",
    "STATUS_FAILED",
    "STATUS_NOT_STARTED",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "TASK_ID_RE",
    "TERMINAL_STATUS",
    "configure_playbooks_dir",
    "delete_playbook_file",
    "ensure_care_playbook",
    "merge_template_upgrade",
    "ensure_default_playbook",
    "has_cycle",
    "is_linear",
    "linear_order",
    "linearize",
    "relink_sequence",
    "story_sequence",
    "list_playbooks",
    "load_care_playbook_template",
    "load_default_playbook_template",
    "load_playbook",
    "normalize_perform",
    "normalize_playbook",
    "normalize_pos",
    "normalize_refs",
    "normalize_task",
    "playbook_path",
    "playbooks_dir",
    "validate_playbook",
    "write_playbook",
]
