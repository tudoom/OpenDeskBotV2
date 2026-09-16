"""语音 Agent 在线时，主动陪伴 / 定时提醒改由它开口：Core 指令队列、桥路由、两个调用方、worker 侧应用。

背景（2026-09-08 实测）：主动开口走 Core 播放通道时，那句话不在语音 Agent 的历史里，主人回话它接不上。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.db_helpers import temp_db  # noqa: F401
from tests.quest_helpers import bind, demo_playbook, quest_env  # noqa: F401
from tests.test_http_api_controls import _Connection, _RemoteConnection, http_api_env  # noqa: F401
from tests.test_rtc_tool_bridge import _Broker, _Chat, _Hub, _Registry  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_queue():
    from deskbot_server.application import rtc_instructions as ri

    ri._reset_for_tests()
    ri.configure(attached_provider=None)
    yield
    ri._reset_for_tests()
    ri.configure(attached_provider=None)


# ── 队列语义 ────────────────────────────────────────────────


def test_queue_enqueue_drain_ttl_and_cap():
    from deskbot_server.application import rtc_instructions as ri

    assert ri.agent_attached("dev") is False  # 未配置 → 不在线
    assert ri.speak_via_agent("dev", "hi", source="x") is False
    ri.configure(attached_provider=lambda dev: dev == "dev")
    assert ri.speak_via_agent("other", "hi", source="x") is False
    assert ri.speak_via_agent("dev", "第一句", source="quest") is True
    ri.enqueue("dev", "第二句", source="reminder", ttl_sec=5)
    assert ri.pending_count("dev") == 2 and ri.snapshot()["pending"] == {"dev": 2}
    items = ri.drain("dev")
    assert [i["text"] for i in items] == ["第一句", "第二句"] and items[0]["source"] == "quest"
    assert ri.drain("dev") == [] and ri.pending_count("dev") == 0
    # 过期的不给
    ri.enqueue("dev", "旧的", source="x", ttl_sec=5)
    assert ri.drain("dev", now=ri.time.time() + 60) == []
    assert ri.snapshot()["expired"] == 1
    # 同设备最多保留最近 3 条
    for i in range(5):
        ri.enqueue("dev", f"m{i}", source="x")
    assert [i["text"] for i in ri.drain("dev")] == ["m2", "m3", "m4"]
    with pytest.raises(ValueError):
        ri.enqueue("", "x", source="x")


# ── Core 桥路由 ──────────────────────────────────────────────


class _GetRequest:
    method = "GET"

    def __init__(self, path: str, token: str | None):
        self.path = path
        self.headers = {"X-Deskbot-RTC-Bridge": token} if token else {}
        self.body = b""


def test_instruction_route_requires_loopback_token_and_drains(http_api_env):
    from deskbot_server.application import rtc_instructions as ri
    from deskbot_server.ws.http_api import _build_http_request_handler

    handler = _build_http_request_handler(_Broker(), _Registry(), asr_chat_hub=_Hub(), chat=_Chat(), rtc_tool_token="bridge-secret")
    ri.enqueue("deskbot_a", "说点什么", source="quest_proactive")
    call = lambda req, conn=None: asyncio.run(handler(conn or _Connection(), req))  # noqa: E731
    assert call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", "wrong")).status_code == 401
    assert call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", None)).status_code == 401
    assert call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", "bridge-secret"), _RemoteConnection()).status_code == 403
    assert call(_GetRequest("/internal/rtc/instructions", "bridge-secret")).status_code == 400
    assert ri.pending_count("deskbot_a") == 1  # 以上都没取走
    resp = call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", "bridge-secret"))
    assert resp.status_code == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["ok"] is True and [i["text"] for i in body["instructions"]] == ["说点什么"]
    assert body["instructions"][0]["source"] == "quest_proactive"
    resp = call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", "bridge-secret"))
    assert json.loads(resp.body.decode("utf-8"))["instructions"] == []


# ── 主动陪伴：语音 Agent 在线时交给它说 ────────────────────────


def test_quest_runner_hands_over_to_voice_agent_when_attached(quest_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application import rtc_instructions as ri
    from tests.test_quest_proactive import FakeHub, FakeRegistry

    pb = demo_playbook()
    pb["tasks"][0]["perform"] = {"mode": "expression", "expression": "happy_smile"}
    pb["tasks"][0]["trigger_hint"] = "主人刚坐下"
    svc.save_playbook("demo", pb)
    bind("demo")
    ri.configure(attached_provider=lambda dev: True)

    async def _boom(*a, **kw):
        raise AssertionError("语音 Agent 在线时不应走 Core 的 run_chat_turn")

    monkeypatch.setattr(qp, "run_chat_turn", _boom)
    monkeypatch.setattr(qp, "WsDownlinkAdapter", lambda ws, **kw: SimpleNamespace(ws=ws))
    played: list[str] = []

    class _Runtime:
        async def play_scene(self, name, **kw):
            played.append(name)
            return SimpleNamespace(ok=True)

    monkeypatch.setattr(qp, "get_expression_runtime", lambda dev: _Runtime())
    runner = qp.QuestProactiveRunner(
        chat=SimpleNamespace(settings=SimpleNamespace()), asr_chat_hub=FakeHub(ws=object()),
        registry=FakeRegistry(), dp_broker=None, daily_limit_provider=lambda: 0, idle_sec_provider=lambda: 120.0,
    )
    assert asyncio.run(runner.attempt("dev1")) is True
    assert played == ["happy_smile"]  # 表情仍在设备 lane 上做
    items = ri.drain("dev1")
    assert len(items) == 1 and items[0]["source"] == "quest_proactive"
    text = items[0]["text"]
    assert "[g_greet] 初次问候" in text and "约 2 分钟" in text and "主人刚坐下" in text
    assert "做了一个「happy_smile」的表情" in text and "skip_goal" in text and "update_task_result" in text
    assert "need_reply" not in text and "tts" not in text  # 语音 Agent 直接说，不要 JSON 契约
    assert runner.last_turn["status"] == "handed_to_rtc_agent" and runner.stats()["today_count"] == 1
    rows = {r["task_id"]: r for r in svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo")}
    assert rows["g_greet"]["attempt_count"] == 0 and rows["g_greet"]["status"] == "running"  # 主线不数次数，等小歪标结果
    # 不在线 → 回到 Core 播放路径（这里 run_chat_turn 被打桩成抛错，正好证明走到了）
    ri.configure(attached_provider=lambda dev: False)
    runner._task_last_attempt.clear()
    assert asyncio.run(runner.attempt("dev1")) is True  # 异常被 runner 兜底记日志
    assert ri.pending_count("dev1") == 0


def test_build_rtc_instruction_is_plain_speech():
    from deskbot_server.application.quest_proactive import build_rtc_instruction

    text = build_rtc_instruction(
        {"task_id": "t1", "title": "知道称呼", "goal": "知道怎么叫主人", "strategy": "顺口问", "attempt_count": 2},
        idle_sec=300, performed="演了一段「挥手」",
    )
    assert text.startswith("主人约 5 分钟没有和你说话") and "[t1] 知道称呼" in text
    assert "已经主动提过 2 次" in text and "演了一段「挥手」" in text and "\\n" not in text


# ── 定时提醒：语音 Agent 在线时交给它说 ────────────────────────


def test_worker_applies_one_instruction_per_poll(monkeypatch):
    from deskbot_server import rtc_livekit_plugins as plugins

    calls: list[dict] = []

    class _Session:
        def generate_reply(self, **kw):
            calls.append(kw)

    n = plugins._deskbot_apply_instructions(_Session(), [{"id": "a", "text": "先说这句", "source": "quest"}, {"id": "b", "text": "再说这句"}])
    assert n == 1 and calls == [{"instructions": "先说这句", "allow_interruptions": True}]
    assert plugins._deskbot_apply_instructions(_Session(), [{"text": ""}]) == 0
    assert plugins._deskbot_apply_instructions(object(), [{"text": "x"}]) == 0
    room = SimpleNamespace(remote_participants={"p": SimpleNamespace(identity="deskbot-usb-deskbot_a"), "q": SimpleNamespace(identity="agent-x")})
    assert plugins._device_id_from_room(room) == "deskbot_a"
    assert plugins._device_id_from_room(SimpleNamespace(remote_participants={})) == ""
    monkeypatch.delenv("DESKBOT_RTC_TOOL_BRIDGE_URL", raising=False)
    assert plugins._instruction_bridge_target() == ("", "")
    monkeypatch.setenv("DESKBOT_RTC_TOOL_BRIDGE_URL", "http://127.0.0.1:9000/internal/rtc/tools")
    monkeypatch.setenv("DESKBOT_RTC_TOOL_BRIDGE_TOKEN", "t")
    assert plugins._instruction_bridge_target() == ("http://127.0.0.1:9000/internal/rtc/instructions", "t")


# ── 语音 Agent 的任务列表跟着状态刷新 ──────────────────────────


def test_instruction_route_refreshes_system_prompt_when_quest_state_changes(http_api_env):
    from deskbot_server.application import quest_service as svc
    from deskbot_server.application import user_activity as ua
    from deskbot_server.quest_playbooks_store import ensure_default_playbook
    from deskbot_server.ws.http_api import _build_http_request_handler

    ensure_default_playbook()
    handler = _build_http_request_handler(_Broker(), _Registry(), asr_chat_hub=_Hub(), chat=_Chat(), rtc_tool_token="bridge-secret")
    call = lambda req, conn=None: json.loads(asyncio.run(handler(conn or _Connection(), req)).body.decode("utf-8"))  # noqa: E731
    first = call(_GetRequest("/internal/rtc/instructions?device_id=deskbot_a", "bridge-secret"))
    assert first["prompt_version"] and first["system_prompt"]  # 刚建会话：先发一份
    same = call(_GetRequest(f"/internal/rtc/instructions?device_id=deskbot_a&prompt_version={first['prompt_version']}", "bridge-secret"))
    assert same["prompt_version"] == first["prompt_version"] and "system_prompt" not in same
    # 小目标有了结果 → 指纹变了 → 重发系统提示
    svc.ensure_instances("local", "xiaoy")
    svc.update_task_result("local", "xiaoy", "g_greet", "success", "回应了")
    changed = call(_GetRequest(f"/internal/rtc/instructions?device_id=deskbot_a&prompt_version={first['prompt_version']}", "bridge-secret"))
    assert changed["prompt_version"] != first["prompt_version"] and changed["system_prompt"]
    # 语音回传带用户话 → 记「主人说话了」
    ua._reset_for_tests()

    class _Post:
        method = "POST"
        path = "/internal/rtc/turns"
        headers = {"X-Deskbot-RTC-Bridge": "bridge-secret"}
        body = json.dumps({"user_text": "在呢", "assistant_text": "好", "device_id": "deskbot_a"}).encode("utf-8")

    assert asyncio.run(handler(_Connection(), _Post())).status_code == 200
    assert ua.last_ts("deskbot_a") > 0


def test_worker_swaps_agent_instructions_when_prompt_arrives():
    from deskbot_server import rtc_livekit_plugins as plugins

    applied: list[str] = []

    class _Agent:
        async def update_instructions(self, text):
            applied.append(text)

    session = SimpleNamespace(current_agent=_Agent())
    assert asyncio.run(plugins._deskbot_apply_system_prompt(session, "新的系统提示")) is True and applied == ["新的系统提示"]
    assert asyncio.run(plugins._deskbot_apply_system_prompt(session, "")) is False
    assert asyncio.run(plugins._deskbot_apply_system_prompt(object(), "x")) is False
