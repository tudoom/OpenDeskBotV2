"""Fail the firmware build when the USB-only boot contract drifts.

This script is intentionally a source-level gate.  The Arduino framework used
by this project is prebuilt, so an sdkconfig.defaults file does not reliably
change IDF console or Arduino loop-task settings.
"""

from pathlib import Path
import re


try:
    Import("env")  # type: ignore[name-defined]  # Provided by PlatformIO/SCons.
    PROJECT_DIR = Path(env.subst("$PROJECT_DIR"))  # type: ignore[name-defined]
except NameError:
    # Also allow `python scripts/check_usb_boot_contract.py` in local checks.
    PROJECT_DIR = Path(__file__).resolve().parents[1]


def _without_c_comments(text: str) -> str:
    """Remove C/C++ comments while preserving strings and line positions."""

    output = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""

        if state == "code":
            if current == "/" and following == "/":
                output.extend((" ", " "))
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                output.extend((" ", " "))
                index += 2
                state = "block_comment"
                continue
            if current in ('"', "'"):
                quote = current
                state = "string"
            output.append(current)
            index += 1
            continue

        if state == "line_comment":
            if current == "\n":
                output.append(current)
                state = "code"
            else:
                output.append(" ")
            index += 1
            continue

        if state == "block_comment":
            if current == "*" and following == "/":
                output.extend((" ", " "))
                index += 2
                state = "code"
            else:
                output.append("\n" if current == "\n" else " ")
                index += 1
            continue

        # String/character literal.
        output.append(current)
        if current == "\\" and following:
            output.append(following)
            index += 2
            continue
        if current == quote:
            state = "code"
        index += 1

    return "".join(output)


def _function_body(source: str, name: str) -> str:
    match = re.search(r"\bvoid\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                      source)
    if match is None:
        return ""
    open_brace = source.find("{", match.start())
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1:index]
    return ""


def _validate() -> None:
    platformio_path = PROJECT_DIR / "platformio.ini"
    sketch_path = PROJECT_DIR / "firmware" / "deskbot_rom.ino"
    transport_path = PROJECT_DIR / "firmware" / "usb_transport.cpp"
    logger_path = PROJECT_DIR / "firmware" / "logger.cpp"
    platformio = platformio_path.read_text(encoding="utf-8")
    sketch = _without_c_comments(sketch_path.read_text(encoding="utf-8"))
    transport = _without_c_comments(
        transport_path.read_text(encoding="utf-8"))
    logger = _without_c_comments(logger_path.read_text(encoding="utf-8"))
    errors = []

    for macro in ("ARDUINO_USB_MODE", "ARDUINO_USB_CDC_ON_BOOT"):
        values = re.findall(
            rf"(?m)^\s*-D{macro}\s*=\s*([^\s;]+)", platformio)
        if values != ["1"]:
            errors.append(
                f"platformio.ini must define exactly -D{macro}=1; got {values}"
            )

    forbidden_platformio = {
        "board_build.sdkconfig_defaults":
            "prebuilt Arduino does not apply sdkconfig.defaults here",
        "CONFIG_ESP_CONSOLE_NONE":
            "command-line CONFIG_* cannot reconfigure the prebuilt IDF",
    }
    for token, explanation in forbidden_platformio.items():
        if token in platformio:
            errors.append(f"remove {token}: {explanation}")
    if re.search(r"(?im)^\s*-D(?:ARDUINO_USB_MODE\s*=\s*0|"
                 r"(?:USE_)?TINYUSB(?:\s*=.*)?)\s*$", platformio):
        errors.append(
            "TinyUSB/native-USB mode is forbidden; use ESP32-S3 HWCDC mode 1"
        )

    setup = _function_body(sketch, "setup")
    if not setup:
        errors.append("deskbot_rom.ino must define setup()")
    else:
        cdc_position = setup.find("usb_transport_cdc_begin(")
        session_position = setup.find("usb_transport_begin(")
        peripheral_calls = (
            "setup_display(",
            "setup_FFat(",
            "setup_led(",
            "setup_head(",
            "setup_audio(",
            "task_setup_mic_capture(",
            "setup_camera(",
            "head_servo_boot_attach(",
            "task_setup_audio_play(",
            "task_setup_display(",
            "task_setup_cpu_runtime_stats(",
            "asrChatClient.enableUsbTransport(",
        )
        peripheral_positions = [
            setup.find(call) for call in peripheral_calls if setup.find(call) >= 0
        ]
        if cdc_position < 0:
            errors.append("setup() must call usb_transport_cdc_begin()")
        if session_position < 0:
            errors.append("setup() must call usb_transport_begin()")
        if (cdc_position >= 0 and peripheral_positions and
                cdc_position > min(peripheral_positions)):
            errors.append(
                "physical CDC must start before every peripheral initializer"
            )
        if (session_position >= 0 and peripheral_positions and
                session_position < max(peripheral_positions)):
            errors.append(
                "DBOT parser/session must start only after peripherals are ready"
            )

    combined = sketch + "\n" + transport + "\n" + logger
    if re.search(r"\bwhile\s*\(\s*!\s*(?:Serial|HWCDCSerial)\s*\)", combined):
        errors.append("blocking while (!Serial/HWCDCSerial) is forbidden")
    if "SET_LOOP_TASK_STACK_SIZE(24576)" not in sketch:
        errors.append(
            "deskbot_rom.ino must set the Arduino loop-task stack to 24576"
        )
    if "HWCDCSerial" not in transport:
        errors.append(
            "usb_transport.cpp must use explicit HWCDCSerial, not Serial alias"
        )
    if re.search(r"\bSerial\s*\.", combined):
        errors.append(
            "firmware transport/logger/startup must not use the Serial alias"
        )

    if errors:
        detail = "\n  - ".join(errors)
        raise RuntimeError(f"USB boot contract failed:\n  - {detail}")
    print("[usb-boot-contract] OK: ESP32-S3 HWCDC boot/session ordering locked")


_validate()
