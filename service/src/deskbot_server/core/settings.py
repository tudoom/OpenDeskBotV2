from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Optional


def _env_bool(name: str) -> Optional[bool]:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _bool_setting(
    config: dict[str, Any],
    *,
    env_name: str,
    key: str,
    legacy_env_name: str,
    legacy_key: str,
    default: bool,
) -> bool:
    """Resolve a public setting while accepting one hidden legacy spelling."""

    for candidate in (env_name, legacy_env_name):
        value = _env_bool(candidate)
        if value is not None:
            return value
    if key in config:
        return bool(config[key])
    if legacy_key in config:
        return bool(config[legacy_key])
    return default


def _asr_float_setting(
    asr: dict[str, Any],
    *,
    env_name: str,
    key: str,
    default: float,
) -> float:
    raw = os.environ.get(env_name)
    value = raw if raw is not None and raw.strip() else asr.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        from deskbot_server.core.ports.asr import AsrConfigurationError

        raise AsrConfigurationError(f"{env_name} 必须是数字。") from exc
    if not math.isfinite(parsed):
        from deskbot_server.core.ports.asr import AsrConfigurationError

        raise AsrConfigurationError(f"{env_name} 必须是有限数字。")
    return parsed


def _asr_int_setting(
    asr: dict[str, Any],
    *,
    env_name: str,
    key: str,
    default: int,
) -> int:
    raw = os.environ.get(env_name)
    value = raw if raw is not None and raw.strip() else asr.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        from deskbot_server.core.ports.asr import AsrConfigurationError

        raise AsrConfigurationError(f"{env_name} 必须是整数。") from exc


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 9000
    ws_ping_interval: Optional[float] = 20.0
    ws_ping_timeout: float = 20.0
    device_pb_only: bool = True
    minimal_device_downlink: bool = False
    web_public_host: str = ""
    # 0 = 启动时按 CPU 核数推导（见 core/concurrency.py）
    max_concurrent_asr: int = 0
    max_concurrent_face_infer: int = 0


@dataclass(frozen=True)
class AudioSettings:
    input_codec: str = "opus"
    output_codec: str = "opus"
    sample_rate: int = 16000
    channels: int = 1


@dataclass(frozen=True)
class RtcSettings:
    enabled: bool = True
    livekit_url: str = "http://127.0.0.1:7880"
    token_endpoint: str = "http://127.0.0.1:18790/rtc/token"
    agent_name: str = "lampgo-jarvis"
    local_server_enabled: bool = True
    local_server_binary: str = ""
    # Safe default: suppress microphone uplink while the speaker is audible.
    # ``esp32_aec`` is opt-in until device AEC has been acoustically calibrated.
    call_mode: str = "stable"


@dataclass(frozen=True)
class AsrTextFilterSettings:
    min_text_len: int = 2
    min_chinese_ratio: float = 0.0


@dataclass(frozen=True)
class AsrSettings:
    provider: str = "doubao_streaming"
    endpoint: str = "https://api.openai.com/v1/audio/transcriptions"
    model: str = "whisper-1"
    timeout_seconds: float = 30.0
    max_audio_bytes: int = 10 * 1024 * 1024
    model_dir: str = ""
    hub: str = "hf"
    language: str = "zh"
    use_quant_onnx: bool = True
    onnx_intra_op_threads: int = 4
    text_filter: AsrTextFilterSettings = field(default_factory=AsrTextFilterSettings)


@dataclass(frozen=True)
class LlmSettings:
    base_url: str = ""
    model_name: str = ""
    system_prompt: str = ""


