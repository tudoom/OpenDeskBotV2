"""固件 0.0.57：待机卡通脸——电脑断开后设备循环播放最后下发的待机位图表情（PSRAM），并可写进 FFat 重启保持。

Core 侧：idle 位图链带 face_keep/face_tag；hello 报 face_tag；运行时在链被接收后自动 face_status_req → face_persist；
表情页"设为设备默认表情"走 /api/face_persist；idle 换回矢量脸时 face_clear。
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def _fw(name: str) -> str:
    return (FW / name).read_text(encoding="utf-8")


def _jpeg_b64(color=(0, 0, 0)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (240, 240), color).save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _bitmap_scene(n: int = 2):
    from deskbot_server.application.expression_catalog import ExpressionScene
    from deskbot_server.ark_image_gen import frames_to_scene
    from deskbot_server.face_expr_scenes_store import decode_scene_assets, normalize_design_scene

    raw = normalize_design_scene(frames_to_scene([_jpeg_b64((i * 60, 0, 0)) for i in range(n)], name="cartoon_idle", title="卡通"))
    return raw, ExpressionScene(name="cartoon_idle", title="卡通", aliases=(), frames=tuple(raw["frames"]), assets=tuple(decode_scene_assets(raw)))


# ---------------- 固件契约 ----------------


def test_firmware_keeps_idle_bitmap_face_and_loops_it_offline():
    version = (ROOT / "hardware" / "VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 57)
    header = _fw("display.h")
    for symbol in (
        "bool display_standby_face_store(",
        "bool display_standby_face_available();",
        "const char* display_standby_face_tag();",
        "void display_standby_face_clear();",
        "bool display_standby_face_visit(",
    ):
        assert symbol in header, symbol
    display = _fw("display.cpp")
    assert "kStandbyHintDelayMs = 10000u" in display  # 断开 10 s 后才叠加"请先连接PC服务"
    assert "static bool display_submit_standby_replay()" in display
    assert "req.pb_tracked = false;" in display and "req.standby_replay = true;" in display
    # 公开接口必须放在匿名 namespace 之外，否则 face_store / usb_transport 链接不到
    store_at = display.index("bool display_standby_face_store(const char* json")
    assert display.rfind("}  // namespace", 0, store_at) > display.index("namespace {")
    asr = _fw("asr_chat_client.cpp")
    assert 'doc["face_keep"]' in asr and 'doc["face_tag"]' in asr
    assert "display_standby_face_store(pb_pending_anim_buf_" in asr
    usb = _fw("usb_transport.cpp")
    assert '\\"face_tag\\":\\"%s\\"' in usb and "display_standby_face_tag()" in usb


def test_firmware_face_store_persists_in_own_task_and_restores_at_boot():
    store = _fw("face_store.cpp")
    for needle in (
        'strcmp(type, "face_persist")',
        'strcmp(type, "face_clear")',
        'strcmp(type, "face_status_req")',
        '\\"face_persist_ack\\"',
        '\\"face_clear_ack\\"',
        '\\"face_status_ack\\"',
        'xTaskCreate(persist_task, "face_persist"',  # FFat 写不阻塞 loopTask（USB 心跳）
        '\\"already\\":true',  # 同一标签不重写 flash
        "kMaxTotalBytes = 256u * 1024u",
        '"/face/meta.json"',
    ):
        assert needle in store, needle
    rom = _fw("deskbot_rom.ino")
    assert "face_store_handle_control_json(frame.payload" in rom
    assert rom.index("task_setup_display();") < rom.index("face_store_load();")


# ---------------- Core：标签与 PB 字段 ----------------


def test_standby_face_tag_is_a_content_hash_shared_by_runtime_and_web_paths():
    from deskbot_server.application.expression_catalog import standby_face_tag

    frames = [
        {"ms": 500, "elements": {"extra": [{"shape": "image", "asset": 0}]}},
        {"ms": 300, "elements": {"extra": [{"shape": "image", "asset": 1}]}},
    ]
    assets = [b"jpeg-a", b"jpeg-b"]
    tag = standby_face_tag(frames, assets)
    assert len(tag) == 16 and int(tag, 16) >= 0
    # 浏览器回传的 anim（ms 是浮点、多了 phoneme 字段、顺序不同的 dict）算出同一个标签
    web = [
        {"phoneme": "", "elements": {"extra": [{"asset": 0, "shape": "image"}]}, "ms": 500.0},
        {"ms": 300, "elements": {"extra": [{"shape": "image", "asset": 1}]}},
    ]
    assert standby_face_tag(web, assets) == tag
    assert standby_face_tag(frames, [b"jpeg-a", b"jpeg-c"]) != tag
    assert standby_face_tag([{**frames[0], "ms": 800}, frames[1]], assets) != tag


def test_build_expression_pb_frames_marks_face_keep_only_when_asked():
    from deskbot_server.application.expression_catalog import (
        build_expression_pb_frames,
        standby_face_tag,
    )

    _raw, scene = _bitmap_scene()
    out: list[list[bytes]] = []
    plain = build_expression_pb_frames(scene, request_id="r1", out_binaries=out)
    assert plain and all("face_keep" not in m and "face_tag" not in m for m in plain)
    tag = standby_face_tag(scene.frames, scene.assets)
    kept = build_expression_pb_frames(scene, request_id="r1", out_binaries=out, face_keep=True, face_tag=tag)
    assert kept and all(m["face_keep"] is True and m["face_tag"] == tag for m in kept)
    assert len(tag) <= 23  # 固件 tag[24]


def test_web_default_and_runtime_idle_compute_the_same_tag():
    """表情页设为默认（anim/assets 走 base64）与语音运行时（catalog 场景）必须算出同一个标签，否则每次连接都重写 flash。"""
    from deskbot_server.application.expression_catalog import standby_face_tag
    from deskbot_server.face_expr_scenes_store import normalize_scene_assets

    raw, scene = _bitmap_scene()
    runtime_tag = standby_face_tag(scene.frames, scene.assets)
    web_anim = [{"elements": f["elements"], "ms": max(1, int(f["ms"]))} for f in raw["frames"]]
    web_assets = [base64.b64decode(a) for a in normalize_scene_assets(raw["assets"])]
    assert standby_face_tag(web_anim, web_assets) == runtime_tag


# ---------------- Core：会话回执与运行时落盘 ----------------


def test_session_face_requests_wait_for_typed_acks():
    from deskbot_server.infrastructure.serial.session import DeviceSession

    class _S:
        sent: list = []
        _control_acks: dict = {}
        _control_ack_events: dict = {}

        async def send_control(self, message):
            self.sent.append(message)
            ack_type = {"face_status_req": "face_status_ack", "face_persist": "face_persist_ack", "face_clear": "face_clear_ack"}[message["type"]]
            self._control_acks[ack_type] = {"type": ack_type, "ok": True, "tag": "t1", "persisted": False}
            self._control_ack_events[ack_type].set()

    _S.control_request = DeviceSession.control_request
    s = _S()
    assert asyncio.run(DeviceSession.face_status_request(s))["tag"] == "t1" and s.sent[-1] == {"type": "face_status_req"}
    assert asyncio.run(DeviceSession.face_persist_request(s))["ok"] is True and s.sent[-1] == {"type": "face_persist"}
    assert asyncio.run(DeviceSession.face_clear_request(s))["ok"] is True and s.sent[-1] == {"type": "face_clear"}
    # 解析分发表里必须登记这三种回执，否则 control_request 永远超时
    src = (ROOT / "service/src/deskbot_server/infrastructure/serial/session.py").read_text(encoding="utf-8")
    table = src[src.index('"thermal_cutoff_ack",') : src.index("self._control_acks[message_type] = dict(message)")]
    assert '"face_persist_ack"' in table and '"face_clear_ack"' in table and '"face_status_ack"' in table


class _FaceSession:
    def __init__(self, tag: str = "", persisted: bool = False, old_firmware: bool = False):
        self.tag, self.persisted, self.old_firmware, self.calls = tag, persisted, old_firmware, []

    async def face_status_request(self):
        self.calls.append("status")
        return None if self.old_firmware else {"type": "face_status_ack", "tag": self.tag, "persisted": self.persisted}

    async def face_persist_request(self):
        self.calls.append("persist")
        self.persisted = True
        return {"type": "face_persist_ack", "ok": True, "tag": self.tag, "bytes": 1234}

    async def face_clear_request(self):
        self.calls.append("clear")
        self.tag, self.persisted = "", False
        return {"type": "face_clear_ack", "ok": True}


def _runtime(session):
    from deskbot_server.application.expression_runtime import RtcExpressionRuntime

    return RtcExpressionRuntime("dev-1", session)


def test_runtime_persists_the_face_the_device_holds_and_skips_when_already_stored():
    sess = _FaceSession(tag="t1", persisted=False)
    rt = _runtime(sess)
    asyncio.run(rt._persist_standby_face("t1"))
    assert sess.calls == ["status", "persist"] and rt.persisted_face_tag == "t1"
    # 已经在盘上：只查不写
    sess2 = _FaceSession(tag="t1", persisted=True)
    rt2 = _runtime(sess2)
    asyncio.run(rt2._persist_standby_face("t1"))
    assert sess2.calls == ["status"] and rt2.persisted_face_tag == "t1"


def test_runtime_skips_persist_when_device_holds_a_different_face_or_old_firmware():
    sess = _FaceSession(tag="other", persisted=False)
    rt = _runtime(sess)
    asyncio.run(rt._persist_standby_face("t1"))
    assert sess.calls == ["status"] and rt.persisted_face_tag == ""
    old = _FaceSession(old_firmware=True)
    rt_old = _runtime(old)
    asyncio.run(rt_old._persist_standby_face("t1"))
    assert old.calls == ["status"] and rt_old.persisted_face_tag == ""


def test_runtime_clears_leftover_cartoon_face_when_idle_is_vector():
    sess = _FaceSession(tag="t1", persisted=True)
    rt = _runtime(sess)
    asyncio.run(rt._persist_standby_face(""))
    assert sess.calls == ["status", "clear"] and sess.tag == ""
    # 设备本来就没有卡通脸：不发 clear
    empty = _FaceSession()
    asyncio.run(_runtime(empty)._persist_standby_face(""))
    assert empty.calls == ["status"]


def test_runtime_submit_marks_idle_bitmap_chain_as_standby_face():
    """_submit 只给 idle 状态的首条 replace 位图链加 face_keep；循环续发（append）与其它状态不加。"""
    src = (ROOT / "service/src/deskbot_server/application/expression_runtime.py").read_text(encoding="utf-8")
    block = src[src.index("face_keep = bool(") : src.index("face_tag = standby_face_tag(scene.frames, scene.assets) if face_keep")]
    for cond in ("scene.assets", 'request.kind == "state"', 'request.state == "idle"', "request.duration_ms is None", "not request.pb_append"):
        assert cond in block, cond
    assert "face_keep=face_keep," in src and "face_tag=face_tag," in src
    # 链被设备接收（accepted）后安排落盘，不等 played
    assert src.index("self._sync_standby_face(face_keep, face_tag, scene)") < src.index("if not request.wait_for_played:")


# ---------------- Core：路由与页面 ----------------


def test_face_routes_registered_and_proxied():
    from deskbot_server.web.blueprints import proxy_bp
    from deskbot_server.ws.routes import ROUTES_API_KEY

    for path in ("/api/face_status", "/api/face_persist", "/api/face_clear"):
        assert path in ROUTES_API_KEY and path in proxy_bp._DEVICE_SCOPED_PATHS  # noqa: SLF001
    assert proxy_bp._PROXY_METHODS["/api/face_status"] == {"GET"}  # noqa: SLF001
    assert proxy_bp._PROXY_METHODS["/api/face_persist"] == {"POST"}  # noqa: SLF001
    assert proxy_bp._PROXY_METHODS["/api/face_clear"] == {"POST"}  # noqa: SLF001


def test_face_persist_route_marks_runtime_and_reports_old_firmware(monkeypatch):
    from deskbot_server.ws.routes import face_store

    class _Req:
        qargs: dict = {"device_id": "d1"}

        def __init__(self, method, body=b""):
            self.method = method
            self.request = type("R", (), {"body": body})()

    class _Hello:
        face_tag = "boot-tag"

    class _Ctx:
        def __init__(self, session):
            self.session = session
            self.serial_manager_provider = lambda: type("M", (), {"session_for_device": lambda _self, dev: self.session})()

        def json_resp(self, status, payload):
            return status, payload

    class _Runtime:
        noted: list = []

        def note_face_persisted(self, tag):
            self.noted.append(tag)

    runtime = _Runtime()
    monkeypatch.setattr(face_store, "_runtime", lambda dev: runtime)

    sess = _FaceSession(tag="t1", persisted=False)
    sess.hello_info = _Hello()
    status, payload = asyncio.run(face_store.handle_face_persist(_Ctx(sess), _Req("POST", b'{"device_id":"d1"}')))
    assert status == 200 and payload["ok"] is True and payload["tag"] == "t1" and payload["bytes"] == 1234
    assert runtime.noted == ["t1"]
    status, payload = asyncio.run(face_store.handle_face_status(_Ctx(sess), _Req("GET")))
    assert status == 200 and payload["persisted"] is True and payload["hello_tag"] == "boot-tag"
    status, payload = asyncio.run(face_store.handle_face_clear(_Ctx(sess), _Req("POST", b'{"device_id":"d1"}')))
    assert status == 200 and payload["ok"] is True and runtime.noted == ["t1", ""]

    old = _FaceSession(old_firmware=True)

    async def _none():
        return None

    old.face_persist_request = _none
    status, payload = asyncio.run(face_store.handle_face_persist(_Ctx(old), _Req("POST", b'{"device_id":"d1"}')))
    assert status == 200 and payload["ok"] is False and payload["supported"] is False


def test_expression_page_persists_default_face_and_http_marks_standby_chain():
    html = (ROOT / "service/src/deskbot_server/web/templates/app2c/expr.html").read_text(encoding="utf-8")
    assert "standby:true" in html and "/proxy/deskbot/api/face_persist" in html and "/proxy/deskbot/api/face_status" in html
    assert "syncStandbyFaceAfterDefault(deviceScene)" in html and "syncStandbyFaceAfterDefault(idleScene)" in html
    assert '[[ standbyFace.text ]]' in html
    api = (ROOT / "service/src/deskbot_server/ws/http_api.py").read_text(encoding="utf-8")
    block = api[api.index('standby = body.get("standby") is True') : api.index('"standby_face_tag": standby_tag')]
    assert 'payload["face_keep"] = True' in block and 'payload["face_tag"] = standby_tag' in block
    doc = (ROOT / "service/docs/esp32_pb_protocol.md").read_text(encoding="utf-8")
    assert "### 5.7 待机卡通脸的保持与持久化" in doc and "face_persist_ack" in doc
    readme = (ROOT / "hardware/README.md").read_text(encoding="utf-8")
    assert "`face_persist` / `face_clear` / `face_status_req`" in readme
