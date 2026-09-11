"""主动陪伴控制台的简化层：把剧本引擎的内部概念折成用户能懂的几件事。

引擎（``quest_service``）有 分数 / 激活线 / 初始状态 / 沙箱设备 / 连线端口 这些机制；
控制台只呈现：
- 一个总开关（主动陪伴）+ 一个当前场景（选中即生效，没有"绑定"这一步）
- 主线场景 = 一条线：小目标按顺序上下排布，不论达成还是未达成都进入顺序里的下一个
  （落到引擎就是每一步的达成边、未达成边都指向下一步，边由顺序生成，用户不碰连线）
- 每个小目标一个状态小标：进行中 / 等待中 / 达成 / 未达成 / 跳过；同时只有一个在进行中
  （当前 = 顺序里第一个还没有结果的，拖动排序、跳过、重开之后都按这条重算）
- 结果只由小歪（按「达成条件」和主人的回复）或主人在控制台明确标；标了结果、大家安静 next_delay_sec
  （默认 10 s）就进入下一个；未达成的由场景的「目标检测间隔」（默认 1 小时）按顺序再提
- 「触发一次」走 Core 的 ``/api/quest_proactive/speak_now``

真实进度只有一份（本机档案 ``LOCAL_PROFILE_DEVICE``），没有沙箱视图。
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from deskbot_server.application import quest_proactive, quest_service
from deskbot_server.application.live_behavior import live_behavior_service
from deskbot_server.application.persona_generation import GenerationError, generate_json
from deskbot_server.application.quest_service import LOCAL_PROFILE_DEVICE
from deskbot_server.device_preferences import (
    load_preferences,
    quiet_hours_active,
    update_preferences,
)
from deskbot_server.pb.scenes import _pb_scene_keys_sorted as expression_scene_names
from deskbot_server.quest_playbooks_store import (
    CARE_DEFAULT_MAX_MISSES,
    DEFAULT_CHECK_INTERVAL_HOURS,
    DEFAULT_NEXT_DELAY_SEC,
    DEFAULT_PLAYBOOK_NAME,
    DEFAULT_REPEAT_INTERVAL_SEC,
    KIND_CARE,
    KIND_STORY,
    KINDS,
    NEXT_DELAY_MAX_SEC,
    PERFORM_EXPRESSION,
    PERFORM_SCENE,
    PERFORM_SPEAK,
    RESULT_STATUS,
    SCHEDULE_TIME_RE,
    SETTLED_STATUS,
    STATUS_FAILED,
    STATUS_NOT_STARTED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_UNMET,
    QuestError,
    clamp_check_interval_hours,
    ensure_default_playbook,
    load_default_playbook_template,
    normalize_perform,
    relink_sequence,
    story_sequence,
)
from deskbot_server.scene_playbooks_store import load_scene_playbooks_file

logger = logging.getLogger("deskbot-server")

TEMPLATE_DEFAULT = "default"
TEMPLATE_BLANK = "blank"
TEMPLATES = (TEMPLATE_DEFAULT, TEMPLATE_BLANK)

# 页面上的状态小标：引擎状态 → 用户看到的词
STATUS_LABEL = {
    STATUS_RUNNING: "进行中",
    STATUS_NOT_STARTED: "等待中",
    STATUS_SUCCESS: "达成",
    STATUS_UNMET: "未达成",
    STATUS_SKIPPED: "跳过",
    STATUS_PAUSED: "已暂停",
    STATUS_FAILED: "未达成",  # 旧数据，读到即迁移
}

_TASK_FIELDS = ("title", "goal", "strategy", "success_condition", "trigger_hint")

# 执行方式模板：用户可以选一个再改，也可以完全自己写
PERFORM_TEMPLATES = [
    {
        "key": "ask",
        "label": "轻声问一句",
        "mode": PERFORM_SPEAK,
        "strategy": "自然地问一句，不要像填表；主人不想说就别再提，下次换个角度。",
    },
    {
        "key": "expression",
        "label": "先做个表情再问",
        "mode": PERFORM_EXPRESSION,
        "strategy": "先用表情引起注意，再轻松地问一句；主人没搭理就先安静。",
    },
    {
        "key": "scene",
        "label": "先演一段表演再说",
        "mode": PERFORM_SCENE,
        "strategy": "表演结束后顺着刚才的话题接一句，不要重复打招呼。",
    },
]


# ── 剧本 ──────────────────────────────────────────────────────


def _new_playbook_name() -> str:
    existing = set(quest_service.list_playbooks())
    for _ in range(20):
        name = "pb_" + secrets.token_hex(3)
        if name not in existing:
            return name
    raise QuestError("生成场景名失败，请重试")


def create_playbook(title: str, template: str = TEMPLATE_BLANK) -> dict:
    """新建剧本：用户只给标题；文件名自动生成；模板 = 随包「与主人的初识」或空白。"""
    title = str(title or "").strip()
    if not title:
        raise QuestError("给场景起个名字")
    if len(title) > 40:
        raise QuestError("场景名最多 40 个字")
    template = str(template or TEMPLATE_BLANK).strip() or TEMPLATE_BLANK
    if template not in TEMPLATES:
        raise QuestError(f"未知模板: {template}")
    name = _new_playbook_name()
    if template == TEMPLATE_DEFAULT:
        data = dict(load_default_playbook_template() or {"tasks": []})
    else:
        data = {"tasks": []}
    data["title"] = title
    data.pop("description", None)
    quest_service.save_playbook(name, data)
    _relink_sequence(name)
    return quest_service.require_playbook(name)


_GENERATE_INSTRUCTION = (
    "现在你要给自己设计一个「陪伴场景」：把主人这一句话拆成 1-8 件你要主动做的事，用你自己的口吻写。"
    "只输出 JSON 对象，不要解释、不要代码块。格式：\n"
    '{"title":"场景名（10 字内）","tasks":[{"kind":"story 或 care","title":"目标名（12 字内）","goal":"要达成什么（一句话）",'
    '"strategy":"怎么开口、有什么分寸","success_condition":"怎样算达成（也写清什么情况算没达成）",'
    '"trigger_hint":"什么情况下提，没有就空串","schedule_time":"每天几点提，HH:MM；不定时就空串",'
    '"repeat_interval_sec":86400}]}\n'
    "kind 怎么选：care = 长期反复做的小事（每天晚上讲个睡前故事、到点提醒健身、偶尔讲个冷笑话），写清什么情况下提、"
    "多久最多一次（repeat_interval_sec 只能是 3600 / 14400 / 86400 / 604800）或者每天几点（schedule_time）；"
    "story = 只做成一次就过、按先后顺序推进的事（比如先约好晚上几点、再确认主人喜欢什么类型的故事），你聊完一步就按达成条件标达成或未达成，然后进入下一个。"
    "硬规矩：每件事都必须直接来自主人这句话；主人说的是反复做的事就只给 care，不要为了凑主线编「认识主人」「问称呼」「问今天状态」这类事；"
    "下面「已有的小目标」和「对主人的了解」里提到的陪伴小目标都是已经在做或做过的，一律不要再列。"
    "「提醒我 xx」这类也算你要主动做的事（care + schedule_time 或 trigger_hint），不要因为像提醒就不给。"
    "你能做的事：说话、做表情、转头、看摄像头、记住主人说的话、控制米家设备（比如用音箱放歌、开关灯——前提是主人家里有这些设备，没有就别写）。"
    "内容要具体、不要空话；每个字段都不要超过 80 字；tasks 至少给 1 件。"
)

_GENERATE_INTERVALS = (3600, 14400, 86400, 604800)


def _existing_goal_titles() -> list[str]:
    """所有场景里已有的小目标名（给模型看、也用来去重）。"""
    out: list[str] = []
    for name in quest_service.list_playbooks():
        try:
            pb = quest_service.require_playbook(name)
        except QuestError:
            continue
        for t in pb.get("tasks") or []:
            title = str(t.get("title") or "").strip()
            if title and title not in out:
                out.append(title)
    return out


def _generated_tasks(data: dict[str, Any]) -> list[dict]:
    """从模型输出里取任务列表：优先 tasks；模型换了键名（goals / items / care / story）也认；goal 缺了用 description 顶上。"""
    rows: Any = data.get("tasks")
    if not isinstance(rows, list):
        rows = []
        for key in ("goals", "items", "story", "care", "list"):
            val = data.get(key)
            if isinstance(val, list):
                rows.extend(val)
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if not str(row.get("goal") or "").strip():
            for alt in ("description", "content", "text", "task"):
                if str(row.get(alt) or "").strip():
                    row["goal"] = row[alt]
                    break
        out.append(row)
    return out[:8]


def generate_playbook(description: str, *, title: str = "", select: bool = True) -> dict[str, Any]:
    """「AI 生成场景」：模型按格式给出几件事 → 主线小目标新建一个场景按顺序排成一条线；
    反复做的（kind=care）直接加进「日常关心」。只给了日常关心时不建空场景。
    模型抄已有小目标（同名）的直接丢掉——User.md 里记着「陪伴小目标 xx 达成」，模型见了爱照抄。

    返回 {"playbook": 场景定义或 None, "story_count", "care_count", "care_titles", "dropped"}。"""
    ensure_default_playbook()  # 随包场景也算「已有的小目标」，模型别再编一遍认识主人
    existing = _existing_goal_titles()
    instruction = _GENERATE_INSTRUCTION
    if existing:
        instruction += "\n已有的小目标（不要再列、不要换个说法重复）：" + "、".join(existing[:40])
    data = generate_json(instruction, description, temperature=0.7)
    raw_tasks = _generated_tasks(data)
    if not raw_tasks:
        logger.warning("[quest] AI 生成场景没有任务，模型原始输出: %s", json.dumps(data, ensure_ascii=False)[:600])
    story: list[dict] = []
    care: list[dict] = []
    dropped: list[str] = []
    for raw in raw_tasks:
        goal = str(raw.get("goal") or "").strip()[:400]
        if not goal:
            continue
        title_text = str(raw.get("title") or "").strip()[:40]
        if title_text and title_text in existing:
            dropped.append(title_text)
            continue
        fields = {
            "title": str(raw.get("title") or "").strip()[:40],
            "goal": goal,
            "strategy": str(raw.get("strategy") or "").strip()[:400],
            "success_condition": str(raw.get("success_condition") or "").strip()[:400],
            "trigger_hint": str(raw.get("trigger_hint") or "").strip()[:200],
        }
        if str(raw.get("kind") or "").strip().lower() == KIND_CARE:
            sched = str(raw.get("schedule_time") or "").strip()
            fields["kind"] = KIND_CARE
            fields["schedule_time"] = sched if SCHEDULE_TIME_RE.match(sched) else ""
            try:
                interval = int(raw.get("repeat_interval_sec") or 0)
            except (TypeError, ValueError):
                interval = 0
            fields["repeat_interval_sec"] = interval if interval in _GENERATE_INTERVALS else DEFAULT_REPEAT_INTERVAL_SEC
            care.append(fields)
        else:
            story.append(fields)
    if not story and not care:
        if dropped:
            raise GenerationError("模型给出的都是已有的小目标（" + "、".join(dropped[:4]) + "），换个说法再试")
        raise GenerationError("模型没有给出小目标，请把想法说具体一点")
    if dropped:
        logger.info("[quest] AI 生成场景丢掉与已有小目标同名的 %d 条: %s", len(dropped), "、".join(dropped))
    pb: dict[str, Any] | None = None
    if story:
        final_title = str(title or data.get("title") or description).strip()[:40] or "新场景"
        pb = create_playbook(final_title, TEMPLATE_BLANK)
        name = pb["name"]
        for fields in story:
            add_task(name, fields)
        if select:
            select_playbook(name)
        pb = quest_service.require_playbook(name)
    care_titles: list[str] = []
    care_scene = quest_service.care_playbook()
    if care and care_scene:
        for fields in care:
            view = add_task(care_scene, fields)
            care_titles.append(str(view.get("title") or ""))
    return {
        "playbook": pb, "story_count": len(story), "care_count": len(care_titles), "care_titles": care_titles,
        "dropped": dropped,
    }


def rename_playbook(name: str, title: str) -> dict:
    return update_playbook_settings(name, {"title": title})


def update_playbook_settings(name: str, raw: dict[str, Any]) -> dict:
    """场景级设置：场景名 / 目标检测间隔（小时，1–24）。"""
    pb = quest_service.require_playbook(name)
    changed = False
    if "title" in raw:
        title = str(raw.get("title") or "").strip()
        if not title:
            raise QuestError("场景名不能为空")
        if len(title) > 40:
            raise QuestError("场景名最多 40 个字")
        pb["title"] = title
        changed = True
    if "check_interval_hours" in raw:
        if quest_service.is_care_scene(name):
            raise QuestError("日常关心没有目标检测间隔")
        try:
            hours = int(raw.get("check_interval_hours"))
        except (TypeError, ValueError) as exc:
            raise QuestError("目标检测间隔必须是整数小时") from exc
        if hours < 1 or hours > 24:
            raise QuestError("目标检测间隔要在 1–24 小时之间")
        pb["check_interval_hours"] = hours
        changed = True
    if not changed:
        raise QuestError("没有可保存的设置")
    return quest_service.save_playbook(name, pb)


def select_playbook(name: str | None) -> dict[str, Any]:
    """选中当前剧本（"" = 不用剧本）。选中即生效，真实进度自动初始化。"""
    value = str(name or "").strip()
    if value:
        quest_service.require_playbook(value)
    update_preferences({"quest": {"playbook": value}})
    if value:
        _relink_sequence(value)
    return overview()


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """总开关 / 冷场时长 / 每日上限 / 空闲张望（开关、档位、频率、等待）。返回最新概览。"""
    out: dict[str, Any] = {}
    if "enabled" in patch:
        out["proactive_enabled"] = bool(patch.get("enabled"))
    for key in ("idle_sec", "daily_limit", "care_daily_limit", "reminder_soon_sec", "care_pause_sec"):
        if key in patch:
            out[key] = patch.get(key)
    if "playbook" in patch:
        value = patch.get("playbook")
        value = "" if value is None else str(value).strip()
        if value:
            quest_service.require_playbook(value)
        out["playbook"] = value
    behavior: dict[str, Any] = {}
    if "idle_live" in patch:
        behavior["idle_live"] = bool(patch.get("idle_live"))
    if "idle_motion" in patch:
        level = str(patch.get("idle_motion") or "").strip().lower()
        if level not in ("gentle", "normal", "bold"):
            raise QuestError("待机动作档位只能是 gentle / normal / bold")
        behavior["idle_motion"] = level
    for key, label in (("wander_per_min", "张望频率"), ("wander_idle_sec", "空闲多久开始张望")):
        if key in patch:
            try:
                behavior[key] = int(patch.get(key))
            except (TypeError, ValueError) as exc:
                raise QuestError(f"{label}必须是整数") from exc
    if not out and not behavior:
        raise QuestError("没有可保存的设置")
    try:
        merged: dict[str, Any] = {}
        if out:
            merged["quest"] = out
        if behavior:
            merged["behavior"] = behavior
        update_preferences(merged)
    except ValueError as exc:
        raise QuestError(str(exc)) from exc
    if out.get("playbook"):
        _relink_sequence(out["playbook"])
    return overview()


# ── 小目标 ────────────────────────────────────────────────────


def _auto_task_id(tasks: list[dict]) -> str:
    used = {str(t.get("id") or "") for t in tasks}
    n = len(tasks) + 1
    while f"t{n}" in used:
        n += 1
    return f"t{n}"


def _pick_fields(raw: dict) -> dict:
    out = {k: str(raw.get(k) or "").strip() for k in _TASK_FIELDS if k in raw}
    if "perform" in raw:
        out["perform"] = normalize_perform(raw.get("perform"))
    if "repeat_interval_sec" in raw:
        try:
            out["repeat_interval_sec"] = max(int(raw.get("repeat_interval_sec") or 0), 60)
        except (TypeError, ValueError) as exc:
            raise QuestError("重复间隔必须是数字") from exc
    if "max_attempts" in raw:
        # 只对日常关心有意义：连续几次没回应就先歇一天（主线不数次数，存时会归零）
        try:
            out["max_attempts"] = max(int(raw.get("max_attempts") or 0), 0)
        except (TypeError, ValueError) as exc:
            raise QuestError("没回应次数必须是数字") from exc
    if "next_delay_sec" in raw:
        try:
            out["next_delay_sec"] = min(max(int(raw.get("next_delay_sec") or 0), 0), NEXT_DELAY_MAX_SEC)
        except (TypeError, ValueError) as exc:
            raise QuestError("完成后多久触发下一个必须是数字（秒）") from exc
    if "schedule_time" in raw:
        val = str(raw.get("schedule_time") or "").strip()
        if val and not SCHEDULE_TIME_RE.match(val):
            raise QuestError("定时要写成 HH:MM，比如 16:00")
        out["schedule_time"] = val
    if "schedule_days" in raw:
        days = raw.get("schedule_days")
        if not isinstance(days, list):
            raise QuestError("schedule_days 必须是列表")
        out["schedule_days"] = [int(d) for d in days if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6]
    if "kind" in raw:
        kind = str(raw.get("kind") or KIND_STORY).strip()
        if kind not in KINDS:
            raise QuestError("类型只能是 主线 或 日常关心")
        out["kind"] = kind
        # 反复做的事是日常关心；主线不回头
        out["repeatable"] = kind == KIND_CARE
    return out


def _move_after(tasks: list[dict], task_id: str, after: str | None) -> None:
    """把 task_id 挪到 after 之后（after 为空 = 挪到末尾）；原地改列表。"""
    idx = next((i for i, t in enumerate(tasks) if str(t.get("id")) == task_id), None)
    if idx is None:
        raise QuestError(f"小目标不存在: {task_id}")
    item = tasks.pop(idx)
    if after:
        pos = next((i for i, t in enumerate(tasks) if str(t.get("id")) == after), None)
        if pos is None:
            raise QuestError(f"小目标不存在: {after}")
        tasks.insert(pos + 1, item)
    else:
        tasks.append(item)


def add_task(name: str, raw: dict, *, after: str | None = None) -> dict:
    """添加小目标：不用给 id；主线默认接在末尾，也可以「插在某一步之后」。"""
    fields = _pick_fields(raw)
    if fields.get("kind") == KIND_CARE or quest_service.is_care_scene(name):
        # 日常关心只住在「日常关心」场景里
        fields["kind"] = KIND_CARE
        fields["repeatable"] = True
        name = quest_service.care_playbook() or name
    if not quest_service.is_care_scene(name):
        quest_service.ensure_linear_playbook(name)
    pb = quest_service.require_playbook(name)
    tasks = pb.get("tasks") or []
    if not fields.get("goal"):
        raise QuestError("写一句它要达成什么（目标）")
    if not fields.get("title"):
        fields["title"] = fields["goal"][:20]
    payload = {"id": _auto_task_id(tasks), "activation_score": 1, **fields}
    task = quest_service.add_task(name, payload)
    after = str(after or "").strip()
    if after and fields.get("kind") != KIND_CARE:
        pb = quest_service.require_playbook(name)
        _move_after(pb["tasks"], task["id"], after)
        quest_service.save_playbook(name, pb)
    _relink_sequence(name)
    return _task_view(name, task["id"])


def reorder_tasks(name: str, ids: list[str]) -> list[dict]:
    """拖动排序：传主线小目标的完整新顺序（id 列表）。结果跟着小目标走，当前按「第一个没结果的」重算。"""
    if quest_service.is_care_scene(name):
        raise QuestError("日常关心不分先后，不用排序")
    quest_service.ensure_linear_playbook(name)
    pb = quest_service.require_playbook(name)
    seq_ids = [str(t.get("id")) for t in story_sequence(pb)]
    wanted = [str(i or "").strip() for i in (ids if isinstance(ids, list) else [])]
    if sorted(wanted) != sorted(seq_ids) or len(set(wanted)) != len(wanted):
        raise QuestError("顺序里的小目标和场景里的对不上，刷新后再试")
    by = {str(t.get("id")): t for t in pb.get("tasks") or []}
    rest = [t for t in pb.get("tasks") or [] if str(t.get("id")) not in set(seq_ids)]
    pb["tasks"] = [by[tid] for tid in wanted] + rest
    quest_service.save_playbook(name, pb)
    _relink_sequence(name)
    return _task_views(name)


def update_task(name: str, task_id: str, raw: dict) -> dict:
    """改文字字段 / 进入下一个的条件 / 类型（主线 ↔ 日常关心会搬家）。顺序用拖动改，不在这里。"""
    fields = _pick_fields(raw)
    if "goal" in fields and not fields["goal"]:
        raise QuestError("目标不能为空")
    if fields.get("kind") == KIND_STORY and quest_service.is_care_scene(name):
        # 日常关心改成主线：得有在用的主线场景可以搬过去，先检查再落盘（别把主线小目标留在日常关心里）
        story = quest_service.bound_playbook()
        if not story or quest_service.is_care_scene(story):
            raise QuestError("没有在用的主线场景，这条日常关心改不成主线小目标")
    if fields:
        quest_service.update_task(name, task_id, fields)
    defn = quest_service.get_task_definition(name, task_id) or {}
    if quest_service.is_care(defn) and not quest_service.is_care_scene(name):
        # 主线里的小目标改成日常关心 → 搬进「日常关心」场景（连线清掉、进度一起搬）
        care = quest_service.care_playbook()
        if care and quest_service.move_tasks_to_care_scene(name, [task_id]):
            _relink_sequence(name)
            _relink_sequence(care)
            return _task_view(care, task_id)
    if not quest_service.is_care(defn) and quest_service.is_care_scene(name):
        # 日常关心改成主线 → 搬进在用的主线场景末尾（不能留在日常关心里，页面上会看不见）
        story = quest_service.bound_playbook() or ""
        quest_service.move_task_to_story(name, task_id, story)
        _relink_sequence(name)
        _relink_sequence(story)
        return _task_view(story, task_id)
    _relink_sequence(name)
    return _task_view(name, task_id)


def approve_proposal(
    name: str,
    task_id: str,
    *,
    kind: str = KIND_CARE,
    after: str | None = None,
    repeat_interval_sec: int | None = None,
    trigger_hint: str | None = None,
    schedule_time: str | None = None,
) -> dict:
    """主人同意小歪提的小目标。

    默认作为「日常关心」：给频率和条件，不进主线；选「主线」才搬进在用的主线场景，
    默认插在当前进行中的那一步之后（也可以指定插在哪一步之后）。"""
    pb = quest_service.require_playbook(name)
    task = next((t for t in pb.get("tasks") or [] if t.get("id") == task_id), None)
    if task is None:
        raise QuestError(f"小目标不存在: {task_id}")
    kind = str(kind or KIND_CARE).strip()
    if kind not in KINDS:
        raise QuestError("类型只能是 主线 或 日常关心")
    if kind == KIND_STORY and quest_service.is_care_scene(name):
        story = quest_service.bound_playbook()
        if not story or quest_service.is_care_scene(story):
            raise QuestError("没有开启的主线场景，只能作为日常关心")
        pb["tasks"] = [t for t in pb.get("tasks") or [] if t.get("id") != task_id]
        quest_service.save_playbook(name, pb)
        quest_service._delete_task_instances(name, task_id)
        quest_service.ensure_linear_playbook(story)
        story_pb = quest_service.require_playbook(story)
        if any(t.get("id") == task_id for t in story_pb.get("tasks") or []):
            raise QuestError(f"主线里已有同名小目标 {task_id}")
        story_pb.setdefault("tasks", []).append(task)
        quest_service.save_playbook(story, story_pb)
        name, pb = story, quest_service.require_playbook(story)
        task = next(t for t in pb["tasks"] if t.get("id") == task_id)
    task["proposed"] = False
    task["kind"] = kind
    if kind == KIND_STORY:
        task["repeatable"] = False
        task["max_attempts"] = 0
        task["next_delay_sec"] = DEFAULT_NEXT_DELAY_SEC
        task["schedule_time"] = ""
        task["schedule_days"] = []
    if kind == KIND_CARE:
        task["repeatable"] = True
        if repeat_interval_sec is not None:
            task["repeat_interval_sec"] = max(int(repeat_interval_sec), 60)
        if trigger_hint is not None:
            task["trigger_hint"] = str(trigger_hint).strip()[:200]
        if schedule_time is not None:
            val = str(schedule_time).strip()
            if val and not SCHEDULE_TIME_RE.match(val):
                raise QuestError("定时要写成 HH:MM，比如 16:00")
            task["schedule_time"] = val
        task["max_attempts"] = int(task.get("max_attempts") or CARE_DEFAULT_MAX_MISSES)
    if kind == KIND_STORY:
        after_id = str(after if after is not None else task.get("proposed_after") or "").strip()
        ids = {str(t.get("id")) for t in pb.get("tasks") or []}
        if not after_id or after_id not in ids:
            after_id = _current_story_id(name) or ""
        if after_id and after_id != task_id and after_id in ids:
            _move_after(pb["tasks"], task_id, after_id)
    quest_service.save_playbook(name, pb)
    _relink_sequence(name)
    return _task_view(name, task_id)


def delete_task(name: str, task_id: str) -> None:
    quest_service.delete_task(name, task_id)
    _relink_sequence(name)


def _current_story_id(name: str) -> str | None:
    """在用的主线里当前进行中的那一步（顺序里第一个还没有结果的）。"""
    if name != quest_service.bound_playbook():
        return None
    try:
        return quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, name)
    except QuestError:
        return None


def _relink_sequence(name: str) -> None:
    """顺序 = 编排：按当前顺序重算主线的边和起点；在用的场景顺带把进度对齐到「第一个没结果的」。"""
    if quest_service.is_care_scene(name):
        pb = quest_service.require_playbook(name)
        dirty = False
        for t in pb.get("tasks") or []:
            if t.get("on_success") or t.get("on_failure"):
                t["on_success"], t["on_failure"] = [], []
                dirty = True
        if dirty:
            quest_service.save_playbook(name, pb)
        return
    quest_service.ensure_linear_playbook(name)
    pb = quest_service.require_playbook(name)
    dirty = relink_sequence(pb)
    if not pb.get("sequence"):
        pb["sequence"] = True
        dirty = True
    if dirty:
        quest_service.save_playbook(name, pb)
    if name == quest_service.bound_playbook():
        quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, name)


# ── 真实进度上的操作 ──────────────────────────────────────────


def _current_or_raise() -> str:
    playbook = quest_service.bound_playbook()
    if not playbook:
        raise QuestError("还没有开启任何主线场景")
    return playbook


def _playbook_for(task_id: str) -> str:
    """task_id 在哪个生效中的场景（主线或日常关心）。"""
    name = quest_service.resolve_task_playbook(task_id)
    if not name:
        raise QuestError(f"小目标不在进行中的场景里: {task_id}")
    return name


def restart_task(task_id: str) -> dict:
    """达成 / 未达成 / 跳过 → 重新开始这个（它变成当前，原来进行中的退回等待中）；日常关心：重新打开 / 现在就恢复。"""
    playbook = _playbook_for(task_id)
    if not quest_service.get_instances(LOCAL_PROFILE_DEVICE, playbook):
        quest_service.ensure_instances(LOCAL_PROFILE_DEVICE, playbook)
    quest_service.restart_task(LOCAL_PROFILE_DEVICE, playbook, task_id)
    if not quest_service.is_care_scene(playbook):
        quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, playbook)
    return _task_view(playbook, task_id)


def start_from(task_id: str) -> dict:
    """「从这个开始」：它前面还没有结果的都记为未达成（主人跳过），它成为当前。"""
    playbook = _playbook_for(task_id)
    if quest_service.is_care_scene(playbook):
        raise QuestError("日常关心不分先后")
    skipped = quest_service.skip_until(LOCAL_PROFILE_DEVICE, playbook, task_id)
    return {"task": _task_view(playbook, task_id), "skipped": skipped}


_MARK_NOTE = {
    STATUS_SUCCESS: "主人在控制台记为达成",
    STATUS_UNMET: "主人在控制台记为未达成",
    STATUS_SKIPPED: "主人在控制台跳过了这个",
}


def mark_task(task_id: str, status: str, note: str = "") -> dict:
    """手动把进行中的小目标记成 达成 / 未达成 / 跳过（与小歪的工具同一条路：标了结果，安静几秒后进入下一个）。"""
    playbook = _playbook_for(task_id)
    status = str(status or "").strip()
    if status == STATUS_FAILED:
        status = STATUS_UNMET
    if status not in RESULT_STATUS:
        raise QuestError("只能记成 达成 / 未达成 / 跳过")
    defn = quest_service.get_task_definition(playbook, task_id) or {}
    if quest_service.is_care(defn) and status != STATUS_SUCCESS:
        note = str(note or "").strip() or "主人在控制台按了暂停"
    else:
        note = str(note or "").strip() or _MARK_NOTE[status]
    out = quest_service.update_task_result(LOCAL_PROFILE_DEVICE, playbook, task_id, status, note)
    if not quest_service.is_care_scene(playbook):
        quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, playbook)
    return {"task": _task_view(playbook, task_id), "activated": [a["task_id"] for a in out.get("activated") or []]}


def reset_progress() -> dict:
    playbook = _current_or_raise()
    return quest_service.reset_instances(LOCAL_PROFILE_DEVICE, playbook)


# ── 概览 ──────────────────────────────────────────────────────


def _task_view(playbook: str, task_id: str) -> dict:
    for item in _task_views(playbook):
        if item["id"] == task_id:
            return item
    raise QuestError(f"小目标不存在: {task_id}")


def _task_views(playbook: str) -> list[dict]:
    pb = quest_service.require_playbook(playbook)
    tasks = pb.get("tasks") or []
    is_care_scene = quest_service.is_care_scene(playbook)
    is_current = playbook == quest_service.bound_playbook() or is_care_scene
    if is_current:
        have = {r["task_id"] for r in quest_service.get_instances(LOCAL_PROFILE_DEVICE, playbook)}
        if any(str(t.get("id")) not in have for t in tasks):
            quest_service.ensure_instances(LOCAL_PROFILE_DEVICE, playbook)
        quest_service.migrate_legacy_statuses(LOCAL_PROFILE_DEVICE, playbook)
        if is_care_scene:
            quest_service.reactivate_due_repeats(LOCAL_PROFILE_DEVICE, playbook)
        else:
            quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, playbook)
    # 非当前场景也显示上次留下的进度（切换时进度各自保留）
    rows = {r["task_id"]: r for r in quest_service.get_instances(LOCAL_PROFILE_DEVICE, playbook)}
    order = {str(t.get("id")): i + 1 for i, t in enumerate(story_sequence(pb))}
    out: list[dict] = []
    for t in tasks:
        tid = str(t.get("id"))
        row = rows.get(tid)
        if t.get("proposed"):
            status = STATUS_NOT_STARTED
        elif row is not None:
            status = row["status"]
        else:
            status = STATUS_RUNNING if t.get("initial_status") == STATUS_RUNNING else STATUS_NOT_STARTED
        perform = normalize_perform(t.get("perform"))
        out.append(
            {
                "id": tid,
                "order": order.get(tid, 0),
                "title": str(t.get("title") or tid),
                "goal": str(t.get("goal") or ""),
                "strategy": str(t.get("strategy") or ""),
                "success_condition": str(t.get("success_condition") or ""),
                "trigger_hint": str(t.get("trigger_hint") or ""),
                "perform": perform,
                "repeatable": bool(t.get("repeatable")),
                "repeat_interval_sec": int(t.get("repeat_interval_sec") or DEFAULT_REPEAT_INTERVAL_SEC),
                "max_attempts": int(t.get("max_attempts") or 0),
                "next_delay_sec": int(t.get("next_delay_sec", DEFAULT_NEXT_DELAY_SEC) or 0),
                "attempt_count": int((row or {}).get("attempt_count") or 0),
                "proposed": bool(t.get("proposed")),
                "proposed_after": str(t.get("proposed_after") or ""),
                "kind": KIND_CARE if quest_service.is_care(t) else KIND_STORY,
                "schedule_time": str(t.get("schedule_time") or ""),
                "schedule_days": list(t.get("schedule_days") or []),
                "paused": bool(quest_service.is_care(t) and status in (STATUS_PAUSED, STATUS_FAILED)),
                "paused_until": (row or {}).get("paused_until"),
                "resume_at": _resume_at(t, row, status),
                "status": status,
                "status_label": STATUS_LABEL.get(status, status),
                "result": (row or {}).get("result"),
                "note": (row or {}).get("strategy_override"),
                "started_at": (row or {}).get("started_at"),
                "finished_at": (row or {}).get("finished_at"),
            }
        )
    return out


def _resume_at(defn: dict, row: dict | None, status: str) -> str | None:
    """日常关心达成后的冷却到期时间（ISO）；不适用返回 None。"""
    if not quest_service.is_care(defn) or not row or status != STATUS_SUCCESS or not row.get("finished_at"):
        return None
    try:
        finished = datetime.fromisoformat(str(row["finished_at"]))
    except ValueError:
        return None
    return (finished + timedelta(seconds=int(defn.get("repeat_interval_sec") or 86400))).isoformat(timespec="seconds")


def _perform_options() -> dict[str, Any]:
    """执行方式可选项：表情名（本机表情文件）、表演（场景编排）、模板。"""
    try:
        expressions = list(expression_scene_names())
    except Exception:  # noqa: BLE001
        expressions = []
    scenes: list[dict[str, str]] = []
    try:
        for row in load_scene_playbooks_file() or []:
            name = str(row.get("name") or "").strip()
            if name:
                scenes.append({"name": name, "title": str(row.get("title") or name)})
    except Exception:  # noqa: BLE001
        scenes = []
    return {"expressions": expressions, "scenes": scenes, "templates": PERFORM_TEMPLATES}


def _playbook_cards(current: str | None) -> list[dict]:
    """页面上每个场景一张卡：定义 + 各自保留的进度；当前场景排最前。"""
    out: list[dict] = []
    for name in quest_service.list_playbooks():
        try:
            pb = quest_service.require_playbook(name)
        except QuestError:
            continue
        is_care_scene = quest_service.is_care_scene(name)
        if not is_care_scene:
            try:
                quest_service.ensure_linear_playbook(name)
                pb = quest_service.require_playbook(name)
            except QuestError:
                pass
        try:
            views = _task_views(name)
        except QuestError:
            views = []
        tasks = [t for t in views if not t["proposed"] and t["kind"] != KIND_CARE]
        care = [t for t in views if not t["proposed"] and t["kind"] == KIND_CARE]
        unmet = [t["id"] for t in tasks if t["status"] in (STATUS_UNMET, STATUS_FAILED)]
        out.append(
            {
                "name": name,
                "title": str(pb.get("title") or name),
                "description": str(pb.get("description") or ""),
                "check_interval_hours": (
                    clamp_check_interval_hours(pb.get("check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS))
                    if not is_care_scene else 0
                ),
                "unmet_count": len(unmet),
                # settled：全部达成或跳过（不会再提）；finished：全都有结果（可能还有未达成的等定时检测）
                "settled": bool(tasks) and all(t["status"] in SETTLED_STATUS for t in tasks),
                "next_check_at": None,
                "task_count": len(tasks),
                "care_count": len(care),
                "care_tasks": care,
                "is_default": name == DEFAULT_PLAYBOOK_NAME or is_care_scene,
                "is_care_scene": is_care_scene,
                # 主线场景：在用的那个；日常关心：随主动陪伴一直生效
                "is_current": (name == current) or is_care_scene,
                "enabled": (name == current) or is_care_scene,
                "tasks": tasks,
                "proposals": [t for t in views if t["proposed"]],
                "finished": quest_service.playbook_finished(LOCAL_PROFILE_DEVICE, name),
            }
        )
    # 顺序：在用的主线 → 日常关心 → 其它主线（默认的在前）
    out.sort(key=lambda c: (0 if (c["enabled"] and not c["is_care_scene"]) else 1 if c["is_care_scene"] else 2, not c["is_default"], c["title"]))
    return out


def reset_playbook_progress(name: str) -> dict:
    quest_service.require_playbook(name)
    out = quest_service.reset_instances(LOCAL_PROFILE_DEVICE, name)
    if not quest_service.is_care_scene(name):
        quest_service.sync_sequence_cursor(LOCAL_PROFILE_DEVICE, name)
    return out


def _fmt_ts(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")


def overview(proactive: dict[str, Any] | None = None, *, live: dict[str, Any] | None = None) -> dict[str, Any]:
    """页面一次拿全：开关、当前剧本、小目标、设置、主动开口状态。

    ``proactive`` 是 Core 进程的循环快照（控制台经 ``/api/quest_proactive`` 取到后传入）；
    None = Core 不可达或循环未启动。``live`` 同理是 Core 的待机张望状态（``/api/live_behavior``）。"""
    ensure_default_playbook()
    prefs = load_preferences()
    quest = dict(prefs.get("quest") or {})
    quiet = dict(prefs.get("quiet_hours") or {})
    behavior = dict(prefs.get("behavior") or {})
    try:
        quest_service.migrate_care_tasks_into_care_scene()
        quest_service.expire_stale_proposals()
    except Exception:  # noqa: BLE001
        pass
    playbook = quest_service.bound_playbook()
    pb = quest_service.get_playbook(playbook) if playbook else None
    cards = _playbook_cards(playbook)
    current_card = next((c for c in cards if c["is_current"] and not c["is_care_scene"]), None)
    care_card = next((c for c in cards if c["is_care_scene"]), None)
    tasks = current_card["tasks"] if current_card else []
    care_tasks = care_card["care_tasks"] if care_card else []
    proposals = care_card["proposals"] if care_card else []
    finished = bool(current_card and current_card["finished"])
    snap = dict(proactive) if proactive else (quest_proactive.status_snapshot() or {})
    _annotate_next_attempt(cards, snap)
    skip = str(snap.get("last_skip_reason") or "")
    titles = {t["id"]: t["title"] for t in tasks}
    last_task = ((snap.get("last_turn") or {}).get("task_id")) if snap else None
    return {
        "enabled": bool(quest.get("proactive_enabled", True)),
        "playbook": (
            {
                "name": playbook,
                "title": str((pb or {}).get("title") or playbook),
                "description": str((pb or {}).get("description") or ""),
                "task_count": len(tasks),
            }
            if playbook
            else None
        ),
        "playbooks": cards,
        "tasks": tasks,
        "care_tasks": care_tasks,
        "proposals": proposals,
        "finished": finished,
        "perform_options": _perform_options(),
        "settings": {
            "idle_sec": int(quest.get("idle_sec") or 60),
            "daily_limit": int(quest.get("daily_limit", 16)),
            "care_daily_limit": int(quest.get("care_daily_limit", 10)),
            "reminder_soon_sec": int(quest.get("reminder_soon_sec", 90)),
            "care_pause_sec": int(quest.get("care_pause_sec", 86400)),
            # 空闲待机张望：从偏好页挪到这里，仍存在 behavior.*
            "idle_live": bool(behavior.get("idle_live", True)),
            "idle_motion": str(behavior.get("idle_motion") or "normal"),
            "wander_per_min": int(behavior.get("wander_per_min", 2)),
            "wander_idle_sec": int(behavior.get("wander_idle_sec", 60)),
        },
        "live": _live_status(live),
        "quiet_hours": {
            "enabled": bool(quiet.get("enabled")),
            "start": str(quiet.get("start") or ""),
            "end": str(quiet.get("end") or ""),
            "active": _quiet_now(),
        },
        "proactive": {
            "service_running": bool(snap.get("running")),
            "today_count": int(snap.get("today_count") or 0),
            "care_today_count": int(snap.get("care_today_count") or 0),
            "last_spoke_at": _fmt_ts(snap.get("last_spoke_at")),
            "last_task_id": last_task,
            "last_task_title": titles.get(last_task) if last_task else None,
            "last_skip_reason": skip,
            "last_skip_text": quest_proactive.skip_reason_text(skip) if skip else "",
        },
    }


def _annotate_next_attempt(cards: list[dict], snap: dict[str, Any]) -> None:
    """给当前场景里进行中的小目标标上：主线——这一轮提过没有（提过就等小歪标结果 / 下次检测）；
    日常关心——最早何时再提。场景卡标上下次目标检测的时间（来自循环的 pass_at）。"""
    last_map = snap.get("task_last_attempt") if isinstance(snap.get("task_last_attempt"), dict) else {}
    pass_map = snap.get("pass_at") if isinstance(snap.get("pass_at"), dict) else {}
    retry = float(snap.get("task_retry_sec") or quest_proactive.QUEST_TASK_RETRY_SEC)
    for card in cards:
        if not card.get("is_current"):
            continue
        if not card.get("is_care_scene"):
            last_pass = pass_map.get(card["name"])
            hours = int(card.get("check_interval_hours") or DEFAULT_CHECK_INTERVAL_HOURS)
            card["next_check_at"] = _fmt_ts(float(last_pass) + hours * 3600.0) if last_pass else None
        for t in list(card.get("tasks") or []) + list(card.get("care_tasks") or []):
            last = last_map.get(t["id"])
            t["next_attempt_at"] = None
            t["attempted_this_round"] = False
            if not last or t.get("status") != STATUS_RUNNING:
                continue
            if t.get("kind") == KIND_CARE:
                gap = max(retry, float(t.get("repeat_interval_sec") or 0))
                t["next_attempt_at"] = _fmt_ts(float(last) + gap)
                continue
            started = _parse_ts(t.get("started_at"))
            t["attempted_this_round"] = not (started and float(last) < started)


def _parse_ts(value: Any) -> float | None:
    """实例里的时间（UTC ISO，可能不带时区）→ epoch。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _live_status(live: dict[str, Any] | None = None) -> dict[str, Any]:
    """待机动作现状：模式、现在为什么没动、最近一次自发动作。

    ``live`` 来自 Core 进程（控制台是另一个进程，本进程里的服务永远是空的）；
    没传时才退回本进程（同进程部署 / 测试）。"""
    try:
        st = dict(live) if live else live_behavior_service().any_stats()
    except Exception:  # noqa: BLE001
        return {"mode": "unknown"}
    next_slot = st.get("next_slot_in")
    return {
        "mode": str(st.get("mode") or "normal"),
        "last_motion_ts": float(st.get("last_motion_ts") or 0.0),
        "last_motion_source": str(st.get("last_motion_source") or ""),
        "wander": int(st.get("wander") or 0),
        "heat_skips": int(st.get("heat_skips") or 0),
        # 现在为什么没动（live_behavior 每拍记录），排障不用猜
        "block_reason": str(st.get("block_reason") or ""),
        "block_text": str(st.get("block_text") or ""),
        "next_slot_in": float(next_slot) if next_slot is not None else None,
    }


def _quiet_now() -> bool:
    try:
        return bool(quiet_hours_active())
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "PERFORM_TEMPLATES",
    "STATUS_LABEL",
    "TEMPLATES",
    "TEMPLATE_BLANK",
    "TEMPLATE_DEFAULT",
    "add_task",
    "approve_proposal",
    "create_playbook",
    "delete_task",
    "generate_playbook",
    "mark_task",
    "overview",
    "rename_playbook",
    "reorder_tasks",
    "reset_playbook_progress",
    "reset_progress",
    "restart_task",
    "save_settings",
    "select_playbook",
    "update_playbook_settings",
    "start_from",
    "update_task",
]
