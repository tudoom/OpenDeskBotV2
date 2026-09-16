"""固件 0.0.53：麦克风按需上行 + 温度断电保护；0.0.54：静音开关、默认持续上行；Core 路由与回执；参数设置页。"""

from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_firmware_gates_mic_uplink_by_energy_vad_with_preroll():
    cpp = (ROOT / "hardware/firmware/asr_chat_client.cpp").read_text(encoding="utf-8")
    assert "bool AsrChatClient::micGateShouldSend(" in cpp
    assert "mic_uplink_mode() == MicUplinkMode::kVad" in cpp
    assert "MIC_VAD_HANGOVER_MS" in cpp and "MIC_VAD_PREROLL_FRAMES" in cpp
    # 关门期间不编码：直接 continue，不进 queueAudioOpusFrame
    gate = cpp[cpp.index("!micGateShouldSend(frame"):cpp.index("bool audio_ok = false;")]
    assert "continue;" in gate and "queueAudioOpusFrame" not in gate
    policy = (ROOT / "hardware/firmware/mic_uplink_policy.cpp").read_text(encoding="utf-8")
    assert '\\"mic_uplink_mode_ack\\"' in policy and "kPrefsNs = \"micup\"" in policy
    header = (ROOT / "hardware/firmware/mic_uplink_policy.h").read_text(encoding="utf-8")
    assert "MIC_VAD_HANGOVER_MS = 2000" in header and "MIC_VAD_PREROLL_FRAMES = 15" in header


def test_firmware_defaults_to_continuous_uplink_and_has_a_mute_switch():
    """0.0.54：默认持续上行（用户要求）；静音开关帧读出即丢，不增强不编码不上行，落 NVS。"""
    header = (ROOT / "hardware/firmware/mic_uplink_policy.h").read_text(encoding="utf-8")
    assert "MIC_UPLINK_DEFAULT_MODE = MicUplinkMode::kContinuous" in header
    assert "bool mic_uplink_muted();" in header and "bool mic_uplink_set_muted(bool muted);" in header
    policy = (ROOT / "hardware/firmware/mic_uplink_policy.cpp").read_text(encoding="utf-8")
    assert "s_mode{static_cast<uint8_t>(MIC_UPLINK_DEFAULT_MODE)}" in policy
    assert 'kPrefsMuteKey = "mute"' in policy and "std::atomic<bool> s_muted{false}" in policy
    assert '\\"mic_mute_ack\\"' in policy and 'strcmp(type, "mic_mute_req")' in policy
    cpp = (ROOT / "hardware/firmware/asr_chat_client.cpp").read_text(encoding="utf-8")
    muted = cpp.index("if (mic_uplink_muted()) {")
    assert muted < cpp.index("enhanceVoice(frame, kFrameSamples20ms);", muted - 400)
    gate = cpp[muted:cpp.index("enhanceVoice(frame, kFrameSamples20ms);", muted)]
    assert "continue;" in gate and "queueAudioOpusFrame" not in gate and "enhanceVoice" not in gate
    # 0.0.55：静音期间也要跑 loopLite()，否则 PB/舵机/回执整轮（30 s）没人服务（0.0.54 张望不动的根因）
    assert "loopLite();" in gate
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 55)
    # 0.0.56：接着 USB 主机（USB-JTAG 收到主机 SOF）就优先走 USB——开机不因 WiFi 先起来绑到 WiFi，WiFi 绑定中发现主机在就切回
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert "usb_host_present_stable()" in usb and 'switch_link(false, "usb_host_present")' in usb
    assert "kUsbHostPresentDebounceMs = 3000u" in usb
    # 0.0.58：主机"不在场"也要连续 3 s 才算；绑到 WiFi 后光有 SOF 不拽回 USB，USB 上真来了字节才回
    # （2026-09-14 黑匣子：拔下 Mac 插到带电脑的口上供电，bind wifi → 3 ms 后 bind usb 反复，WiFi 握手永远收不完整）
    assert "usb_host_absent_stable();" in usb and "kUsbHostAbsentDebounceMs = 3000u" in usb
    assert "!usb_host_present_now() &&" not in usb
    assert "usb_host_present_stable() && usb_bytes_since_wifi_bind" in usb and "s_wifi_bound_at_ms = millis()" in usb
    # 0.0.59：主机信号在、但 USB 上 20 s 没有任何字节也走 WiFi（插在别的电脑/扩展坞上供电时 SOF 一直在）
    assert "kUsbHostQuietFallbackMs = 20000u" in usb and "usb_quiet_ms >= kUsbHostQuietFallbackMs" in usb
    assert 'switch_link(true, host_absent ? "usb_idle_wifi_ready" : "usb_host_quiet_wifi_ready")' in usb
    wifi = (ROOT / "hardware/firmware/wifi_link.cpp").read_text(encoding="utf-8")
    # 绑在 USB 上时把没人读的 TCP 字节丢掉，对端关闭才看得见（否则 MSG_PEEK 永远"有数据"，不重连）
    drain = wifi[wifi.index("if (!usb_transport_active_link_is_wifi()) {"):wifi.index('wifi_link_drop_connection("peer closed")')]
    assert "recv(s_stream.fd, scratch, sizeof(scratch), MSG_DONTWAIT)" in drain
    # 0.0.59：WiFi 收包整段 recv——逐字节 read() 每字节一次 lwIP 系统调用，3 ms 泵预算只够 ~150 B，
    # 5.7 KB 接收窗口被表情帧塞满后 PC 侧零窗口探测按秒退避（2026-09-14 实测帧延迟 5～16 s，6 s 确认超时反复杀会话）
    assert "size_t readBytes(uint8_t* buffer, size_t length) override" in wifi
    bulk = wifi[wifi.index("size_t readBytes(uint8_t* buffer, size_t length) override"):wifi.index("size_t readBytes(char* buffer, size_t length) override")]
    assert "recv(fd, buffer, length, MSG_DONTWAIT)" in bulk and "rx_bytes += static_cast<uint32_t>(n);" in bulk
    pump = usb[usb.index("while (available > 0 && drained < 16u * 1024u &&"):usb.index("parser_idle_tick();")]
    assert "if (!link_is_usb) {" in pump and "link->readBytes(s_wifi_rx_bulk, want)" in pump
    assert "consume_rx_byte(s_wifi_rx_bulk[i]);" in pump and "drained += got;" in pump
    # Core 侧：WiFi 会话的帧确认超时放宽到 12 s（死链路仍由 6.5 s 心跳兜底），USB 保持 6 s
    manager = (ROOT / "service/src/deskbot_server/infrastructure/serial/manager.py").read_text(encoding="utf-8")
    wifi_ctor = manager[manager.index("link_token_validator=link_token_validator,"):manager.index("self._sessions_by_port[name] = session")]
    assert "frame_ack_timeout=12.0," in wifi_ctor
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 59)
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 56)
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert '\\"mic_muted\\":%s' in usb
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 54)


