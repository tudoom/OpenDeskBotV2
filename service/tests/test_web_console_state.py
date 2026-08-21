from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def console_client(monkeypatch, tmp_path):
    db_path = tmp_path / "console.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.hardware_catalog import ensure_local_device

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    reset_engine()
    init_engine(db_path)
    init_database()
    from deskbot_server.web.app import create_app

    ensure_local_device("deskbot_console")
    client = create_app().test_client()
    client.post("/app/api/devices/select", json={"device_id": "deskbot_console"})
    try:
        yield client
    finally:
        reset_engine()


def test_device_api_does_not_report_offline_when_realtime_service_is_down(
    console_client, monkeypatch
):
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app_bp.fetch_live_device_snapshot",
        lambda **_kwargs: {
            "ok": False,
            "observed_at": "2026-07-27T10:00:00+00:00",
            "error": "upstream_unreachable",
            "devices": {},
        },
    )

    response = console_client.get("/app/api/devices")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["control_plane"]["ok"] is False
    assert payload["devices"][0]["online"] is None
    assert payload["devices"][0]["presence_state"] == "control_plane_down"
    assert payload["observed_at"]


def test_device_api_reports_real_offline_from_healthy_registry(console_client, monkeypatch):
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app_bp.fetch_live_device_snapshot",
        lambda **_kwargs: {
            "ok": True,
            "observed_at": "2026-07-27T10:00:00+00:00",
            "error": None,
            "devices": {},
        },
    )

    payload = console_client.get("/app/api/devices").get_json()

    assert payload["control_plane"]["ok"] is True
    assert payload["devices"][0]["online"] is False
    assert payload["devices"][0]["presence_state"] == "offline"


def test_device_api_and_console_expose_microphone_acoustic_alarm(
    console_client,
    monkeypatch,
):
    health = {
        "status": "mic_no_acoustic_signal",
        "observed_audio_ms": 20_000,
        "window_audio_ms": 20_000,
        "ac_rms": 11.2,
        "short_term_variation": 30.4,
        "frame_count": 200,
    }
    monkeypatch.setattr(
        "deskbot_server.web.blueprints.app_bp.fetch_live_device_snapshot",
        lambda **_kwargs: {
            "ok": True,
            "observed_at": "2026-07-30T04:00:00+00:00",
            "error": None,
            "devices": {
                "deskbot_console": {
                    "online": True,
                    "transport": "usb_cdc",
                    "interaction_state": "IDLE",
                    "microphone_health": health,
                }
            },
        },
    )

    payload = console_client.get("/app/api/devices").get_json()
    assert payload["devices"][0]["microphone_health"] == health

    root = Path(__file__).parents[1] / "src" / "deskbot_server" / "web"
    console = (root / "static" / "console_2c.js").read_text(encoding="utf-8")
    home = (root / "templates" / "app2c" / "home.html").read_text(
        encoding="utf-8"
    )
    message = "麦克风没有收到声音，请检查扩展板或麦克风"
    assert message in console
    assert message in home
    assert "mic_no_acoustic_signal" in console
    assert "mic_no_acoustic_signal" in home
    assert "data-page-microphone-health-alert" in home


def test_live_snapshot_preserves_microphone_health(monkeypatch):
    from deskbot_server.web import helpers

    health = {
        "status": "mic_no_acoustic_signal",
        "observed_audio_ms": 20_000,
    }
    monkeypatch.setattr(
        helpers,
        "_fetch_upstream_devices_snapshot",
        lambda **_kwargs: (
            [
                {
                    "device_id": "deskbot_console",
                    "online": True,
                    "microphone_health": health,
                }
            ],
            None,
        ),
    )

    snapshot = helpers.fetch_live_device_snapshot()
    assert snapshot["devices"]["deskbot_console"]["microphone_health"] == health


