from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def local_client(tmp_path, monkeypatch):
    db_path = tmp_path / "web.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    reset_engine()
    init_engine(db_path)
    init_database()
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    try:
        yield app.test_client(), app
    finally:
        reset_engine()


@pytest.mark.parametrize(
    "path",
    [
        "/home",
        "/voice",
        "/expr",
        "/lab",
        "/advanced",
        "/memories",
        "/reminders",
        "/sessions",
        "/preferences",
        "/people",
        "/devices",
        "/miot",
    ],
)
def test_product_pages_render_without_login_or_connected_robot(local_client, path):
    client, _app = local_client
    response = client.get(path)
    assert response.status_code == 200
    assert b"owner_user_id" not in response.data
    assert b'href="/login"' not in response.data
    assert b'href="/register"' not in response.data


def test_identity_and_legacy_page_routes_are_not_registered(local_client):
    client, app = local_client
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    obsolete = {
        "/login",
        "/logout",
        "/register",
        "/debug/users",
        "/api/debug/reset-account",
        "/my/memories",
        "/my/reminders",
        "/my/sessions",
        "/my/preferences",
        "/my/people",
        "/my/devices",
        "/my/miot",
    }
    assert rules.isdisjoint(obsolete)
    for path in sorted(obsolete):
        assert client.get(path).status_code == 404


def test_navigation_exposes_local_features_not_account_controls(local_client):
    client, _app = local_client
    html = client.get("/home").get_data(as_text=True)
    assert "/voice" in html
    assert "/expr" in html
    assert "/memories" in html
    assert "/reminders" in html
    assert "/my/" not in html
    assert "/login" not in html
    assert "/logout" not in html
    assert "/register" not in html
    assert "reset-account" not in html
    assert "owner_user_id" not in html


def test_url_for_uses_canonical_root_page_routes(local_client):
    _client, app = local_client
    from flask import url_for

    expected = {
        "memories": "/memories",
        "reminders": "/reminders",
        "sessions": "/sessions",
        "preferences": "/preferences",
        "people": "/people",
        "devices": "/devices",
        "miot": "/miot",
    }
    with app.test_request_context():
        assert {
            endpoint: url_for(f"app2c.{endpoint}") for endpoint in expected
        } == expected


def test_expression_editor_keeps_local_library_and_device_preview_separate(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)
    assert "saveExpressionCopy" in html
    assert "updateUserExpression" in html
    assert "copyExpressionScene" not in html
    assert "removeUserExpression" in html
    assert "userExpressionScenes" in html
    assert 'type="color"' in html
    assert "保存到库" in html
    assert "face_parts_editor_2c.js" in html
    assert "sendPreviewToDevice" in html
    assert "/proxy/deskbot/api/device_pb_anim" in html
    assert "/proxy/deskbot/api/expression_runtime" in html
    assert "expression_name:String(scene&&scene.name||'adhoc_web')" in html
    assert "runtimeFingerprint" in html
    assert "runtimeFinalFingerprint" in html
    assert "runtimeHistory" in html
    assert "最近表情切换记录" in html
    assert "runtimeMouthOverlay" in html
    assert "完整脸未切换" in html
    assert "webPreviewParity" in html
    assert "Web 下发校验" in html
    assert "设备解码后的最终完整脸与 Web 下发内容相同" in html
    assert "已被覆盖" in html
    assert "尚未取得固件 played 终态" in html
    assert "设备已确认执行的完整表情" in html
    assert "设备实际显示" not in html
    assert "owner_user_id" not in html
    assert "downloadSvg" not in html
    assert "上传的图片会发送至火山引擎 Ark 第三方服务" in html


def test_debug_pipeline_does_not_advertise_removed_visual_motion():
    page = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "templates"
        / "debug_devices.html"
    ).read_text(encoding="utf-8")

    assert "auto_face_follow" not in page
    assert "人脸跟随" not in page
    assert "idle 自动下发" not in page
    assert "cvServoAuto" not in page
    assert "servoAuto" not in page
    assert "_cvSendServoByInts" not in page
    assert "cvServoStatus" not in page


