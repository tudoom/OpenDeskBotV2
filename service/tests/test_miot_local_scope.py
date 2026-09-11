from __future__ import annotations

import inspect


def test_miot_business_api_has_no_robot_hardware_scope():
    from deskbot_server import miot_service as service
    from deskbot_server.miot_tools import execute_miot_tool

    functions = (
        service.get_bind_url,
        service.authorize_and_sync,
        service.unbind,
        service.get_status,
        service.sync_homes,
        service.load_homes_cache,
        service.list_devices_cached_or_live,
        service.resolve_device,
        service.resolve_scene,
        service.llm_miot_prompt_appendix,
        execute_miot_tool,
    )
    for function in functions:
        assert "device_id" not in inspect.signature(function).parameters


def test_miot_tool_status_uses_pc_local_service(monkeypatch):
    from deskbot_server import miot_service as service
    from deskbot_server import miot_tools

    calls: list[bool] = []

    monkeypatch.setattr(service, "miot_sdk_available", lambda: (True, ""))
    monkeypatch.setattr(miot_tools, "_load_miot_util", lambda: ({0}, lambda value: value))

    def _status(*, refresh=False):
        calls.append(bool(refresh))
        return {"bound": True, "token_valid": True}

    monkeypatch.setattr(service, "get_status", _status)

    result = miot_tools.execute_miot_tool({"action": "status"})

    assert result["ok"] is True
    assert calls == [False, True]