def test_console_microphone_alarm_renders_and_clears_on_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console microphone alarm test")
    console_path = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
        / "console_2c.js"
    )
    script = f"""
const fs = require("fs");
global.window = global;
global.location = {{
  origin: "http://deskbot.test",
  href: "http://deskbot.test/advanced",
}};
global.BroadcastChannel = undefined;
global.CustomEvent = class {{ constructor(type, options) {{
  this.type = type; this.detail = options && options.detail;
}} }};
global.addEventListener = () => {{}};
global.dispatchEvent = () => {{}};
const classes = new Set();
const attrs = {{}};
const notice = {{
  hidden: true,
  innerHTML: "",
  classList: {{
    add(name) {{ classes.add(name); }},
    remove(name) {{ classes.delete(name); }},
  }},
  setAttribute(name, value) {{ attrs[name] = value; }},
}};
global.document = {{
  hidden: false,
  addEventListener() {{}},
  getElementById(id) {{ return id === "globalDeviceNotice" ? notice : null; }},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
}};
let healthStatus = "mic_no_acoustic_signal";
global.fetch = async () => ({{
  ok: true,
  status: 200,
  async json() {{ return {{
    ok: true,
    current_device_id: "deskbot_console",
    devices: [{{
      device_id: "deskbot_console",
      display_name: "小歪",
      online: true,
      presence_state: "online",
      microphone_health: {{status: healthStatus}},
    }}],
  }}; }},
}});
eval(fs.readFileSync({json.dumps(str(console_path))}, "utf8"));
(async () => {{
  await DeskbotConsole.refresh();
  const alarm = {{
    hidden: notice.hidden,
    html: notice.innerHTML,
    role: attrs.role,
    live: attrs["aria-live"],
    errorClass: classes.has("is-error"),
  }};
  healthStatus = "ok";
  await DeskbotConsole.refresh();
  process.stdout.write(JSON.stringify({{
    alarm,
    recovered: {{
      hidden: notice.hidden,
      html: notice.innerHTML,
      errorClass: classes.has("is-error"),
    }},
  }}));
}})().catch((error) => {{
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
}});
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    assert payload["alarm"]["hidden"] is False
    assert "麦克风没有收到声音，请检查扩展板或麦克风" in payload["alarm"]["html"]
    assert payload["alarm"]["role"] == "alert"
    assert payload["alarm"]["live"] == "assertive"
    assert payload["alarm"]["errorClass"] is True
    assert payload["recovered"] == {
        "hidden": True,
        "html": "",
        "errorClass": False,
    }


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("/home?device_id=deskbot_console", "预览打招呼（SIM）"),
        ("/devices", "USB DIRECT"),
        ("/memories", "它记得的事"),
        ("/people", "认识的人"),
        ("/advanced?tab=llm", "method:'PATCH'"),
    ],
)
def test_console_pages_render_reliability_controls(console_client, path, needle):
    response = console_client.get(path)

    assert response.status_code == 200
    assert needle.encode("utf-8") in response.data


def test_console_shell_has_mobile_navigation_and_shared_status_store(console_client):
    response = console_client.get("/home")

    assert response.status_code == 200
    assert b'id="mobileNavToggle"' in response.data
    assert b'id="sideNav"' in response.data
    assert b"console_2c.js" in response.data
    assert b'id="globalDeviceNotice"' in response.data


def test_console_and_device_pages_fence_out_of_order_responses():
    root = Path(__file__).parents[1] / "src" / "deskbot_server" / "web"
    console = (root / "static" / "console_2c.js").read_text(encoding="utf-8")
    assert "const seq = ++refreshSeq" in console
    assert "seq !== refreshSeq" in console
    assert "requested !== requestedDeviceId()" in console

    for name in (
        "reminders.html",
        "memories.html",
        "people.html",
        "preferences.html",
        "sessions.html",
        "voice.html",
    ):
        page = (root / "templates" / "app2c" / name).read_text(encoding="utf-8")
        assert "loadSeq" in page, name
        assert "seq!==this.loadSeq" in page, name


def test_control_pages_wait_for_device_terminal_completion():
    root = Path(__file__).parents[1] / "src" / "deskbot_server" / "web"
    console = (root / "static" / "console_2c.js").read_text(encoding="utf-8")
    lab = (root / "templates" / "app2c" / "lab.html").read_text(encoding="utf-8")
    home = (root / "templates" / "app2c" / "home.html").read_text(encoding="utf-8")
    expr = (root / "templates" / "app2c" / "expr.html").read_text(encoding="utf-8")
    debug = (root / "templates" / "debug_devices.html").read_text(encoding="utf-8")

    assert "/api/control_operation" in console
    assert "operation.status !== \"completed\"" in console
    assert "submitControlOperation" in console
    assert "执行状态未知" in console
    assert "operation_id:operationId" in lab
    assert lab.count("submitControlOperation(") >= 7
    for completed_message in (
        "舵机动作已在设备执行完成",
        "PB 表情已在设备播放完成",
        "Face catalog 表情已在设备播放完成",
        "PB Anim 已在设备播放完成",
        "Expr Scene 已在设备播放完成",
    ):
        assert completed_message in lab
    assert "TTS 已在设备播放完成" in lab
    assert "场景编排已在设备完成" in lab

    assert "home-servo" in home
    assert "submitControlOperation(" in home
    assert "动作已在 " in home
    assert "expr-preview" in expr
    assert "submitControlOperation(" in expr
    # 预览成功文案随 CRC 四态升级为“设备已确认执行…”，断言公共前缀
    assert "设备已确认执行" in expr

    assert "operation_id: operationId" in debug
    assert "waitForControlOperation(" in debug
    assert "submitAndWaitControlOperation(" in debug
    assert 'newControlOperationId("servo")' in debug
    assert 'newControlOperationId("face")' in debug
    assert 'newControlOperationId("pb-scene")' in debug
    assert "设备 ${deviceId} 已执行完成" in debug
    assert 'url.searchParams.set("text"' not in debug


def test_control_poll_recovers_after_transient_network_failure():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console polling behavior test")
    console_path = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
        / "console_2c.js"
    )
    script = f"""
