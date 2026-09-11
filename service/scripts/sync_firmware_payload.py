#!/usr/bin/env python3
"""把构建好的固件同步进安装包负载目录，版本号取自 hardware/VERSION。

用法（仓库根目录）: python service/scripts/sync_firmware_payload.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / "hardware" / "VERSION"
BUILT = ROOT / "hardware" / ".pio" / "build" / "deskbot_v2" / "firmware.bin"
PAYLOAD_DIR = ROOT / "service" / "src" / "deskbot_server" / "firmware_payload"
RELEASE_BIN = ROOT / "release" / "firmware" / "firmware.bin"


def main() -> int:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not BUILT.is_file():
        print(f"未找到构建产物: {BUILT}（先 pio run -e deskbot_v2）", file=sys.stderr)
        return 1
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUILT, PAYLOAD_DIR / "deskbot_v2.bin")
    manifest = PAYLOAD_DIR / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    data.update(version=version, file="deskbot_v2.bin")
    data.setdefault("chip", "esp32s3")
    data.setdefault("flash_offset", "0x10000")
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if RELEASE_BIN.parent.is_dir():
        shutil.copyfile(BUILT, RELEASE_BIN)
    print(f"firmware {version}: {BUILT.stat().st_size} bytes -> {PAYLOAD_DIR.name}/ (+release)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
