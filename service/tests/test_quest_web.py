"""主动陪伴控制台 /quest 页面与 /api/quest/* 路由（概览 / 设置 / 小目标 / 预演 / 导入导出）。"""

from __future__ import annotations

import pytest

from tests.quest_helpers import demo_playbook, quest_env  # noqa: F401


@pytest.fixture()
def core(monkeypatch):
    """模拟 Core 进程：默认不可达（status 0）；测试可改 state 让它「在跑」。"""
    from deskbot_server.web.blueprints import quest_bp

    state = {"reachable": False, "snapshot": {}, "speak": (200, {"ok": True, "started": True}), "calls": []}

    def _fake_core_call(method, path, *, timeout, body=None):
        state["calls"].append((method, path, body) if body else (method, path))
        if not state["reachable"]:
            return 0, {}
        if path == "/api/quest_proactive":
            return 200, {"ok": True, "running": bool(state["snapshot"]), "snapshot": state["snapshot"]}
        if path == "/api/quest_proactive/speak_now":
            return state["speak"]
        if path == "/api/live_behavior":
            return 200, {"ok": True, "live": state.get("live") or {}}
        return 404, {"ok": False}

    monkeypatch.setattr(quest_bp, "_core_call", _fake_core_call)
    return state


@pytest.fixture()
def client(quest_env, core):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _ov(client):
    data = client.get("/api/quest/overview").get_json()
    assert data["ok"] is True
    return data


def _by_id(ov):
    return {t["id"]: t for t in ov["tasks"]}


def test_quest_page_renders_with_nav_and_no_binding_concept(client):
    resp = client.get("/quest")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="questApp"' in html
    assert 'data-label="主动陪伴"' in html
    assert "/api/quest/overview" in html and "/api/quest/settings" in html
    # 页面上不再出现引擎术语；也没有「现在试一句」「在这之后」「看看达成会怎样」
    for banned in ("绑定", "解绑", "沙箱", "激活分数", "initial_status", "activation_score",
                   "现在试一句", "在这之后", "看看达成会怎样", "看看没达成会怎样", "剧场", "剧本", "导入剧场"):
        assert banned not in html, banned
    assert "触发一次" in html and "/speak`" in html
    assert "目前已有场景" in html and "选择场景" in html and 'data-label="表演"' in html and "场景编排" not in html
    assert "storyPlaybooks" in html and "onPlaybookChange" in html and "toggleScene" not in html  # 顶部下拉只列主线，卡片上没有开关
    # 页面自己选当前连着的小歪，不弹「还没选择当前小歪」
    assert 'data-suppress-device="1"' in html and "autoSelectDevice" in html
    # 首页有「小歪正在悄悄做的事」入口
    home = client.get("/home").get_data(as_text=True)
    assert "小歪正在悄悄做的事" in home and "/api/quest/overview" in home


