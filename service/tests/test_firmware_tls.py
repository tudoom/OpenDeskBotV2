"""The production firmware is deliberately transport-local.

The historical filename remains so downstream CI selections keep working.
These assertions now protect the stronger property: network and credential
stacks are absent from the firmware image rather than merely configured well.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def _production_text() -> str:
    paths = [*FW.glob("*.cpp"), *FW.glob("*.h")]
    paths.append(ROOT / "hardware" / "platformio.ini")
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_network_and_remote_transport_sources_are_physically_absent():
    removed = (
        "wifi_provision.cpp",
        "wifi_provision.h",
        "ws_uplink.cpp",
        "ws_uplink.h",
        "device_credential.cpp",
        "device_credential.h",
        "deskbot_tls.cpp",
        "deskbot_tls.h",
        "pairing_code.cpp",
        "pairing_code.h",
        "deskbot_secrets.h",
        "deskbot_secrets.example.h",
    )
    for name in removed:
        assert not (FW / name).exists(), name


def test_production_firmware_has_no_network_headers_or_client_symbols():
    text = _production_text()
    forbidden = (
        "#include <WiFi",
        "#include <WebServer",
        "#include <HTTPClient",
        "#include <WebSockets",
        "#include <WiFiClientSecure",
        "beginSslWithCA",
        "setInsecure",
        "configTime(",
        "DESKBOT_WS_",
        "ASR_CHAT_HOST",
        "X-Deskbot-Device-Token",
        "X-Deskbot-Pairing-Code",
    )
    for marker in forbidden:
        assert marker not in text, marker


def test_platformio_has_no_network_library_or_patch_hook():
    ini = (ROOT / "hardware" / "platformio.ini").read_text(encoding="utf-8")
    assert "WebSockets" not in ini
    assert "pre:scripts/check_usb_boot_contract.py" in ini
    assert "patch_wifi" not in ini
    assert "patch_websockets" not in ini
    assert "-DARDUINO_USB_MODE=1" in ini
    assert "-DARDUINO_USB_CDC_ON_BOOT=1" in ini


def test_single_image_contains_no_deployment_specific_configuration():
    config = (FW / "deskbot_config.h").read_text(encoding="utf-8")
    assert "factory eFuse MAC" in config
    assert "no deployment-specific or per-device value" in config
    assert "SSID" not in config
    assert "PASSWORD" not in config
    assert "API_KEY" not in config
    assert "HOST" not in config
    assert "CERT" not in config


def test_usb_is_the_only_runtime_transport():
    boot = (FW / "deskbot_rom.ino").read_text(encoding="utf-8")
    transport = (FW / "usb_transport.cpp").read_text(encoding="utf-8")
    camera = (FW / "camera.cpp").read_text(encoding="utf-8")
    asr = (FW / "asr_chat_client.cpp").read_text(encoding="utf-8")
    assert "usb_transport_begin(" in boot
    assert "usb_transport_poll()" in boot
    assert "usb_transport_send(" in transport
    assert "DESKBOT_USB_CAMERA_JPEG" in camera
    assert "DESKBOT_USB_AUDIO_UP_OPUS" in asr
    assert "DESKBOT_USB_AUDIO_DOWN_OPUS" in asr
