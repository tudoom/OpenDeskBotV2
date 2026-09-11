"""Core 侧路由 /api/quest_proactive（状态）与 /api/quest_proactive/speak_now（现在试一句）。

控制台是另一个进程，只能经这两条 loopback + API Key 路由拿到循环状态 / 触发一轮。
"""

from __future__ import annotations

import asyncio
import json

from tests.test_http_api_controls import (  # noqa: F401
    _call,
    _Connection,
    _handler,
    _json,
    _local_key,
    _Request,
    http_api_env,
)


class _Loop:
    def __init__(self, result=True, delay=0.0, reason=""):
        self.result, self.delay, self.reason = result, delay, reason
        self.calls = 0

    def snapshot(self):
        return {"running": True, "today_count": 3, "last_skip_reason": self.reason}

    def last_runner_skip_reason(self):
        return self.reason

    async def trigger_now(self, device_id=None, *, task_id=None):
        self.calls += 1
        self.last_task_id = task_id
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def test_quest_routes_require_key_and_report_not_started(http_api_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.ws.routes import ROUTES_API_KEY

    assert "/api/quest_proactive" in ROUTES_API_KEY and "/api/quest_proactive/speak_now" in ROUTES_API_KEY
    monkeypatch.setattr(qp, "_active_loop", None)
    handler = _handler()
    assert _call(handler, _Request("/api/quest_proactive")).status_code == 401
    key = _local_key()
    resp = _call(handler, _Request("/api/quest_proactive", api_key=key))
    assert resp.status_code == 200 and _json(resp) == {"ok": True, "running": False, "snapshot": {}}
    resp = _call(handler, _Request("/api/quest_proactive/speak_now", api_key=key, method="POST"))
    assert resp.status_code == 409 and _json(resp)["reason"] == "not_started"
    assert _call(handler, _Request("/api/quest_proactive/speak_now", api_key=key)).status_code == 405


def test_quest_speak_now_reports_started_skipped_and_pending(http_api_env, monkeypatch):
    from deskbot_server.application import quest_proactive as qp
    from deskbot_server.ws.routes import quest as route

    key = _local_key()
    handler = _handler()
    lp = _Loop(result=True)
    monkeypatch.setattr(qp, "_active_loop", lp)
    resp = _call(handler, _Request("/api/quest_proactive", api_key=key))
    assert _json(resp)["running"] is True and _json(resp)["snapshot"]["today_count"] == 3
    resp = _call(handler, _Request("/api/quest_proactive/speak_now", api_key=key, method="POST"))
    assert resp.status_code == 200 and _json(resp) == {"ok": True, "started": True}
    assert lp.calls == 1 and lp.last_task_id is None
    resp = _call(handler, _Request("/api/quest_proactive/speak_now", api_key=key, method="POST", body=b'{"task_id": "g_greet"}'))
    assert resp.status_code == 200 and lp.last_task_id == "g_greet"
    # 循环说现在不能开口 → 409 + 用户能懂的原因
    monkeypatch.setattr(qp, "_active_loop", _Loop(result=False, reason="daily_limit"))
    resp = _call(handler, _Request("/api/quest_proactive/speak_now", api_key=key, method="POST"))
    body = _json(resp)
    assert resp.status_code == 409 and body["reason"] == "daily_limit" and "用完" in body["error"]
    # 一轮说到一半（超过等待窗口）→ 按已发起返回，且不取消那一轮
    slow = _Loop(result=True, delay=0.3)
    monkeypatch.setattr(qp, "_active_loop", slow)
    monkeypatch.setattr(route, "_SPEAK_WAIT_SEC", 0.05)

    async def _go():
        resp = await handler(_Connection(), _Request("/api/quest_proactive/speak_now", api_key=key, method="POST"))
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True, "started": True, "pending": True}
        await asyncio.sleep(0.4)  # 那一轮继续跑完
        assert slow.calls == 1

    asyncio.run(_go())
