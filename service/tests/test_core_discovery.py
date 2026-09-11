"""机器人按名字找 Core：身份、UDP 应答、USB 会话刷新、固件契约。"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def identity_home(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.net.core_identity.local_data_dir",
        lambda: local,
    )
    return local


def test_core_id_is_minted_once_and_survives_reload(identity_home):
    from deskbot_server.infrastructure.net.core_identity import CORE_ID_PREFIX, core_id

    first = core_id()
    assert first.startswith(CORE_ID_PREFIX) and len(first) == len(CORE_ID_PREFIX) + 12
    assert core_id() == first
    stored = json.loads((identity_home / "core_identity.json").read_text(encoding="utf-8"))
    assert stored["core_id"] == first
    # 文件损坏也不能换身份以外的东西：重新铸造一个并写回，而不是抛错。
    (identity_home / "core_identity.json").write_text("{not json", encoding="utf-8")
    again = core_id()
    assert again.startswith(CORE_ID_PREFIX)


def test_discover_request_parsing_rejects_foreign_datagrams():
    from deskbot_server.infrastructure.net.core_discovery import parse_discover_request

    assert parse_discover_request(b"") is None
    assert parse_discover_request(b"\xff\xfe") is None
    assert parse_discover_request(b'{"type":"something_else"}') is None
    assert parse_discover_request(b"x" * 600) is None
    req = parse_discover_request(b'{"type":"deskbot_discover","device_id":"deskbot_1","core_id":"core_abc","v":1}')
    assert req == {"device_id": "deskbot_1", "core_id": "core_abc", "v": 1}


def test_reply_only_for_my_core_or_unbound_robots():
    from deskbot_server.infrastructure.net.core_discovery import build_discover_reply

    mine = build_discover_reply({"core_id": "core_me"}, core_id="core_me", port=9020, name="mac")
    assert json.loads(mine) == {"type": "deskbot_core", "core_id": "core_me", "port": 9020, "name": "mac", "v": 1}
    unbound = build_discover_reply({"core_id": ""}, core_id="core_me", port=9020, name="mac")
    assert unbound is not None
    # 别人的机器人问的是别的 Core：保持沉默，绝不抢配对。
    assert build_discover_reply({"core_id": "core_other"}, core_id="core_me", port=9020, name="mac") is None


def test_udp_responder_answers_from_the_request_socket():
    from deskbot_server.infrastructure.net.core_discovery import DiscoveryResponder

    responder = DiscoveryResponder(core_id="core_me", link_port=9020, name="mac", udp_port=0)
    # udp_port=0 让内核挑一个空闲端口；实际端口从 socket 读回。
    assert responder.start()
    try:
        port = responder._sock.getsockname()[1]  # noqa: SLF001 - 测试读回端口
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2.0)
        client.sendto(b'{"type":"deskbot_discover","device_id":"deskbot_1","core_id":"core_me","v":1}', ("127.0.0.1", port))
        data, addr = client.recvfrom(512)
        assert addr[1] == port
        assert json.loads(data)["core_id"] == "core_me"
        assert json.loads(data)["port"] == 9020
        client.sendto(b'{"type":"deskbot_discover","device_id":"deskbot_2","core_id":"core_other","v":1}', ("127.0.0.1", port))
        with pytest.raises(socket.timeout):
            client.recvfrom(512)
        client.close()
        assert responder.answered == 1 and responder.ignored == 1
    finally:
        responder.stop()
    assert not responder.running


def test_bonjour_command_carries_service_type_and_core_id():
    from deskbot_server.infrastructure.net.core_discovery import SERVICE_TYPE, BonjourAdvertiser

    adv = BonjourAdvertiser(core_id="core_me", link_port=9020, name="mac")
    cmd = adv.dns_sd_command()
    assert cmd[1:3] == ["-R", "Deskbot Core mac"]
    assert SERVICE_TYPE in cmd and "9020" in cmd and "core_id=core_me" in cmd


# ── USB 会话刷新 ─────────────────────────────────────────────────────────────
class _Session:
    def __init__(self, transport="usb_cdc", status=None, reply=None):
        self.transport = transport
        self.device_id = "deskbot_1"
        self._status = status
        self._reply = reply
        self.sent: list[dict] = []

    async def request_wifi_status(self, timeout=4.0):
        return self._status

    async def send_wifi_config(self, config, timeout=6.0):
        self.sent.append(dict(config))
        return self._reply


def _desired():
    from deskbot_server.application.core_binding_refresh import desired_binding

    return desired_binding(host="192.168.31.63", port=9020, core_id="core_me", token="")


def test_binding_refresh_decisions():
    from deskbot_server.application.core_binding_refresh import binding_needs_refresh

    d = _desired()
    assert binding_needs_refresh(None, d) == (False, "no_status")
    assert binding_needs_refresh({"configured": True, "host": "1.2.3.4"}, d) == (False, "firmware_too_old")
    assert binding_needs_refresh({"configured": False, "core_id": ""}, d) == (False, "unprovisioned")
    assert binding_needs_refresh({"configured": True, "core_id": "core_me", "host": "192.168.31.163", "port": 9020}, d) == (True, "host")
    assert binding_needs_refresh({"configured": True, "core_id": "", "host": "192.168.31.63", "port": 9020}, d) == (True, "core_id")
    assert binding_needs_refresh({"configured": True, "core_id": "core_me", "host": "192.168.31.63", "port": 9020}, d) == (False, "up_to_date")


def test_refresh_pushes_host_only_config_with_token_and_no_ssid():
    from deskbot_server.application.core_binding_refresh import refresh_core_binding

    stale = {"configured": True, "core_id": "core_me", "host": "192.168.31.163", "port": 9020}
    fresh = {"configured": True, "core_id": "core_me", "host": "192.168.31.63", "port": 9020}
    session = _Session(status=stale, reply=fresh)

    async def token(dev):
        return "tok-" + dev

    result = asyncio.run(refresh_core_binding(session, desired=_desired(), token_provider=token))
    assert result["action"] == "refreshed" and result["reason"] == "host"
    assert session.sent == [{"host": "192.168.31.63", "port": 9020, "core_id": "core_me", "token": "tok-deskbot_1"}]
    assert "ssid" not in session.sent[0]


def test_refresh_skips_wifi_sessions_and_old_firmware():
    from deskbot_server.application.core_binding_refresh import refresh_core_binding

    over_wifi = _Session(transport="wifi_tcp", status={"configured": True, "core_id": "x"})
    assert asyncio.run(refresh_core_binding(over_wifi, desired=_desired()))["action"] == "skipped"
    old = _Session(status={"configured": True, "host": "1.2.3.4"})
    assert asyncio.run(refresh_core_binding(old, desired=_desired()))["reason"] == "firmware_too_old"
    assert old.sent == []


# ── 固件契约 ────────────────────────────────────────────────────────────────
def test_firmware_mirrors_the_discovery_protocol():
    from deskbot_server.infrastructure.net import core_discovery as cd

    cpp = (ROOT / "hardware/firmware/wifi_link.cpp").read_text(encoding="utf-8")
    service, proto = cd.SERVICE_TYPE.split(".")
    assert f'kDiscoveryService = "{service}"' in cpp
    assert f'kDiscoveryProto = "{proto}"' in cpp
    assert f"kDiscoveryUdpPort = {cd.DISCOVERY_UDP_PORT}" in cpp
    assert f'kDiscoverRequestType = "{cd.DISCOVER_REQUEST_TYPE}"' in cpp
    assert f'kDiscoverReplyType = "{cd.DISCOVER_REPLY_TYPE}"' in cpp
    # 设备只认配对过的 core_id；host-only 刷新不带 ssid、不重启。
    assert "is not mine" in cpp
    assert "core binding refreshed" in cpp
    assert '\\"core_id\\":\\"%s\\"' in cpp and '\\"host_source\\":\\"%s\\"' in cpp
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 49)