def test_overview_defaults_to_bundled_playbook_with_start_running(client):
    ov = _ov(client)
    assert ov["enabled"] is True
    assert ov["playbook"]["name"] == "xiaoy" and ov["playbook"]["title"] == "与主人的初识"
    assert [p["name"] for p in ov["playbooks"]] == ["xiaoy", "care"]  # 开启的主线在前，日常关心紧随
    tasks = _by_id(ov)
    assert tasks["g_greet"]["status"] == "running" and tasks["g_greet"]["order"] == 1
    assert tasks["g_learn_name"]["status"] == "not_started" and tasks["g_learn_name"]["order"] == 2
    assert [t["id"] for t in ov["tasks"]] == ["g_greet", "g_learn_name", "g_daily_mood", "g_learn_daily", "g_learn_hobby"]
    assert tasks["g_learn_hobby"]["order"] == 5  # 最后一步（认脸已随人脸功能下线）
    assert tasks["g_greet"]["perform"] == {"mode": "expression", "expression": "happy", "scene": ""}
    # 提醒喝水休息是日常关心：不在主线里，默认每天一次、连续 2 次没回应歇一天
    assert "g_care_break" not in tasks
    care = {t["id"]: t for t in ov["care_tasks"]}
    assert care["g_care_break"]["kind"] == "care" and care["g_care_break"]["repeat_interval_sec"] == 86400
    assert care["g_care_break"]["max_attempts"] == 2 and care["g_care_break"]["status"] == "running"
    assert tasks["g_greet"]["max_attempts"] == 0 and tasks["g_greet"]["next_delay_sec"] == 10 and "failure_condition" not in tasks["g_greet"]
    assert ov["proposals"] == [] and ov["finished"] is False
    card = ov["playbooks"][0]
    assert card["is_current"] is True and card["enabled"] is True and card["task_count"] == 5 and card["care_count"] == 0
    assert card["finished"] is False and card["settled"] is False and card["unmet_count"] == 0 and card["is_care_scene"] is False
    assert card["check_interval_hours"] == 1 and card["next_check_at"] is None  # Core 不可达时没有下次检测时间
    assert [t["id"] for t in card["tasks"]] == [t["id"] for t in ov["tasks"]] and card["proposals"] == []
    care_card = ov["playbooks"][1]
    assert care_card["is_care_scene"] is True and care_card["is_default"] is True and care_card["care_count"] == 2
    assert [t["id"] for t in care_card["care_tasks"]] == ["g_care_break", "g_care_neck"]  # 随包：喝水休息 + 活动颈椎
    assert care_card["enabled"] is True and care_card["check_interval_hours"] == 0
    assert [t["key"] for t in ov["perform_options"]["templates"]] == ["ask", "expression", "scene"]
    names = {sc["name"] for sc in ov["perform_options"]["scenes"]}
    assert {"new_year", "birthday", "good_morning", "disco", "want_attention"} <= names and "demo_greet" not in names
    assert ov["settings"] == {"idle_sec": 60, "daily_limit": 16, "care_daily_limit": 10, "care_pause_sec": 86400, "wander_per_min": 2, "wander_idle_sec": 60, "idle_live": True, "idle_motion": "normal"}
    assert ov["proactive"]["service_running"] is False  # Core 不可达
    # Core 不可达时「现在试一句」给出友好错误
    r = client.post("/api/quest/speak_now")
    assert r.status_code == 409 and "稍后再试" in r.get_json()["error"]


def test_overview_reads_loop_state_from_core_process(client, core):
    """循环在 Core 进程里，控制台经 /api/quest_proactive 取快照并转发「现在试一句」。"""
    import time

    core["reachable"] = True
    core["snapshot"] = {
        "running": True,
        "today_count": 2,
        "last_spoke_at": time.time() - 90,
        "last_skip_reason": "quiet_hours",
        "last_turn": {"task_id": "g_greet"},
        "idle_sec": 120,
        "task_last_attempt": {"g_greet": time.time() + 5},
        "task_retry_sec": 1800,
        "pass_at": {"xiaoy": time.time() - 600},
    }
    ov = _ov(client)
    pa = ov["proactive"]
    assert _by_id(ov)["g_greet"]["attempted_this_round"] is True  # 这轮提过 → 页面标「等小歪标结果」
    assert _by_id(ov)["g_learn_name"]["attempted_this_round"] is False
    assert ov["playbooks"][0]["next_check_at"]  # 上次检测 + 间隔 = 下次检测
    assert pa["service_running"] is True and pa["today_count"] == 2
    assert pa["last_task_title"] == "第一次打招呼" and pa["last_spoke_at"]
    assert pa["last_skip_reason"] == "quiet_hours" and pa["last_skip_text"] == "现在是勿扰时段"
    # 待机张望状态同样来自 Core 进程：为什么没动 / 下一次多久 / 已张望几次
    core["live"] = {"mode": "gentle", "block_reason": "waiting_slot", "block_text": "等这一分钟里的随机时刻",
                    "next_slot_in": 12.5, "wander": 3, "heat_skips": 0, "last_motion_ts": time.time() - 30, "last_motion_source": "live_wander"}
    live = _ov(client)["live"]
    assert live["block_reason"] == "waiting_slot" and live["next_slot_in"] == 12.5 and live["wander"] == 3
    assert ("GET", "/api/live_behavior") in core["calls"]
    # 改设置后返回的概览也带 Core 快照（不会闪回「还没启动」）
    ov = client.put("/api/quest/settings", json={"daily_limit": 10}).get_json()
    assert ov["proactive"]["service_running"] is True and ov["settings"]["daily_limit"] == 10
    # 触发一次（就某个小目标）：成功 / Core 说不能开口 / 一轮还没说完
    r = client.post("/api/quest/tasks/g_greet/speak")
    assert r.status_code == 200 and r.get_json()["started"] is True
    assert core["calls"][-1] == ("POST", "/api/quest_proactive/speak_now", {"task_id": "g_greet"})
    core["speak"] = (409, {"ok": False, "error": "今天的主动开口次数已用完", "reason": "daily_limit"})
    r = client.post("/api/quest/speak_now")
    assert r.status_code == 409 and r.get_json()["reason"] == "daily_limit"
    core["speak"] = (200, {"ok": True, "started": True, "pending": True})
    assert client.post("/api/quest/speak_now").get_json()["pending"] is True
    assert ("POST", "/api/quest_proactive/speak_now") in core["calls"]


