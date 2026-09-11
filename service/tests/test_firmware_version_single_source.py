from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_hardware_version_file_is_the_only_source():
    """固件版本只在 hardware/VERSION 维护：manifest 与 common.h 都从它派生。"""
    version = (ROOT / "hardware" / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    manifest = json.loads(
        (ROOT / "service/src/deskbot_server/firmware_payload/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == version
    common = (ROOT / "hardware/firmware/common.h").read_text(encoding="utf-8")
    assert '#define VERSION "' not in common.replace('#define VERSION "0.0.0-dev"', "")
    assert "version_gen.h" in common
    ini = (ROOT / "hardware/platformio.ini").read_text(encoding="utf-8")
    assert "pre:scripts/gen_version.py" in ini


def test_bundled_firmware_matches_the_latest_build_when_present():
    """随包固件必须是当前源码构建出来的那一份。

    2026-09-03：固件改了、pio run 了，却忘了 sync_firmware_payload.py，
    结果打出「都叫 0.0.27、内容不同」的包。构建产物不存在（CI 未编固件）时跳过。
    """
    import hashlib

    built = ROOT / "hardware/.pio/build/deskbot_v2/firmware.bin"
    payload = ROOT / "service/src/deskbot_server/firmware_payload/deskbot_v2.bin"
    if not built.is_file():
        import pytest

        pytest.skip("本机没有固件构建产物")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert digest(built) == digest(payload), (
        "随包固件与最近一次构建产物不一致——改完固件请运行 "
        "python service/scripts/sync_firmware_payload.py"
    )


def test_bundled_binary_actually_carries_the_manifest_version():
    """二进制里编进去的版本必须等于 manifest 版本。

    2026-09-03：在错误目录跑 pio run（platformio.ini 在 hardware/），固件根本没重编，
    同步脚本却照样把旧二进制标成新版本——哈希一致也查不出来，只有读二进制里的
    字符串才能发现。VERSION 是编译期常量，一定出现在 .bin 里。
    """
    payload = ROOT / "service/src/deskbot_server/firmware_payload/deskbot_v2.bin"
    version = (ROOT / "hardware" / "VERSION").read_text(encoding="utf-8").strip()
    blob = payload.read_bytes()
    assert version.encode() in blob, (
        f"随包固件里找不到版本字符串 {version}——固件可能没有用当前 VERSION 重新编译"
    )
