#!/usr/bin/env bash
# 本地一键：校验 Python → 准备 venv → 启动主服务（可选 Flask 调试台）
# 支持 Linux / macOS / Windows Git Bash（不调用 apt/yum，系统依赖请自行安装）
#
# 用法（在 service 目录）:
#   ./start.sh
#
# 可选环境变量:
#   PYTHON_VERSION=3.11     目标 Python 主次版本
#   PYTHON_BIN=             显式指定 Python 可执行文件（跳过自动查找）
#   SKIP_SETUP=1            跳过 venv/依赖安装，仅启动服务
#   FAST_START=1            跳过 pip 安装（venv 须已完整）；未设置时若依赖已就绪也会自动跳过
#   DESKBOT_START_WEB=1     同时启动 Flask 调试台（默认 1，DESKBOT_WEB_PORT=5050）
#   DESKBOT_START_WEB=0     不启动调试台
#   SKIP_MODEL_DOWNLOAD=1   跳过人脸 / VAD 模型自动下载
#   SKIP_SYSTEM_CHECK=1     跳过 ffmpeg 等系统依赖警告

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/platform.sh"

_parse_python_version() {
  local v="${1:-3.11}"
  PY_MAJOR="${v%%.*}"
  local rest="${v#*.}"
  PY_MINOR="${rest%%.*}"
  PYTHON_MM="${PY_MAJOR}.${PY_MINOR}"
}
_parse_python_version "${PYTHON_VERSION:-3.11}"

ensure_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if platform_python_version_ok "$PYTHON_BIN" "$PY_MAJOR" "$PY_MINOR"; then
      PYTHON_BIN="$(platform_resolve_python_executable "$PYTHON_BIN")"
      echo "Python: $PYTHON_BIN"
      export PYTHON_BIN
      return 0
    fi
    echo "PYTHON_BIN=$PYTHON_BIN 不满足 Python ${PYTHON_MM}。" >&2
    exit 1
  fi

  if PYTHON_BIN="$(platform_find_python "$PYTHON_MM")"; then
    echo "Python: $PYTHON_BIN"
    export PYTHON_BIN
    return 0
  fi

  echo "未找到 Python ${PYTHON_MM}。" >&2
  if platform_is_windows; then
    echo "Windows 请从 https://www.python.org/downloads/ 安装，或使用: py -${PYTHON_MM}" >&2
  else
    echo "请用系统包管理器安装 python${PYTHON_MM} 与 venv 支持后重试。" >&2
  fi
  echo "也可显式指定: PYTHON_BIN=/path/to/python ./start.sh" >&2
  exit 1
}

setup_venv() {
  echo "[setup] venv（云 ASR 默认依赖 + Python ${PYTHON_MM}）..."
  (
    cd "$ROOT"
    export PYTHON_BIN
    export SETUP_ONLY=1
    export FAST_START="${FAST_START:-0}"
    platform_run_sh "$ROOT/scripts/setup_venv.sh"
  )
}

venvs_look_ready() {
  local py
  py="$(platform_venv_python "$ROOT" 2>/dev/null)" || return 1
  "$py" -c "import numpy, serial, websockets, yaml, webrtcvad, opuslib_next, croniter, deskbot_server" >/dev/null 2>&1 || return 1
}

ensure_local_scripts() {
  if [[ ! -f "$ROOT/scripts/setup_venv.sh" ]]; then
    echo "缺少脚本: $ROOT/scripts/setup_venv.sh" >&2
    exit 1
  fi
}

FACE_MODEL_PATH="$ROOT/models/mediapipe/face_landmarker.task"
# 国内网络可用 DESKBOT_FACE_MODEL_URL / DESKBOT_SILERO_VAD_URL 指向镜像，
# 并可选用 DESKBOT_FACE_MODEL_SHA256 / DESKBOT_SILERO_VAD_SHA256 做强校验。
FACE_MODEL_URL="${DESKBOT_FACE_MODEL_URL:-https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task}"
FACE_MODEL_MIN_BYTES=$((1024 * 1024))   # 官方模型约 3.6MB，小于 1MB 视为损坏
SILERO_VAD_MODEL_PATH="$ROOT/models/silero_vad/silero_vad.onnx"
# 官方地址与 tools/fetch_test_assets.py 内固定的下载源保持一致。
SILERO_VAD_MODEL_URL="${DESKBOT_SILERO_VAD_URL:-https://raw.githubusercontent.com/snakers4/silero-vad/76e3dc408eb2a5c655c34e230d2d5459b4439daa/src/silero_vad/data/silero_vad.onnx}"
SILERO_VAD_MIN_BYTES=$((100 * 1024))    # 官方模型约 2.3MB，小于 100KB 视为损坏