const fs = require("fs");
global.window = global;
global.location = {{origin: "http://deskbot.test", href: "http://deskbot.test/home"}};
global.document = {{hidden: true, addEventListener() {{}}}};
global.addEventListener = () => {{}};
global.BroadcastChannel = undefined;
let calls = 0;
global.fetch = async () => {{
  calls += 1;
  if (calls === 1) throw new TypeError("temporary network failure");
  const completed = calls >= 3;
  return {{
    ok: true,
    status: 200,
    async json() {{
      return {{
        ok: true,
        terminal: completed,
        operation: {{
          terminal: completed,
          status: completed ? "completed" : "running",
        }},
      }};
    }},
  }};
}};
eval(fs.readFileSync({json.dumps(str(console_path))}, "utf8"));
(async () => {{
  const payload = await DeskbotConsole.waitControlOperation({{
    baseUrl: "/proxy/deskbot",
    deviceId: "deskbot_console",
    operationId: "transient-poll",
    pollMs: 1,
    timeoutMs: 3000,
  }});
  const pollCalls = calls;
  calls = 0;
  global.fetch = async (_url, options) => {{
    calls += 1;
    if (options && options.method === "POST") {{
      throw new TypeError("response lost after submit");
    }}
    if (calls === 2) {{
      return {{
        ok: false,
        status: 404,
        async json() {{ return {{ok: false, error: "not found"}}; }},
      }};
    }}
    return {{
      ok: true,
      status: 200,
      async json() {{
        return {{
          ok: true,
          terminal: true,
          operation: {{terminal: true, status: "completed"}},
        }};
      }},
    }};
  }};
  const recovered = await DeskbotConsole.submitControlOperation({{
    url: "/proxy/deskbot/api/device_servo?operation_id=uncertain-submit",
    requestOptions: {{method: "POST"}},
    baseUrl: "/proxy/deskbot",
    deviceId: "deskbot_console",
    operationId: "uncertain-submit",
    pollMs: 1,
    timeoutMs: 3000,
  }});
  process.stdout.write(JSON.stringify({{
    pollCalls,
    submitCalls: calls,
    status: payload.operation.status,
    submitStatus: recovered.terminal.operation.status,
  }}));
}})().catch((error) => {{
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
}});
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(completed.stdout) == {
        "pollCalls": 3,
        "submitCalls": 3,
        "status": "completed",
        "submitStatus": "completed",
    }


