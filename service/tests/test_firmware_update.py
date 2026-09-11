from __future__ import annotations

from types import SimpleNamespace

from deskbot_server.firmware_update import (
    bundled_manifest,
    manifest_response,
    parse_version,
)


def test_parse_version_orders_naturally():
    assert parse_version("0.0.17") > parse_version("0.0.16")
    assert parse_version("0.1.0") > parse_version("0.0.99")
    assert parse_version("1.0.0") > parse_version("0.9.9")
    assert parse_version("0.0.16") == parse_version("0.0.16")
    # 脏输入不炸、按 0 处理
    assert parse_version("") == (0,)
    assert parse_version("v2") == (2,)


def test_bundled_manifest_present_and_consistent():
    m = bundled_manifest()
    assert m is not None, "安装包必须内置固件（firmware_payload/）"
    assert m["path"].is_file()
    assert m["size"] > 100_000
    assert parse_version(m["version"]) > (0, 0, 0)


class _FakeManager:
    def __init__(self, session):
        self._session = session

    def session_for_device(self, device_id):
        return self._session


def _session(firmware: str, transport: str):
    return SimpleNamespace(
        transport=transport,
        port="COM99",
        hello_info=SimpleNamespace(firmware=firmware),
    )


def test_manifest_response_flags_update_only_when_newer():
    bundled = bundled_manifest()
    older = manifest_response(_FakeManager(_session("0.0.1", "usb_cdc")), "d1")
    assert older["update_available"] is True
    assert older["update_ready"] is True

    same = manifest_response(
        _FakeManager(_session(bundled["version"], "usb_cdc")), "d1"
    )
    assert same["update_available"] is False
    assert same["update_ready"] is False

    newer = manifest_response(
        _FakeManager(_session("99.0.0", "usb_cdc")), "d1"
    )
    assert newer["update_available"] is False


def test_manifest_response_wifi_shows_update_but_not_ready():
    resp = manifest_response(_FakeManager(_session("0.0.1", "wifi_tcp")), "d1")
    assert resp["update_available"] is True
    assert resp["update_ready"] is False


def test_manifest_response_without_device():
    resp = manifest_response(_FakeManager(None), "")
    assert resp["device_version"] is None
    assert resp["update_available"] is False
