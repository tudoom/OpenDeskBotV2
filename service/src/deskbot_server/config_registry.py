"""环境变量配置的唯一登记处 + 启动时的"生效值与来源"报告。

代码里散落 100 多个 ``os.environ.get``，同一类设置又同时存在于 .env、
config.yaml、JSON 存储与数据库 revision——没人能背出优先级。这里先做两件
能立刻落地的事：

1. 所有环境变量键都在 ``ENV_KEYS`` 登记（说明 + 是否敏感）；测试保证代码里
   新出现的 ``environ.get("X")`` 必须先在这里登记，否则红。
2. 启动时打印一张"键 → 生效值（敏感遮蔽）→ 来源（.env / 进程环境 / 默认）"
   的表，排查配置问题不用再猜。

后续把 ``core.settings.AppSettings`` 收成唯一读取点时，这张表就是迁移清单。
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from deskbot_server.env import read_env_file

logger = logging.getLogger("deskbot-server")

# name -> (说明, 是否敏感)
ENV_KEYS: dict[str, tuple[str, bool]] = {
    # ---- Core 服务 ----
    "DESKBOT_SERVER_HOST": ("Core HTTP/WS 绑定地址（默认 127.0.0.1）", False),
    "DESKBOT_SERVER_PORT": ("Core 端口（默认 9000）", False),
    "DESKBOT_SERVER_CONFIG": ("config.yaml 路径", False),
    "DESKBOT_SERVER_LOG_LEVEL": ("日志级别", False),
    "DESKBOT_SERVER_LOG_FILE": ("core.log 路径", False),
    "DESKBOT_SERVER_TLS_CERT": ("TLS 证书", False),
    "DESKBOT_SERVER_TLS_KEY": ("TLS 私钥路径", True),
    "DESKBOT_TLS_TERMINATED_BY_PROXY": ("TLS 由前置代理终止", False),
    "DESKBOT_SERVER_INTERNAL_BASE": ("Core 内部访问基址", False),
    "DESKBOT_WS_PATH": ("WS 路径", False),
    "DESKBOT_WS_PUBLIC_BASE": ("WS 对外基址", False),
    "DESKBOT_WS_PING_INTERVAL": ("WS ping 间隔", False),
    "DESKBOT_WS_PING_TIMEOUT": ("WS ping 超时", False),
    "WS_SEND_TIMEOUT_SEC": ("WS 发送超时", False),
    "DESKBOT_DB_PATH": ("SQLite 路径", False),
    "DESKBOT_ENV": ("运行环境标识", False),
    "DESKBOT_KEEP_PROXY": ("保留系统代理环境变量", False),
    "DESKBOT_STABLE_BIN_DIR": ("稳定二进制目录（livekit-server 等）", False),
    "DESKBOT_LOG_CONTENT": ("日志是否含对话内容", False),
    "DESKBOT_MEMORY_CONSOLIDATION_HOUR": ("记忆整理时刻", False),
    "DESKBOT_LLM_CONFIG_POLL_SECONDS": ("LLM 配置 revision 轮询", False),
    "DESKBOT_LLM_TTS_RESERVE_BYTES": ("LLM/TTS 日配额预留", False),
    "DESKBOT_DEBUG_WS_TOKEN_SECONDS": ("调试 WS token 有效期", False),
    "DESKBOT_ALLOW_DEBUG_TOKEN_IN_QUERY": ("允许 query 携带调试 token", False),
    "DESKBOT_ALLOW_API_KEY_IN_QUERY": ("允许 query 携带 API Key", False),
    "PATH": ("进程 PATH", False),
    # ---- Web 控制台 ----
    "DESKBOT_WEB_HOST": ("控制台绑定地址", False),
    "DESKBOT_WEB_PORT": ("控制台端口（默认 5050）", False),
    "DESKBOT_WEB_DEBUG": ("控制台调试模式", False),
    "DESKBOT_WEB_SECRET_KEY": ("Flask 会话密钥", True),
    "DESKBOT_WEB_PUBLIC_HOST": ("控制台对外主机名", False),
    "DESKBOT_WEB_THREADS": ("控制台 waitress 线程数（默认 16）", False),
    # ---- USB / WiFi 链路 ----
    "DESKBOT_USB_SERIAL_PORTS": ("固定串口列表", False),
    "DESKBOT_SERIAL_PORTS": ("固定串口列表（旧名，兼容）", False),
    "DESKBOT_USB_SERIAL_BAUD": ("串口波特率", False),
    "DESKBOT_WIFI_LINK_PORT": ("WiFi 备用链路监听端口（默认 9020）", False),
    "DESKBOT_WIFI_LINK_ADVERTISE_HOST": ("下发给设备的 PC 地址", False),
    "DESKBOT_DISCOVERY_UDP_PORT": ("机器人找 Core 的 UDP 广播应答端口（默认 9021）", False),
    "DESKBOT_TEMP_WARN_C": ("片内温度告警阈值 ℃（默认 70）", False),
    "DESKBOT_TEMP_CRIT_C": ("片内温度严重阈值 ℃（默认 85，自动减负）", False),
    # ---- RTC / LiveKit ----
    "DESKBOT_RTC_ENABLED": ("启用 RTC", False),
    "DESKBOT_RTC_CALL_MODE": ("RTC 呼叫模式", False),
    "DESKBOT_RTC_AGENT_NAME": ("Agent 名", False),
    "DESKBOT_RTC_SDK_LOG": ("Agent SDK 日志级别", False),
    "DESKBOT_RTC_SYSTEM_PROMPT": ("语音人设覆盖", False),
    "DESKBOT_RTC_SPEECH_ADAPTER": ("语音适配器选择", False),
    "DESKBOT_RTC_TOKEN_ENDPOINT": ("RTC token 端点", False),
    "DESKBOT_RTC_TOOL_BRIDGE_URL": ("RTC 工具桥地址", False),
    "DESKBOT_RTC_TOOL_BRIDGE_TOKEN": ("RTC 工具桥令牌", True),
    "DESKBOT_RTC_WEB_SEARCH": ("语音联网开关", False),
    "DESKBOT_RTC_ARK_WEB_SEARCH_ACTIVE": ("语音端内置联网已激活（进程内）", False),
    "DESKBOT_LIVEKIT_URL": ("LiveKit 地址", False),
    "DESKBOT_LOCAL_LIVEKIT_BINARY": ("本机 livekit-server 路径", False),
    "LIVEKIT_API_KEY": ("LiveKit API Key", True),
    "LIVEKIT_API_SECRET": ("LiveKit API Secret", True),
    "LAMPGO_LIVEKIT_ALLOW_INTERRUPTIONS": ("允许打断", False),
    "DESKBOT_BARGE_IN_NEAR_END_P95": ("打断近端 P95", False),
    # ---- LLM ----
    "LLM_API_KEY": ("LLM 密钥", True),
    "LLM_BASE_URL": ("LLM 接入点", False),
    "LLM_MODEL": ("LLM 模型", False),
    "LLM_PROTOCOL": ("LLM 协议", False),
    "LLM_FIRST_TOKEN_TIMEOUT": ("首 token 超时", False),
    "ARK_MODEL": ("方舟模型（测试/兼容）", False),
    "ARK_VISION_MODEL": ("方舟视觉模型", False),
    "ARK_IMAGE_TO_SVG_MODEL": ("表情生成模型", False),
    "ARK_IMAGE_GEN_API_KEY": ("卡通表情生图密钥（留空复用 LLM 密钥）", True),
    "ARK_WEB_SEARCH_API_KEY": ("联网检索密钥（火山方舟；留空时大模型是方舟就复用）", True),
    "ARK_WEB_SEARCH_MODEL": ("联网检索模型（方舟联网插件）", False),
    "ARK_IMAGE_GEN_URL": ("卡通表情生图接口地址（Seedream）", False),
    "ARK_IMAGE_GEN_MODEL": ("卡通表情生图模型（Seedream）", False),
    "DESKBOT_CARTOON_GEN_DAILY_LIMIT": ("卡通表情生图本机日配额（张/天，默认 400）", False),
    "ARK_IMAGE_TO_SVG_THINKING": ("表情生成 thinking", False),
    "ARK_IMAGE_TO_SVG_MAX_OUTPUT_TOKENS": ("表情生成最大输出", False),
    "VOLCENGINE_LLM_MODEL": ("火山 LLM 模型（兼容）", False),
    # ---- ASR ----
    "ASR_PROVIDER": ("ASR 提供方", False),
    "ASR_API_KEY": ("ASR 密钥", True),
    "ASR_ENDPOINT": ("ASR 端点", False),
    "ASR_LANGUAGE": ("ASR 语言", False),
    "ASR_MODEL": ("ASR 模型", False),
    "ASR_MODEL_DIR": ("本地 ASR 模型目录", False),
    "ASR_RESOURCE_ID": ("ASR 资源 ID", False),
    "DESKBOT_ASR_ONNX_THREADS": ("本地 ASR 线程数", False),
    "VOLCENGINE_ASR_API_KEY": ("火山 ASR 密钥", True),
    "VOLCENGINE_ASR_ENDPOINT": ("火山 ASR 端点", False),
    "VOLCENGINE_ASR_RESOURCE_ID": ("火山 ASR 资源 ID", False),
    # ---- TTS ----
    "TTS_PROVIDER": ("TTS 提供方", False),
    "TTS_WS_URL": ("TTS WS 地址", False),
    "TTS_SPK_ID": ("TTS 音色 id", False),
    "TTS_SAMPLE_RATE": ("TTS 采样率", False),
    "DOUBAO_TTS_API_KEY": ("豆包 TTS 密钥", True),
    "DOUBAO_TTS_APP_ID": ("豆包 TTS App ID（旧）", True),
    "DOUBAO_TTS_ACCESS_TOKEN": ("豆包 TTS Access Token（旧）", True),
    "DOUBAO_TTS_SPEAKER": ("默认音色", False),
    "DOUBAO_TTS_RESOURCE_ID": ("TTS 资源 ID", False),
    "DOUBAO_TTS_MODEL": ("TTS 模型", False),
    "DOUBAO_TTS_WS_URL": ("TTS WS 地址", False),
    "DOUBAO_TTS_SAMPLE_RATE": ("TTS 采样率", False),
    "DOUBAO_TTS_FORMAT": ("TTS 音频格式", False),
    "DOUBAO_TTS_ENABLE_TIMESTAMP": ("TTS 时间戳", False),
    "DOUBAO_TTS_VOICE_CLONE_RESOURCE_ID": ("声音复刻资源 ID", False),
    "DOUBAO_TTS_VOICE_CLONE_URL": ("声音复刻接口", False),
    "DOUBAO_TTS_VOICE_STATUS_URL": ("复刻状态接口", False),
    "DOUBAO_TTS_VOICE_CLONE_SPEAKER_ID": ("复刻用音色 ID（带 key 版预置）", False),
    # ---- PB / 播放 ----
    "PB_CHUNK_MS_MAX": ("PB 分片最大时长", False),
    "PB_CHUNK_GAP_MS": ("PB 分片间隔", False),
    "PB_JSON_BIN_GAP_MS": ("PB JSON/二进制间隔", False),
    "PB_MAX_WIRE_JSON_BYTES": ("PB JSON 最大字节", False),
    "PB_WAIT_ACK": ("PB 等待 ACK", False),
    "PB_WAIT_ACK_TIMEOUT_SEC": ("PB ACK 超时", False),
    "PB_WAIT_PLAYED_MARGIN_SEC": ("PB 播放完成裕量", False),
    "PB_MAX_PCM_BIN_BYTES": ("PB 单个 PCM 二进制上限", False),
    "DESKBOT_PB_FACE_BUNDLE": ("表情包选择", False),
    "DESKBOT_PB_FACE_BUNDLE_JSON": ("表情包 JSON", False),
    "DESKBOT_PB_FACE_BUNDLE_FILE": ("表情包文件", False),
    # ---- 视觉 ----
    "CAMERA_UNDISTORT": ("相机去畸变", False),
    "CAMERA_STATE_TTL_SEC": ("相机状态缓存 TTL", False),
    "CAMERA_CALIB_JSON": ("相机标定文件", False),
    "CAMERA_NUM_FACES": ("最多人脸数", False),
    "CAMERA_MIN_FACE_DETECTION_CONFIDENCE": ("人脸检测阈值", False),
    "CAMERA_MIN_FACE_PRESENCE_CONFIDENCE": ("人脸存在阈值", False),
    "CAMERA_FACE_LANDMARKER_PATH": ("人脸关键点模型", False),
    "FACE_EMBEDDING_MODEL": ("人脸 embedding 模型", False),
    "FACE_EMBEDDING_RECOGNITION_ONNX": ("人脸识别 ONNX", False),
    "FACE_EMBEDDING_USE_CUDA": ("人脸 CUDA", False),
    # ---- 米家 ----
    "MIOT_CTL_HOME": ("米家数据目录", False),
}


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:3] + "…" if len(value) > 6 else "…"


def effective_settings(env_file_keys: Iterable[str] | None = None) -> list[dict[str, str]]:
    """每个已登记键的生效值与来源。``env_file_keys`` 是 .env 里出现过的键。"""
    file_keys = set(env_file_keys or ())
    rows: list[dict[str, str]] = []
    for name, (description, secret) in ENV_KEYS.items():
        if name == "PATH":
            continue
        raw = os.environ.get(name)
        if raw is None or raw == "":
            source, shown = "default", ""
        else:
            source = ".env" if name in file_keys else "process"
            shown = _mask(raw) if secret else raw[:80]
        rows.append({"key": name, "value": shown, "source": source, "description": description})
    return rows


def log_effective_settings() -> None:
    """启动时打印非默认的设置（敏感值遮蔽）——排查"到底哪份配置生效"用。"""
    file_keys: set[str] = set()
    try:
        parsed = read_env_file()
        file_keys = set(parsed.keys()) if isinstance(parsed, dict) else set()
    except Exception:  # noqa: BLE001
        pass
    rows = [r for r in effective_settings(file_keys) if r["source"] != "default"]
    if not rows:
        logger.info("[config] 无非默认环境配置")
        return
    logger.info("[config] 生效的非默认配置 %d 项：", len(rows))
    for row in rows:
        logger.info("[config]   %-38s = %-40s (%s)", row["key"], row["value"], row["source"])


__all__ = ["ENV_KEYS", "effective_settings", "log_effective_settings"]