def test_firmware_thermal_cutoff_sleeps_after_sustained_overtemp():
    cpp = (ROOT / "hardware/firmware/thermal_cutoff.cpp").read_text(encoding="utf-8")
    header = (ROOT / "hardware/firmware/thermal_cutoff.h").read_text(encoding="utf-8")
    assert "THERMAL_CUTOFF_DEFAULT_C = 70" in header
    assert "THERMAL_CUTOFF_SUSTAIN_MS = 30000" in header
    assert "THERMAL_CUTOFF_SLEEP_SEC = 600" in header
    assert "esp_deep_sleep_start()" in cpp and "esp_sleep_enable_timer_wakeup" in cpp
    assert '\\"thermal_shutdown\\"' in cpp  # 睡前通知 PC
    assert "digitalWrite(kAmpCtrlGpio, LOW)" in cpp
    rom = (ROOT / "hardware/firmware/deskbot_rom.ino").read_text(encoding="utf-8")
    assert "thermal_cutoff_tick();" in rom and "thermal_cutoff_load_prefs();" in rom
    assert "mic_uplink_handle_control_json(frame.payload" in rom
    usb = (ROOT / "hardware/firmware/usb_transport.cpp").read_text(encoding="utf-8")
    assert '\\"mic_uplink_mode\\"' in usb and '\\"thermal_cutoff_c\\"' in usb
    version = (ROOT / "hardware/VERSION").read_text(encoding="utf-8").strip()
    assert tuple(int(x) for x in version.split(".")) >= (0, 0, 53)


