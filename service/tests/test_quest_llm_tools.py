"""剧情工具 update_task_result / update_task_strategy 经 LLM tool runner 执行；
三条链路的能力矩阵与 RTC 原生 schema 都登记了这两个工具。"""

from __future__ import annotations

from tests.quest_helpers import bind, demo_playbook, quest_env  # noqa: F401


def _setup_bound():
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("demo")
    return svc


def _run_tools(tools: list[dict], device_id: str = "robot-1") -> list[dict]:
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    return execute_llm_tools(tools, device_id=device_id)


def test_update_task_result_via_tool(quest_env):
    svc = _setup_bound()
    results = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": "用户回应了问候"}]
    )
    assert results[0]["ok"] is True
    assert results[0]["task"]["status"] == "success"
    assert results[0]["propagated"][0]["task_id"] == "g_learn_name"
    assert results[0]["activated"][0]["task_id"] == "g_learn_name"
    # 真实运行态落在本地档案，与调用方 device_id 无关
    rows = {r["task_id"]: r for r in svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo")}
    assert rows["g_greet"]["status"] == "success"


def test_update_task_strategy_via_tool(quest_env):
    svc = _setup_bound()
    results = _run_tools([{"tool": "update_task_strategy", "task_id": "g_greet", "strategy": "问候要短"}])
    assert results[0]["ok"] is True
    assert svc.get_effective_strategy(svc.LOCAL_PROFILE_DEVICE, "demo", "g_greet") == "问候要短"


def test_quest_tools_unbound_and_bad_params(quest_env):
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("")
    r = _run_tools([{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": "x"}])
    assert r[0]["ok"] is False and "未绑定" in r[0]["error"]

    bind("demo")
    r = _run_tools([{"tool": "update_task_result", "status": "success", "result": "x"}])
    assert r[0]["ok"] is False and "task_id" in r[0]["error"]
    r = _run_tools([{"tool": "update_task_result", "task_id": "g_greet", "status": "done", "result": "x"}])
    assert r[0]["ok"] is False and "status" in r[0]["error"]
    r = _run_tools([{"tool": "update_task_result", "task_id": "g_learn_name", "status": "success", "result": "x"}])
    assert r[0]["ok"] is False and "未激活" in r[0]["error"]
    r = _run_tools([{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": ""}])
    assert r[0]["ok"] is False and "结果" in r[0]["error"]
    r = _run_tools([{"tool": "update_task_strategy", "task_id": "ghost", "strategy": "x"}])
    assert r[0]["ok"] is False and "不存在" in r[0]["error"]


def test_sandbox_tool_call_uses_explicit_playbook(quest_env):
    from deskbot_server.application import quest_service as svc

    svc.save_playbook("demo", demo_playbook())
    bind("")  # 未绑定也能对沙箱显式指定剧本
    out = svc.execute_quest_tool(
        {"tool": "update_task_result", "task_id": "g_greet", "status": "failed", "result": "没回应", "playbook": "demo"},
        device_id=svc.DESIGN_SANDBOX_DEVICE,
    )
    assert out["ok"] is True and out["task"]["status"] == "unmet"  # 旧名 failed 折成 unmet
    assert svc.get_instances(svc.LOCAL_PROFILE_DEVICE, "demo") == []


def test_quest_tools_registered_in_matrix_and_rtc_schemas():
    from deskbot_server.application import tool_executor as te
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    for channel in te.ToolChannel:
        allowed = te.tools_for_channel(channel)
        assert "update_task_result" in allowed and "update_task_strategy" in allowed
        assert "propose_goal" in allowed
    names = {s["name"]: s for s in build_rtc_tool_schemas()}
    assert set(names["propose_goal"]["parameters"]["required"]) == {"title", "goal"}
    assert set(names["update_task_result"]["parameters"]["required"]) == {"task_id", "status", "result"}
    assert names["update_task_result"]["parameters"]["properties"]["status"]["enum"] == ["success", "unmet"]
    assert "failure_condition" not in names["propose_goal"]["parameters"]["properties"]
    assert set(names["update_task_strategy"]["parameters"]["required"]) == {"task_id", "strategy"}
