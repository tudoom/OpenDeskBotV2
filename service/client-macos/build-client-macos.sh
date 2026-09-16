#!/usr/bin/env bash
# Open Desk Bot V2 macOS 客户端构建（对应 client/Build-Client.ps1）。
#
# 产出 service/dist/OpenDeskBotV2.app 与 OpenDeskBotV2.dmg（Apple Silicon）。
# 与 Windows 版一致的原则：外部产物全部按固定 SHA-256 校验后才进包；
# 默认不含人脸视觉栈；不打包 .env / 数据库 / 日志 / LiveKit 本机凭据。
#
# 与 Windows 版的刻意差异：无杀软实扫问题，因此不做 pyc 无源码化、stdlib
# zip 压缩与多线程解包那套优化；.app 目录即运行时，无自解压载荷。
#
# 用法（在 service 目录或任意位置）:
#   ./client-macos/build-client-macos.sh
#
# 可选参数:
#   --include-face-stack   打包人脸视觉栈（mediapipe/opencv/insightface）与
#                          人脸模型（对应 Build-Client.ps1 -IncludeFaceStack）
#   --seed-env <path>      内部分发：把指定 .env 作为种子打进包（首启且用户
#                          没有 .env 时才落地，绝不覆盖）
#                          不指定时自动采用 service/client/seed.env（若存在）
#   --no-seed-env          强制打公开版：即使 client/seed.env 存在也不打入凭证
#   --skip-dmg             只出 .app，不打 DMG
#   --skip-smoke           跳过烟测门槛（仅调试构建脚本时用）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_ROOT="$SERVICE_ROOT/build-macos"
DOWNLOADS="$BUILD_ROOT/downloads"
STAGE="$BUILD_ROOT/stage"
DIST="$SERVICE_ROOT/dist"
APP_NAME="OpenDeskBotV2"
APP_VERSION="2.0.0"
BUNDLE_ID="com.opendeskbot.OpenDeskBotV2"
MIN_MACOS="13.0"

# ---- 固定下载源与校验值 ---------------------------------------------------
# python-build-standalone：可重定位 CPython 3.11（aarch64 install_only）。
PBS_TAG="20250818"
PBS_PY="3.11.13"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PBS_PY}+${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_SHA256="${DESKBOT_PYTHON_SHA256:-317fda280cb51852a346da5f595131fcd3e0dfda421b3983f675cb1994159838}"
PYTHON_ARCHIVE="$DOWNLOADS/cpython-${PBS_PY}.tar.gz"

# LiveKit Server：上游 GitHub Releases 自 1.9.x 起不再发 darwin 二进制，
# 改用 Homebrew bottle（内容寻址，URL 本身即 SHA-256 承诺）。1.13.6 与
# Windows 锁定的 1.13.5 仅差一个 patch 版本。
LIVEKIT_VERSION="1.13.6"
LIVEKIT_SHA256="0b31e11a9780f2e0b72796b6d60d6f3fd7fa48c11dcaee62af4d480e74f45416"
LIVEKIT_URL="https://ghcr.io/v2/homebrew/core/livekit/blobs/sha256:${LIVEKIT_SHA256}"
LIVEKIT_ARCHIVE="$DOWNLOADS/livekit-${LIVEKIT_VERSION}.arm64_sonoma.bottle.tar.gz"

# libopus（opuslib_next 的 ctypes 运行时依赖；Win 侧由 PyOgg 带 opus.dll）。
OPUS_VERSION="1.6.1"
OPUS_SHA256="9b159efdfe3b5a31e5f4db9bac8cf40054ac301c508215f7db13cfc6ca661a66"
OPUS_URL="https://ghcr.io/v2/homebrew/core/opus/blobs/sha256:${OPUS_SHA256}"
OPUS_ARCHIVE="$DOWNLOADS/opus-${OPUS_VERSION}.arm64_sonoma.bottle.tar.gz"

# Silero VAD 模型：与 tools/fetch_test_assets.py 同源同校验。
SILERO_COMMIT="76e3dc408eb2a5c655c34e230d2d5459b4439daa"
SILERO_URL="https://raw.githubusercontent.com/snakers4/silero-vad/${SILERO_COMMIT}/src/silero_vad/data/silero_vad.onnx"
SILERO_SHA256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_TARGET="$DOWNLOADS/silero_vad.onnx"

# MediaPipe 人脸模型（仅 --include-face-stack 时打包，与 start.sh 同源）。
FACE_MODEL_URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
FACE_MODEL_TARGET="$DOWNLOADS/face_landmarker.task"

# 带 key 版（内部分发）必须内置的默认项。种子 .env 里缺哪条就补哪条，
# 已有的值不覆盖。只放公共标识符；任何账号相关的 ID（如复刻音色）一律
# 只写在 client/seed.env 里，不进仓库。
BUNDLED_ENV_DEFAULTS=(
)

# ---- 参数 -----------------------------------------------------------------
INCLUDE_FACE_STACK=0
SEED_ENV=""
NO_SEED_ENV=0
SKIP_DMG=0
SKIP_SMOKE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-face-stack) INCLUDE_FACE_STACK=1; shift ;;
    --seed-env) SEED_ENV="${2:?--seed-env 需要路径}"; shift 2 ;;
    --no-seed-env) NO_SEED_ENV=1; shift ;;
    --skip-dmg) SKIP_DMG=1; shift ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ---- 工具函数 -------------------------------------------------------------
log() { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

# 下载到 <target>，校验 SHA-256，失败即删除并终止（与 Win 版同策略）。
fetch_pinned() {
  local url="$1" target="$2" expected="$3" auth="${4:-}"
  if [[ -f "$target" ]]; then
    if [[ "$(sha256_of "$target")" == "$expected" ]]; then
      log "已缓存: $(basename "$target")"
      return 0
    fi
    rm -f "$target"
  fi
  log "下载 $(basename "$target") ..."
  local -a curl_args=(-L --fail --retry 3 --connect-timeout 15 -o "$target.tmp")
  [[ -n "$auth" ]] && curl_args+=(-H "Authorization: Bearer QQ==")
  curl "${curl_args[@]}" "$url" || die "下载失败: $url"
  local actual
  actual="$(sha256_of "$target.tmp")"
  if [[ "$actual" != "$expected" ]]; then
    rm -f "$target.tmp"
    die "校验失败 $(basename "$target"): expected=$expected actual=$actual"
  fi
  mv -f "$target.tmp" "$target"
}

# ---- 环境检查 -------------------------------------------------------------
[[ "$(uname -m)" == "arm64" ]] || die "本脚本只支持 Apple Silicon 构建机。"
command -v swiftc >/dev/null 2>&1 \
  || die "缺少 swiftc。请安装 Xcode Command Line Tools: xcode-select --install"
command -v curl >/dev/null 2>&1 || die "缺少 curl。"
[[ -f "$SERVICE_ROOT/pyproject.toml" ]] || die "未找到 service/pyproject.toml。"
if [[ -n "$SEED_ENV" && ! -f "$SEED_ENV" ]]; then
  die "--seed-env 指定的文件不存在: $SEED_ENV"
fi
# 与 Build-Client.ps1 对齐：没显式指定就自动采用 client/seed.env（该文件在
# .gitignore 里，只存在于内部构建机）。两边约定一致，带 key 版不会因为忘了
# 传参数而打成普通版。
if [[ "$NO_SEED_ENV" == "1" ]]; then
  if [[ -n "$SEED_ENV" ]]; then
    die "--seed-env 与 --no-seed-env 不能同时使用"
  fi
elif [[ -z "$SEED_ENV" && -f "$SERVICE_ROOT/client/seed.env" ]]; then
  SEED_ENV="$SERVICE_ROOT/client/seed.env"
  log "自动采用内部种子 .env: service/client/seed.env"
fi
# 版本形态一句话讲清楚，避免把带凭证的内部版误当公开版发出去。
if [[ -n "$SEED_ENV" ]]; then
  log "构建形态: 内部分发版（内置凭证，种子 .env: $SEED_ENV）"
else
  log "构建形态: 公开版（不含任何凭证）"
fi

mkdir -p "$DOWNLOADS" "$DIST"
rm -rf "$STAGE"
mkdir -p "$STAGE"

# ---- 1. 下载外部产物 -------------------------------------------------------
fetch_pinned "$PYTHON_URL" "$PYTHON_ARCHIVE" "$PYTHON_SHA256"
fetch_pinned "$LIVEKIT_URL" "$LIVEKIT_ARCHIVE" "$LIVEKIT_SHA256" ghcr
fetch_pinned "$OPUS_URL" "$OPUS_ARCHIVE" "$OPUS_SHA256" ghcr
fetch_pinned "$SILERO_URL" "$SILERO_TARGET" "$SILERO_SHA256"
if [[ "$INCLUDE_FACE_STACK" == "1" && ! -f "$FACE_MODEL_TARGET" ]]; then
  log "下载 MediaPipe 人脸模型 ..."
  curl -L --fail --retry 3 -o "$FACE_MODEL_TARGET.tmp" "$FACE_MODEL_URL" \
    || die "人脸模型下载失败"
  [[ $(wc -c < "$FACE_MODEL_TARGET.tmp") -gt 1048576 ]] \
    || die "人脸模型体积异常（<1MB）"
  mv -f "$FACE_MODEL_TARGET.tmp" "$FACE_MODEL_TARGET"
fi

# ---- 2. 展开运行时 Python 并安装依赖 ---------------------------------------
RUNTIME="$STAGE/runtime"
mkdir -p "$RUNTIME"
log "展开 CPython ${PBS_PY} ..."
tar -xzf "$PYTHON_ARCHIVE" -C "$RUNTIME"   # 解出 runtime/python/
PY="$RUNTIME/python/bin/python3.11"
[[ -x "$PY" ]] || die "解包后未找到 $PY"

log "安装服务依赖（pip install，默认不含人脸栈）..."
# 全新运行时没有任何 pip 配置，默认官方 PyPI 在国内网络下极慢。
# 与 scripts/setup_venv.sh 同源：默认 TUNA 镜像，可用 PIP_INDEX_URL 覆盖。
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
"$PY" -m pip install --quiet --upgrade pip
if [[ "$INCLUDE_FACE_STACK" == "1" ]]; then
  "$PY" -m pip install --quiet "$SERVICE_ROOT[face]"
else
  "$PY" -m pip install --quiet "$SERVICE_ROOT"
fi

# 家居控制依赖 miloco-miot SDK。上游只发布 darwin/linux 整包、没有 wheel，
# wheel 随仓库提交，构建期离线安装（与 Win 版 Install-MilocoMiotWheel 一致）。
# --no-deps 是刻意的：SDK 声明了 av / fastmcp / paho-mqtt / zeroconf 等重依赖，
# 云端家居控制只用 miot.cloud / const / spec / storage / types 五个模块。
MILOCO_WHEEL="$(ls "$SERVICE_ROOT"/src/deskbot_server/iotctl/wheels/miloco_miot-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$MILOCO_WHEEL" ]] || die "未找到 miloco-miot wheel（src/deskbot_server/iotctl/wheels/）"
log "安装 miloco-miot SDK（$(basename "$MILOCO_WHEEL")）..."
"$PY" -m pip install --quiet --no-deps --no-index --upgrade "$MILOCO_WHEEL"