def test_servo_web_controls_submit_logical_steps_atomically():
    root = Path(__file__).parents[1] / "src" / "deskbot_server" / "web"
    lab = (root / "templates" / "app2c" / "lab.html").read_text(
        encoding="utf-8"
    )
    home = (root / "templates" / "app2c" / "home.html").read_text(
        encoding="utf-8"
    )
    debug = (root / "templates" / "debug_devices.html").read_text(
        encoding="utf-8"
    )

    # The 2C page adapts its internal view model to the canonical persisted
    # schema and uses the same safe Y range as the service and firmware.
    for field in (
        "xMin:c.x_min",
        "xMax:c.x_max",
        "yMin:c.y_min",
        "yMax:c.y_max",
        "xReverse:c.x_reverse",
        "yReverse:c.y_reverse",
    ):
        assert field in lab
    # 限位/时长边界不再内联硬编码：全部来自 /api/servo_contract 契约端点。
    assert "/api/servo_contract" in lab
    assert "y_min:70,y_max:110" not in lab
    assert "c.yMin ?? env.yMin" in lab
    assert "c.yMax ?? env.yMax" in lab
    assert (
        'v-model.number="servo.direct.ms" type="number" '
        ':min="contractLimits.minSegmentDurationMs"' in lab
    )

    # A preset is one control operation containing every step; preview gets
    # the same complete array and owns one uninterrupted sequence token.
    assert "submitServoSteps(steps" in lab
    assert "steps:arr" in lab
    assert "this.previewServoSteps(arr)" in lab
    assert "Promise.allSettled([" in lab
    assert "steps[0]" not in lab
    assert lab.count("proxyPath('/api/device_servo'") == 1
    assert "const requestBody={steps,action:'replace',level:3,operation_id:operationId}" in home
    assert "this.playHomeRobotMotion(steps)" in home
    assert "Promise.allSettled([" in home
    assert "for(const step of steps){" not in home
    assert home.count("/proxy/deskbot/api/device_servo") == 1
    assert "if(moves.length) out.moves=moves" in lab
    assert "if(anims.length) out.anims=anims" in lab
    assert "c.text||c.moves||c.anims" in lab
    assert "c.servo.x!=null || c.servo.y!=null" in lab
    assert "out.expr=" not in lab
    assert "out.servo=" not in lab

    # The legacy debug page no longer reverses outbound coordinates or emits
    # one HTTP operation per step. Reverse remains only for decoding pb_ack.
    assert "sendDeviceServoSteps(arr" in debug
    assert "steps: logicalSteps" in debug
    assert "body: JSON.stringify(requestBody)" in debug
    assert debug.count('deskbotApiUrl(this.httpBase, "/api/device_servo")') == 1
    assert "_servoStepToProtocol" not in debug
    assert "_servoStepsToProtocol" not in debug
    assert "_manualDelay(" not in debug
    assert 'url.searchParams.set("dyaw"' not in debug
    assert 'url.searchParams.set("dpitch"' not in debug
    assert "旧版记录是已反向的协议坐标" in debug
    # 调试页限位/默认预设不再硬编码：以 /api/servo_contract 为单一源。
    assert "/api/servo_contract" in debug
    assert "servoCfgYMin: null" in debug
    assert "servoCfgYMax: null" in debug
    assert "DEFAULT_SERVO_PRESETS" not in debug

    # Scene data is canonical chunks only. Parallel lanes survive a no-op edit
    # and participate in dependency scanning, duration, summary and preview.
    for legacy_track in ("servo_track", "expr_track", "text_track"):
        assert legacy_track not in debug
    assert "if (moves.length) out.moves = moves" in debug
    assert "if (anims.length) out.anims = anims" in debug
    assert "return (this.spPlaybooks || []).map((row) => this.spNormalizePlaybook(row))" in debug
    assert "moves.push(...JSON.parse(JSON.stringify(draft.moves)))" in debug
    assert "anims.push(...JSON.parse(JSON.stringify(draft.anims)))" in debug
    assert "out.expr =" not in debug
    assert "out.servo =" not in debug
    assert "for (const move of (Array.isArray(chunk.moves)" in debug
    assert "for (const anim of (Array.isArray(chunk.anims)" in debug
    assert "for (const move of (c && c.moves)" in debug