def test_expression_editor_uses_one_revisioned_transaction_contract(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)

    assert "fetch('/api/face_expression_transaction')" in html
    assert "expected_revision:this.revision" in html
    assert "scenes:{create:" in html
    assert "scenes:{update:" in html
    assert "scenes:{delete:" in html
    assert "map_patch" in html
    assert "phonemes:{upsert:" in html
    assert "response.status===409" in html

    assert "/api/face_expr_scenes" not in html
    assert "/api/emotion_expr_map" not in html
    assert "/api/face_mouth_by_phoneme" not in html
    assert "localStorage" not in html
    assert "deskbot_saved_expression_svgs_v1" not in html
    assert "diyDesign" not in html
    assert "svgToCustomVectorPrimitives" not in html


def test_expression_editor_honors_origin_and_preserves_scene_frames(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)

    assert "scene&&scene.origin" in html
    assert '@click="copyExpressionScene(s)"' not in html
    assert '@click="updateUserExpression(s)"' in html
    assert '@click="removeUserExpression(s)"' in html

    # Editing replaces only the current frame and preserves all other frames.
    # The helper owns the unified part split and retains untouched extra data.
    assert "draft.frames.splice(index,1,frame)" in html
    assert "FACE_PARTS_EDITOR.updateEditorPart" in html
    assert "FACE_PARTS_EDITOR.composeElements" in html
    assert "frame[FACE_PARTS_EDITOR.EDITOR_META_KEY]" in html
    assert "base+'（'+index+'）'" in html


def test_expression_editor_uses_unified_parts_shapes_and_static_first_frame(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)
    helper_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
        / "face_parts_editor_2c.js"
    )
    helper = helper_path.read_text(encoding="utf-8")

    assert "exprTab:'face'" in html
    assert "previewPlaying:false" in html
    assert "图片生成</button>" not in html
    assert "tab==='professional'||tab==='image'" in html
    assert "停止动画" in html
    assert "resetCurrentFrame" in html
    assert ":disabled=\"previewPlaying || !currentEditorPart\"" in html
    assert 'class="slider-card color-card"' in html
    assert "<span>[[ selectedPartLabel ]]</span>" not in html
    assert ".system-expression-row{display:flex" in html
    assert "眼距" not in html
    assert "嘴角" not in html
    assert "face-editor-controls" not in html

    assert "新增一帧" in html
    assert "删除本帧" in html
    assert "addPreviewFrame" in html
    assert "deletePreviewFrame" in html
    assert "draft.frames.splice(index+1,0,frame)" in html
    assert "draft.frames.splice(index,1)" in html
    assert ':disabled="previewPlaying || previewFrameCount<=1"' in html
    assert 'grid-template-areas:"prev next play reset" "add delete brush undo"' in html
    assert "画笔" in html
    assert "撤销绘画" in html
    assert "addBrushStroke" in html
    assert "undoBrushStroke" in html
    assert "onPreviewPointerDown" in html
    assert "data-resize-handle" in html
    assert 'class="blocktitle-row"' in html
    assert "<h3>[[ selectedPartLabel ]]</h3>" not in html

    for label in ("左眼", "右眼", "嘴巴", "鼻子", "左腮红", "右腮红", "眉毛", "眼泪", "汗滴"):
        assert f'label: "{label}"' in helper
    for shape in (
        "circle_fill",
        "ellipse_outline",
        "triangle_fill",
        "square_outline",
        "round_rect_fill",
        "diamond_outline",
        "drop_fill",
        "heart_fill",
        "cross",
    ):
        assert f'value: "{shape}"' in helper


