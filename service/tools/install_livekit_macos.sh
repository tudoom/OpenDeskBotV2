#!/usr/bin/env bash
# 开发模式（./start.sh）在 macOS 上安装本机 LiveKit Server。
# 对应 tools/install_livekit_windows.ps1：锁定版本 + SHA-256 校验。
#
# LiveKit 上游 GitHub Releases 自 1.9.x 起不再发布 darwin 二进制，改用
# Homebrew bottle（内容寻址：URL 即 SHA-256 承诺）。桌面客户端构建
# （client-macos/build-client-macos.sh）自带同版本二进制，不需要本脚本。
#
# 用法（在 service 目录）:
#   ./tools/install_livekit_macos.sh

set -euo pipefail

VERSION="1.13.6"
# arm64_sonoma bottle；Go 静态二进制，Sonoma 及更新版本通用。
EXPECTED_SHA256="0b31e11a9780f2e0b72796b6d60d6f3fd7fa48c11dcaee62af4d480e74f45416"

[[ "$(uname -m)" == "arm64" ]] || {
  echo "[error] 本脚本只支持 Apple Silicon。" >&2
  exit 1
}

SERVICE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="$SERVICE_ROOT/data/local/livekit"
BINARY_PATH="$INSTALL_ROOT/livekit-server"
DOWNLOAD_URL="https://ghcr.io/v2/homebrew/core/livekit/blobs/sha256:${EXPECTED_SHA256}"

TEMP_ROOT="$(mktemp -d -t deskbot-livekit-install)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

ARCHIVE="$TEMP_ROOT/livekit.bottle.tar.gz"
curl -L --fail --retry 3 --connect-timeout 15 \
  -H "Authorization: Bearer QQ==" \
  -o "$ARCHIVE" "$DOWNLOAD_URL"

ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "[error] LiveKit 校验失败: expected=$EXPECTED_SHA256 actual=$ACTUAL_SHA256" >&2
  exit 1
fi

tar -xzf "$ARCHIVE" -C "$TEMP_ROOT"
EXTRACTED="$(find "$TEMP_ROOT" -type f -name livekit-server | head -1)"
[[ -n "$EXTRACTED" ]] || {
  echo "[error] bottle 中未找到 livekit-server" >&2
  exit 1
}

mkdir -p "$INSTALL_ROOT"
cp "$EXTRACTED" "$BINARY_PATH"
chmod 755 "$BINARY_PATH"
"$BINARY_PATH" --version
echo "Installed and verified: $BINARY_PATH"