def test_settings_switch_and_limits(client):
    r = client.put("/api/quest/settings", json={"enabled": False, "idle_sec": 300, "daily_limit": 0})
    assert r.status_code == 200
    ov = r.get_json()
    assert ov["enabled"] is False
    assert ov["settings"] == {"idle_sec": 300, "daily_limit": 0, "care_daily_limit": 10, "care_pause_sec": 86400, "wander_per_min": 2, "wander_idle_sec": 60, "idle_live": True, "idle_motion": "normal"}
    ov = client.put("/api/quest/settings", json={"wander_per_min": 4, "care_pause_sec": 7200}).get_json()
    assert (ov["settings"]["wander_per_min"], ov["settings"]["care_pause_sec"]) == (4, 7200)
    # 勿扰时段从行为偏好页搬到这里（2026-09-14）
    ov = client.put("/api/quest/settings", json={"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:30"}}).get_json()
    assert (ov["quiet_hours"]["enabled"], ov["quiet_hours"]["start"], ov["quiet_hours"]["end"]) == (True, "23:00", "07:30")
    assert client.put("/api/quest/settings", json={"care_pause_sec": 1}).status_code == 400
    assert client.put("/api/quest/settings", json={"wander_per_min": 11}).status_code == 400
    ov = client.put("/api/quest/settings", json={"idle_live": False}).get_json()
    assert ov["settings"]["idle_live"] is False
    assert client.get("/app/api/preferences").get_json()["preferences"]["behavior"]["idle_live"] is False
    # 偏好页的「行为」卡也走同一段合并（之前 behavior 段根本不落盘）
    r = client.patch("/app/api/preferences", json={"preferences": {"behavior": {"idle_motion": "normal"}}})
    assert r.status_code == 200
    beh = client.get("/app/api/preferences").get_json()["preferences"]["behavior"]
    assert beh["idle_motion"] == "normal" and beh["idle_live"] is False  # 局部更新不吞掉另一项
    prefs = client.get("/app/api/preferences").get_json()["preferences"]["quest"]
    assert prefs["proactive_enabled"] is False and prefs["idle_sec"] == 300 and prefs["daily_limit"] == 0
    assert client.put("/api/quest/settings", json={"idle_sec": 5}).status_code == 400
    assert client.put("/api/quest/settings", json={}).status_code == 400
    assert client.put("/api/quest/settings", json={"playbook": "ghost"}).status_code == 400
    # 关掉主线：没有主线在推进，日常关心照旧
    ov = client.put("/api/quest/settings", json={"playbook": ""}).get_json()
    assert ov["playbook"] is None and ov["tasks"] == [] and [t["id"] for t in ov["care_tasks"]] == ["g_care_break", "g_care_neck"]
    appendix = client.get("/api/quest/prompt_preview").get_json()["appendix"]
    assert "当前剧情任务" not in appendix and "可以顺带关心的事" in appendix
    assert client.post("/api/quest/runtime/reset").status_code == 400
    # 日常关心场景不能删
    assert client.delete("/api/quest/playbooks/care").status_code == 400


