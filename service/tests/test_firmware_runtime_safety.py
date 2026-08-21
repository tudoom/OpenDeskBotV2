"""Static firmware invariants for the USB-only runtime.

These checks run without an ESP32 toolchain. Compilation and hardware-in-loop
remain separate gates, while this file protects the session/media safety
contract in the normal service test job.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def _read(name: str) -> str:
    return (FW / name).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_v2_servo_axes_match_production_mechanics_and_safe_travel():
    config = _read("deskbot_config.h")
    head = _read("head.h")
    assert "DESKBOT_ROM_X_PIN (DESKBOT_BOARD_V2 ? 16 : 8)" in config
    assert "DESKBOT_ROM_Y_PIN (DESKBOT_BOARD_V2 ? 15 : 4)" in config
    assert "#define X_MIN_LIMIT 10" in head
    assert "#define X_MAX_LIMIT 170" in head
    assert "#define Y_MIN_LIMIT 70" in head
    assert "#define Y_MAX_LIMIT 110" in head
    assert "head_y_logic_to_pwm(int y_logic) { return 180 - y_logic; }" in head
    assert "const int pwm_deg = head_y_logic_to_pwm(clamped);" in _read("head.cpp")


def test_device_identity_comes_from_factory_efuse():
    common = _read("common.cpp")
    boot = _read("deskbot_rom.ino")
    assert "esp_efuse_mac_get_default(mac)" in common
    assert '"deskbot_%02x%02x%02x%02x%02x%02x"' in common
    assert "(mac[0] & 0x01u) == 0" in common
    assert "device_id_available()" in boot
    assert "hardware device identity unavailable; startup halted" in boot


def test_usb_frames_have_header_and_payload_crc_and_bounded_channels():
    header = _read("usb_transport.h")
    source = _read("usb_transport.cpp")
    assert "DESKBOT_USB_HEADER_SIZE = 24u" in header
    assert "header_crc32" in header
    assert "payload_crc32" in header
    assert "usb_transport_crc32(s_parser.header, 20)" in source
    assert "expected_crc != actual_crc" in source
    assert 'usb_transport_end_session("payload_crc_mismatch")' in source
    assert 'usb_transport_end_session("invalid_frame_header")' in source
    assert (
        'usb_transport_end_session("rx_payload_allocation_failed")'
        in source
    )
    limits = _between(
        source, "size_t channel_payload_limit", "void free_parser_payload"
    )
    for channel in (
        "DESKBOT_USB_CONTROL_JSON",
        "DESKBOT_USB_PB_WIRE",
        "DESKBOT_USB_AUDIO_UP_OPUS",
        "DESKBOT_USB_AUDIO_DOWN_OPUS",
        "DESKBOT_USB_CAMERA_JPEG",
        "DESKBOT_USB_LOG",
    ):
        assert channel in limits
    assert "payload_length > channel_payload_limit(channel)" in source


def test_inline_parser_storage_is_released_before_zero_length_frames():
    source = _read("usb_transport.cpp")
    cleanup = _between(source, "void free_parser_payload", "void reset_parser")
    free_branch_end = cleanup.index("}", cleanup.index("heap_caps_free"))

    # The pointer reset must be outside the heap-only branch.  Otherwise an
    # inline payload leaves a non-null pointer behind and a later legal,
    # zero-length CANCEL_STREAM frame is rejected as malformed.
    pointer_reset = cleanup.index("s_parser.payload = nullptr")
    assert pointer_reset > free_branch_end
    assert cleanup.count("s_parser.payload = nullptr") == 1


def test_session_epoch_heartbeat_and_client_nonce_are_fail_closed():
    source = _read("usb_transport.cpp")
    assert "kHeartbeatTimeoutMs = 15000u" in source
    assert 'usb_transport_end_session("heartbeat_timeout")' in source
    assert "frame.session_epoch != s_session_epoch" in source
    assert '\\"ack_client_nonce\\":%u' in source
    assert "client_nonce != 0 && same_nonce && s_session_epoch != 0" in source
    assert "kHelloReplayWindowMs = 3000u" in source
    assert "s_session_epoch = make_session_epoch()" in source

    # A replayed nonce may only short-circuit the handshake while the session
    # is alive on both layers (transport active, no deferred link-down); a
    # failed hello_ack falls through to a full re-handshake instead of
    # resurrecting a half-dead session the application already left.
    hello = _between(source, "void accept_hello", "bool process_transport_control")
    replay = hello[: hello.index("const bool was_active =")]
    assert "s_active.load(std::memory_order_acquire)" in replay
    assert "!s_link_down_pending.load" in replay
    assert "if (send_hello_ack(rx_sequence, client_nonce))" in replay
    assert "s_active = true" not in replay
    assert "notify_link(true)" not in replay


def test_session_boundary_cancels_stale_writes_and_cleans_cdc_state():
    source = _read("usb_transport.cpp")
    writer = _between(source, "bool write_all", "bool send_with_epoch")
    sender = _between(source, "bool send_with_epoch", "void notify_link")
    hello = _between(source, "void accept_hello", "bool process_transport_control")
    poll = _between(source, "void usb_transport_poll", "bool usb_transport_is_active")
    end_session = source[source.index("void usb_transport_end_session") :]

    assert "s_session_epoch.load(std::memory_order_relaxed) != epoch" in writer
    assert "Revalidate after locking" in sender
    assert sender.count("s_session_epoch.load(std::memory_order_relaxed) != epoch") == 1
    fresh = hello[hello.index("const bool was_active =") :]
    assert fresh.index("s_active.exchange(false") < fresh.index("flush_stale_cdc_tx()")
    assert fresh.index("flush_stale_cdc_tx()") < fresh.index("make_session_epoch()")
    # A deferred link-down raised for the dying epoch is consumed behind the
    # TX mutex boundary, before the new epoch exists, so a stale flag can
    # never kill the freshly negotiated session on the next poll.
    assert fresh.index("flush_stale_cdc_tx()") < fresh.index(
        "s_link_down_pending.exchange(false"
    )
    assert fresh.index("s_link_down_pending.exchange(false") < fresh.index(
        "make_session_epoch()"
    )
    assert "s_parser_reset_pending.exchange(false" in poll
    assert "s_link_down_pending.exchange(false" in poll
    assert "s_cdc_tx_cleanup_pending.load" in poll
    assert "s_active.exchange(false" in end_session
    assert "s_parser_reset_pending.store(true" in end_session
    assert "HWCDCSerial.flush()" not in end_session
    assert "flush_stale_cdc_tx()" not in end_session


def test_end_session_sends_bounded_best_effort_goodbye():
    source = _read("usb_transport.cpp")

    # Link-dead triggers must never queue a goodbye behind a link nobody
    # drains; every other trigger sends one bounded, unretried notice.
    helper = _between(
        source,
        "bool session_end_reason_link_dead",
        "void send_session_end_notice",
    )
    assert '"frame_ack_send_failed"' in helper
    assert '"heartbeat_timeout"' in helper

    notice = _between(
        source,
        "void send_session_end_notice",
        "bool send_hello_ack",
    )
    assert '\\"type\\":\\"session_end\\"' in notice
    assert "kSessionEndNoticeTxWaitMs" in notice

    end_session = source[source.index("void usb_transport_end_session") :]
    assert "session_end_reason_link_dead(reason)" in end_session
    assert "send_session_end_notice(reason)" in end_session
    # The goodbye must run while the epoch is still active, or
    # send_with_epoch(require_active=true) can never write it.
    assert end_session.index("send_session_end_notice(reason)") < (
        end_session.index("s_active.exchange(false")
    )


def test_heartbeat_failures_retry_and_transport_acks_are_consumed():
    source = _read("usb_transport.cpp")
    heartbeat = _between(
        source,
        "void heartbeat_tick()",
        "void parser_idle_tick()",
    )
    control = _between(
        source,
        "bool process_transport_control",
        "void dispatch_complete_frame",
    )

    assert "kHeartbeatRetryMs = 100u" in source
    assert "const bool sent" in heartbeat
    assert "if (sent)" in heartbeat
    success = heartbeat[heartbeat.index("if (sent)") :]
    assert "s_last_heartbeat_tx_ms.store(now" in success
    assert "heartbeat send failed; retrying" in heartbeat
    assert "s_last_heartbeat_attempt_ms.store(now" in heartbeat
    assert 'control_type_is(doc, "heartbeat_ack")' in control
    assert 'control_type_is(doc, "pong")' in control


def test_ack_required_frames_are_confirmed_before_application_dispatch():
    source = _read("usb_transport.cpp")
    dispatch = _between(
        source,
        "void dispatch_complete_frame",
        "void consume_rx_byte",
    )

    assert '\\"type\\":\\"frame_ack\\"' in source
    assert "DESKBOT_USB_FLAG_ACK_REQUIRED" in dispatch
    assert "s_frame_handler(frame, s_callback_context)" in dispatch
    assert dispatch.index("send_frame_ack(frame.sequence, channel_raw)") < (
        dispatch.index("s_frame_handler(frame, s_callback_context)")
    )
    assert 'usb_transport_end_session("frame_ack_send_failed")' in dispatch


def test_usb_dispatch_defers_blocking_commands_to_loop_mailbox():
    boot = _read("deskbot_rom.ino")
    cmd = _read("cmd.cpp")
    cmd_header = _read("cmd.h")

    # Frame dispatch only parses and queues; execution happens in loop() after
    # usb_transport_poll() returns.  A factory command therefore never runs
    # inside dispatch while the parser is parked mid-frame.
    parse = _between(cmd, "void handle_cmd", "void cmd_mailbox_service")
    assert "cmd_mailbox_push(" in parse
    assert "executeFactoryCommand(" not in parse

    service = _between(cmd, "void cmd_mailbox_service", "void cmd_mailbox_clear")
    assert "executeFactoryCommand(" in service

    # Bounded mailbox: overflow drops with a log instead of blocking dispatch;
    # a link-down clears commands queued by the dead session.
    assert "kCmdMailboxDepth = 8" in cmd
    assert "mailbox full" in cmd
    clear = cmd[cmd.index("void cmd_mailbox_clear") :]
    assert "s_cmd_mailbox_count = 0" in clear
    assert "void cmd_mailbox_service();" in cmd_header
    assert "void cmd_mailbox_clear(" in cmd_header

    loop_body = boot[boot.index("void loop()") :]
    assert loop_body.index("usb_transport_poll();") < loop_body.index(
        "cmd_mailbox_service();"
    )
    link = _between(boot, "void on_usb_link", "}  // namespace")
    assert "cmd_mailbox_clear(" in link


def test_factory_gesture_command_layer_is_removed():
    cmd = _read("cmd.cpp")
    cmd_header = _read("cmd.h")
    head = _read("head.cpp")
    head_header = _read("head.h")
    asr = _read("asr_chat_client.cpp")

    # C2 decision: the CONTROL_JSON gesture/motion command layer is deleted.
    # Head motion now has exactly one ingress (PB servo[] batches); the
    # surviving factory surface is read-only queries and maintenance only.
    assert "executeCommand" not in cmd
    assert "executeCommand" not in cmd_header
    assert '"actions"' not in cmd
    assert 'cmd.startsWith("head_move' not in cmd
    assert 'cmd.startsWith("adjust_x")' not in cmd
    assert 'cmd.startsWith("adjust_y")' not in cmd
    assert 'cmd.startsWith("audio_test")' not in cmd
    assert 'cmd.startsWith("asr_chat")' not in cmd
    assert "runVoiceRound" not in cmd
    assert '"head_nod"' not in cmd
    assert '"head_shake"' not in cmd
    assert '"head_clear_pending"' not in cmd
    assert '"delay"' not in cmd
    # Retained factory commands: head_pos (used by service/tools/
    # usb_device_smoke.py), task dump, and reboot.
    assert '"head_pos"' in cmd
    assert "head_log_position();" in cmd
    assert '"task"' in cmd
    assert '"reboot"' in cmd

    # head.cpp keeps only the PB batch ingress plus the async boot-centering
    # submit; every gesture helper and the synchronous wait wrapper are gone.
    for module in (head, head_header):
        assert "head_nod" not in module
        assert "head_shake" not in module
        assert "head_roll" not in module
        assert "head_move" not in module
        assert "adjust_x_center" not in module
        assert "adjust_y_center" not in module
        assert "SERVO_DELAY" not in module
    assert "s_motor_done_sem" not in head
    assert "s_motor_caller_lock" not in head
    assert "notify_sem" not in head
    assert "void submit_motor" in head
    assert "submit_motor(HEAD_SERVO_ABS, X_CENTER, HEAD_SERVO_ABS, Y_CENTER" in head
    # The PB cancel entry point survives for session-level aborts.
    assert "void head_clear_motor_pending()" in head
    assert "void head_clear_motor_pending();" in head_header

    # The voice-round no-op handle_cmd("") pump calls are gone with the layer.
    assert "handle_cmd" not in asr


def test_usb_poll_rejects_reentrancy_before_feeding_watchdog():
    transport = _read("usb_transport.cpp")
    poll = _between(
        transport, "void usb_transport_poll", "bool usb_transport_is_active"
    )
    # Nested polling consumes host bytes while the outer parser is parked
    # mid-frame; reject it loudly.  The guard sits before the supervisor mark
    # so a rejected nested call cannot feed the usb_poll watchdog.
    assert "s_poll_in_progress.exchange(true" in poll
    assert "usb_transport_poll re-entered" in poll
    assert poll.index("s_poll_in_progress.exchange(true") < poll.index(
        "runtime_supervisor_mark_usb_poll()"
    )
    assert "s_poll_in_progress.store(false" in poll


def test_truncated_frame_cannot_swallow_a_later_hello_forever():
    source = _read("usb_transport.cpp")
    timeout = _between(source, "void parser_idle_tick", "}  // namespace")
    assert "kParserIdleTimeoutMs = 2500u" in source
    assert "kParserAbsoluteTimeoutMs = 15000u" in source
    assert "s_parser.state == RxState::kSeekMagic" in timeout
    assert "now - s_parser.last_progress_ms" in timeout
    assert "now - s_parser.started_ms" in timeout
    assert "reset_parser()" in timeout
    assert '"partial_frame_lifetime_timeout"' in timeout
    assert '"partial_frame_idle_timeout"' in timeout


def test_logs_never_pollute_framed_mode():
    logger = _read("logger.cpp")
    transport = _read("usb_transport.cpp")
    assert "if (usb_transport_framed_mode())" in logger
    assert "usb_transport_send_log(framed, n)" in logger
    assert "Serial.write" not in logger[
        logger.index("if (usb_transport_framed_mode())") :
        logger.index("Serial.write")
    ]
    send_log = transport[transport.index("bool usb_transport_send_log") :]
    assert "!s_framed_mode || !s_active" in send_log
    assert "DESKBOT_USB_LOG" in send_log


def test_pb_audio_camera_and_control_are_channel_isolated():
    asr = _read("asr_chat_client.cpp")
    dispatch = _between(
        asr,
        "void AsrChatClient::dispatchUsbFrame",
        "void AsrChatClient::pbFreePendingAssets",
    )
    assert "DESKBOT_USB_CONTROL_JSON" in dispatch
    assert "DESKBOT_USB_PB_WIRE" in dispatch
    assert "DESKBOT_USB_AUDIO_DOWN_OPUS" in dispatch
    assert 'strcmp(type, "pb_cancel") != 0' in dispatch
    assert "drop PB message on CONTROL_JSON" in dispatch
    assert "pb_audio_stream_started_ || pbHasPendingAudioTerminalTracks()" in dispatch
    assert "kUsbAudioDownBatchMaxFrames" in dispatch
    assert "DESKBOT_USB_FLAG_END_STREAM" in dispatch
    assert "rtc_audio_downlink_enqueue(" in dispatch
    assert "opus_downlink_decode(" not in dispatch
    rtc_audio = _read("rtc_audio_downlink.cpp")
    assert "xQueueCreate(kQueueDepth" in rtc_audio
    assert "rtc_audio_task" in rtc_audio
    assert "rtc_audio_downlink_ready_to_close" in rtc_audio
    assert "DESKBOT_USB_AUDIO_UP_OPUS" in asr
    assert "DESKBOT_USB_CAMERA_JPEG" in _read("camera.cpp")


def test_media_free_pb_preserves_standalone_rtc_audio():
    asr = _read("asr_chat_client.cpp")
    dispatch = _between(
        asr,
        "void AsrChatClient::dispatchUsbFrame",
        "void AsrChatClient::pbFreePendingAssets",
    )
    drain = _between(
        asr,
        "void AsrChatClient::pbDrainWorkersForNewSequence",
        "void AsrChatClient::pbOnSequenceComplete",
    )
    submit_audio = _between(
        asr,
        "bool AsrChatClient::pbSubmitPendingAudio",
        "void AsrChatClient::pbFreeExpectedBin",
    )
    parse_pb = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )

    assert "bool replace_audio" in drain
    assert drain.count("if (replace_audio)") >= 2
    assert "const bool preserve_standalone_rtc" in parse_pb
    assert "!preserve_standalone_rtc && !voice_servo_only" in parse_pb
    assert "drop standalone audio while PB downlink is active" not in dispatch
    assert "rtc_audio_downlink_cancel()" in submit_audio
    assert "speaker handoff RTC->PB" in submit_audio

    deferred = _between(
        asr,
        "void AsrChatClient::flushDeferredPbJson",
        "void AsrChatClient::pbDiscardDeferredJsonQueue",
    )
    wire = asr[asr.index("void AsrChatClient::onWirePayload") :]
    assert "if (pb_expect_bin_)" in deferred
    assert "overwrite the active request" in deferred
    assert "pb_doc_is_servo_only_gesture(doc)" in wire
    assert "!pbAnyDownlinkBinExpected()" in wire


def test_pb_audio_download_pauses_mic_uplink_before_playback():
    asr = _read("asr_chat_client.cpp")
    suppression = _between(
        asr,
        "bool AsrChatClient::shouldSuppressMicUplink()",
        "void AsrChatClient::engageVoiceUplink()",
    )
    recorder = _between(
        asr,
        "bool AsrChatClient::runVoiceRound(",
        "void AsrChatClient::onWirePayload",
    )
    assert (
        "tts_active_ && !deskbot_uplink_rtc_interruptible()" in suppression
    )
    assert (
        "stable_tts_gate || !deskbot_uplink_capture_allowed()" in suppression
    )
    assert recorder.count("shouldSuppressMicUplink()") >= 4


def test_pb_ack_uses_pb_wire_json_and_retains_retry_state():
    asr = _read("asr_chat_client.cpp")
    flush = _between(
        asr,
        "void AsrChatClient::flushPendingPbAck",
        "void AsrChatClient::pbSignalTtsRoundComplete",
    )
    assert "DESKBOT_USB_PB_WIRE" in flush
    assert "DESKBOT_USB_FLAG_JSON" in flush
    assert "sendJson(msg" not in flush
    assert "if (!usb_transport_send(" in flush
    assert "pb_ack_bypass_throttle_ = true" in flush
    assert "pb_ack_out_pending_ = false" in flush
    mic_hint = _between(
        asr,
        "const PbMicHint mic_hint_early",
        "const uint32_t chunk_ms = preflight.chunk_ms",
    )
    assert "sendJson(" not in mic_hint
    assert "pb_ack_out_phase_ = \"accepted\"" in mic_hint
    assert "pb_ack_out_pending_ = true" in mic_hint
    assert "flushPendingPbAck()" in mic_hint

    # The remaining generic CONTROL_JSON sends are boot/audio controls only.
    for line in asr.splitlines():
        if "sendJson(" in line and not line.lstrip().startswith("bool "):
            assert "ack" not in line.lower()


def test_transport_abort_clears_every_media_worker():
    asr = _read("asr_chat_client.cpp")
    abort = _between(
        asr,
        "void AsrChatClient::abortRuntimeState",
        "void AsrChatClient::resetTransportSession",
    )
    assert "discardPendingUplinkMedia()" in abort
    assert "pbDiscardDeferredJsonQueue()" in abort
    assert "audio_play_emergency_flush()" in abort
    assert "display_render_replace(/*preserve_baseline=*/true)" in abort
    assert "display_render_reset()" not in abort
    # 断开后回到待机屏（内建默认脸 + 「请连接PC服务」）；基线仍保留供 CRC 连续。
    assert "display_standby_set(true)" in abort
    assert "head_clear_motor_pending()" in abort
    assert "pbReset(/*stop_audio=*/false)" in abort
    assert "link_abort_round_ = true" in abort


def test_repeated_critical_write_failures_invalidate_usb_session():
    asr = _read("asr_chat_client.cpp")
    failure = _between(
        asr,
        "void AsrChatClient::noteTransportSendFail",
        "bool AsrChatClient::connect",
    )
    reset = _between(
        asr,
        "void AsrChatClient::resetTransportSession",
        "bool AsrChatClient::transportCanSend",
    )
    assert "kTransportSendFailResetThreshold" in failure
    assert "resetTransportSession(" in failure
    assert "usb_transport_end_session(why)" in reset


def test_camera_is_throttled_not_starved_by_listening_window():
    source = _read("camera.cpp")
    capture_pause = _between(
        source, "bool capture_paused()", "uint32_t upload_interval_ms()"
    )
    interval = _between(
        source,
        "uint32_t upload_interval_ms()",
        "CameraUploadResult try_upload_one_frame(",
    )
    uploader = _between(
        source, "void camera_uplink_task(void*)", "}  // namespace"
    )
    assert "asr_chat_voice_uplink_busy()" not in capture_pause
    assert "asr_chat_voice_uplink_busy()" in interval
    transport = _read("usb_transport.cpp")
    assert "kCameraTxMutexWaitMs = 25u" in transport
    assert "pdMS_TO_TICKS(kCameraTxMutexWaitMs)" in transport
    assert (
        "upload_result != CameraUploadResult::kSent"
        in uploader
    )
    assert "upload retry reason=%s" in uploader
    retry_branch = uploader[
        uploader.index(
            "if (upload_result != CameraUploadResult::kSent)"
        ) :
        uploader.index("last_upload_ms = millis();")
    ]
    assert "vTaskDelay(pdMS_TO_TICKS(25))" in retry_branch
    assert "continue;" in retry_branch


def test_camera_health_uses_valid_frames_and_brackets_servo_attach():
    head = _read("head.cpp")
    boot = _read("deskbot_rom.ino")
    camera = _read("camera.cpp")
    setup = _between(boot, "void setup()", "void loop()")

    before = (
        "camera_boot_probe(CameraBootProbeStage::kBeforeServo)"
    )
    after = "camera_boot_probe(CameraBootProbeStage::kAfterServo)"
    assert setup.index("setup_camera()") < setup.index(before)
    assert setup.index("setup_camera()") < setup.index("setup_display()")
    assert setup.index("setup_camera()") < setup.index("setup_audio()")
    assert setup.index("setup_camera()") < setup.index("audio_frontend_setup()")
    assert setup.index("setup_camera()") < setup.index("task_setup_mic_capture()")
    assert setup.index(before) < setup.index("head_servo_boot_attach()")
    assert setup.index("head_servo_boot_attach()") < setup.index(after)
    assert "if (s_camera_ok && camera_frame_before_servo)" in setup

    assert "ESP32PWM::timerCount" not in head
    assert "ESP32PWM::allocateTimer(" not in head
    assert "head_servo_protect_camera_xclk_once" not in head
    assert "recheck camera XCLK" not in head
    assert "#include <driver/ledc.h>" in head
    assert "ESP32Servo" not in head
    assert "MCPWM" not in head
    assert "kServoLedcMode = LEDC_LOW_SPEED_MODE" in head
    assert "kServoLedcTimer = LEDC_TIMER_3" in head
    assert "kServoLedcXChannel = LEDC_CHANNEL_6" in head
    assert "kServoLedcYChannel = LEDC_CHANNEL_7" in head
    assert "kServoPwmHz = 50" in head
    assert "ledc_set_duty_and_update(kServoLedcMode" not in head
    assert "ledc_set_duty(" in head
    assert "ledc_update_duty(" in head
    assert "ledc_get_duty(" in head
    assert head.index("observed_previous_duty") < head.index(
        "ledc_set_duty(kServoLedcMode"
    )
    assert "prior PWM verify %s failed" in head
    boot_attach = _between(
        head, "void head_servo_boot_attach()", "/**\n *"
    )
    assert "head_servo_write_x(" not in boot_attach
    assert "head_servo_write_y(" not in boot_attach
    assert "HeadPbTerminalState::kFailed" in head
    assert "LEDC_TIMER_0" in camera
    assert "LEDC_CHANNEL_0" in camera
    assert "constexpr ledc_timer_t kCameraXclkTimer = LEDC_TIMER_0;" in camera
    assert "ESP32-S3" in camera
    assert "generates XCLK with LCD_CAM" in camera
    assert "ledc_get_freq(" not in camera
    assert "ledc_get_duty(" not in camera
    assert "camera_xclk_guard" not in camera
    assert "camera_jpeg_valid(fb)" in camera
    assert "sample_camera_sync_activity(&record)" in camera
    assert "edges(v/h/p)" in camera
    assert "report_boot_camera_probes_once()" in camera
    assert "[CAMERA_DIAG] runtime fb_get begin" in camera
    assert "[CAMERA_DIAG] runtime fb_get end" in camera


def test_camera_v2_uses_psram_double_buffer_latest_mode():
    camera = _read("camera.cpp")
    header = _read("camera.h")
    platformio = (
        ROOT / "hardware" / "platformio.ini"
    ).read_text(encoding="utf-8")
    production = platformio.split("[env:deskbot_v2]", 1)[1].split(
        "[env:deskbot_v2_low_cost_afe]", 1
    )[0]
    setup = _between(
        camera,
        "bool setup_camera()",
        "bool camera_boot_probe(CameraBootProbeStage stage)",
    )
    jpeg_check = _between(
        camera,
        "bool camera_jpeg_valid",
        "void sample_camera_sync_activity",
    )

    assert "#define DESKBOT_CAMERA_INIT_FRAME_SIZE FRAMESIZE_QVGA" in camera
    assert (
        "#define DESKBOT_CAMERA_RUNTIME_FRAME_SIZE FRAMESIZE_QVGA"
        in camera
    )
    assert "config.frame_size = kCameraInitFrameSize;" in setup
    assert "sensor->set_framesize(sensor, kCameraRuntimeFrameSize);" in setup
    assert "#define DESKBOT_CAMERA_FB_COUNT 1" in camera
    assert "#define DESKBOT_CAMERA_GRAB_LATEST 0" in camera
    assert "config.fb_count = DESKBOT_CAMERA_FB_COUNT;" in setup
    assert "CAMERA_GRAB_LATEST" in setup
    assert "CAMERA_GRAB_WHEN_EMPTY" in setup
    assert "CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM" in setup
    assert "config.frame_size = FRAMESIZE_UXGA;" not in setup
    assert "config.fb_count = 2;" not in setup
    assert "config.grab_mode = CAMERA_GRAB_LATEST;" not in setup
    assert "psramFound()" in setup
    driver_init = _between(
        camera,
        "esp_err_t initialise_camera_driver(",
        "enum class CameraProbeStatus",
    )
    assert "esp_camera_set_psram_mode(" not in driver_init
    assert setup.index("initialise_camera_driver(&config)") < setup.index(
        "esp_camera_set_psram_mode(true)"
    )
    assert "!esp_camera_get_psram_mode()" in setup
    assert "reinitialising legacy DMA mode" in setup
    assert setup.count("initialise_camera_driver(&config)") >= 2
    assert "!usb_transport_framed_mode()" in driver_init
    assert "set_camera_driver_log_level(ESP_LOG_NONE)" in driver_init
    assert "-DDESKBOT_CAMERA_FB_COUNT=2" in production
    assert "-DDESKBOT_CAMERA_GRAB_LATEST=1" in production
    assert "-DDESKBOT_CAMERA_PSRAM_DMA=1" in production
    assert (
        "-DDESKBOT_CAMERA_UPLINK_INTERVAL_DURING_LISTEN_MS=500"
        in production
    )
    assert "-DDESKBOT_CAMERA_HMIRROR=1" in production
    assert "-DDESKBOT_CAMERA_VFLIP=1" in production
    assert 'sensor, "hmirror", sensor->set_hmirror' in setup
    assert 'sensor, "vflip", sensor->set_vflip' in setup
    assert "#define DESKBOT_CAMERA_DIAGNOSTICS 0" in header
    assert "[env:seeed_xiao_esp32s3_camera_diag]" in platformio
    assert "-DDESKBOT_CAMERA_DIAGNOSTICS=1" in platformio
    assert (
        "[env:seeed_xiao_esp32s3_camera_diag_psram_dma]"
        in platformio
    )
    assert "[env:seeed_xiao_esp32s3_camera_diag_rgb565]" in platformio
    assert "fb->buf[0] == 0xff" in jpeg_check
    assert "fb->buf[fb->len - 1] == 0xd9" in jpeg_check


def test_camera_capture_failures_reinitialise_driver_with_rate_limit():
    camera = _read("camera.cpp")
    uploader = _between(
        camera, "void camera_uplink_task(void*)", "}  // namespace"
    )

    assert "kCameraFailureRecoveryThreshold = 3u" in camera
    assert "kCameraRecoveryMinIntervalMs = 10000u" in camera
    assert "upload_result == CameraUploadResult::kNoFrame" in uploader
    assert "upload_result == CameraUploadResult::kInvalidFrame" in uploader
    assert "esp_camera_deinit()" in uploader
    assert "if (setup_camera())" in uploader
    assert "recovery rate-limited" in uploader
    assert "recovery complete" in uploader


def test_pb_cam_fps_is_lease_controller_only_and_snapshot_survives():
    camera = _read("camera.cpp")
    camera_header = _read("camera.h")
    asr = _read("asr_chat_client.cpp")
    asr_header = _read("asr_chat_client.h")
    boot = _read("deskbot_rom.ino")
    reset = _between(
        camera, "void camera_reset_session_state()", "CameraHealthSnapshot"
    )
    link = _between(boot, "void on_usb_link", "}  // namespace")

    # A1: the LLM-writable cam_fps channel is sealed at the SERVER side
    # (parse_llm_reply no longer carries the field).  The firmware keeps the
    # wire channel for its sole legitimate producer — the PC-side
    # CameraCadenceController (preview leases + bounded capture boosts) —
    # with range validation and a documented single-producer contract.
    assert 'doc["cam_fps"]' in asr
    assert 'pb_json_integer_in_range(doc["cam_fps"], 0, UINT16_MAX' in asr
    assert "camera_set_fps" in camera
    assert "camera_set_fps" in camera_header
    assert "CameraCadenceController" in asr or "CameraCadenceController" in camera

    # camera_once one-shot snapshots take priority over cadence updates.
    pb_camera = _between(asr, "const bool camera_once", 'if (doc["pb_ver"]')
    assert "camera_request_snapshot();" in pb_camera
    assert "camera_set_fps((uint32_t)fps);" in pb_camera
    assert "isVisionUplinkPaused" not in asr
    assert "isVisionUplinkPaused" not in asr_header

    assert "DESKBOT_CAMERA_UPLINK_INTERVAL_MS" in reset
    assert "s_pending_snapshots.store(0" in reset
    assert "if (!ready)" in link
    assert "camera_reset_session_state();" in link


def test_camera_one_shot_refills_between_stale_discard_and_fresh_capture():
    camera = _read("camera.cpp")
    uploader = _between(
        camera, "CameraUploadResult try_upload_one_frame(", "void camera_uplink_task"
    )
    supervisor = _between(
        camera, "void camera_uplink_task(void*)", "}  // namespace"
    )
    warmup = _between(
        camera, "bool discard_camera_warmup_frames()", "bool apply_sensor_int_setting"
    )
    setup = _between(
        camera,
        "bool setup_camera()",
        "bool camera_boot_probe(CameraBootProbeStage stage)",
    )

    stale_discard = 'discard_one_camera_frame("one_shot_stale")'
    assert stale_discard not in uploader
    assert stale_discard in supervisor
    assert "!DESKBOT_CAMERA_GRAB_LATEST" in supervisor
    assert "bool snapshot_warmed = false" in supervisor
    assert "kCameraSnapshotRefillDelayMs" in supervisor
    assert supervisor.index(stale_discard) < supervisor.index(
        "kCameraSnapshotRefillDelayMs"
    )
    assert "try_upload_one_frame(capture_epoch)" in supervisor
    assert "snapshot_warmed = false;" in supervisor
    assert "kCameraWarmupFrames = 2u" in camera
    assert "i < kCameraWarmupFrames" in warmup
    assert 'discard_one_camera_frame("warmup")' in warmup

    null_sensor = setup[setup.index("if (sensor == nullptr)") :]
    null_sensor = null_sensor[: null_sensor.index("if (sensor->id.PID")]
    assert "esp_camera_deinit();" in null_sensor
    assert "framesize_rc != 0" in setup
    assert 'sensor, "quality", sensor->set_quality, 18, true' in setup
    assert "!required_settings_ok || !discard_camera_warmup_frames()" in setup
    assert "esp_camera_deinit();" in setup


def test_camera_one_shot_wakes_non_starving_supervisor_task():
    camera = _read("camera.cpp")
    requester = _between(
        camera, "void camera_request_snapshot()", "void camera_reset_session_state()"
    )
    setup = camera[camera.index("void task_setup_camera()") :]

    assert "constexpr UBaseType_t kCameraTaskPriority = 2u" in camera
    assert "xTaskNotifyGive(s_camera_task)" in requester
    assert "ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(25))" in camera
    assert "kCameraTaskPriority" in setup
    assert "PRO_CPU_NUM" in setup


def test_camera_supervisor_retries_failed_boot_with_bounded_backoff():
    camera = _read("camera.cpp")
    boot = _read("deskbot_rom.ino")
    uploader = _between(
        camera, "void camera_uplink_task(void*)", "}  // namespace"
    )
    setup = _between(boot, "void setup()", "void loop()")

    assert "kCameraInitialRetryBaseMs = 1000u" in camera
    assert "kCameraInitialRetryMaxMs = 30000u" in camera
    assert "kCameraNoMemoryRetryMs = 300000u" in camera
    assert "if (!s_camera_ok.load(std::memory_order_acquire))" in uploader
    assert "if (setup_camera())" in uploader
    assert "kCameraInitialRetryMaxMs" in uploader
    assert "setup_error == ESP_ERR_NO_MEM" in uploader
    assert "NO_MEM rate-limited" in uploader
    assert "next_setup_attempt_ms" in uploader
    assert setup.count("task_setup_camera();") == 1
    assert setup.index("usb_transport_begin") < setup.index("task_setup_camera();")
    assert "if (s_camera_ok) {\n    task_setup_camera();" not in setup


def test_camera_health_is_reported_in_hello_and_both_heartbeats():
    camera = _read("camera.cpp")
    header = _read("camera.h")
    transport = _read("usb_transport.cpp")
    health = _between(
        camera, "CameraHealthSnapshot camera_health_snapshot()", "void task_setup_camera()"
    )

    for field in (
        "ready",
        "last_frame_ms",
        "last_upload_ms",
        "capture_failures",
        "transport_failures",
        "init_failures",
        "recovery_attempts",
        "recovery_successes",
        "pending_snapshots",
        "interval_ms",
    ):
        assert field in header
        assert f"snapshot.{field}" in health

    for field in (
        "camera_ready",
        "camera_last_frame_ms",
        "camera_capture_failures",
        "camera_recovery_count",
        "usb_partial_tx_failures",
    ):
        assert transport.count(f'\\"{field}\\"') >= 3
    assert "char json[1536];" in transport
    assert transport.count("CameraHealthSnapshot camera_health") >= 3


def test_partial_usb_frame_fails_epoch_closed_and_defers_link_callback():
    transport = _read("usb_transport.cpp")
    header = _read("usb_transport.h")
    camera = _read("camera.cpp")
    writer = _between(transport, "bool write_all", "void notify_link(bool ready);")
    sender = _between(transport, "bool send_with_epoch", "void notify_link(bool ready) {")
    poll = _between(
        transport, "void usb_transport_poll", "bool usb_transport_is_active"
    )

    assert "size_t* bytes_written" in writer
    assert "*bytes_written = 0" in writer
    assert "*bytes_written = offset" in writer
    assert "frame_bytes_written += part_written" in sender
    assert "frame_bytes_written > 0" in sender
    assert "s_session_epoch.load(std::memory_order_relaxed) == epoch" in sender
    assert "s_active.exchange(false, std::memory_order_acq_rel)" in sender
    assert "s_parser_reset_pending.store(true" in sender
    assert "s_partial_tx_failures.fetch_add(1" in sender
    # The link callback frees parser-task-owned PB JSON state, so writer tasks
    # (camera, logging) may only flag the failure while still holding the TX
    # mutex; usb_transport_poll delivers notify_link(false) on loopTask.
    assert "s_link_down_pending.store(true" in sender
    assert sender.index("s_link_down_pending.store(true") < sender.rindex(
        "xSemaphoreGive(s_tx_mutex);"
    )
    assert "notify_link(false);" not in sender
    assert "s_link_down_pending.exchange(false" in poll
    assert "notify_link(false);" in poll

    assert "bool usb_transport_send_for_epoch(" in header
    assert "bool usb_transport_send_for_epoch(" in transport
    assert "expected_epoch, true" in transport
    assert "const uint32_t capture_epoch = usb_transport_session_epoch();" in camera
    assert "usb_transport_send_for_epoch(" in camera


def test_opus_decode_stack_uses_psram_to_preserve_camera_dram():
    source = _read("opus_downlink.cpp")
    ensure_task = _between(
        source,
        "static bool ensure_decode_task(void)",
        "static bool dispatch_decode_job",
    )

    assert "xTaskCreatePinnedToCoreWithCaps(" in ensure_task
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in ensure_task
    assert "xTaskCreatePinnedToCore(" not in ensure_task
    assert "downlink stack low-water" in source
    assert "memory=psram" in ensure_task
    assert "nullptr, 4, &s_task" in ensure_task
    assert "APP_CPU_NUM" in ensure_task


def test_realtime_audio_workers_leave_cpu0_for_esp_sr_idle_headroom():
    uplink = _read("opus_uplink.cpp")
    rtc_downlink = _read("rtc_audio_downlink.cpp")
    frontend = _read("audio_frontend_esp_sr.cpp")
    asr = _read("asr_chat_client.cpp")

    assert '"opus_enc", stack_words, nullptr, 4' in uplink
    assert "APP_CPU_NUM" in uplink
    assert '"rtc_opus_dec"' in rtc_downlink
    assert "nullptr, 4, &created" in rtc_downlink
    assert "APP_CPU_NUM" in rtc_downlink
    assert '"esp_sr_feed"' in frontend
    assert "PRO_CPU_NUM" in frontend
    assert "vTaskDelay(1);" in frontend
    link_ready = _between(asr, "void AsrChatClient::onUsbLinkState", "void AsrChatClient::resetUsbPbJsonAssembly")
    assert "opus_downlink_init" not in link_ready


def test_production_diagnostics_do_not_sample_every_task_on_both_ticks():
    trace = _read("task_trace.cpp")
    config = _read("deskbot_config.h")

    setup = trace[trace.index("void task_setup_cpu_runtime_stats()") :]
    assert "esp_register_freertos_idle_hook_for_cpu" in setup
    assert "esp_register_freertos_tick_hook_for_cpu" not in setup
    assert "idle_headroom" in trace
    assert "max_gap_ms" in trace
    assert "#define DESKBOT_AEC_CORRELATION_DIAGNOSTICS 0" in config
    assert "#if DESKBOT_AEC_CORRELATION_DIAGNOSTICS" in _read(
        "audio_frontend_esp_sr.cpp"
    )


def test_muted_voice_round_blocks_instead_of_busy_spinning():
    source = _read("asr_chat_client.cpp")
    recorder = _between(
        source,
        "bool AsrChatClient::runVoiceRound(",
        "void AsrChatClient::onWirePayload",
    )
    muted_branch = recorder[
        recorder.index("if (shouldSuppressMicUplink())") :
        recorder.index("if (!capture_was_allowed_)")
    ]

    assert "vTaskDelay(pdMS_TO_TICKS(5))" in muted_branch
    assert "burns 100% CPU" in muted_branch


def test_mic_hint_is_a_real_capture_gate():
    state = _read("deskbot_uplink_state.cpp")
    display = _read("display.cpp")
    display_header = _read("display.h")
    asr = _read("asr_chat_client.cpp")
    assert "std::atomic<bool> s_mic_enabled{true}" in state
    assert "if (!s_mic_enabled.load" in state
    # The one-line setter forwarder that used to live in the display module is
    # removed; asr_chat drives deskbot_uplink_state directly.
    assert "deskbot_mic_uplink_set_active" not in display
    assert "deskbot_mic_uplink_set_active" not in display_header
    assert "deskbot_mic_uplink_set_active" not in asr
    assert "deskbot_uplink_set_mic_enabled(active)" in asr
    assert "micUplinkSetActive(mic_hint == PbMicHint::kOpen)" in asr


def test_mic_capture_queue_is_small_and_initialization_fails_closed():
    source = _read("audio_capture.cpp")
    setup = _between(
        source, "void task_setup_mic_capture()", "void mic_capture_flush_queue()"
    )
    consumer = source[source.index("bool mic_consumer_read") :]

    assert "kMicCaptureQueueDepth = 16" in source
    assert "xQueueCreate(kMicCaptureQueueDepth, sizeof(MicCaptureFrame))" in setup
    assert "kDepth = 600" not in source
    assert "if (!s_mic_q)" in setup
    assert "if (!s_consumer_mutex)" in setup
    assert "rc != pdPASS || !created_task" in setup
    assert setup.count("mic_release_unstarted_resources()") == 3
    assert "capture disabled" in setup

    assert "if (!s_mic_q || !s_mic_task || !s_consumer_mutex || !s_mic_rx" in consumer
    assert "memset(dst, 0, length * sizeof(int16_t))" in consumer
    assert "vTaskDelay(fallback_ticks > 0 ? fallback_ticks : 1)" in consumer
    assert "if (!mic_capture_signal_healthy())" in consumer
    assert "vTaskDelay(frame_ticks > 0 ? frame_ticks : 1)" in consumer
    assert "i2s_channel_read" not in consumer
    assert "i2s_channel_read(s_mic_rx" in source
    assert "&bytes_read,\n                         250)" in source
    assert "[MIC] I2S RX stalled" in source
    assert "mic_enqueue_drop_oldest(frame)" in source
    assert "audio_frontend_output_healthy()" in consumer


def test_esp_sr_uses_official_afe_and_raw_pcm_failover():
    frontend = _read("audio_frontend_esp_sr.cpp")
    header = _read("audio_frontend_esp_sr.h")

    assert 'afe_config_init("MR", &no_models, AFE_TYPE_VC' in frontend
    assert "kSelectedAecMode = AEC_MODE_VOIP_HIGH_PERF" in frontend
    assert "config->aec_mode = kSelectedAecMode" in frontend
    assert "config->afe_type = AFE_TYPE_VC" in frontend
    assert "config->fixed_first_channel = false" in frontend
    assert "kProcessedFreezeFrames = 64" in frontend
    assert "s_output_healthy.store(false" in frontend
    assert "switching uplink to raw microphone PCM" in frontend
    assert "processed PCM recovered" in frontend
    assert "bool audio_frontend_output_healthy()" in header


def test_esp_sr_vad_is_noise_adaptive_and_playback_stabilized():
    frontend = _read("audio_frontend_esp_sr.cpp")
    asr = _read("asr_chat_client.cpp")

    assert "kVadStartMarginDb" in frontend
    assert "kVadStartConfirmFrames" in frontend
    assert "kVadEndConfirmFrames" in frontend
    assert "s_vad_noise_floor_db" in frontend
    assert "observe_stable_vad(result->vad_state, vad_volume_db)" in frontend
    assert "vad_playback_or_tail_active()" in frontend
    assert "kVadPlaybackTailMs" in frontend
    assert "s_vad_reset_generation.fetch_add" in frontend
    assert "now_sec - sec_stats_start_ms >= 10000" in asr
    assert 'log_info("[ASR_CHAT] round=%u 10s samp_min=' in asr


def test_esp_sr_fetch_is_bounded_and_stall_degrades_without_reboot():
    frontend = _read("audio_frontend_esp_sr.cpp")
    header = _read("audio_frontend_esp_sr.h")
    supervisor = _read("runtime_supervisor.cpp")

    assert "kFetchTimeoutMs = 100" in frontend
    assert "kFetchFailureFallbackThreshold = 3" in frontend
    assert "s_afe_handle->fetch_with_delay(" in frontend
    assert "pdMS_TO_TICKS(kFetchTimeoutMs)" in frontend
    assert "!s_afe_handle->fetch_with_delay" in frontend
    assert 'audio_frontend_force_raw_fallback("fetch_timeout_or_error")' in frontend
    assert "void audio_frontend_force_raw_fallback(" in header

    afe_guard = _between(
        supervisor,
        "if (audio_frontend_ready())",
        "}\n  }\n}",
    )
    assert 'audio_frontend_force_raw_fallback("esp_sr_task_stall")' in afe_guard
    assert 'restart_for_stall("esp_sr_task_stall"' not in afe_guard


def test_usb_hello_surfaces_hardware_reset_diagnostics():
    supervisor = _read("runtime_supervisor.cpp")
    supervisor_header = _read("runtime_supervisor.h")
    transport = _read("usb_transport.cpp")

    assert "s_hardware_reset_reason = esp_reset_reason()" in supervisor
    for reason in (
        "ESP_RST_PANIC",
        "ESP_RST_INT_WDT",
        "ESP_RST_TASK_WDT",
        "ESP_RST_WDT",
        "ESP_RST_BROWNOUT",
        "ESP_RST_PWR_GLITCH",
        "ESP_RST_CPU_LOCKUP",
    ):
        assert reason in supervisor
    for function in (
        "runtime_supervisor_hardware_reset_reason",
        "runtime_supervisor_hardware_reset_code",
        "runtime_supervisor_last_reset_was_panic",
        "runtime_supervisor_last_restart_uptime_ms",
    ):
        assert function in supervisor_header
        assert function in transport
    for field in (
        "hardware_reset_reason",
        "hardware_reset_code",
        "last_panic",
        "last_restart_uptime_ms",
    ):
        assert f'\\"{field}\\"' in transport


def test_esp_sr_full_duplex_uses_voice_communication_high_perf_pipeline():
    frontend = _read("audio_frontend_esp_sr.cpp")
    config = _read("deskbot_config.h")

    assert 'afe_config_init("MR", &no_models, AFE_TYPE_VC' in frontend
    assert "AFE_MODE_HIGH_PERF" in frontend
    assert "kSelectedAecMode = AEC_MODE_VOIP_HIGH_PERF" in frontend
    assert "config->aec_mode = kSelectedAecMode" in frontend
    assert "#define DESKBOT_AFE_LOW_COST 0" in config
    assert "AEC_MODE_VOIP_LOW_COST" in frontend
    assert "config->afe_type = AFE_TYPE_VC" in frontend
    assert "config->aec_init = true" in frontend
    assert "config->ns_init = true" in frontend
    assert "config->vad_init = true" in frontend


def test_rtc_audio_cancel_flag_flushes_all_device_playback_layers():
    header = _read("usb_transport.h")
    asr = _read("asr_chat_client.cpp")
    dispatch = _between(
        asr,
        "void AsrChatClient::dispatchUsbFrame",
        "void AsrChatClient::pbFreePendingAssets",
    )

    assert any(
        declaration in header
        for declaration in (
            "DESKBOT_USB_FLAG_CANCEL_STREAM = 0x40",
            "DESKBOT_USB_FLAG_CANCEL_STREAM = (1u << 6)",
            "DESKBOT_USB_FLAG_CANCEL_STREAM = 1u << 6",
        )
    )
    assert "DESKBOT_USB_FLAG_CANCEL_STREAM" in dispatch
    cancel_branch = dispatch[
        dispatch.index("DESKBOT_USB_FLAG_CANCEL_STREAM") :
    ]
    assert "rtc_audio_downlink_cancel()" in cancel_branch
    assert "audio_play_emergency_flush()" in cancel_branch
    assert cancel_branch.index("rtc_audio_downlink_cancel()") < (
        cancel_branch.index("audio_play_emergency_flush()")
    )
    assert "rtc_audio_downlink_enqueue(" not in cancel_branch.split(
        "DESKBOT_USB_FLAG_END_STREAM",
        1,
    )[0]


def test_cancel_clears_aec_reference_and_rtc_closes_after_audio_tail():
    player = _read("audio_player.cpp")
    emergency = _between(
        player,
        "void audio_play_emergency_flush()",
        "unsigned audio_play_input_queue_depth()",
    )
    assert "audio_frontend_abort_playback_reference()" in emergency
    assert emergency.index("audio_frontend_abort_playback_reference()") < (
        emergency.index("s_audio_cancel_epoch.fetch_add")
    )

    replace = _between(
        player,
        "bool audio_stream_pcm16_replace",
        "void audio_stream_pcm16_stop",
    )
    assert "audio_frontend_abort_playback_reference()" in replace

    stop = _between(
        player,
        "void audio_stream_pcm16_stop()",
        "void audio_play_emergency_flush()",
    )
    assert "s_stream_client_session.exchange(false" in stop

    client = _read("asr_chat_client.cpp")
    loop_lite = _between(
        client,
        "void AsrChatClient::loopLite()",
        "void AsrChatClient::serviceLoop",
    )
    stop_at = loop_lite.index("audio_stream_pcm16_stop()")
    close_at = loop_lite.index("rtc_audio_downlink_mark_closed()")
    assert stop_at < close_at
    assert "} else {" in loop_lite[stop_at:close_at]


def test_rtc_stream_replacement_preserves_new_producer_session():
    player = _read("audio_player.cpp")
    replace = _between(
        player,
        "bool audio_stream_pcm16_replace",
        "void audio_stream_pcm16_stop",
    )
    worker = _between(
        player,
        "void audio_play_task_main",
        "bool ensure_audio_play_task",
    )
    emergency = _between(
        worker,
        "case AudioPlayJob::Kind::kEmergencyFlush",
        "case AudioPlayJob::Kind::kReset",
    )
    assert "flush.preserve_client_session = true" in replace
    assert "if (!job.preserve_client_session)" in emergency
    assert "s_stream_client_session = false" in emergency


def test_boot_connect_is_only_marked_after_successful_send():
    source = _read("asr_chat_client.cpp")
    function = source[source.index("void AsrChatClient::maybeSendBootConnect") :]
    assert 'if (sendJson("{\\"type\\":\\"boot_connect\\"}"' in function
    assert function.index("if (sendJson") < function.index(
        "boot_connect_sent_ = true"
    )


def test_pb_ack_has_distinct_accepted_and_played_phases():
    source = _read("asr_chat_client.cpp")
    assert '\\"phase\\":\\"%s\\"' in source
    assert 'pb_ack_out_phase_ = "accepted"' in source
    assert 'pb_ack_out_phase_ = "played"' in source
    barrier = _between(
        source,
        "void AsrChatClient::pbEvaluateTerminalTrack",
        "void AsrChatClient::pbSealCurrentTerminalTrack",
    )
    assert "!track.sealed" in barrier
    assert "!track.audio_expected || track.audio_completed" in barrier
    assert "track.display_completed == track.display_expected" in barrier
    assert "track.motor_completed == track.motor_expected" in barrier
    assert "PbSequenceOutcome::kPlayed" in barrier


def test_partition_layout_has_one_factory_app_and_no_update_metadata():
    partitions = (
        ROOT / "hardware" / "partitions" / "deskbot_rom_8MB.csv"
    ).read_text(encoding="utf-8")
    assert "app0,     app,  factory, 0x10000, 0x6B0000," in partitions
    assert "coredump, data, coredump,0x6C0000,0x40000," in partitions
    assert "ffat,     data, fat,     0x700000, 0x100000," in partitions
    assert "otadata" not in partitions


def test_microphone_and_speaker_use_one_new_i2s_driver_family():
    capture = _read("audio_capture.cpp")
    audio = _read("audio_player.cpp")
    platformio = (ROOT / "hardware" / "platformio.ini").read_text(
        encoding="utf-8"
    )
    header = _read("audio_player.h")

    assert "#include <driver/i2s_pdm.h>" in capture
    assert "i2s_new_channel(&channel, nullptr, &s_mic_rx)" in capture
    assert "I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG" in capture
    assert "I2S_PDM_SLOT_BOTH" in capture
    assert "I2S_PDM_DSR_8S" in capture
    assert "i2s_channel_init_pdm_rx_mode" in capture
    assert "i2s_channel_read(s_mic_rx" in capture

    assert "#include <driver/i2s_std.h>" in audio
    assert "i2s_new_channel(&channel, &s_speaker_tx, nullptr)" in audio
    assert "I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG" in audio
    assert "i2s_channel_init_std_mode" in audio
    assert "i2s_channel_write(" in audio
    assert "i2s_channel_reconfig_std_clock" in audio

    combined = capture + audio + header
    assert "#include <driver/i2s.h>" not in combined
    assert "i2s_driver_install(" not in combined
    assert "i2s_set_clk(" not in combined
    assert "i2s_read(" not in combined
    assert "i2s_write(" not in combined
    assert "--wrap=i2s_hal_pdm_enable_rx_channel" not in platformio
    assert not (FW / "i2s_hal_compat.cpp").exists()


def test_esp_sr_restores_v2_microphone_level_inside_the_official_afe():
    frontend = _read("audio_frontend_esp_sr.cpp")

    assert "constexpr float kAfeLinearGain = 5.0f;" in frontend
    assert "config->afe_linear_gain = kAfeLinearGain;" in frontend
    assert "linear_gain_x100=%d" in frontend
    # WebRTC AGC 的自适应级只在有语音时收敛（绑定后首次开口约 30s 爬升）；
    # 提高立即生效的 compression gain 缩小冷启动响度差，限幅器仍钳 target level。
    # 12 dB：15 dB 曾把 AEC 残留抬过设备 VAD 阈值造成整句自打断。
    assert "#define DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB 12" in frontend
    assert (
        "config->agc_compression_gain_db = DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB;"
        in frontend
    )
    assert "agc_gain_db=%d" in frontend


def test_production_mic_capture_has_no_startup_pdm_diagnostic():
    capture = _read("audio_capture.cpp")

    assert "run_startup_pdm_diagnostic" not in capture
    assert "PDM_AB" not in capture
    assert "PdmDiagnosticSlot" not in capture
    assert "I2S_PDM_RX_LINE0_SLOT_RIGHT" not in capture


def test_production_mic_capture_keeps_runtime_safety_contract():
    capture = _read("audio_capture.cpp")
    task = _between(
        capture, "void mic_capture_task", "void task_setup_mic_capture"
    )
    setup = _between(
        capture, "void task_setup_mic_capture", "void mic_capture_flush_queue"
    )

    assert "kMicCaptureQueueDepth = 16" in capture
    assert '"mic_cap",\n      8192,' in setup
    assert "mic_release_unstarted_resources();" in setup
    assert "vTaskDelay(1)" in task
    assert "i2s_channel_read(s_mic_rx" in task
    assert "i2s_zero_dma_buffer" not in capture


def test_failed_pdm_signal_uses_bounded_restarts_and_degraded_backoff():
    capture = _read("audio_capture.cpp")
    task = _between(
        capture, "void mic_capture_task", "void task_setup_mic_capture"
    )

    assert "kMicNoSignalQuickRestarts = 2" in capture
    assert "kMicNoSignalRetryBackoffMs = 30000" in capture
    assert "kMicNoSignalLogIntervalMs = 10000" in capture
    assert "no_signal_restart_count < kMicNoSignalQuickRestarts" in task
    assert "no_live_pdm_slot_backoff" in task
    assert "next_no_signal_retry_ms = 0" in task
    assert "s_raw_signal_healthy.store(true" in task


def test_degraded_mic_wait_is_audio_paced_and_log_rate_limited():
    capture = _read("audio_capture.cpp")
    asr = _read("asr_chat_client.cpp")
    consumer = _between(
        capture, "bool mic_consumer_read", "uint32_t mic_capture_last_attempt_ms"
    )
    round_loop = _between(
        asr,
        "bool AsrChatClient::runVoiceRound",
        "void AsrChatClient::onWirePayload",
    )

    assert "if (!mic_capture_signal_healthy())" in consumer
    assert "length * 1000U + SAMPLE_RATE - 1U" in consumer
    assert "vTaskDelay(frame_ticks > 0 ? frame_ticks : 1)" in consumer
    assert "last_capture_timeout_log_ms" in round_loop
    assert ">=\n              10000U" in round_loop


def test_dead_client_pump_surfaces_are_removed():
    asr = _read("asr_chat_client.cpp")
    asr_header = _read("asr_chat_client.h")
    boot = _read("deskbot_rom.ino")

    # Zero-caller surfaces removed: the free-function cooperative pump, its
    # AsrChatClient::cooperativePump body, the AsrChatClient::loop() alias,
    # and serviceLoop's vestigial allow_camera parameter (now hard-coded off).
    assert "asr_chat_cooperative_pump" not in asr
    assert "asr_chat_cooperative_pump" not in asr_header
    assert "cooperativePump" not in asr
    assert "cooperativePump" not in asr_header
    assert "void AsrChatClient::loop()" not in asr
    assert "allow_camera" not in asr
    assert "allow_camera" not in asr_header
    assert "void AsrChatClient::serviceLoop()" in asr
    assert "void serviceLoop();" in asr_header
    assert "asrChatClient.serviceLoop();" in boot