@dataclass(frozen=True)
class TtsSettings:
    provider: str = "doubao"
    ws_url: str = ""
    lang: str = "zh"
    spk_id: int = 0
    sample_rate: int = 16000
    pb_face_bundle_json: str = ""
    pb_face_bundle_file: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppSettings:
    """统一运行时配置：YAML + 环境变量覆盖。"""

    server: ServerSettings
    audio: AudioSettings
    rtc: RtcSettings
    asr: AsrSettings
    llm: LlmSettings
    tts: TtsSettings
    camera_face: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> AppSettings:
        srv = dict(config.get("server") or {})
        audio = dict(config.get("audio") or {})
        rtc = dict(config.get("rtc") or {})
        asr = dict(config.get("asr") or {})
        llm = dict(config.get("llm") or {})
        tts = dict(config.get("tts") or {})
        tf = dict(asr.get("text_filter") or {})

        pb_only = _bool_setting(
            srv,
            env_name="DESKBOT_DEVICE_PB_ONLY",
            key="device_pb_only",
            legacy_env_name="DESKBOT_ASR_CHAT_DEVICE_PB_ONLY",
            legacy_key="asr_chat_device_pb_only",
            default=True,
        )

        minimal = _bool_setting(
            srv,
            env_name="DESKBOT_MINIMAL_DEVICE_DOWNLINK",
            key="minimal_device_downlink",
            legacy_env_name="DESKBOT_ASR_CHAT_MINIMAL_DOWNLINK",
            legacy_key="asr_chat_minimal_device_downlink",
            default=False,
        )

        if os.environ.get("TTS_PROVIDER"):
            tts["provider"] = os.environ["TTS_PROVIDER"]
        if os.environ.get("TTS_WS_URL"):
            tts["ws_url"] = os.environ["TTS_WS_URL"]
        if os.environ.get("TTS_SPK_ID"):
            tts["spk_id"] = int(os.environ["TTS_SPK_ID"])
        if os.environ.get("TTS_SAMPLE_RATE"):
            tts["sample_rate"] = int(os.environ["TTS_SAMPLE_RATE"])

        tts_extra = {
            k: v
            for k, v in tts.items()
            if k
            not in (
                "provider",
                "ws_url",
                "lang",
                "spk_id",
                "sample_rate",
                "pb_face_bundle_json",
                "pb_face_bundle_file",
            )
        }

        return cls(
            server=ServerSettings(
                host=os.environ.get("DESKBOT_SERVER_HOST")
                or str(srv.get("host", "127.0.0.1")),
                port=int(os.environ.get("DESKBOT_SERVER_PORT") or srv.get("port", 9000)),
                ws_ping_interval=_parse_ping_interval(
                    os.environ.get("DESKBOT_WS_PING_INTERVAL"),
                    srv.get("ws_ping_interval", 20),
                ),
                ws_ping_timeout=float(
                    os.environ.get("DESKBOT_WS_PING_TIMEOUT") or srv.get("ws_ping_timeout", 20)
                ),
                device_pb_only=pb_only,
                minimal_device_downlink=minimal,
                web_public_host=str(srv.get("web_public_host") or ""),
                max_concurrent_asr=int(srv.get("max_concurrent_asr", 0)),
                max_concurrent_face_infer=int(srv.get("max_concurrent_face_infer", 0)),
            ),
            audio=AudioSettings(
                input_codec=str(audio.get("input_codec", "opus")),
                output_codec=str(audio.get("output_codec", "opus")),
                sample_rate=int(audio.get("sample_rate", 16000)),
                channels=int(audio.get("channels", 1)),
            ),
            rtc=RtcSettings(
                enabled=(
                    _env_bool("DESKBOT_RTC_ENABLED")
                    if _env_bool("DESKBOT_RTC_ENABLED") is not None
                    else bool(rtc.get("enabled", True))
                ),
                livekit_url=str(
                    os.environ.get("DESKBOT_LIVEKIT_URL")
                    or rtc.get("livekit_url")
                    or "http://127.0.0.1:7880"
                ).strip(),
                token_endpoint=str(
                    os.environ.get("DESKBOT_RTC_TOKEN_ENDPOINT")
                    or rtc.get("token_endpoint")
                    or "http://127.0.0.1:18790/rtc/token"
                ).strip(),
                agent_name=str(
                    os.environ.get("DESKBOT_RTC_AGENT_NAME")
                    or rtc.get("agent_name")
                    or "lampgo-jarvis"
                ).strip(),
                call_mode=str(
                    os.environ.get("DESKBOT_RTC_CALL_MODE")
                    or rtc.get("call_mode")
                    or "esp32_aec"
                ).strip().lower().replace("-", "_"),
                local_server_enabled=(
                    _env_bool("DESKBOT_LOCAL_LIVEKIT_ENABLED")
                    if _env_bool("DESKBOT_LOCAL_LIVEKIT_ENABLED") is not None
                    else bool(rtc.get("local_server_enabled", True))
                ),
                local_server_binary=str(
                    os.environ.get("DESKBOT_LOCAL_LIVEKIT_BINARY")
                    or rtc.get("local_server_binary")
                    or ""
                ).strip(),
            ),
            asr=AsrSettings(
                provider=str(
                    os.environ.get("ASR_PROVIDER")
                    or asr.get("provider")
                    or "doubao_streaming"
                ).strip().lower(),
                endpoint=str(
                    os.environ.get("ASR_ENDPOINT")
                    or asr.get("endpoint")
                    or "https://api.openai.com/v1/audio/transcriptions"
                ).strip(),
                model=str(
                    os.environ.get("ASR_MODEL")
                    or asr.get("model")
                    or "whisper-1"
                ).strip(),
                timeout_seconds=max(
                    1.0,
                    _asr_float_setting(
                        asr,
                        env_name="ASR_TIMEOUT_SECONDS",
                        key="timeout_seconds",
                        default=30,
                    ),
                ),
                max_audio_bytes=max(
                    1024,
                    _asr_int_setting(
                        asr,
                        env_name="ASR_MAX_AUDIO_BYTES",
                        key="max_audio_bytes",
                        default=10 * 1024 * 1024,
                    ),
                ),
                model_dir=str(asr.get("model_dir", "")),
                hub=str(asr.get("hub", "hf")),
                language=str(
                    os.environ.get("ASR_LANGUAGE")
                    or asr.get("language", "zh")
                ).strip(),
                use_quant_onnx=bool(asr.get("use_quant_onnx", True)),
                onnx_intra_op_threads=max(
                    1, int(asr.get("onnx_intra_op_threads", 4))
                ),
                text_filter=AsrTextFilterSettings(
                    min_text_len=int(tf.get("min_text_len", 2)),
                    min_chinese_ratio=float(tf.get("min_chinese_ratio", 0.2)),
                ),
            ),
            llm=LlmSettings(
                base_url=str(llm.get("base_url", "")),
                model_name=str(llm.get("model_name", "")),
                system_prompt=str(llm.get("system_prompt", "")),
            ),
            tts=TtsSettings(
                provider=str(tts.get("provider") or "doubao").strip().lower(),
                ws_url=str(tts.get("ws_url", "")),
                lang=str(tts.get("lang", "zh")),
                spk_id=int(tts.get("spk_id", 0)),
                sample_rate=int(tts.get("sample_rate", 16000)),
                pb_face_bundle_json=str(tts.get("pb_face_bundle_json") or ""),
                pb_face_bundle_file=str(tts.get("pb_face_bundle_file") or ""),
                extra=tts_extra,
            ),
            camera_face=dict(config.get("camera_face") or {}),
            raw=config,
        )

    @property
    def tts_cfg(self) -> dict[str, Any]:
        """兼容旧代码对 dict 形式 tts 配置的访问。"""
        base = {
            "provider": self.tts.provider,
            "ws_url": self.tts.ws_url,
            "lang": self.tts.lang,
            "spk_id": self.tts.spk_id,
            "sample_rate": self.tts.sample_rate,
            "pb_face_bundle_json": self.tts.pb_face_bundle_json,
            "pb_face_bundle_file": self.tts.pb_face_bundle_file,
        }
        base["output_codec"] = self.audio.output_codec
        base.update(self.tts.extra)
        return base


def _parse_ping_interval(env_val: Optional[str], cfg_val: Any) -> Optional[float]:
    raw = env_val if env_val is not None else str(cfg_val)
    raw = str(raw).strip().lower()
    if raw in ("0", "none", "off", "false"):
        return None
    return max(5.0, float(raw))