def test_create_playbook_add_tasks_and_relations(client):
    # 新建空白剧本：只给标题，文件名自动生成，创建即启用
    r = client.post("/api/quest/playbooks", json={"title": "认识我的作息", "template": "blank"})
    assert r.status_code == 200
    ov = r.get_json()
    name = ov["playbook"]["name"]
    assert name.startswith("pb_") and ov["playbook"]["title"] == "认识我的作息" and ov["tasks"] == []
    assert client.post("/api/quest/playbooks", json={"title": ""}).status_code == 400
    assert client.post("/api/quest/playbooks", json={"title": "x", "template": "nope"}).status_code == 400

    base = f"/api/quest/playbooks/{name}/tasks"
    # 第一个小目标：第 1 步，直接进行中；标题缺省取目标前 20 字
    r = client.post(base, json={"goal": "知道主人几点起床"})
    assert r.status_code == 200
    t1 = r.get_json()["task"]
    assert t1["id"] == "t1" and t1["title"] == "知道主人几点起床"
    assert t1["order"] == 1 and t1["status"] == "running"
    assert client.post(base, json={"goal": ""}).status_code == 400
    # 第二个默认接在末尾 → 第 2 步，等待中
    r = client.post(base, json={"title": "知道几点睡", "goal": "知道主人几点睡觉"})
    t2 = r.get_json()["task"]
    assert t2["id"] == "t2" and t2["status"] == "not_started" and t2["order"] == 2
    # 第三个插在 t1 之后 → 它成了第 2 步，t2 退到第 3 步
    r = client.post(base, json={"title": "换个话题", "goal": "聊点别的", "after": "t1"})
    t3 = r.get_json()["task"]
    assert t3["order"] == 2
    ov = _ov(client)
    assert [t["id"] for t in ov["tasks"]] == ["t1", "t3", "t2"]
    # 引擎里：边由顺序生成（达成、未达成都指向下一步），第一步自动 running
    from deskbot_server.application import quest_service as svc

    by = {t["id"]: t for t in svc.get_playbook(name)["tasks"]}
    assert by["t1"]["on_success"] == [{"id": "t3", "score": 1}] and by["t1"]["on_failure"] == [{"id": "t3", "score": 1}]
    assert by["t3"]["on_success"] == [{"id": "t2", "score": 1}] and by["t2"]["on_success"] == [] and by["t1"]["initial_status"] == "running"
    assert by["t3"]["initial_status"] == "not_started"
    # 拖动排序：整份顺序提交；结果跟着小目标走，当前按「第一个没结果的」重算
    r = client.put(f"/api/quest/playbooks/{name}/order", json={"ids": ["t2", "t1", "t3"]})
    assert r.status_code == 200
    ov = r.get_json()
    assert [t["id"] for t in ov["tasks"]] == ["t2", "t1", "t3"]
    tasks = _by_id(ov)
    assert tasks["t2"]["status"] == "running" and tasks["t1"]["status"] == "not_started"  # 当前挪到了 t2
    assert client.put(f"/api/quest/playbooks/{name}/order", json={"ids": ["t1", "t2"]}).status_code == 400  # 对不上
    client.put(f"/api/quest/playbooks/{name}/order", json={"ids": ["t1", "t2", "t3"]})
    assert _by_id(_ov(client))["t1"]["status"] == "running"

    # 编辑：文字 / 执行方式 / 进入下一个的条件 一起保存；主线不可重复；「下一步」字段不再认
    r = client.put(
        f"{base}/t1",
        json={
            "strategy": "顺口问一句",
            "on_success_id": "t3",
            "trigger_hint": "主人说累了的时候",
            "perform": {"mode": "scene", "scene": "birthday"},
            "max_attempts": 5,
            "next_delay_sec": 25,
            "repeatable": True,
            "repeat_interval_sec": 3600,
        },
    )
    assert r.status_code == 200
    t1 = r.get_json()["task"]
    assert t1["strategy"] == "顺口问一句" and t1["trigger_hint"] == "主人说累了的时候"
    assert t1["perform"] == {"mode": "scene", "expression": "", "scene": "birthday"}
    assert t1["max_attempts"] == 0 and t1["next_delay_sec"] == 25 and t1["repeatable"] is False  # 主线不数次数
    assert [t["id"] for t in r.get_json()["tasks"]] == ["t1", "t2", "t3"]
    # 表情模式但没选表情 → 退回只说话；坏的输入不落盘
    r = client.put(f"{base}/t1", json={"perform": {"mode": "expression", "expression": ""}})
    assert r.get_json()["task"]["perform"]["mode"] == "speak"
    assert client.put(f"{base}/t1", json={"goal": ""}).status_code == 400
    assert client.put(f"{base}/t1", json={"max_attempts": "x"}).status_code == 400
    assert client.put(f"{base}/t1", json={"next_delay_sec": "x"}).status_code == 400

    # 手动记为达成 → 进入下一个 t2；记为未达成 → 进入 t3（未达成留给定时检测）
    r = client.post("/api/quest/tasks/t1/mark", json={"status": "success"})
    assert r.status_code == 200
    tasks = _by_id(r.get_json())
    assert tasks["t1"]["status"] == "success" and tasks["t2"]["status"] == "running"
    r = client.post("/api/quest/tasks/t2/mark", json={"status": "unmet"})
    tasks = _by_id(r.get_json())
    assert tasks["t2"]["status"] == "unmet" and tasks["t2"]["status_label"] == "未达成" and tasks["t3"]["status"] == "running"
    assert client.post("/api/quest/tasks/t2/mark", json={"status": "unmet"}).status_code == 400  # 已有结果
    assert client.post("/api/quest/tasks/t3/mark", json={"status": "bogus"}).status_code == 400
    card = next(c for c in r.get_json()["playbooks"] if c["name"] == name)
    assert card["unmet_count"] == 1 and card["settled"] is False
    # 重新开始一个已有结果的：它变成当前，原来进行中的退回等待中
    r = client.post("/api/quest/tasks/t2/restart")
    tasks = _by_id(r.get_json())
    assert tasks["t2"]["status"] == "running" and tasks["t3"]["status"] == "not_started"
    # 从这个开始：前面没结果的记为未达成（跳过）
    r = client.post("/api/quest/tasks/t3/start_from")
    assert r.status_code == 200 and r.get_json()["skipped"] == ["t2"]
    tasks = _by_id(r.get_json())
    assert tasks["t2"]["status"] == "skipped" and "跳到了后面" in tasks["t2"]["result"] and tasks["t3"]["status"] == "running"
    # 删除当前这一步 → 都有结果了 = 走完；从头开始 → 第 1 步进行中
    r = client.delete(f"{base}/t3")
    assert r.status_code == 200
    ov = r.get_json()
    assert "t3" not in _by_id(ov) and ov["finished"] is True
    assert client.delete(f"{base}/t3").status_code == 400
    r = client.post("/api/quest/runtime/reset")
    assert r.status_code == 200
    tasks = _by_id(r.get_json())
    assert tasks["t1"]["status"] == "running" and tasks["t2"]["status"] == "not_started"


