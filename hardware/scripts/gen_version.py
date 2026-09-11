"""PlatformIO pre 脚本：从 hardware/VERSION 生成 firmware/version_gen.h。

固件版本只在 hardware/VERSION 一处维护；common.h 包含生成头。安装包的
firmware_payload/manifest.json 由 service/scripts/sync_firmware_payload.py
从同一文件生成，三处不再手工同步。
"""
from pathlib import Path

Import("env")  # noqa: F821 —— PlatformIO 注入

ROOT = Path(env["PROJECT_DIR"])  # noqa: F821
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
out = ROOT / "firmware" / "version_gen.h"
content = (
    "// 自动生成：来源 hardware/VERSION，勿手改。\n"
    "#pragma once\n"
    f'#define DESKBOT_VERSION "{version}"\n'
)
if not out.exists() or out.read_text(encoding="utf-8") != content:
    out.write_text(content, encoding="utf-8")
    print(f"[gen_version] firmware version {version}")