def test_expression_device_preview_has_no_persistence_side_effect(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)
    submit_method = html.split("async submitSceneToDevice(scene){", 1)[1]
    submit_method = submit_method.split(
        "\n    },\n    async setAsDeviceDefaultExpression(){", 1
    )[0]
    preview_method = html.split("async sendPreviewToDevice(){", 1)[1]
    preview_method = preview_method.split("\n    }\n  },", 1)[0]

    assert "/proxy/deskbot/api/device_pb_anim" in submit_method
    assert "method:'POST'" in submit_method
    assert "anim" in submit_method
    assert "submitSceneToDevice(sentScene)" in preview_method
    assert "expectedDisplayCrc32:String(expression&&expression.expected_display_crc32||'')" in preview_method
    assert "deviceDisplayCrc32:String(expression&&expression.device_display_crc32||'')" in preview_method
    assert "operationId:submitted.operationId" in preview_method
    assert "startPreviewPlayback(false)" not in preview_method
    assert "postExpressionTransaction" not in preview_method
    assert "saveCurrentExpression" not in preview_method
    assert "map_patch" not in preview_method


def test_expression_device_status_distinguishes_crc_match_override_and_failure(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)

    assert "expected_display_crc32" in html
    assert "device_display_crc32" in html
    assert "协议一致：设备解码后的最终完整脸与 Web 下发内容相同" in html
    assert "协议不一致：同一操作的预期 CRC 与设备最终显示 CRC 不同" in html
    assert "已被覆盖：" in html
    assert "没有独立显示 CRC" in html
    assert "当前屏幕归属：" in html
    assert "failedStates=new Set" in html
    for status in ("cancelled", "coalesced", "timeout", "disconnected", "device_unavailable"):
        assert status in html
    assert "Array.isArray((runtime||{}).operations)" in html


def test_expression_library_writes_never_change_lifecycle_mappings_implicitly(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)

    create_method = html.split("async createExpression(scene, title, options){", 1)[1]
    create_method = create_method.split("\n    },\n    async saveExpressionCopy(){", 1)[0]
    update_method = html.split("async updateUserExpression(scene){", 1)[1]
    update_method = update_method.split("\n    },\n    async removeUserExpression(scene){", 1)[0]
    professional_method = html.split("professionalTransaction(design){", 1)[1]
    professional_method = professional_method.split(
        "\n    },\n    async saveProfessionalDesign(){", 1
    )[0]

    assert "map_patch" not in create_method
    assert "map_patch" not in update_method
    assert "map_patch" not in professional_method
    assert "emotionMapFromDesign" not in html


def test_expression_device_default_is_persisted_and_sent(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)
    method = html.split("async setAsDeviceDefaultExpression(){", 1)[1]
    method = method.split("\n    },\n    async sendPreviewToDevice(){", 1)[0]

    assert "设为设备默认表情" in html
    assert "postExpressionTransaction(mutation)" in method
    assert "map_patch:Object.assign({},pendingMap,{idle:targetName})" in method
    assert "map_create_refs:{idle:0}" in method
    assert "String(payload.map&&payload.map.idle||'')!==targetName" in method
    assert "postExpressionTransaction({map_patch:{idle:targetName}})" in method
    assert "默认表情映射未保存" in method
    assert "queueDefaultSceneToDevice(deviceScene)" in method
    assert "已设为设备默认表情并在设备播放完成" in method
    assert "默认表情映射已保存，但设备应用失败" in method

    queue_method = html.split("async queueDefaultSceneToDevice(scene){", 1)[1]
    queue_method = queue_method.split(
        "\n    },\n    async setAsDeviceDefaultExpression(){", 1
    )[0]
    assert "/proxy/deskbot/api/device_pb_anim" in queue_method
    assert "newControlOperationId('expr-default')" in queue_method
    assert "window.DeskbotConsole.submitControlOperation" in queue_method
    assert "baseUrl:'/proxy/deskbot'" in queue_method
    assert "fetch('/proxy/deskbot/api/device_pb_anim'" not in queue_method