def test_switch_playbooks_keeps_progress_and_delete_falls_back(client):
    ov = client.post("/api/quest/playbooks", json={"title": "副本", "template": "default"}).get_json()
    copy_name = ov["playbook"]["name"]
    assert ov["playbook"]["task_count"] == 5 and _by_id(ov)["g_greet"]["status"] == "running"
    # 在副本里推进一步
    client.post("/api/quest/tasks/g_greet/mark", json={"status": "success"})
    # 切回默认剧场：默认剧场从起点开始；副本这张卡仍显示它保留的进度（当前卡排最前）
    ov = client.put("/api/quest/settings", json={"playbook": "xiaoy"}).get_json()
    assert _by_id(ov)["g_greet"]["status"] == "running"
    cards = {c["name"]: c for c in ov["playbooks"]}
    assert ov["playbooks"][0]["name"] == "xiaoy" and cards[copy_name]["is_current"] is False
    copy_tasks = {t["id"]: t for t in cards[copy_name]["tasks"]}
    assert copy_tasks["g_greet"]["status"] == "success" and copy_tasks["g_learn_name"]["status"] == "running"
    # 非当前剧场也能从头开始
    ov = client.post(f"/api/quest/playbooks/{copy_name}/reset").get_json()
    copy_tasks = {t["id"]: t for t in {c["name"]: c for c in ov["playbooks"]}[copy_name]["tasks"]}
    assert copy_tasks["g_greet"]["status"] == "running" and copy_tasks["g_learn_name"]["status"] == "not_started"
    assert client.post("/api/quest/playbooks/ghost/reset").status_code == 400
    # 不立刻使用的新建
    ov = client.post("/api/quest/playbooks", json={"title": "备用", "template": "blank", "select": False}).get_json()
    assert ov["playbook"]["name"] == "xiaoy" and any(c["title"] == "备用" and not c["is_current"] for c in ov["playbooks"])
    ov = client.put("/api/quest/settings", json={"playbook": copy_name}).get_json()
    assert _by_id(ov)["g_greet"]["status"] == "running"
    # 重命名
    ov = client.put(f"/api/quest/playbooks/{copy_name}", json={"title": "副本二"}).get_json()
    assert ov["playbook"]["title"] == "副本二"
    assert client.put(f"/api/quest/playbooks/{copy_name}", json={"title": ""}).status_code == 400
    # 场景设置：目标检测间隔（小时，1–24）
    ov = client.put(f"/api/quest/playbooks/{copy_name}", json={"check_interval_hours": 3}).get_json()
    assert next(c for c in ov["playbooks"] if c["name"] == copy_name)["check_interval_hours"] == 3
    assert client.put(f"/api/quest/playbooks/{copy_name}", json={"check_interval_hours": 0}).status_code == 400
    assert client.put("/api/quest/playbooks/care", json={"check_interval_hours": 2}).status_code == 400
    # 导出 / 导入为新剧本
    r = client.get(f"/api/quest/playbooks/{copy_name}/export")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    ov = client.post("/api/quest/playbooks/import", json=demo_playbook()).get_json()
    assert ov["ok"] is True and ov["playbook"]["task_count"] == 3 and ov["playbook"]["title"] == "demo"
    assert client.post("/api/quest/playbooks/import", json=[1]).status_code == 400
    assert client.post("/api/quest/playbooks/import", json={"name": "x", "tasks": [{"id": "g"}]}).status_code == 400
    # 删除当前剧本 → 没有剧本在用
    imported = ov["playbook"]["name"]
    ov = client.delete(f"/api/quest/playbooks/{imported}").get_json()
    assert ov["ok"] is True and ov["playbook"] is None
    assert imported not in [p["name"] for p in ov["playbooks"]]
    assert client.delete("/api/quest/playbooks/ghost").status_code == 404