model_file_size_ok() {
  local path="$1" min_bytes="$2" size
  [[ -f "$path" ]] || return 1
  size="$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')"
  [[ "$size" =~ ^[0-9]+$ ]] && (( size >= min_bytes ))
}

model_file_sha256_ok() {
  local path="$1" expected="$2" actual=""
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$path" 2>/dev/null | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$path" 2>/dev/null | awk '{print $1}')"
  else
    actual="$("$PYTHON_BIN" -c \
      'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
      "$path" 2>/dev/null)"
  fi
  [[ -n "$actual" && "$actual" == "$expected" ]]
}

# 通用模型下载：先写同目录 <target>.tmp（curl/wget 自带重试），体积与可选
# 校验函数通过后再原子 mv 落位；任何失败都清理临时文件并返回 1，半截
# 文件不可能占住目标路径。
download_model_file() {
  local url="$1" target="$2" min_bytes="$3" verify_fn="${4:-}"
  local tmp="${target}.tmp"
  mkdir -p "$(dirname "$target")"
  rm -f "$tmp"
  if command -v curl >/dev/null 2>&1; then
    if ! curl -L --fail --retry 3 --connect-timeout 10 --max-time 300 \
      -o "$tmp" "$url"; then
      echo "[warn] 下载失败（curl 重试后仍不可达）: $url" >&2
      rm -f "$tmp"
      return 1
    fi
  elif command -v wget >/dev/null 2>&1; then
    if ! wget -q --tries=3 --connect-timeout=10 --timeout=60 -O "$tmp" "$url"; then
      echo "[warn] 下载失败（wget 重试后仍不可达）: $url" >&2
      rm -f "$tmp"
      return 1
    fi
  else
    echo "[warn] 未找到 curl/wget，无法下载: $url" >&2
    return 1
  fi
  if ! model_file_size_ok "$tmp" "$min_bytes"; then
    echo "[warn] 下载产物过小（<${min_bytes} 字节，疑似被截断/劫持），已丢弃: $url" >&2
    rm -f "$tmp"
    return 1
  fi
  if [[ -n "$verify_fn" ]] && ! "$verify_fn" "$tmp"; then
    echo "[warn] 下载产物校验失败，已丢弃: $url" >&2
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$target"
}

# 模型缺失/下载失败时的降级提示：服务继续启动，只有相关功能不可用。
warn_model_unavailable() {
  local label="$1" target="$2" url="$3" impact="$4" mirror_var="$5"
  {
    echo "[warn] ============================================================"
    echo "[warn] ${label} 不可用，服务仍将继续启动。"
    echo "[warn] 影响: ${impact}；其余功能不受影响。"
    echo "[warn] 手动放置: 下载 ${url}"
    echo "[warn]     保存为 ${target}"
    echo "[warn] 镜像: 网络受限可设 ${mirror_var}=<镜像地址> 后重启自动重下，"
    echo "[warn]     并可选设 ${mirror_var%_URL}_SHA256=<期望值> 做强校验。"
    echo "[warn] ============================================================"
  } >&2
}

face_model_checksum_ok() {
  local path="$1"
  [[ -z "${DESKBOT_FACE_MODEL_SHA256:-}" ]] && return 0
  model_file_sha256_ok "$path" "$DESKBOT_FACE_MODEL_SHA256"
}