def test_session_control_requests_wait_for_typed_acks():
    from deskbot_server.infrastructure.serial.session import DeviceSession

    class _S:
        sent: list = []
        _control_acks: dict = {}
        _control_ack_events: dict = {}

        async def send_control(self, message):
            self.sent.append(message)
            if message["type"].startswith("mic_uplink"):
                ack_type = "mic_uplink_mode_ack"
            elif message["type"].startswith("mic_mute"):
                ack_type = "mic_mute_ack"
            else:
                ack_type = "thermal_cutoff_ack"
            self._control_acks[ack_type] = {"type": ack_type, "mode": "vad", "cutoff_c": 70, "muted": message.get("muted", False)}
            self._control_ack_events[ack_type].set()

    _S.control_request = DeviceSession.control_request  # 复用真实的等待回执逻辑
    s = _S()
    ack = asyncio.run(DeviceSession.mic_uplink_mode_request(s, "vad"))
    assert ack["mode"] == "vad" and s.sent[-1] == {"type": "mic_uplink_mode", "mode": "vad"}
    ack = asyncio.run(DeviceSession.thermal_cutoff_request(s, 70))
    assert ack["cutoff_c"] == 70 and s.sent[-1] == {"type": "thermal_cutoff", "c": 70}
    ack = asyncio.run(DeviceSession.thermal_cutoff_request(s))
    assert s.sent[-1] == {"type": "thermal_cutoff_req"}
    ack = asyncio.run(DeviceSession.mic_mute_request(s, True))
    assert ack["muted"] is True and s.sent[-1] == {"type": "mic_mute", "muted": True}
    ack = asyncio.run(DeviceSession.mic_mute_request(s))
    assert s.sent[-1] == {"type": "mic_mute_req"}


def test_thermal_guard_records_device_shutdown():
    from deskbot_server.application.thermal_guard import ThermalGuard, on_session_telemetry

    g = ThermalGuard(clock=lambda: 100.0)
    import deskbot_server.application.thermal_guard as tg

    tg._guard = g  # noqa: SLF001

    class _Sess:
        device_id = "dev"
        transport = "usb_cdc"

    on_session_telemetry(_Sess(), {"thermal_shutdown": {"temp_c": 72, "cutoff_c": 70, "sleep_s": 600, "trips": 1}})
    snap = g.snapshot("dev")
    assert snap["level"] == "cutoff" and "断电休眠 10 分钟" in snap["message"] and snap["shutdown"]["trips"] == 1
    assert g.all_alerts()[0]["device_id"] == "dev"
    # 重新开机后温度正常 → 回到 ok
    assert g.observe("dev", 45)["level"] == "ok"
    tg._guard = None  # noqa: SLF001


def test_routes_registered_and_proxied():
    from deskbot_server.web.blueprints import proxy_bp
    from deskbot_server.ws.routes import ROUTES_API_KEY

    assert "/api/mic_uplink_mode" in ROUTES_API_KEY and "/api/thermal_cutoff" in ROUTES_API_KEY
    assert "/api/mic_uplink_mode" in proxy_bp._PROXY_METHODS and "/api/thermal_cutoff" in proxy_bp._PROXY_METHODS  # noqa: SLF001
    assert "/api/mic_mute" in ROUTES_API_KEY and proxy_bp._PROXY_METHODS["/api/mic_mute"] == {"GET", "POST"}  # noqa: SLF001
    assert "/api/live_behavior" in ROUTES_API_KEY and proxy_bp._PROXY_METHODS["/api/live_behavior"] == {"GET"}  # noqa: SLF001


def test_params_page_owns_power_settings_and_motion_page_is_motion_only():
    """用户要求：省电与温度保护从动作页搬到独立的「参数设置」；「配置」改名「模型配置」；动作页只管动作。"""
    templates = ROOT / "service/src/deskbot_server/web/templates"
    params = (templates / "app2c/params.html").read_text(encoding="utf-8")
    lab = (templates / "app2c/lab.html").read_text(encoding="utf-8")
    base = (templates / "base_2c.html").read_text(encoding="utf-8")
    advanced = (templates / "app2c/advanced.html").read_text(encoding="utf-8")
    assert "<h1>参数设置</h1>" in params
    for needle in ("/api/mic_mute", "/api/mic_uplink_mode", "/api/thermal_cutoff", "静音", "温度断电阈值"):
        assert needle in params, needle
    assert "micMode:'continuous'" in params and 'value="continuous">持续（默认' in params
    assert "muted:false" in params  # 静音默认关闭
    # 「应用到设备」在卡片左下角：不再和输入框并排
    assert params.count('<div class="lab-actions">') == 2 and "lab-field-btn" not in params
    assert "justify-content:flex-start" in params[params.index(".lab-actions{"):params.index(".lab-actions{") + 80]
    for gone in ("省电与温度保护", "mic_uplink_mode", "thermal_cutoff", "实验台", "loadPowerSettings"):
        assert gone not in lab, gone
    assert "<h1>动作</h1>" in lab and "{% block page_title %}动作 · 小歪{% endblock %}" in lab
    assert 'data-label="模型配置"' in base and 'data-label="参数设置"' in base and 'data-label="配置"' not in base
    assert base.index('data-label="模型配置"') < base.index('data-label="参数设置"')
    assert "<h1>模型配置</h1>" in advanced


