"""Static contracts for the ESP32 expression compositor.

The firmware is compiled separately by PlatformIO.  These checks keep the
wire-shape and frame-presence semantics reviewable in the normal pytest suite.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPLAY = (ROOT / "hardware" / "firmware" / "display.cpp").read_text(encoding="utf-8")
ROM = (ROOT / "hardware" / "firmware" / "deskbot_rom.ino").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    start_at = DISPLAY.index(start)
    return DISPLAY[start_at : DISPLAY.index(end, start_at)]


def test_all_persisted_expression_shapes_have_real_draw_and_lerp_paths():
    draw = _between("static void draw_prim(", "static void draw_prim_lerp(")
    lerp = _between("static void draw_prim_lerp(", "static void draw_layer_lerp(")

    for shape in (
        "Pixel",
        "HLine",
        "VLine",
        "Triangle",
        "TriangleFill",
        "RotatedRectOutline",
        "RotatedRectFill",
    ):
        assert f"case PbShape::{shape}" in draw

    assert "pb_draw_thick_line" in draw
    assert "pb_draw_rotated_ellipse" in draw
    assert "pb_fill_rotated_round_rect" in draw
    assert "pb_draw_rotated_round_rect_outline" in draw
    assert "tween.stroke_width" in lerp
    assert "tween.angle_deg" in lerp
    assert "draw_prim(tween)" in lerp


def test_anim_layer_presence_builds_and_commits_a_complete_composite_frame():
    compose = _between(
        "static bool stored_from_elements_v(",
        "static int pb_jpeg_draw_cb(",
    )
    render = _between(
        "static bool pb_render_anim_array_timed(",
        "static bool pb_render_vector_json(",
    )

    # Seed from the previous full frame. Missing keys therefore inherit.
    for layer in ("bg", "nose", "mouth", "eye_l", "eye_r", "extra"):
        assert f"memcpy({layer}, &s_prev_{layer}" in compose

    # An explicit [] is still JsonArrayConst and calls json_fill_layer(),
    # whose first operation clears the selected layer.
    assert 'elements["mouth"].is<JsonArrayConst>()' in compose
    assert 'json_fill_layer(elements["mouth"].as<JsonArrayConst>(), mouth)' in compose
    assert "pb_commit_prev(cbg, cn, cm, cel, cer, cex)" in render


def test_resolved_empty_frame_fails_closed_and_runtime_never_injects_idle():
    render = _between(
        "static bool pb_render_anim_array_timed(",
        "static bool pb_render_vector_json(",
    )
    task = _between("void display_render_task(", "bool ensure_render_task(")

    assert "composite_has_renderable" in render
    assert "if (!composite_has_renderable)" in render
    assert "had_inheritable_state" not in render
    assert "return false;" in render

    assert "!cancelled && (!render_ok || !pb_prev_has_renderable())" in task
    assert "retained previous panel frame" in task
    runtime_failure = task[
        task.index("!cancelled && (!render_ok || !pb_prev_has_renderable())") :
        task.index("display_free_render_assets();")
    ]
    assert "pb_show_builtin_idle" not in runtime_failure
    assert "PC owns every normal" in task
    assert "DISPLAY_SCENE_RESET" not in DISPLAY
    assert "display_render_reset" not in DISPLAY


def test_image_layers_require_current_assets_and_never_cross_request_boundaries():
    parse = _between("static bool json_fill_layer(", "static bool stored_layers_have_renderable(")
    asset_check = _between(
        "static bool pb_image_asset_available(",
        "static void layer_drop_images(",
    )
    task = _between("void display_render_task(", "bool ensure_render_task(")

    assert "pb_image_asset_available(p.asset_index)" in parse
    assert "asset_index >= s_render_asset_count" in asset_check
    assert "blob.data[0] == 0xFFu" in asset_check
    assert "s_draw_gfx != s_canvas" in asset_check
    assert "pb_drop_inherited_images();" in task
    assert "Assets are request-owned" in task


def test_mouth_overlays_preserve_the_full_face_background_color():
    voice = _between(
        "static void pb_commit_voice_base()",
        "static void pb_show_builtin_idle(",
    )
    mouth_only = _between(
        "static bool pb_render_anim_array_timed(",
        "static bool pb_render_vector_json(",
    )

    assert "s_voice_base_frame_bg = s_prev_frame_bg" in voice
    assert "fillScreen(s_voice_base_frame_bg)" in voice
    assert "mouth_only_baseline_bg = s_prev_frame_bg" in mouth_only
    assert "fillScreen(mouth_only_baseline_bg)" in mouth_only
    assert "s_prev_frame_bg = mouth_only_baseline_bg" in mouth_only


def test_voice_base_snapshot_uses_psram_instead_of_static_internal_dram():
    voice = _between(
        "static void pb_commit_voice_base()",
        "static void pb_show_builtin_idle(",
    )

    assert "static StoredLayer s_voice_base_" not in DISPLAY
    assert "VoiceBaseStorage* s_voice_base" in DISPLAY
    assert "heap_caps_calloc(" in voice
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in voice
    assert "mouth overlay disabled" in voice


def test_initialization_shows_standby_screen_and_builtin_only_on_failure():
    """未连接 PC 时显示待机屏（内建默认脸 + 「请连接PC服务」）。

    产品需求显式推翻了旧「正常启动不绘制内建脸」约定：待机屏由 display worker
    的空闲 tick 绘制（单一所有者），hello 成功后清除并交还 PC 端表情系统。
    """
    setup = _between("void task_setup_display()", "static void display_fill_request_assets")
    face = _between(
        "static void pb_build_builtin_face_layers(",
        "static void pb_show_builtin_idle(",
    )
    idle = _between(
        "static void pb_show_builtin_idle(",
        "static constexpr char kStandbyHintText",
    )

    assert "if (!ensure_render_task())" in setup
    assert 'pb_show_builtin_idle("render worker unavailable")' in setup
    assert "display_render_reset();" not in setup
    assert "display_standby_set(true);" in setup
    assert "PbShape::EllipseFill" in face
    assert "PbShape::RoundRectOutline" in face
    assert "pb_commit_prev(" in idle

    steady_state = ROM[
        ROM.index("task_setup_display();") : ROM.index("void loop()")
    ]
    assert 'display_boot_show("Deskbot USB ready"' not in steady_state
    assert "display_render_reset();" not in steady_state


def test_standby_screen_is_worker_owned_and_invisible_to_pb_machinery():
    standby = _between(
        "static void display_draw_standby_screen(",
        "static bool pb_play_layers_interpolated(",
    )
    task = _between("void display_render_task(", "bool ensure_render_task(")
    asr = (ROOT / "hardware" / "firmware" / "asr_chat_client.cpp").read_text(
        encoding="utf-8"
    )

    # 待机文案：display_text 内置文泉驿 12px GB2312 点阵已覆盖这些汉字。
    assert '"请连接PC服务"' in DISPLAY
    assert "display_text_draw(s_draw_gfx" in standby
    # 只描画像素：不进入表情基线，也不产生 pb_ack/终态事件。
    assert "pb_commit_prev(" not in standby
    assert "display_emit_pb_terminal" not in standby
    # 绘制只发生在 render worker 的空闲 tick（单一所有者）。
    assert "s_standby_active.load" in task
    assert "s_standby_dirty.exchange(false" in task
    assert "display_draw_standby_screen();" in task

    # hello 成功清除待机屏；会话断开/结束回到待机屏。
    link_up = asr[
        asr.index("void AsrChatClient::onUsbLinkState") :
        asr.index("void AsrChatClient::resetUsbPbJsonAssembly")
    ]
    assert "display_standby_set(false);" in link_up
    abort = asr[
        asr.index("void AsrChatClient::abortRuntimeState") :
        asr.index("void AsrChatClient::resetTransportSession")
    ]
    assert "display_standby_set(true);" in abort


def test_display_runtime_prefers_psram_and_fails_closed_when_unavailable():
    allocation = _between(
        "static QueueHandle_t display_create_queue_prefer_psram(",
        "static void display_emit_pb_terminal(",
    )
    ensure = _between("bool ensure_render_task()", "}  // namespace")
    submit = _between(
        "bool display_pb_submit_vector_json_owned(",
        "bool display_take_pb_terminal_event(",
    )

    assert "xQueueCreateWithCaps(" in allocation
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in allocation
    assert "return xQueueCreate(depth, item_size);" in allocation
    assert "free_int=%u free_psram=%u" in allocation

    assert "xTaskCreatePinnedToCoreWithCaps(" in ensure
    assert "xTaskCreatePinnedToCore(" in ensure
    assert "rc != pdPASS || created_task == nullptr" in ensure
    assert 'display_log_runtime_allocation_failure("queues")' in ensure
    assert 'display_log_runtime_allocation_failure("task")' in ensure
    assert ensure.index("return false;") < ensure.rindex("return true;")

    # The owning submitter must never touch a null queue.  It releases every
    # transferred allocation and lets the PB transaction fail immediately.
    runtime_guard = submit.index("if (!ensure_render_task())")
    queue_send = submit.index("xQueueSend(s_queue, &req, 0)")
    assert runtime_guard < queue_send
    unavailable = submit[runtime_guard:queue_send]
    assert "::free(req.json_payload)" in unavailable
    assert "display_free_request_assets(req)" in unavailable
    assert "return false;" in unavailable


def test_display_governor_limits_spi_work_during_full_duplex_audio():
    panel = (ROOT / "hardware" / "firmware" / "display_panel.h").read_text(
        encoding="utf-8"
    )
    interpolation = _between(
        "static bool pb_play_layers_interpolated(",
        "static bool pb_render_anim_array_timed(",
    )

    assert "DESKBOT_DISPLAY_SPI_HZ 40000000UL" in panel
    assert "kDisplayVoiceFramePeriodMs = 100" in DISPLAY
    assert "kDisplayIdleFramePeriodMs = 67" in DISPLAY
    assert "audio_play_stream_pcm_active()" in DISPLAY
    assert "vTaskDelayUntil(&frame_anchor_tick, period_ticks)" in interpolation
    assert "current[index] == s_last_panel_frame[index]" in DISPLAY
    assert "writeRGB565WindowStrided" in DISPLAY


def test_full_face_replace_resets_old_layers_and_rejects_over_limit_layers():
    replace = _between(
        "void display_render_replace(bool preserve_baseline)",
        "void display_voice_mouth_set_level(",
    )
    parse = _between(
        "static bool json_fill_layer(",
        "static bool stored_layers_have_renderable(",
    )
    task = _between("void display_render_task(", "bool ensure_render_task(")

    assert "s_render_cancel_epoch.fetch_add" in replace
    assert "s_reset_interp_on_next_pb.store(!preserve_baseline" in replace
    assert "pb_show_builtin_idle" not in replace
    assert "DISPLAY_SCENE_RESET" not in replace
    assert "pb_vector_interp_reset();" in task
    assert "s_reset_interp_on_next_pb.exchange(false" in task
    assert "arr.size() > kPbMaxPrimsPerLayer" in parse
    assert "return false;" in parse

    asr = (ROOT / "hardware" / "firmware" / "asr_chat_client.cpp").read_text(
        encoding="utf-8"
    )
    start = asr.index("if (qd == PbQueueDecision::kClear || force_restart_same_req)")
    new_sequence = asr[start : asr.index("pb_inflight_seq_count_++;", start)]
    assert "/*preserve_display_baseline=*/" in new_sequence
    assert "preflight.mouth_only" in new_sequence


def test_mouth_only_phonemes_restore_the_selected_face_before_terminal_ack():
    render = _between(
        "static bool pb_render_anim_array_timed(",
        "static bool pb_render_vector_json(",
    )
    task = _between("void display_render_task(", "bool ensure_render_task(")

    assert "StoredLayer mouth_only_baseline" in render
    assert "memcpy(&mouth_only_baseline, &s_prev_mouth" in render
    assert "restore_mouth_only_baseline" in render
    assert "memcpy(&s_prev_mouth, &mouth_only_baseline" in render
    assert render.rindex("restore_mouth_only_baseline();") < render.rindex(
        "return seg_idx > 0"
    )
    # The renderer returns only after restoration; the task emits the terminal
    # completion afterwards, so callers cannot observe a stale final phoneme.
    render_call = task.index("pb_render_vector_json(")
    assert render_call < task.index("display_emit_pb_terminal(", render_call)


def test_rtc_local_mouth_overlay_keeps_base_face_and_refreshes_at_low_rate():
    task = _between("void display_render_task(", "bool ensure_render_task(")
    overlay = _between(
        "static void pb_draw_voice_mouth(",
        "static void pb_show_builtin_idle(",
    )
    rtc = (ROOT / "hardware" / "firmware" / "rtc_audio_downlink.cpp").read_text(
        encoding="utf-8"
    )

    assert "xQueueReceive(s_queue, &req, pdMS_TO_TICKS(50))" in task
    assert "s_voice_mouth_level" in task
    assert "now_ms - s_voice_last_refresh_ms >= 120u" in task
    assert "s_voice_base_eye_l" in overlay
    assert "s_voice_base_eye_r" in overlay
    assert "s_voice_base_extra" in overlay
    assert "pcm_mouth_level(" in rtc
    assert "display_voice_mouth_set_level" in rtc
    assert "display_voice_mouth_stop" in rtc


def test_firmware_reports_independent_final_display_crc():
    header = (ROOT / "hardware" / "firmware" / "display.h").read_text(
        encoding="utf-8"
    )
    client = (
        ROOT / "hardware" / "firmware" / "asr_chat_client.cpp"
    ).read_text(encoding="utf-8")

    assert "display_current_semantic_crc32" in DISPLAY
    assert "display_crc32_layer" in DISPLAY
    assert "display_crc_valid" in header
    assert "display_crc32" in header
    assert "display_crc32" in client