# Silero 校验优先级：显式 DESKBOT_SILERO_VAD_SHA256 > 官方地址时用
# tools/fetch_test_assets.py 的固定 checksum > 自定义镜像仅做体积检查。
silero_vad_model_checksum_ok() {
  local path="$1" py
  if [[ -n "${DESKBOT_SILERO_VAD_SHA256:-}" ]]; then
    model_file_sha256_ok "$path" "$DESKBOT_SILERO_VAD_SHA256"
    return
  fi
  if [[ -n "${DESKBOT_SILERO_VAD_URL:-}" ]]; then
    return 0
  fi
  py="$(platform_venv_python "$ROOT" 2>/dev/null)" || return 0
  "$py" "$ROOT/tools/fetch_test_assets.py" \
    --silero-target "$path" \
    --check-only >/dev/null 2>&1
}

face_model_ready() {
  [[ -f "$FACE_MODEL_PATH" ]] || return 1
  if ! model_file_size_ok "$FACE_MODEL_PATH" "$FACE_MODEL_MIN_BYTES"; then
    echo "[warn] 人脸模型体积异常（半截/损坏文件），删除后重下: $FACE_MODEL_PATH" >&2
    rm -f "$FACE_MODEL_PATH"
    return 1
  fi
  face_model_checksum_ok "$FACE_MODEL_PATH"
}

silero_vad_model_ready() {
  [[ -f "$SILERO_VAD_MODEL_PATH" ]] || return 1
  if ! model_file_size_ok "$SILERO_VAD_MODEL_PATH" "$SILERO_VAD_MIN_BYTES"; then
    echo "[warn] Silero VAD 模型体积异常（半截/损坏文件），删除后重下: $SILERO_VAD_MODEL_PATH" >&2
    rm -f "$SILERO_VAD_MODEL_PATH"
    return 1
  fi
  silero_vad_model_checksum_ok "$SILERO_VAD_MODEL_PATH"
}

ensure_deskbot_env() {
  if [[ ! -f "$ROOT/.env" && -f "$ROOT/.env.example" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "[setup] 已从 .env.example 创建 .env"
    echo "[setup] 请编辑 .env 并填写独立的 ASR_API_KEY 与 LLM 凭证"
  fi

  if [[ -f "$ROOT/.env" ]]; then
    # shellcheck source=/dev/null
    set -a && source "$ROOT/.env" && set +a
  fi

  local web_secret="${DESKBOT_WEB_SECRET_KEY:-}"
  local secure_deployment=0
  case "${DESKBOT_ENV:-}" in
    prod|production|PROD|PRODUCTION) secure_deployment=1 ;;
  esac
  case "${DESKBOT_TLS_TERMINATED_BY_PROXY:-}" in
    1|true|TRUE|yes|YES|on|ON) secure_deployment=1 ;;
  esac
  case "${DESKBOT_WEB_TLS_TERMINATED_BY_PROXY:-}" in
    1|true|TRUE|yes|YES|on|ON) secure_deployment=1 ;;
  esac
  if [[ -n "${DESKBOT_SERVER_TLS_CERT:-}" && -n "${DESKBOT_SERVER_TLS_KEY:-}" ]]; then
    secure_deployment=1
  fi
  if (( ${#web_secret} < 32 )); then
    if (( secure_deployment )); then
      echo "[error] 生产环境必须持久配置至少 32 字符的 DESKBOT_WEB_SECRET_KEY；TLS 配置已视为生产部署。" >&2
      exit 1
    fi
    DESKBOT_WEB_SECRET_KEY="$(
      "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))'
    )"
    export DESKBOT_WEB_SECRET_KEY
    echo "[security] 本次启动已生成临时 Web/WS 签名密钥；重启会注销现有会话。"
  fi

  if [[ -z "${ARK_API_KEY:-}${LLM_API_KEY:-}${VOLCENGINE_API_KEY:-}${DASHSCOPE_API_KEY:-}${QWEN_API_KEY:-}" ]]; then
    echo "[warn] 未设置 ARK_API_KEY（或 LLM_API_KEY / DASHSCOPE_API_KEY），语音对话将无法调用大模型。" >&2
    echo "[warn] 请编辑 .env 后重启。" >&2
  fi
  local asr_provider
  asr_provider="$(printf '%s' "${ASR_PROVIDER:-openai_compatible}" | tr '[:upper:]' '[:lower:]')"
  asr_provider="${asr_provider//-/_}"
  case "$asr_provider" in
    funasr|local|sensevoice|sense_voice)
      ;;
    *)
      if [[ -z "${ASR_API_KEY:-}" ]]; then
        echo "[warn] 云 ASR 尚未配置 ASR_API_KEY；服务仍会启动，首次语音会返回可操作的配置错误。" >&2
      fi
      ;;
  esac
}