def test_face_preview_renderer_covers_device_line_and_rotated_rect_contract():
    static_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
    )
    source = (static_dir / "face_preview_2c.js").read_text(encoding="utf-8")
    # 别名表单一来源：生成的 primitive_spec.js（由 pb/primitive_spec.py 生成）。
    spec_source = (static_dir / "generated" / "primitive_spec.js").read_text(
        encoding="utf-8"
    )

    expected_aliases = {
        '"draw_line": "line"',
        '"h_line": "hline"',
        '"drawfasthline": "hline"',
        '"v_line": "vline"',
        '"drawfastvline": "vline"',
        '"draw_rotated_rect": "rotated_rect_outline"',
        '"drawrotatedrect": "rotated_rect_outline"',
        '"fill_rotated_rect": "rotated_rect_fill"',
        '"fillrotatedrect": "rotated_rect_fill"',
    }
    assert all(alias in spec_source for alias in expected_aliases)
    assert "SPEC.SHAPE_ALIASES" in source
    assert 'if (shape === "hline")' in source
    assert 'if (shape === "vline")' in source
    assert 'shape === "rotated_rect_outline"' in source
    assert 'shape === "rotated_rect_fill"' in source
    # Rotated-rectangle x/y are device-contract center coordinates.
    assert 'x="${cx - w / 2}"' in source
    assert 'y="${cy - h / 2}"' in source
    assert 'shape === "svg_path"' not in source
    assert "interpolatedElements" in source
    assert "interpolatePrimitive" in source


def test_face_preview_composites_layers_and_matches_firmware_shape_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the face preview behavior test")

    static_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
    )
    spec_source = (static_dir / "generated" / "primitive_spec.js").read_text(
        encoding="utf-8"
    )
    preview_source = (static_dir / "face_preview_2c.js").read_text(encoding="utf-8")
    script = f"""
global.window = globalThis;
eval({json.dumps(spec_source)});
eval({json.dumps(preview_source)});
const preview = window.DeskbotFacePreview;
const inheritedScene = {{frames: [
  {{elements: {{
    bg: [{{shape: 'rect', marker: 'bg'}}],
    nose: [{{shape: 'pixel', marker: 'nose'}}],
    mouth: [{{shape: 'line', marker: 'mouth'}}],
    eye_l: [{{shape: 'pixel', marker: 'left'}}],
    eye_r: [{{shape: 'pixel', marker: 'right'}}],
    extra: [{{shape: 'pixel', marker: 'extra'}}],
  }}}},
  {{elements: {{mouth: []}}}},
  {{elements: {{bg: [], nose: [], mouth: [{{shape: 'pixel', marker: 'new-mouth'}}]}}}},
]}};
const frame1 = preview.frameElements(inheritedScene, 1);
const frame2 = preview.frameElements(inheritedScene, 2);
const parityScene = {{frames: [{{elements: {{
  bg: [{{shape: 'rect', x: 0, y: 0, w: 1, h: 1, color: '#110000'}}],
  nose: [{{shape: 'ellipse', x: 4, y: 5, w: 6, h: 7, color: '#220000'}}],
  mouth: [{{shape: 'triangle', x: 8, y: 9, x1: 10, y1: 11, x2: 12, y2: 13, color: '#330000'}}],
  eye_l: [{{shape: 'hline', x: 10, y: 11, w: 4, stroke_width: 3, rotation: 45, color: '#440000'}}],
  eye_r: [{{shape: 'vline', x: 20, y: 21, h: 4, color: '#550000'}}],
  extra: [
    {{shape: 'round_rect_outline', x: 1, y: 2, w: 20, h: 10, r: 5, color: '#660000'}},
    {{shape: 'text', x: 7, y: 8, text: 'T', size: 9, color: '#770000'}},
    {{shape: 'draw_line', x1: 30, y1: 31, x2: 32, y2: 33, color: '#880000'}},
  ],
}}}}]}};
process.stdout.write(JSON.stringify({{
  frame1: {{
    bg: frame1.bg[0].marker,
    nose: frame1.nose[0].marker,
    mouthLength: frame1.mouth.length,
    left: frame1.eye_l[0].marker,
  }},
  frame2: {{
    bgLength: frame2.bg.length,
    noseLength: frame2.nose.length,
    mouth: frame2.mouth[0].marker,
    right: frame2.eye_r[0].marker,
  }},
  svg: preview.sceneToSvg(parityScene, 0),
}}));
"""
    completed = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["frame1"] == {
        "bg": "bg",
        "nose": "nose",
        "mouthLength": 0,
        "left": "left",
    }
    assert result["frame2"] == {
        "bgLength": 0,
        "noseLength": 0,
        "mouth": "new-mouth",
        "right": "right",
    }

    svg = result["svg"]
    # The browser intentionally round-trips CSS colors through RGB565 so its
    # preview matches the panel rather than promising unavailable RGB888 shades.
    colors = (
        "#100000",
        "#210000",
        "#310000",
        "#420000",
        "#520000",
        "#630000",
        "#730000",
        "#8c0000",
    )
    assert [svg.index(color) for color in colors] == sorted(
        svg.index(color) for color in colors
    )
    assert 'rx="6" ry="7" fill="none" stroke="#210000" stroke-width="1"' in svg
    assert 'points="8,9 10,11 12,13" fill="none" stroke="#310000" stroke-width="1"' in svg
    assert 'x1="10" y1="11" x2="13" y2="11" stroke="#420000" stroke-width="3"' in svg
    assert 'transform="rotate(45 11.5 11)"' in svg
    assert 'x1="20" y1="21" x2="20" y2="24" stroke="#520000" stroke-width="1"' in svg
    assert 'rx="5" fill="none" stroke="#630000" stroke-width="1"' in svg
    assert 'font-size="24" text-anchor="start" dominant-baseline="text-before-edge"' in svg
    assert 'x1="30" y1="31" x2="32" y2="33" stroke="#8c0000" stroke-width="1"' in svg


