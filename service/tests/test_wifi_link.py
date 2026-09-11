from __future__ import annotations

import socket

import pytest


@pytest.fixture()
def wifi_home(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.net.wifi_link.local_data_dir",
        lambda: local,
    )
    return local


def test_link_token_is_minted_once_and_survives_reload(wifi_home):
    from deskbot_server.infrastructure.net.wifi_link import (
        ensure_link_token,
        validate_link_token,
    )

    first = ensure_link_token("deskbot_aabbccddeeff")
    second = ensure_link_token("deskbot_aabbccddeeff")
    assert first == second
    assert len(first) == 32

    assert validate_link_token("deskbot_aabbccddeeff", first)
    assert not validate_link_token("deskbot_aabbccddeeff", "wrong")
    assert not validate_link_token("deskbot_aabbccddeeff", "")
    # 没配过网的设备不可能通过校验——空令牌绝不能当万能钥匙。
    assert not validate_link_token("deskbot_000000000001", "")


def test_socket_serial_maps_timeout_and_eof_like_pyserial():
    """read() 超时返回空串继续等；对端断开必须抛错让会话关闭。"""
    from deskbot_server.infrastructure.net.wifi_link import SocketSerial

    left, right = socket.socketpair()
    adapter = SocketSerial(left)
    try:
        assert adapter.read(64) == b""  # timeout, link healthy

        right.sendall(b"DBOT")
        assert adapter.read(64) == b"DBOT"

        assert adapter.write(b"xy") == 2
        assert right.recv(2) == b"xy"

        right.close()
        with pytest.raises(OSError):
            # 断开后可能还需一次读干净缓冲，再读必须报错。
            for _ in range(3):
                adapter.read(64)
    finally:
        adapter.close()


def test_socket_serial_close_is_idempotent():
    from deskbot_server.infrastructure.net.wifi_link import SocketSerial

    left, right = socket.socketpair()
    adapter = SocketSerial(left)
    adapter.close()
    adapter.close()
    right.close()


class _FakeSession:
    def __init__(self, port: str, transport: str, device_id: str) -> None:
        self.port = port
        self.transport = transport
        self.device_id = device_id
        self.is_closed = False
        self.close_reason = ""
        self.hello_info = object()

    @property
    def is_ready(self) -> bool:
        return not self.is_closed

    async def close(self, *, reason: str = "") -> None:
        self.is_closed = True
        self.close_reason = reason


class _Hello:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id


def _manager():
    from deskbot_server.infrastructure.serial.manager import (
        SerialDeviceManager,
        SerialManagerConfig,
    )

    return SerialDeviceManager(SerialManagerConfig(enabled=False))


def test_wifi_session_is_rejected_while_usb_is_live():
    import asyncio

    async def scenario():
        manager = _manager()
        usb = _FakeSession("COM9", "usb_cdc", "deskbot_aabbccddeeff")
        manager._sessions_by_port["COM9"] = usb
        manager._sessions_by_device["deskbot_aabbccddeeff"] = usb

        wifi = _FakeSession(
            "tcp://192.168.1.30:52000", "wifi_tcp", "deskbot_aabbccddeeff"
        )
        manager._sessions_by_port[wifi.port] = wifi
        await manager._session_ready(wifi, _Hello("deskbot_aabbccddeeff"))

        assert wifi.is_closed
        assert "priority" in wifi.close_reason
        assert not usb.is_closed
        assert (
            manager._sessions_by_device["deskbot_aabbccddeeff"] is usb
        )

    asyncio.run(scenario())


def test_usb_session_supersedes_a_live_wifi_session():
    import asyncio

    async def scenario():
        manager = _manager()
        wifi = _FakeSession(
            "tcp://192.168.1.30:52000", "wifi_tcp", "deskbot_aabbccddeeff"
        )
        manager._sessions_by_port[wifi.port] = wifi
        manager._sessions_by_device["deskbot_aabbccddeeff"] = wifi

        usb = _FakeSession("COM9", "usb_cdc", "deskbot_aabbccddeeff")
        manager._sessions_by_port["COM9"] = usb
        await manager._session_ready(usb, _Hello("deskbot_aabbccddeeff"))

        assert wifi.is_closed
        assert not usb.is_closed
        assert (
            manager._sessions_by_device["deskbot_aabbccddeeff"] is usb
        )

    asyncio.run(scenario())


def test_wifi_sessions_never_enter_the_com_retry_planner():
    """网络会话关闭后由设备端重连，PC 不能为它排串口重试计划。"""
    import asyncio

    async def scenario():
        manager = _manager()
        wifi = _FakeSession(
            "tcp://192.168.1.30:52000", "wifi_tcp", "deskbot_aabbccddeeff"
        )
        manager._sessions_by_port[wifi.port] = wifi
        manager._sessions_by_device["deskbot_aabbccddeeff"] = wifi

        await manager._session_closed(wifi, None)

        assert wifi.port not in manager._sessions_by_port
        assert "deskbot_aabbccddeeff" not in manager._sessions_by_device
        assert wifi.port not in manager._retry_after

    asyncio.run(scenario())