def test_proposals_become_care_goals_and_story_insert_keeps_chain(client, monkeypatch):
    """小歪提的小目标默认是「日常关心」：同意时只定频率和条件，不进主线；选主线时插入而不替换；
    场景走完的判定忽略日常关心；日常关心达成后到期自动恢复。"""
    from datetime import timedelta

    from deskbot_server.application import quest_service as svc
    from deskbot_server.application.llm_tool_runner import execute_llm_tools
    from deskbot_server.core import clock

    ov = client.post("/api/quest/playbooks", json={"title": "作息", "template": "blank"}).get_json()
    name = ov["playbook"]["name"]
    base = f"/api/quest/playbooks/{name}/tasks"
    client.post(base, json={"title": "知道几点起", "goal": "知道主人几点起床"})
    client.post(base, json={"title": "知道几点睡", "goal": "知道主人几点睡觉", "after": "t1"})
    # 提议 → 挂起；一次只能挂一个
    out = execute_llm_tools([{"tool": "propose_goal", "title": "活动颈椎", "goal": "写代码久了提醒起身", "trigger_hint": "主人连续写代码一小时"}], device_id="dev1")
    assert out[0]["ok"] is True and out[0]["task_id"] == "p1"
    ov = _ov(client)
    assert [p["id"] for p in ov["proposals"]] == ["p1"] and ov["proposals"][0]["kind"] == "care"
    assert "p1" not in _by_id(ov) and [t["task_id"] for t in svc.get_current_tasks()] == ["t1", "g_care_break", "g_care_neck"]
    assert execute_llm_tools([{"tool": "propose_goal", "title": "x", "goal": "y"}], device_id="dev1")[0]["ok"] is False
    # 同意为日常关心：频率 + 条件；不进主线，t1 的下一步还是 t2
    ov = client.post("/api/quest/proposals/p1/approve", json={"repeat_interval_sec": 14400, "trigger_hint": "主人写代码超过一小时"}).get_json()
    assert ov["proposals"] == [] and "p1" not in _by_id(ov)
    care = {t["id"]: t for t in ov["care_tasks"]}
    assert care["p1"]["kind"] == "care" and care["p1"]["status"] == "running" and care["p1"]["paused"] is False
    assert care["p1"]["repeat_interval_sec"] == 14400 and care["p1"]["trigger_hint"] == "主人写代码超过一小时"
    assert care["p1"]["order"] == 0  # 日常关心不在线上
    assert [t["id"] for t in ov["tasks"]] == ["t1", "t2"]
    story_card = next(c for c in ov["playbooks"] if c["name"] == name)
    care_card = next(c for c in ov["playbooks"] if c["is_care_scene"])
    assert story_card["care_count"] == 0 and story_card["task_count"] == 2
    assert [t["id"] for t in care_card["care_tasks"]] == ["g_care_break", "g_care_neck", "p1"]  # 提议同意后住在日常关心场景里
    # 主线的编辑不认「下一步」字段（顺序就是编排），日常关心也不会被拉进线里
    assert client.put(f"{base}/t1", json={"on_success_id": "p1"}).status_code == 200
    assert [t["id"] for t in _ov(client)["tasks"]] == ["t1", "t2"]
    # 主动开口的候选：主线在前，日常关心在后
    assert [t["task_id"] for t in svc.get_current_tasks()] == ["t1", "g_care_break", "g_care_neck", "p1"]

    # 第二个提议选主线，插在 t1 之后：t1 → p2 → t2
    execute_llm_tools([{"tool": "propose_goal", "title": "问问加班", "goal": "知道主人今天加不加班"}], device_id="dev1")
    ov = client.post("/api/quest/proposals/p2/approve", json={"kind": "story", "after": "t1"}).get_json()
    tasks = _by_id(ov)
    assert tasks["p2"]["kind"] == "story" and tasks["p2"]["order"] == 2 and tasks["t2"]["order"] == 3
    assert "p2" not in {t["id"] for c in ov["playbooks"] if c["is_care_scene"] for t in c["care_tasks"]}  # 已从日常关心搬走
    assert [t["id"] for t in ov["tasks"]] == ["t1", "p2", "t2"] and tasks["p2"]["status"] == "not_started"
    assert client.post("/api/quest/proposals/ghost/approve").status_code == 400

    # 暂停 / 重新打开 / 达成后冷却
    ov = client.post("/api/quest/tasks/p1/mark", json={"status": "skipped"}).get_json()
    care = {t["id"]: t for t in ov["care_tasks"]}
    assert care["p1"]["paused"] is True and care["p1"]["status"] == "paused" and "暂停" in care["p1"]["result"]
    assert _by_id(ov)["t1"]["status"] == "running"  # 日常关心的暂停不影响主线
    ov = client.post("/api/quest/tasks/p1/restart").get_json()
    assert {t["id"]: t for t in ov["care_tasks"]}["p1"]["status"] == "running"
    ov = client.post("/api/quest/tasks/p1/mark", json={"status": "success"}).get_json()
    care = {t["id"]: t for t in ov["care_tasks"]}
    assert care["p1"]["status"] == "success" and care["p1"]["resume_at"]
    # 场景走完只看主线：主线全部完成 → finished，即便日常关心还在
    for tid in ("t1", "p2", "t2"):
        client.post(f"/api/quest/tasks/{tid}/mark", json={"status": "success"})
    ov = _ov(client)
    assert ov["finished"] is True
    # 冷却到期（4 小时）自动恢复
    real_now = clock.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: real_now + timedelta(hours=5))
    ov = _ov(client)
    assert {t["id"]: t for t in ov["care_tasks"]}["p1"]["status"] == "running"
    assert ov["finished"] is True  # 日常关心恢复了也不算“没走完”

    # 新加的日常关心立刻有实例：即便库里还留着已删小目标的旧实例（按 id 补齐，不按条数）
    client.post(base, json={"kind": "care", "title": "远眺", "goal": "写代码久了看远处"})
    from deskbot_server.core.clock import utcnow
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import QuestInstance, _new_id
    session = get_session()
    session.add(QuestInstance(id=_new_id(), device_id="local", playbook="care", task_id="ghost_deleted", status="running", current_score=0, created_at=utcnow(), updated_at=utcnow()))
    session.commit()
    session.close()
    r = client.post(base, json={"kind": "care", "title": "喝水", "goal": "提醒喝水"})
    new_id = r.get_json()["task"]["id"]
    assert new_id in [t["task_id"] for t in svc.get_current_tasks()]
    # 页面：每个主线场景一个「开启」开关，日常关心卡不带开关；同意提议只填频率/条件/定时
    html = client.get("/quest").get_data(as_text=True)
    assert "定时提醒" in html and "多久提一次" in html and "同意，作为定时提醒" in html
    assert "onPlaybookChange" in html and "随主动陪伴生效" in html and "选择场景" in html and "toggleScene" not in html
    # 「没有开启」的提示只给关着的主线场景看（曾经 v-else-if 挂错位置，开着的也显示）
    assert 'v-if="!pb.is_care_scene && !pb.enabled && pb.tasks.some(' in html