def test_expression_editor_rebases_dirty_mapping_and_guards_mutations(local_client):
    client, _app = local_client
    html = client.get("/expr").get_data(as_text=True)

    assert "pendingMapPatch=this.mapDirty?this.changedMapPatch():null" in html
    assert "this.load({keepDraft:true,pendingMapPatch})" in html
    assert "this.mapDirty=Object.keys(this.changedMapPatch()).length>0" in html
    assert "if(this.mutationBusy)throw new Error" in html
    assert "finally{this.mutationBusy=false;}" in html
    assert "if(this.sending)return" in html
    assert ':disabled="mutationBusy"' in html
    assert ':disabled="!mapDirty || mutationBusy"' in html
    assert ':disabled="savingProfessional || mutationBusy || !professionalDesign"' in html


def test_advanced_llm_card_offers_switchable_providers(local_client):
    client, _app = local_client
    html = client.get("/advanced").get_data(as_text=True)

    # 中性标题与描述：不再以 mimo 为默认叙事。
    assert "通用大模型" in html
    assert "选择提供方后自动填入推荐模型与接入点" in html
    assert "不与豆包语音共用凭证" in html
    assert "未配置 Ark Key 时会复用这里的 LLM Key" in html

    # 三个内置预设：默认模型 + Base URL + Key 管理入口。
    assert "deepseek-chat" in html
    assert "https://api.deepseek.com" in html
    assert "https://platform.deepseek.com/api_keys" in html
    assert "doubao-seed-2-0-lite-260428" in html
    assert "https://ark.cn-beijing.volces.com/api/v3" in html
    assert "https://console.volcengine.com/ark" in html
    assert "mimo-v2.5" in html
    assert "https://api.xiaomimimo.com/v1" in html
    assert "https://platform.xiaomimimo.com/#/console/api-keys" in html

    # 提供方切换控件与 base_url 反推逻辑；自定义态可用。
    assert 'v-model="setup.provider"' in html
    assert "applySetupProvider" in html
    assert "inferSetupProvider" in html
    assert '<option value="custom">自定义</option>' in html

    # 旧「默认 mimo」文案彻底消失。
    assert "<h2>Xiaomi MiMo</h2>" not in html
    assert "默认使用 <code>mimo-v2.5</code>" not in html
    assert "粘贴 Xiaomi MiMo API Key" not in html
    assert "Xiaomi MiMo 的 LLM_API_KEY" not in html


