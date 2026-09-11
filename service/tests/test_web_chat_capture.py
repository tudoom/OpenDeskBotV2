from __future__ import annotations

import json

from deskbot_server.application import web_chat_capture as wcc
from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY

# 最小合法 JPEG（1x1 灰度），供 make_transient_vision_image 校验通过。
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432"
    "ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


def test_capture_tool_names():
    assert wcc.is_capture_tool("capture_camera")
    assert wcc.is_capture_tool("capture_and_describe")
    assert not wcc.is_capture_tool("websearch")


def test_fetch_core_capture_returns_transient_image_on_jpeg():
    seen: dict = {}

    def _fetch(url: str):
        seen["url"] = url
        return 200, "image/jpeg", _TINY_JPEG

    out = wcc.fetch_core_camera_capture("dev-1", base_url="http://core:9000", fetch=_fetch)
    assert seen["url"] == "http://core:9000/api/camera_snapshot?device_id=dev-1"
    assert out["ok"] is True
    assert out["source"] == "core_snapshot"
    assert out["jpeg_bytes"] == len(_TINY_JPEG)
    assert TRANSIENT_VISION_IMAGE_KEY in out and out[TRANSIENT_VISION_IMAGE_KEY].get("bytes")


def test_fetch_core_capture_surfaces_core_error_and_missing_device():
    def _fetch(url: str):
        return 503, "application/json", json.dumps({"ok": False, "error": "暂无相机帧"}).encode()

    out = wcc.fetch_core_camera_capture("dev-1", base_url="http://core:9000", fetch=_fetch)
    assert out["ok"] is False and "暂无相机帧" in out["error"]
    assert wcc.fetch_core_camera_capture("", base_url="http://core:9000", fetch=_fetch)["ok"] is False


def test_fetch_core_capture_retries_once_after_empty_frame(monkeypatch):
    monkeypatch.setattr(wcc.time, "sleep", lambda s: None)
    calls: list[str] = []

    def _fetch(url: str):
        calls.append(url)
        if len(calls) == 1:
            return 503, "application/json", json.dumps({"ok": False, "error": "暂无相机帧"}).encode()
        return 200, "image/jpeg", _TINY_JPEG

    out = wcc.fetch_core_camera_capture("dev-1", base_url="http://core:9000", fetch=_fetch)
    assert out["ok"] is True
    assert len(calls) == 2


def test_execute_web_chat_tools_routes_capture_to_core_and_keeps_order(monkeypatch):
    calls: list[list[str]] = []

    def _fake_execute(tools, *, device_id=None, user_confirmed=False):
        calls.append([t["tool"] for t in tools])
        return [{"tool": t["tool"], "ok": True, "echo": True} for t in tools]

    monkeypatch.setattr(
        "deskbot_server.application.llm_tool_runner.execute_llm_tools", _fake_execute
    )
    monkeypatch.setattr(
        "deskbot_server.web.helpers.deskbot_upstream_base", lambda: "http://core:9000"
    )
    fetch = lambda url: (200, "image/jpeg", _TINY_JPEG)  # noqa: E731
    tools = [
        {"tool": "websearch", "query": "x"},
        {"tool": "capture_camera", "display": False},
        {"tool": "miot", "action": "list"},
    ]
    results = wcc.execute_web_chat_tools(tools, device_id="dev-1", fetch=fetch)
    assert [r["tool"] for r in results] == ["websearch", "capture_camera", "miot"]
    assert results[1]["ok"] is True and TRANSIENT_VISION_IMAGE_KEY in results[1]
    # 非拍照工具按原顺序分两批交给同步执行器，中间夹着抓拍
    assert calls == [["websearch"], ["miot"]]