def test_netsh_field_parsing_handles_localized_labels(monkeypatch):
    from deskbot_server import wifi_credentials

    interfaces_cn = (
        "\n    名称                   : WLAN"
        "\n    SSID                   : HomeNet-5G"
        "\n    BSSID                  : aa:bb:cc:dd:ee:ff"
        "\n    信号                   : 96%\n"
    )
    profile_cn = (
        "\n    安全设置"
        "\n    ----------"
        "\n    身份验证               : WPA2 - 个人"
        "\n    关键内容               : hunter2secret\n"
    )

    calls = []

    def fake_run(args):
        calls.append(args)
        if "interfaces" in args:
            return interfaces_cn
        return profile_cn

    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: False)
    monkeypatch.setattr(wifi_credentials, "_run_netsh", fake_run)
    ssid, key, enterprise = wifi_credentials.current_wifi_credentials()
    assert ssid == "HomeNet-5G"
    assert key == "hunter2secret"
    assert enterprise is False
    # BSSID 行同样含 "ssid"，不能把它当成网络名。
    assert "name=HomeNet-5G" in " ".join(calls[1])


def _darwin_fake_runner(outputs: dict[str, str]):
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=None):
        calls.append(list(args))
        for key, text in outputs.items():
            if key in " ".join(args):
                return text
        return ""

    return fake_run, calls


_HW_PORTS = "Hardware Port: Ethernet\nDevice: en0\n\nHardware Port: Wi-Fi\nDevice: en1\n"


def test_darwin_redacted_ssid_reports_associated_with_candidates(monkeypatch):
    """macOS 14+ 无定位权限时 SSID 全是 <redacted>：要能区分"没连 WiFi"和"名字被藏"。"""
    from deskbot_server import wifi_credentials

    fake_run, calls = _darwin_fake_runner(
        {
            "listallhardwareports": _HW_PORTS,
            "getsummary": "  BSSID : <redacted>\n  SSID : <redacted>\n  Security : WPA2_PSK\n",
            "getairportnetwork": "You are not associated with an AirPort network.\n",
            "listpreferredwirelessnetworks": "Preferred networks on en1:\n\tMicband\n\tMicband_5G\n\txiaoyu205\n",
        }
    )
    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: True)
    monkeypatch.setattr(wifi_credentials, "_run_darwin", fake_run)
    st = wifi_credentials.pc_wifi_status()
    assert st.associated is True
    assert st.ssid == ""
    assert st.ssid_hidden_by_os is True
    assert st.candidates == ["Micband", "Micband_5G", "xiaoyu205"]
    assert wifi_credentials.current_wifi_credentials() == ("", "", False)
    # 名字都拿不到时不能去碰钥匙串（会弹授权框）
    assert not any("find-generic-password" in " ".join(c) for c in calls)


def test_darwin_visible_ssid_reads_password_from_keychain(monkeypatch):
    from deskbot_server import wifi_credentials

    fake_run, calls = _darwin_fake_runner(
        {
            "listallhardwareports": _HW_PORTS,
            "getsummary": "  SSID : HomeNet\n  Security : WPA2_PSK\n",
            "find-generic-password": "hunter2secret\n",
        }
    )
    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: True)
    monkeypatch.setattr(wifi_credentials, "_run_darwin", fake_run)
    assert wifi_credentials.current_wifi_credentials() == ("HomeNet", "hunter2secret", False)
    st = wifi_credentials.pc_wifi_status()
    assert st.associated and st.ssid == "HomeNet" and not st.ssid_hidden_by_os


def test_darwin_not_on_wifi(monkeypatch):
    from deskbot_server import wifi_credentials

    fake_run, _calls = _darwin_fake_runner(
        {"listallhardwareports": _HW_PORTS, "getsummary": "  InterfaceType : WiFi\n"}
    )
    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: True)
    monkeypatch.setattr(wifi_credentials, "_run_darwin", fake_run)
    st = wifi_credentials.pc_wifi_status()
    assert st.associated is False and st.ssid == "" and st.candidates == []


def test_missing_key_reports_empty_not_error(monkeypatch):
    from deskbot_server import wifi_credentials

    def fake_run(args):
        if "interfaces" in args:
            return "\n    SSID                   : OfficeNet\n"
        return "\n    Security key           : Present\n"  # key=clear 被系统拒绝

    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: False)
    monkeypatch.setattr(wifi_credentials, "_run_netsh", fake_run)
    ssid, key, enterprise = wifi_credentials.current_wifi_credentials()
    assert ssid == "OfficeNet"
    assert key == ""
    assert enterprise is False


def test_enterprise_network_is_flagged_not_password_prompted(monkeypatch):
    """802.1X 网络没有 PSK，必须提示换网而不是等用户输一个不存在的密码。"""
    from deskbot_server import wifi_credentials

    def fake_run(args):
        if "interfaces" in args:
            return "\n    SSID                   : MIOffice-5G\n"
        return (
            "\n    Authentication         : WPA2-Enterprise"
            "\n    Cipher                 : CCMP"
            "\n    Security key           : Absent\n"
        )

    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: False)
    monkeypatch.setattr(wifi_credentials, "_run_netsh", fake_run)
    ssid, key, enterprise = wifi_credentials.current_wifi_credentials()
    assert ssid == "MIOffice-5G"
    assert key == ""
    assert enterprise is True


def test_darwin_pc_wifi_credentials_probes_once_and_reads_key(monkeypatch):
    """一次探测同时给出状态与密钥；名字被藏时不碰钥匙串。"""
    from deskbot_server import wifi_credentials

    fake_run, calls = _darwin_fake_runner(
        {
            "listallhardwareports": _HW_PORTS,
            "getsummary": "  SSID : HomeNet\n",
            "find-generic-password": "pw\n",
        }
    )
    monkeypatch.setattr(wifi_credentials, "is_darwin", lambda: True)
    monkeypatch.setattr(wifi_credentials, "_run_darwin", fake_run)
    status, key, enterprise = wifi_credentials.pc_wifi_credentials()
    assert status.ssid == "HomeNet" and key == "pw" and enterprise is False
    assert sum(1 for c in calls if "listallhardwareports" in " ".join(c)) == 1
