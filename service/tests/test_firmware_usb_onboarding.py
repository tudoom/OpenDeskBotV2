"""Contracts for the USB-only firmware startup and session boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def test_usb_onboarding_requires_no_per_device_firmware_edit():
    config = (FW / "deskbot_config.h").read_text(encoding="utf-8")
    common = (FW / "common.cpp").read_text(encoding="utf-8")
    readme = (ROOT / "hardware" / "README_zh.md").read_text(encoding="utf-8")
    assert "Every unit receives the same firmware image" in config
    assert "esp_efuse_mac_get_default(mac)" in common
    assert "所有机器人烧录完全相同的固件镜像" in readme


def test_device_hands_display_to_worker_without_drawing_from_link_callback():
    boot = (FW / "deskbot_rom.ino").read_text(encoding="utf-8")
    (FW / "display.cpp").read_text(encoding="utf-8")
    assert 'display_boot_show("Deskbot USB ready", "Waiting for PC service")' not in boot
    assert "task_setup_display();" in boot
    assert "display_render_reset();" not in boot
    assert "if (ready)" in boot
    callback = boot[boot.index("void on_usb_link") : boot.index("}  // namespace")]
    assert "display_boot_" not in callback
    assert "runtime_supervisor_start()" in boot


def test_hello_ack_exposes_identity_and_capabilities():
    transport = (FW / "usb_transport.cpp").read_text(encoding="utf-8")
    assert '\\"type\\":\\"hello_ack\\"' in transport
    assert '\\"device_id\\":\\"%s\\"' in transport
    assert '\\"ack_client_nonce\\":%u' in transport
    assert '\\"mic_signal_healthy\\":%s' in transport
    assert "mic_capture_signal_healthy()" in transport
    for field in (
        "servo_ready",
        "servo_backend",
        "servo_x_pin",
        "servo_y_pin",
        "servo_pwm_hz",
        "servo_x_pulse_us",
        "servo_y_pulse_us",
        "servo_write_failures",
    ):
        assert field in transport
    assert "head_servo_health_snapshot()" in transport
    for capability in (
        "control_json",
        "pb_wire",
        "audio_up_opus",
        "audio_down_opus",
        "camera_jpeg",
        "log",
        "audio_aec",
        "audio_ns",
        "audio_vad_esp_sr",
    ):
        assert f'\\"{capability}\\"' in transport
    assert "audio_frontend_ready()" in transport


def test_official_esp_sr_afe_owns_uplink_enhancement_and_speaker_reference():
    frontend = (FW / "audio_frontend_esp_sr.cpp").read_text(encoding="utf-8")
    capture = (FW / "audio_capture.cpp").read_text(encoding="utf-8")
    player = (FW / "audio_player.cpp").read_text(encoding="utf-8")

    assert 'afe_config_init("MR"' in frontend
    assert "config->aec_init = true" in frontend
    assert "config->ns_init = true" in frontend
    assert "config->vad_init = true" in frontend
    assert "AFE_NS_MODE_WEBRTC" in frontend
    assert "audio_frontend_submit_mic" in capture
    assert "audio_frontend_submit_playback_reference" in player


def test_disconnect_cancels_outputs_without_direct_tft_access():
    boot = (FW / "deskbot_rom.ino").read_text(encoding="utf-8")
    asr = (FW / "asr_chat_client.cpp").read_text(encoding="utf-8")
    callback = boot[boot.index("void on_usb_link") : boot.index("}  // namespace")]
    assert "display_boot_" not in callback
    assert 'abortRuntimeState("USB session ended")' in asr
    assert "audio_play_emergency_flush()" in asr
    abort = asr[
        asr.index("void AsrChatClient::abortRuntimeState") :
        asr.index("void AsrChatClient::resetTransportSession")
    ]
    assert "display_render_replace(/*preserve_baseline=*/true)" in abort
    assert "display_render_reset()" not in abort
    assert "head_clear_motor_pending()" in asr


def test_first_install_documentation_requires_full_erase_of_legacy_state():
    readme = (ROOT / "hardware" / "README_zh.md").read_text(encoding="utf-8")
    assert "第一次安装" in readme
    assert "整片擦除" in readme
    assert "旧 NVS 数据" in readme