def test_local_llm_setup_api_needs_no_identity_or_device(local_client):
    client, _app = local_client
    response = client.get("/api/setup/llm")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert "config" in payload

    # A transport hint cannot select another configuration workspace.
    other = client.get("/api/setup/llm?device_id=other-hardware")
    assert other.status_code == 200
    assert other.get_json()["config"] == payload["config"]


def test_local_tts_config_api_has_no_permission_capability(local_client):
    client, _app = local_client
    response = client.get("/api/doubao_tts/config")
    assert response.status_code == 200
    assert set(response.get_json()) == {"ok", "scope", "config", "t"}


def test_web_sources_contain_no_account_or_owner_dependencies():
    web_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
    )
    forbidden = (
        "owner_user_id",
        "create_user(",
        "bind_device(",
        "list_devices_for_user(",
        "flask_login",
        "login_required",
        "DESKBOT_LEGACY_ACCOUNT_MODE",
    )
    offenders: list[str] = []
    for path in web_root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(web_root).as_posix()}: {token}")
    assert offenders == []


def test_console_responses_carry_script_src_csp(local_client):
    client, _app = local_client
    csp = client.get("/home").headers.get("Content-Security-Policy", "")
    # 外部脚本只允许同源；其余指令与 X-Frame-Options DENY 对齐。
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_web_sources_reference_no_external_cdn_origins():
    web_root = Path(__file__).resolve().parents[1] / "src" / "deskbot_server" / "web"
    forbidden = (
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "cdnjs.cloudflare.com",
    )
    offenders: list[str] = []
    for path in web_root.rglob("*"):
        if not path.is_file() or path.suffix not in {".html", ".js", ".css"}:
            continue
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for origin in forbidden:
            if origin in text:
                offenders.append(f"{path.relative_to(web_root).as_posix()}: {origin}")
    assert offenders == []


def test_local_vendor_assets_exist_and_are_plausible():
    static_vendor = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
        / "vendor"
    )
    three = static_vendor / "three.module.js"
    vue = static_vendor / "vue.global.prod.min.js"
    assert three.is_file() and three.stat().st_size > 500_000
    assert vue.is_file() and vue.stat().st_size > 100_000
    head = three.read_text(encoding="utf-8")[:400]
    assert "Three.js" in head


def test_face_preview_neutralizes_untrusted_scene_colors():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the face preview behavior test")

    static_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "web"
        / "static"
    )
    spec_source = (static_dir / "generated" / "primitive_spec.js").read_text(
        encoding="utf-8"
    )
    preview_source = (static_dir / "face_preview_2c.js").read_text(encoding="utf-8")
    scene = {
        "frames": [
            {
                "elements": {
                    "mouth": [
                        {
                            "shape": "circle",
                            "x": 1,
                            "y": 2,
                            "r": 3,
                            "color": '"/><script>alert(1)</script><circle fill="',
                        },
                        {"shape": "circle", "x": 4, "y": 5, "r": 6, "color": "red"},
                        {
                            "shape": "circle",
                            "x": 7,
                            "y": 8,
                            "r": 9,
                            "color": "rgb(255, 103, 0)",
                        },
                        {
                            "shape": "circle",
                            "x": 1,
                            "y": 1,
                            "r": 1,
                            "color": "url(javascript:alert(1))",
                        },
                    ],
                }
            }
        ]
    }
    script = f"""
global.window = globalThis;
eval({json.dumps(spec_source)});
eval({json.dumps(preview_source)});
const preview = window.DeskbotFacePreview;
process.stdout.write(preview.sceneToSvg({json.dumps(scene)}, 0));
"""
    completed = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    svg = completed.stdout

    assert "<script" not in svg
    assert "alert(1)" not in svg
    assert "javascript:" not in svg
    # 非法颜色回退 mouth 图层缺省色（#ff6700 → RGB565 round-trip）。
    fills = re.findall(r'fill="([^"]*)"', svg)
    assert fills and all(re.fullmatch(r"#[0-9a-f]{6}|none", f) for f in fills)
    assert 'fill="#ff0000"' in svg