def test_servo_control_pages_render_after_atomic_contract_change(console_client):
    for path, marker in (
        ("/home", "const requestBody={steps"),
        ("/lab?tab=servo", "submitServoSteps"),
        ("/debug/devices", "sendDeviceServoSteps"),
    ):
        response = console_client.get(path)
        assert response.status_code == 200
        assert marker.encode("utf-8") in response.data


def test_scene_web_adapters_round_trip_canonical_parallel_lanes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the scene Web adapter contract test")
    root = Path(__file__).parents[1] / "src" / "deskbot_server" / "web"
    lab_path = root / "templates" / "app2c" / "lab.html"
    debug_path = root / "templates" / "debug_devices.html"
    script = f"""
const fs = require("fs");
function loadComponent(path) {{
  const raw = fs.readFileSync(path, "utf8");
  const scripts = [...raw.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)];
  let source = scripts[scripts.length - 1][1].replace(/\\{{\\{{.*?\\}}\\}}/g, "null");
  let captured = null;
  const Vue = {{
    createApp(options) {{ captured = options; return {{ mount() {{}} }}; }},
    markRaw(value) {{ return value; }},
  }};
  const window = {{__LAB_FACE__: {{}}, location: {{search: ""}}}};
  new Function("Vue", "window", "document", source)(Vue, window, {{}});
  return captured;
}}
function assert(condition, message) {{ if (!condition) throw new Error(message); }}
const legacy = {{
  name: "round_trip", title: "Round trip", chunks: [{{
    id: "c1", text: "hello",
    servo: {{preset: "center", ms: 300}},
    expr: {{scene: "happy", ms: 400}},
    moves: [{{move: "look_left", ms: 500}}],
    anims: [{{anim: "blink", ms: 600}}],
  }}],
}};

const lab = loadComponent({json.dumps(str(lab_path))});
const labCtx = Object.assign({{}}, lab.methods);
const labDraft = labCtx.editablePlaybook(legacy);
const labSaved = labCtx.cleanPlaybook(labDraft);
const labChunk = labSaved.chunks[0];
assert(!("servo" in labChunk) && !("expr" in labChunk), "lab emitted singular fields");
assert(labChunk.moves.length === 2 && labChunk.anims.length === 2, "lab lost or duplicated lanes");
assert(labChunk.moves[0].move === "center" && labChunk.moves[1].move === "look_left", "lab move order changed");
assert(labChunk.anims[0].anim === "happy" && labChunk.anims[1].anim === "blink", "lab anim order changed");

const debug = loadComponent({json.dumps(str(debug_path))});
const debugCtx = Object.assign({{}}, debug.methods);
const normalized = debugCtx.spNormalizePlaybook(legacy);
const normalizedChunk = normalized.chunks[0];
assert(!("servo" in normalizedChunk) && !("expr" in normalizedChunk), "debug normalize emitted singular fields");
assert(normalizedChunk.moves.length === 2 && normalizedChunk.anims.length === 2, "debug normalize duplicated lanes");
const draft = debugCtx.spChunkToDraft(normalizedChunk);
debugCtx.spEditDialog = {{name: normalized.name, title: normalized.title, chunks: [draft]}};
const saved = debugCtx.spParseEditDialog();
const savedChunk = saved.chunks[0];
assert(!("servo" in savedChunk) && !("expr" in savedChunk), "debug save emitted singular fields");
assert(savedChunk.moves.length === 2 && savedChunk.anims.length === 2, "debug edit lost or duplicated lanes");
assert(savedChunk.moves[0].move === "center" && savedChunk.moves[1].move === "look_left", "debug move order changed");
assert(savedChunk.anims[0].anim === "happy" && savedChunk.anims[1].anim === "blink", "debug anim order changed");
process.stdout.write("ok");
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.stdout == "ok"