# 项目本体从 site-packages 卸掉：业务码统一从 app/src 以 PYTHONPATH 加载
# （与 Win 版一致，现场排障要能对上源码行号）；依赖保留。
"$PY" -m pip uninstall --quiet -y deskbot-server

log "预编译字节码（加速冷启动；运行期 PYTHONDONTWRITEBYTECODE=1 不落盘）..."
"$PY" -m compileall -q -j 0 "$RUNTIME/python/lib/python3.11/site-packages" || true
# 分发包里不留构建机的绝对路径（公开发布隐私扫描项）：pip 生成的控制台脚本
# shebang 指向 stage 目录，本地 wheel 安装留下的 direct_url.json 记着 file:// 路径。
# shebang 改成 pip 自己在解释器路径含空格时用的可搬迁写法：按脚本所在目录找解释器。
log "抹掉运行时里的构建机绝对路径 ..."
for f in "$RUNTIME"/python/bin/*; do
  [[ -f "$f" && ! -L "$f" ]] || continue
  if head -n 1 "$f" 2>/dev/null | grep -q "^#!.*$STAGE"; then
    tmp="$f.tmp"
    {
      printf '#!/bin/sh\n'
      printf '%s\n' "'''exec' \"\$(dirname -- \"\$0\")/python3.11\" \"\$0\" \"\$@\""
      printf '%s\n' "' '''"
      tail -n +2 "$f"
    } > "$tmp"
    chmod +x "$tmp"
    mv -f "$tmp" "$f"
  fi
done
find "$RUNTIME/python/lib/python3.11/site-packages" -path '*.dist-info/direct_url.json' -delete
if grep -rIl -F "$STAGE" "$RUNTIME" >/dev/null 2>&1; then
  die "运行时里仍有构建机路径: $(grep -rIl -F "$STAGE" "$RUNTIME" | head -3 | tr '\n' ' ')"
fi

# ---- 3. 业务源码 / 模型 / 二进制 / 原生库 ----------------------------------
APPDIR="$RUNTIME/app"
mkdir -p "$APPDIR/bin" "$APPDIR/lib" "$APPDIR/models/silero_vad"

log "拷贝业务源码 app/src ..."
mkdir -p "$APPDIR/src"
rsync -a --exclude '__pycache__' "$SERVICE_ROOT/src/" "$APPDIR/src/"
"$PY" -m compileall -q -j 0 "$APPDIR/src" || true

log "放置模型 ..."
cp "$SILERO_TARGET" "$APPDIR/models/silero_vad/silero_vad.onnx"
if [[ "$INCLUDE_FACE_STACK" == "1" ]]; then
  mkdir -p "$APPDIR/models/mediapipe"
  cp "$FACE_MODEL_TARGET" "$APPDIR/models/mediapipe/face_landmarker.task"
fi

log "提取 livekit-server（Homebrew bottle）..."
BOTTLE_TMP="$BUILD_ROOT/bottle-tmp"
rm -rf "$BOTTLE_TMP"; mkdir -p "$BOTTLE_TMP"
tar -xzf "$LIVEKIT_ARCHIVE" -C "$BOTTLE_TMP"
LIVEKIT_BIN="$(find "$BOTTLE_TMP" -type f -name livekit-server | head -1)"
[[ -n "$LIVEKIT_BIN" ]] || die "bottle 中未找到 livekit-server"
cp "$LIVEKIT_BIN" "$APPDIR/bin/livekit-server"
chmod 755 "$APPDIR/bin/livekit-server"

log "提取 libopus ..."
rm -rf "$BOTTLE_TMP"; mkdir -p "$BOTTLE_TMP"
tar -xzf "$OPUS_ARCHIVE" -C "$BOTTLE_TMP"
OPUS_DYLIB="$(find "$BOTTLE_TMP" -type f -name 'libopus.0.dylib' | head -1)"
[[ -n "$OPUS_DYLIB" ]] || die "bottle 中未找到 libopus.0.dylib"
cp "$OPUS_DYLIB" "$APPDIR/lib/libopus.0.dylib"
# find_library("opus") 会按 libopus.dylib 名称 dlopen，两个名字都给。
cp "$OPUS_DYLIB" "$APPDIR/lib/libopus.dylib"
rm -rf "$BOTTLE_TMP"

# ---- 3.9 随包固件新鲜度闸门 ------------------------------------------------
# 固件源码改了却忘了跑 sync_firmware_payload.py，会打出「版本号没变、内容变了」
# 的包（2026-09-03 实际发生过）。构建机上若存在固件构建产物，就必须与随包固件
# 逐字节一致，否则直接失败并给出修法。
FW_BUILT="$SERVICE_ROOT/../hardware/.pio/build/deskbot_v2/firmware.bin"
FW_PAYLOAD="$SERVICE_ROOT/src/deskbot_server/firmware_payload/deskbot_v2.bin"
if [[ -f "$FW_BUILT" && -f "$FW_PAYLOAD" ]]; then
  if ! cmp -s "$FW_BUILT" "$FW_PAYLOAD"; then
    die "随包固件与最近一次构建产物不一致。先运行：python service/scripts/sync_firmware_payload.py（改了固件源码请先 pio run -e deskbot_v2）"
  fi
  FW_VERSION="$(cat "$SERVICE_ROOT/../hardware/VERSION" 2>/dev/null || echo '')"
  # 版本必须真的编进二进制：在错误目录跑 pio run 会让固件根本没重编，
  # 而同步脚本照样把旧二进制标成新版本，哈希比对查不出来。
  if [[ -n "$FW_VERSION" ]] && ! grep -qa "$FW_VERSION" "$FW_PAYLOAD"; then
    die "随包固件里没有版本字符串 $FW_VERSION。请在 hardware/ 目录下重新编译：cd hardware && pio run -e deskbot_v2，然后 python service/scripts/sync_firmware_payload.py"
  fi
  log "随包固件校验通过: ${FW_VERSION:-?}"
fi

# ---- 4. 种子配置 -----------------------------------------------------------
log "放置种子配置 seed/ ..."
SEED="$RUNTIME/seed"
mkdir -p "$SEED/data"
cp "$SERVICE_ROOT/config.yaml" "$SEED/config.yaml"
cp "$SERVICE_ROOT/.env.example" "$SEED/.env.example"
rsync -a "$SERVICE_ROOT/data/global/" "$SEED/data/global/"
if [[ -n "$SEED_ENV" ]]; then
  cp "$SEED_ENV" "$SEED/.env"
  # 带 key 版必须内置的默认项：种子文件没写就补上，避免每次打包靠人记。
  # 只在缺失时追加，种子里已有的值（包括故意留空）一律不动。
  # 种子文件末尾若没有换行，直接追加会粘到上一行（把上一条配置写坏）。
  if [[ -s "$SEED/.env" && -n "$(tail -c1 "$SEED/.env")" ]]; then
    printf '\n' >> "$SEED/.env"
  fi
  for kv in "${BUNDLED_ENV_DEFAULTS[@]}"; do
    key="${kv%%=*}"
    if ! grep -qE "^[[:space:]]*${key}=" "$SEED/.env"; then
      printf '%s\n' "$kv" >> "$SEED/.env"
      log "种子 .env 补入默认项: $key"
    fi
  done
  log "已打入种子 .env（仅首启且用户无 .env 时落地）"
  # 内部分发版还可附带 client/seed.d/ 里的任意文件（同样在 .gitignore 里，例如企业 CA 证书包），
  # 与 .env 一起进 seed/，首启落到用户目录；公开版没有这个目录，安静跳过。
  SEED_EXTRA_DIR="$SERVICE_ROOT/client/seed.d"
  if [[ -d "$SEED_EXTRA_DIR" ]]; then
    for f in "$SEED_EXTRA_DIR"/*; do
      [[ -f "$f" ]] || continue
      cp "$f" "$SEED/$(basename "$f")"
      log "种子附加文件: $(basename "$f")"
    done
  fi
fi

# ---- 5. 烟测门槛（对应 Invoke-StageSmokeGates）-----------------------------
run_smoke() {
  local label="$1"; shift
  log "烟测: $label"
  env -i \
    HOME="$HOME" \
    PATH="$RUNTIME/python/bin:/usr/bin:/bin" \
    PYTHONHOME="$RUNTIME/python" \
    PYTHONPATH="$APPDIR/src:$RUNTIME/python/lib/python3.11/site-packages" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    DYLD_FALLBACK_LIBRARY_PATH="$APPDIR/lib" \
    "$PY" -c "$1" || die "烟测未通过: $label"
}

if [[ "$SKIP_SMOKE" != "1" ]]; then
  run_smoke "核心依赖导入" \
    "import flask, sqlalchemy, numpy, aiohttp, livekit, serial, websockets, yaml, croniter"
  run_smoke "libopus 装载（opuslib_next）" \
    "import opuslib_next; print('opus ok')"
  run_smoke "业务包导入（launcher 式 PYTHONPATH）" \
    "import deskbot_server; print(deskbot_server.__file__)"
  run_smoke "miloco-miot 导入（家居控制 SDK）" \
    "import miot, miot.cloud; print('miot ok')"
  if [[ "$INCLUDE_FACE_STACK" == "1" ]]; then
    run_smoke "人脸栈导入" "import mediapipe, cv2, insightface"
  else
    # 反向断言：防止依赖解析变化后人脸栈悄悄漏回载荷（与 Win 版一致）。
    run_smoke "人脸栈缺席断言" "
for name in ('mediapipe', 'cv2', 'insightface'):
    try:
        __import__(name)
    except ImportError:
        continue
    raise SystemExit(f'face stack leaked into default build: {name}')
print('faceless ok')"
  fi
  log "烟测: livekit-server 可执行"
  "$APPDIR/bin/livekit-server" --version || die "livekit-server 无法运行"
fi

# ---- 6. 应用图标（app.ico → AppIcon.icns，用 Pillow 转）--------------------
log "生成应用图标 ..."
ICONSET="$BUILD_ROOT/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
"$PY" - "$SERVICE_ROOT/client/app.ico" "$ICONSET" <<'PYEOF'
import sys
from PIL import Image
source, iconset = sys.argv[1], sys.argv[2]
ico = Image.open(source)
frames = {}
for size in getattr(ico, "info", {}).get("sizes", set()) or {(256, 256)}:
    ico.size = size
    frames[size[0]] = ico.copy().convert("RGBA")
best = frames[max(frames)]
for edge, name in [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"),
                   (64, "32x32@2x"), (128, "128x128"), (256, "128x128@2x"),
                   (256, "256x256"), (512, "256x256@2x"), (512, "512x512"),
                   (1024, "512x512@2x")]:
    src = frames.get(edge, best)
    src.resize((edge, edge), Image.LANCZOS).save(f"{iconset}/icon_{name}.png")
PYEOF
iconutil -c icns "$ICONSET" -o "$BUILD_ROOT/AppIcon.icns"

# ---- 7. 组装 .app ----------------------------------------------------------
log "编译 Swift 启动器 ..."
APP="$DIST/$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 -target "arm64-apple-macos$MIN_MACOS" \
  "$SCRIPT_DIR/main.swift" \
  -o "$APP/Contents/MacOS/$APP_NAME"

log "写入 Info.plist ..."
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Open Desk Bot V2</string>
    <key>CFBundleDisplayName</key><string>Open Desk Bot V2</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key><string>$APP_VERSION</string>
    <key>CFBundleShortVersionString</key><string>$APP_VERSION</string>
    <key>CFBundleExecutable</key><string>$APP_NAME</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key>
    <string>OpenDeskBot Contributors — GPL-3.0</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
    </dict>
</dict>
</plist>
PLIST

cp "$BUILD_ROOT/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

log "拷贝运行时进 .app（rsync）..."
rsync -a "$RUNTIME/" "$APP/Contents/Resources/runtime/"

# ---- 8. 签名与 DMG ---------------------------------------------------------
log "Ad-hoc 签名（无 Developer ID；分发后首次打开需右键 → 打开）..."
codesign --force --deep --sign - "$APP"

APP_SIZE="$(du -sh "$APP" | awk '{print $1}')"
log ".app 完成: $APP ($APP_SIZE)"

if [[ "$SKIP_DMG" != "1" ]]; then
  log "打包 DMG ..."
  DMG="$DIST/$APP_NAME.dmg"
  DMG_STAGE="$BUILD_ROOT/dmg-stage"
  rm -rf "$DMG_STAGE" "$DMG"
  mkdir -p "$DMG_STAGE"
  cp -R "$APP" "$DMG_STAGE/"
  ln -s /Applications "$DMG_STAGE/Applications"
  hdiutil create -quiet -volname "Open Desk Bot V2" \
    -srcfolder "$DMG_STAGE" -format UDZO "$DMG"
  rm -rf "$DMG_STAGE"
  DMG_SHA="$(sha256_of "$DMG")"
  echo "$DMG_SHA  $(basename "$DMG")" > "$DMG.sha256"
  log "DMG 完成: $DMG ($(du -sh "$DMG" | awk '{print $1}'))"
  log "SHA-256: $DMG_SHA"
fi

log "构建结束。人脸栈: $([[ "$INCLUDE_FACE_STACK" == "1" ]] && echo included || echo excluded)"