def test_mic_mute_route_validates_boolean():
    from deskbot_server.ws.routes import device_power

    class _Req:
        method = "POST"
        qargs: dict = {}

        def __init__(self, body):
            self.request = type("R", (), {"body": body})()

    class _Session:
        calls: list = []

        async def mic_mute_request(self, muted=None):
            self.calls.append(muted)
            return {"type": "mic_mute_ack", "muted": bool(muted), "mode": "continuous"}

    class _Ctx:
        def __init__(self):
            self.session = _Session()
            self.serial_manager_provider = lambda: type("M", (), {"session_for_device": lambda _self, dev: self.session})()

        def json_resp(self, status, payload):
            return status, payload

    ctx = _Ctx()
    status, payload = asyncio.run(device_power.handle_mic_mute(ctx, _Req(b'{"device_id":"d1","muted":true}')))
    assert status == 200 and payload["muted"] is True and ctx.session.calls == [True]
    status, payload = asyncio.run(device_power.handle_mic_mute(ctx, _Req(b'{"device_id":"d1","muted":"maybe"}')))
    assert status == 400 and "boolean" in payload["error"]


def _pref_env(tmp_path, monkeypatch):
    from deskbot_server import device_data

    monkeypatch.setattr(device_data, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", tmp_path / "data" / "local")


def test_mute_is_pc_owned_default_off_and_corrects_device_leftover(tmp_path, monkeypatch):
    """2026-09-14 用户要求：静音开关以 PC 端为准、默认关闭。设备 NVS 里残留「开」时，读一次就被纠正成 PC 的值；
    POST 先写 PC 偏好再推设备；设备连上时也按 PC 偏好推送。"""
    import asyncio

    from deskbot_server.application.device_power_sync import apply_pc_power_settings
    from deskbot_server.device_preferences import load_preferences
    from deskbot_server.ws.routes import device_power

    _pref_env(tmp_path, monkeypatch)
    assert load_preferences()["power"]["mic_muted"] is False  # 出厂默认关

    class _Req:
        qargs = {"device_id": "d1"}

        def __init__(self, method, body=b""):
            self.method = method
            self.request = type("R", (), {"body": body})()

    class _Session:
        def __init__(self, device_muted):
            self.device_muted = device_muted
            self.calls = []

        async def mic_mute_request(self, muted=None):
            self.calls.append(muted)
            if muted is not None:
                self.device_muted = bool(muted)
            return {"type": "mic_mute_ack", "muted": self.device_muted, "mode": "continuous"}

    class _Ctx:
        def __init__(self, session):
            self.session = session
            self.serial_manager_provider = lambda: type("M", (), {"session_for_device": lambda _self, dev: session})()

        def json_resp(self, status, payload):
            return status, payload

    # 设备残留「开」，PC 默认关：GET 返回关，并把设备纠正成关
    session = _Session(device_muted=True)
    status, payload = asyncio.run(device_power.handle_mic_mute(_Ctx(session), _Req("GET")))
    assert status == 200 and payload["muted"] is False and payload["source"] == "pc"
    assert session.calls == [None, False] and session.device_muted is False
    # POST 开：PC 偏好落盘，设备同步
    status, payload = asyncio.run(device_power.handle_mic_mute(_Ctx(session), _Req("POST", b'{"device_id":"d1","muted":true}')))
    assert status == 200 and payload["muted"] is True and load_preferences()["power"]["mic_muted"] is True
    assert session.device_muted is True
    # 设备连上：按 PC 偏好推送（此时是开）；固件太旧不回执也不报错
    fresh = _Session(device_muted=False)
    ack = asyncio.run(apply_pc_power_settings(fresh, device_id="d1"))
    assert ack["muted"] is True and fresh.calls == [True]

    class _Old:
        async def mic_mute_request(self, muted=None):
            return None

    assert asyncio.run(apply_pc_power_settings(_Old(), device_id="d1")) is None


def test_params_page_says_mute_follows_this_pc():
    html = (ROOT / "service/src/deskbot_server/web/templates/app2c/params.html").read_text(encoding="utf-8")
    assert "以这台电脑的设置为准" in html and "两项都保存在设备上" not in html
    integration = (ROOT / "service/src/deskbot_server/infrastructure/serial/integration.py").read_text(encoding="utf-8")
    assert "apply_pc_power_settings(session, device_id=hello.device_id)" in integration