download_face_model() {
  echo "[setup] 下载 MediaPipe 人脸模型（约 3.6MB）..."
  download_model_file "$FACE_MODEL_URL" "$FACE_MODEL_PATH" \
    "$FACE_MODEL_MIN_BYTES" face_model_checksum_ok
}

download_silero_vad_model() {
  echo "[setup] 下载 Silero VAD 模型（约 2.3MB）..."
  download_model_file "$SILERO_VAD_MODEL_URL" "$SILERO_VAD_MODEL_PATH" \
    "$SILERO_VAD_MIN_BYTES" silero_vad_model_checksum_ok
}

warn_face_model_unavailable() {
  warn_model_unavailable "MediaPipe 人脸模型" \
    "$FACE_MODEL_PATH" \
    "$FACE_MODEL_URL" \
    "camera_frame 人脸检测/跟随不可用" \
    "DESKBOT_FACE_MODEL_URL"
}

warn_silero_vad_model_unavailable() {
  warn_model_unavailable "Silero VAD 模型" \
    "$SILERO_VAD_MODEL_PATH" \
    "$SILERO_VAD_MODEL_URL" \
    "RTC 语音端点检测（VAD）不可用" \
    "DESKBOT_SILERO_VAD_URL"
}

ensure_models() {
  if [[ "${SKIP_MODEL_DOWNLOAD:-0}" == "1" ]]; then
    echo "SKIP_MODEL_DOWNLOAD=1，跳过模型下载检查。"
    if ! face_model_ready; then
      warn_face_model_unavailable
    fi
    if ! silero_vad_model_ready; then
      warn_silero_vad_model_unavailable
    fi
    return 0
  fi

  if face_model_ready; then
    echo "[setup] 人脸模型已就绪: $FACE_MODEL_PATH"
  elif ! download_face_model; then
    warn_face_model_unavailable
  fi

  if silero_vad_model_ready; then
    echo "[setup] Silero VAD 模型已就绪: $SILERO_VAD_MODEL_PATH"
  elif ! download_silero_vad_model; then
    warn_silero_vad_model_unavailable
  fi
}

run_services() {
  trap 'trap - INT TERM EXIT; kill 0 2>/dev/null || true' INT TERM EXIT

  if [[ "${DESKBOT_START_WEB:-1}" == "1" ]]; then
    local web_port="${DESKBOT_WEB_PORT:-5050}"
    local web_host="${DESKBOT_WEB_HOST:-127.0.0.1}"
    echo "[web] 启动 Flask 控制台 ${web_host}:${web_port}"
    (
      cd "$ROOT"
      # shellcheck source=/dev/null
      [[ -f .env ]] && set -a && source .env && set +a
      web_py="$(platform_venv_python "$ROOT")"
      export DESKBOT_WEB_HOST="${DESKBOT_WEB_HOST:-127.0.0.1}"
      export DESKBOT_WEB_PORT="${DESKBOT_WEB_PORT:-5050}"
      exec "$web_py" -m deskbot_server.web
    ) &
  fi

  echo "[1/1] 启动 deskbot-server ($ROOT) ..."
  cd "$ROOT"
  exec env SKIP_SETUP=1 bash "$ROOT/scripts/setup_venv.sh"
}

# --- main ---
export DESKBOT_START_WEB="${DESKBOT_START_WEB:-1}"

ensure_python
platform_warn_system_deps
ensure_local_scripts

if [[ "${SKIP_SETUP:-0}" != "1" ]]; then
  if [[ "${FAST_START:-0}" != "1" ]] && venvs_look_ready; then
    echo "[setup] 检测到 venv 依赖已就绪，跳过 pip 安装（等同 FAST_START=1）。"
    export FAST_START=1
  fi
  setup_venv
else
  echo "SKIP_SETUP=1，跳过 venv/依赖安装。"
fi

ensure_deskbot_env
ensure_models

run_services
