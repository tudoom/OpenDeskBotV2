"""Lifecycle manager for the team-owned LampGo LiveKit Agent SDK."""

from __future__ import annotations

import asyncio
import getpass
import importlib.util
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from deskbot_server.core.settings import AppSettings

logger = logging.getLogger("deskbot-server")

AGENT_SDK_PORT = 18790
AGENT_SDK_PACKAGE = "lampgo-livekit-agent-sdk"
AGENT_SDK_MODULE = "lampgo_livekit_agent"
AGENT_SDK_BINARIES = ("lampgo-livekit-agent",)
AGENT_SDK_ENTRY_MODULE = "deskbot_server.rtc_agent_entry"
# On Windows, the first import of LiveKit, AV and Silero in the SDK's spawned
# HTTP/worker processes takes roughly 20-30 seconds even before registration.
# Cold Windows starts can spend about a minute importing the HTTP process and
# its spawned LiveKit/Silero workers before registration even begins.  Keep a
# two-minute lifecycle deadline; the health loop still returns immediately on
# success or a real process/bind failure.
_SDK_READY_TIMEOUT_SEC = 120.0
_SDK_HEALTH_TIMEOUT_SEC = 1.0
_RECOVERY_INITIAL_DELAY_SEC = 1.0
_RECOVERY_MAX_DELAY_SEC = 30.0
# The orphan-tracking psutil sweep walks the whole system process table; it
# only feeds ``_known_sdk_pids`` cleanup and does not need the 0.5s cadence of
# parent-exit detection.
_SDK_CHILD_SCAN_INTERVAL_SEC = 2.5
_BIND_FAILURE_MARKERS = (
    "address already in use",
    "errno 48",
    "errno 98",
    "winerror 10048",
)
_WORKER_FATAL_MARKERS = (
    "failed to connect to livekit after",
    "error in _connection_task",
)
_LOCAL_NO_PROXY_HOSTS = (
    "127.0.0.1",
    "localhost",
    "::1",
    "0.0.0.0",
    ".local",
    "192.168.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
)

_SITECUSTOMIZE_CODE = r'''
import asyncio
import logging
import os
import sys
import time

logging.basicConfig(
    level=os.environ.get("DESKBOT_RTC_SDK_LOG", "INFO"),
    format="[%(levelname)s][%(name)s][pid=%(process)d] %(message)s",
    stream=sys.stderr,
    force=True,
)

try:
    from livekit.plugins import silero as _deskbot_silero

    _original_silero_vad_load = _deskbot_silero.VAD.load

    def _deskbot_silero_vad_load(*args, **kwargs):
        # ESP-SR has already removed echo/noise on the device.  The stock 0.5
        # activation threshold was unnecessarily discarding quiet, short
        # phrases after AFE; keep Silero as the Agent endpoint detector with a
        # more suitable clean-speech profile.
        kwargs.setdefault("min_speech_duration", 0.08)
        kwargs.setdefault("min_silence_duration", 0.30)
        kwargs.setdefault("prefix_padding_duration", 0.30)
        kwargs.setdefault("activation_threshold", 0.20)
        kwargs.setdefault("deactivation_threshold", 0.10)
        return _original_silero_vad_load(*args, **kwargs)

    _deskbot_silero.VAD.load = staticmethod(_deskbot_silero_vad_load)
    print(
        "[deskbot-rtc] Silero endpoint profile activation=0.20 "
        "deactivation=0.10 silence=0.30s",
        file=sys.stderr,
        flush=True,
    )
except Exception as exc:
    print(
        "[deskbot-rtc] Silero endpoint profile failed: %r" % (exc,),
        file=sys.stderr,
        flush=True,
    )

try:
    from deskbot_server.rtc_livekit_plugins import install_lampgo_speech_adapters
    installed = install_lampgo_speech_adapters()
    if (
        os.environ.get("DESKBOT_RTC_SPEECH_ADAPTER", "").strip().lower()
        == "deskbot_api_key"
        and not installed
    ):
        raise RuntimeError("required Deskbot Seed Speech adapters were not installed")
    print(
        "[deskbot-rtc] Seed Speech adapters active=%s pid=%d"
        % (installed, os.getpid()),
        file=sys.stderr,
        flush=True,
    )
except Exception as exc:
    print(
        "[deskbot-rtc] speech adapter setup failed: %r" % (exc,),
        file=sys.stderr,
        flush=True,
    )
    if (
        os.environ.get("DESKBOT_RTC_SPEECH_ADAPTER", "").strip().lower()
        == "deskbot_api_key"
    ):
        raise SystemExit(78) from exc

try:
    from deskbot_server.rtc_worker_tools import install_lampgo_deskbot_tools
    tools_installed = install_lampgo_deskbot_tools()
    print(
        "[deskbot-rtc] Core tools active=%s pid=%d"
        % (tools_installed, os.getpid()),
        file=sys.stderr,
        flush=True,
    )
except Exception as exc:
    print(
        "[deskbot-rtc] Core tool setup failed: %r" % (exc,),
        file=sys.stderr,
        flush=True,
    )
    if os.environ.get("DESKBOT_RTC_TOOL_BRIDGE_TOKEN", "").strip():
        raise SystemExit(78) from exc

try:
    from livekit.agents.voice.agent_session import AgentSession
    from deskbot_server.rtc_barge_in import (
        BARGE_IN_TOPIC,
        PlaybackEchoGuard,
        SPEECH_ACTIVITY_TOPIC,
        USER_SPEECH_CONFIRMED_END_PAYLOAD,
        USER_SPEECH_START_PAYLOAD,
        is_explicit_barge_in,
        should_confirm_barge_in,
    )

    _original_agent_session_init = AgentSession.__init__
    _allow_interruptions = (
        os.environ.get("LAMPGO_LIVEKIT_ALLOW_INTERRUPTIONS", "1")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"}
    )

    def _deskbot_agent_session_init(self, *args, **kwargs):
        kwargs.setdefault("allow_interruptions", _allow_interruptions)
        # Keep the injected worker fallback aligned with the primary
        # rtc_livekit_plugins latency contract.  Either patch may wrap
        # AgentSession first depending on worker import order.
        kwargs.setdefault("preemptive_generation", True)
        kwargs.setdefault("min_endpointing_delay", 0.08)
        kwargs.setdefault("max_endpointing_delay", 0.45)
        if _allow_interruptions:
            # ESP-SR is already feeding echo-cancelled audio, so a multi-second
            # software AEC warmup would unnecessarily swallow barge-in speech.
            kwargs.setdefault("aec_warmup_duration", None)
        result = _original_agent_session_init(self, *args, **kwargs)
        guard = getattr(self, "_deskbot_playback_echo_guard", None)
        if guard is None:
            guard = PlaybackEchoGuard()
            self._deskbot_playback_echo_guard = guard
            for adapter in (kwargs.get("stt"), kwargs.get("tts")):
                bind_guard = getattr(adapter, "bind_playback_echo_guard", None)
                if callable(bind_guard):
                    bind_guard(guard)
        if not hasattr(self, "_deskbot_control_publish_lock"):
            self._deskbot_control_publish_lock = asyncio.Lock()
        return result

    AgentSession.__init__ = _deskbot_agent_session_init

    # rtc_livekit_plugins normally installs this control path first. Keep a
    # local fallback in the injected worker patch so SDK lifecycle changes can
    # never silently remove confirmed barge-in delivery.
    _original_agent_session_start = AgentSession.start
    if not getattr(_original_agent_session_start, "_deskbot_barge_in_control", False):
        async def _deskbot_agent_session_start(self, *args, **kwargs):
            room = kwargs.get("room")
            self._deskbot_barge_in_room = room
            self._deskbot_agent_state = "initializing"
            self._deskbot_last_speaking_mono = 0.0

            def _queue_fixed_controls(packets, label):
                if room is None or not packets:
                    return

                async def _publish_controls():
                    async with self._deskbot_control_publish_lock:
                        for topic, payload in packets:
                            await room.local_participant.publish_data(
                                payload,
                                reliable=True,
                                topic=topic,
                            )

                asyncio.create_task(
                    _publish_controls(),
                    name="deskbot-rtc-%s-control" % label,
                )

            @self.on("agent_state_changed")
            def _deskbot_agent_state_changed(event):
                now = time.monotonic()
                old_state = str(getattr(event, "old_state", "") or "")
                new_state = str(getattr(event, "new_state", "") or "")
                self._deskbot_agent_state = new_state
                if old_state == "speaking" or new_state == "speaking":
                    self._deskbot_last_speaking_mono = now
                self._deskbot_playback_echo_guard.set_playback_active(
                    new_state == "speaking",
                    now=now,
                )

            @self.on("user_state_changed")
            def _deskbot_user_state_changed(event):
                new_state = str(getattr(event, "new_state", "") or "")
                if new_state == "speaking":
                    _queue_fixed_controls(
                        ((SPEECH_ACTIVITY_TOPIC, USER_SPEECH_START_PAYLOAD),),
                        "speech-start",
                    )

            @self.on("user_input_transcribed")
            def _deskbot_user_input_transcribed(event):
                transcript = str(getattr(event, "transcript", "") or "").strip()
                if not getattr(event, "is_final", False) or not transcript:
                    return
                now = time.monotonic()
                playback_recent = not (
                    self._deskbot_agent_state != "speaking"
                    and now - self._deskbot_last_speaking_mono > 1.2
                )
                is_echo, _similarity = (
                    self._deskbot_playback_echo_guard.classify(
                        transcript,
                        now=now,
                    )
                )
                # The Seed STT adapter only emits playback-time final text
                # after echo and sustained-speech validation. The event has
                # no duration field, so repeating should_confirm_barge_in()
                # here would reject every ordinary interruption.
                confirmed_barge_in = bool(playback_recent and not is_echo)
                packets = []
                if confirmed_barge_in:
                    try:
                        self.interrupt()
                    except RuntimeError:
                        pass
                    packets.append((BARGE_IN_TOPIC, b"cancel"))
                packets.append(
                    (
                        SPEECH_ACTIVITY_TOPIC,
                        USER_SPEECH_CONFIRMED_END_PAYLOAD,
                    )
                )
                _queue_fixed_controls(
                    tuple(packets),
                    (
                        "barge-in-and-speech-end"
                        if confirmed_barge_in
                        else "speech-end"
                    ),
                )

            return await _original_agent_session_start(self, *args, **kwargs)

        _deskbot_agent_session_start._deskbot_barge_in_control = True
        AgentSession.start = _deskbot_agent_session_start
    print(
        "[deskbot-rtc] interruptions=%s pid=%d"
        % (_allow_interruptions, os.getpid()),
        file=sys.stderr,
        flush=True,
    )
except Exception as exc:
    print(
        "[deskbot-rtc] interruption patch failed: %r" % (exc,),
        file=sys.stderr,
        flush=True,
    )

'''


@dataclass(frozen=True)
class _SDKPortOwner:
    listener_pid: int
    root_pid: int
    process_name: str


def _command_runs_agent_sdk(command: list[str]) -> bool:
    normalized = [str(token).strip().casefold() for token in command]
    for index, token in enumerate(normalized[:-1]):
        if token == "-m" and normalized[index + 1] == AGENT_SDK_ENTRY_MODULE:
            return True
    expected = {item.casefold() for item in AGENT_SDK_BINARIES}
    for token in command:
        executable = str(token).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        if executable in expected:
            return True
    return False


def _livekit_ws_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("https://"):
        return "wss://" + raw[8:]
    if raw.startswith("http://"):
        return "ws://" + raw[7:]
    return raw


def _livekit_http_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("wss://"):
        return "https://" + raw[6:]
    if raw.startswith("ws://"):
        return "http://" + raw[5:]
    return raw


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _stable_python_executable() -> str:
    """Return the version-stable interpreter path, or sys.executable.

    The desktop launcher copies the portable Python runtime to a path that does
    not change between upgrades and exports its parent as DESKBOT_STABLE_BIN_DIR.
    Using that python.exe keeps the RTC agent's socket-binding processes on a
    firewall-stable image path. Falls back to sys.executable outside the desktop
    client (developer runs, tests) where the variable is unset.
    """

    stable_bin_dir = str(os.environ.get("DESKBOT_STABLE_BIN_DIR") or "").strip()
    if stable_bin_dir:
        candidate = Path(stable_bin_dir) / "python" / "python.exe"
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
    return sys.executable


def _allow_agent_interruptions(call_mode: str) -> bool:
    """Enable Agent barge-in when the selected mode has an AEC-safe uplink.

    ``esp32_aec`` is negotiated per connected device by the RTC gateway. A
    device without the required AEC/full-duplex capabilities is downgraded to
    ``stable`` before audio starts, so only the two interruptible modes reach
    this Agent setting.
    """

    return str(call_mode or "").strip().lower() in {
        "interruptible",
        "esp32_aec",
    }


class RtcAgentSdkManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task | None = None
        self.process_watch_task: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self.roles_path: Path | None = None
        self.patch_dir: Path | None = None
        self._ready_event = asyncio.Event()
        self._startup_failed_event = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._last_error = ""
        self._speech_adapter = ""
        self._shutdown_requested = False
        self._worker_registered_mono = 0.0
        self._recovery_attempt = 0
        self._known_sdk_pids: set[int] = set()
        self._local_livekit_api_key = ""
        self._local_livekit_api_secret = ""
        server = getattr(settings, "server", None)
        bridge_port = int(getattr(server, "port", 9000) or 9000)
        self._tool_bridge_url = (
            f"http://127.0.0.1:{bridge_port}/internal/rtc/tools"
        )
        self._tool_bridge_token = secrets.token_urlsafe(32)

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def is_ready(self) -> bool:
        return self.is_running and self._ready_event.is_set()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "status": (
                "ready" if self.is_ready else
                "recovering" if self.recovery_task is not None and not self.recovery_task.done() else
                "starting" if self.is_running else "offline"
            ),
            "process_running": self.is_running,
            "worker_registered": self.is_running and self._ready_event.is_set(),
            "recovery_attempt": self._recovery_attempt,
            "last_error": self._last_error,
            "registered_age_seconds": (
                round(max(0.0, time.monotonic() - self._worker_registered_mono), 1)
                if self._worker_registered_mono else None
            ),
        }

    @property
    def token_endpoint(self) -> str:
        return f"http://127.0.0.1:{AGENT_SDK_PORT}/rtc/token"

    @property
    def tool_bridge_token(self) -> str:
        return self._tool_bridge_token

    @property
    def tool_bridge_url(self) -> str:
        return self._tool_bridge_url

    def configure_tool_bridge(self, *, scheme: str, port: int) -> None:
        requested_scheme = str(scheme or "http").strip().lower()
        normalized_scheme = {
            "http": "http",
            "https": "https",
            "ws": "http",
            "wss": "https",
        }.get(requested_scheme)
        if normalized_scheme is None:
            raise ValueError(
                "RTC tool bridge scheme must be http, https, ws, or wss"
            )
        bridge_port = max(1, min(65535, int(port)))
        self._tool_bridge_url = (
            f"{normalized_scheme}://127.0.0.1:{bridge_port}/internal/rtc/tools"
        )

    def configure_local_livekit(self, *, api_key: str, api_secret: str) -> None:
        """Use LampGo's native local mode instead of cloud token exchange."""

        key = str(api_key or "").strip()
        secret = str(api_secret or "").strip()
        if not key or not secret:
            raise ValueError("local LiveKit api_key and api_secret are required")
        self._local_livekit_api_key = key
        self._local_livekit_api_secret = secret

    def _set_last_error(self, value: str) -> None:
        self._last_error = str(value or "")

    def _resolve_speech_credentials(self) -> tuple[str, str, str]:
        asr_api_key = _first_env("VOLCENGINE_ASR_API_KEY", "ASR_API_KEY")
        tts_api_key = _first_env(
            "DOUBAO_TTS_API_KEY",
            "VOLCENGINE_TTS_API_KEY",
            "SEED_TTS_API_KEY",
            "BYTEPLUS_SEED_SPEECH_API_KEY",
        )
        if asr_api_key and tts_api_key:
            return "deskbot_api_key", "deskbot-api-key-adapter", "deskbot-api-key-adapter"

        app_id = _first_env(
            "DOUBAO_ASR_APP_ID",
            "DOUBAO_TTS_APP_ID",
            "VOLCENGINE_APP_ID",
        )
        access_token = _first_env(
            "DOUBAO_ASR_ACCESS_TOKEN",
            "DOUBAO_TTS_ACCESS_TOKEN",
            "VOLCENGINE_ACCESS_TOKEN",
        )
        if app_id and access_token:
            return "native", app_id, access_token
        return "", "", ""

    def _can_start(self) -> bool:
        self._set_last_error("")
        missing: list[str] = []
        if not self.settings.rtc.livekit_url:
            missing.append("rtc.livekit_url")
        if not self.settings.rtc.token_endpoint:
            missing.append("rtc.token_endpoint")
        if not self.settings.rtc.agent_name:
            missing.append("rtc.agent_name")

        llm_key = _first_env("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY")
        llm_base = _first_env("LLM_BASE_URL") or self.settings.llm.base_url
        llm_model = _first_env("LLM_MODEL") or self.settings.llm.model_name
        if not llm_key:
            missing.append("LLM_API_KEY")
        if not str(llm_base or "").strip():
            missing.append("LLM_BASE_URL")
        if not str(llm_model or "").strip():
            missing.append("LLM_MODEL")

        adapter, _app_id, _token = self._resolve_speech_credentials()
        if not adapter:
            missing.append(
                "ASR_API_KEY + DOUBAO_TTS_API_KEY "
                "(or legacy VOLCENGINE_APP_ID + VOLCENGINE_ACCESS_TOKEN)"
            )
        if missing:
            self._set_last_error("Missing RTC configuration: " + ", ".join(missing))
            logger.error("[rtc] %s", self._last_error)
            return False
        self._speech_adapter = adapter

        if importlib.util.find_spec(AGENT_SDK_MODULE) is None:
            self._set_last_error(f"{AGENT_SDK_PACKAGE} package is not installed")
            logger.error("[rtc] %s", self._last_error)
            return False
        return self._local_livekit_server_reachable()

    def _local_livekit_server_reachable(self) -> bool:
        parsed = urlparse(self.settings.rtc.livekit_url)
        host = parsed.hostname or ""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return True
        port = parsed.port or (443 if parsed.scheme in {"wss", "https"} else 80)
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            self._set_last_error(
                f"LiveKit is not reachable at {self.settings.rtc.livekit_url}"
            )
            logger.error("[rtc] %s", self._last_error)
            return False

    def _rtc_system_prompt(self) -> str:
        override = str(os.environ.get("DESKBOT_RTC_SYSTEM_PROMPT") or "").strip()
        base = override or (
            "你是“小歪”，一个住在桌面上的活泼、温柔、偶尔俏皮的小型陪伴机器人。"
            "使用自然、简短的中文口语回答，像熟悉的朋友；不要客套开场。"
            "只输出要直接说给用户听的话，不输出 JSON、Markdown、表情符号或动作说明，"
            "通常控制在五十个汉字以内。不确定时坦率说不确定，不要编造。"
        )
        return base + (
            "涉及当前环境、眼前画面、用户手中物体、颜色、人数等视觉事实时，"
            "必须调用 capture_and_describe 获取当前照片后回答，绝不能凭空猜测，"
            "也不要用只返回元数据的 capture_camera。普通对话不要拍照。"
            "照片仅供当前一轮回答使用，不得声称会记住或长期保存图片。"
        )

    def _roles(self) -> dict:
        adapter, volc_app_id, volc_token = self._resolve_speech_credentials()
        if not adapter:
            raise RuntimeError("RTC speech credentials disappeared during startup")
        llm_api_key = _first_env(
            "ARK_API_KEY",
            "LLM_API_KEY",
            "OPENAI_API_KEY",
        )
        llm_base = _first_env("LLM_BASE_URL") or self.settings.llm.base_url
        llm_model = _first_env("LLM_MODEL") or self.settings.llm.model_name
        livekit: dict[str, str] = {
            "url": _livekit_ws_url(self.settings.rtc.livekit_url),
        }
        cloud: dict[str, object] = {}
        if self._local_livekit_api_key and self._local_livekit_api_secret:
            livekit.update(
                {
                    "api_key": self._local_livekit_api_key,
                    "api_secret": self._local_livekit_api_secret,
                }
            )
        else:
            livekit_http = _livekit_http_url(self.settings.rtc.livekit_url)
            cloud = {
                "platform": {
                    "rtc_token_endpoint": self.settings.rtc.token_endpoint,
                    "agent_token_endpoint": livekit_http.rstrip("/") + "/agent/token",
                    "rtc_token_api_key": _first_env("DESKBOT_RTC_TOKEN_API_KEY")
                    or "livekit-token",
                },
                "agent": {
                    "name_prefix": "deskbot-agent",
                    "registration_token": _first_env(
                        "DESKBOT_RTC_AGENT_REGISTRATION_TOKEN"
                    )
                    or "livekit-token",
                },
            }
        roles = {
            "version": 1,
            "livekit": livekit,
            "providers": {
                "deskbot_llm": {
                    "type": "openai",
                    "api_key": llm_api_key,
                    "base_url": llm_base,
                },
                "volc_main": {
                    "type": "volcengine",
                    # LampGo 0.3 validates these legacy fields before our
                    # worker-level API-key adapters are installed.
                    "app_id": volc_app_id,
                    "access_token": volc_token,
                },
            },
            "defaults": {
                "stt": {
                    "provider": "volc_main",
                    "options": {
                        "resource_id": _first_env("VOLCENGINE_ASR_RESOURCE_ID")
                        or "volc.seedasr.sauc.duration",
                        "sample_rate": 16000,
                        "result_type": "single",
                    },
                },
                "llm": {
                    "provider": "deskbot_llm",
                    "options": {
                        "model": llm_model,
                        "temperature": 0.3,
                        "max_tokens": 160,
                        # LiveKit's OpenAI adapter forwards provider-specific
                        # fields through extra_body. Passing ``thinking`` at
                        # the top level crashes LLM construction before the
                        # RTC job can attach to the room.
                        "extra_body": {"thinking": {"type": "disabled"}},
                    },
                },
                "tts": {
                    "provider": "volc_main",
                    "options": {
                        "cluster": _first_env("VOLCENGINE_TTS_CLUSTER")
                        or "volcano_tts",
                        "voice": _first_env(
                            "DOUBAO_TTS_SPEAKER",
                            "DOUBAO_TTS_VOICE",
                            "VOLCENGINE_TTS_VOICE",
                        )
                        or "zh_female_vv_uranus_bigtts",
                        "sample_rate": int(
                            _first_env("DOUBAO_TTS_SAMPLE_RATE") or 16000
                        ),
                        "streaming": True,
                    },
                },
            },
            "voice_agents": [
                {
                    "name": self.settings.rtc.agent_name,
                    "display_name": "Deskbot realtime voice assistant",
                    "llm": {"system_prompt": self._rtc_system_prompt()},
                }
            ],
        }
        roles.update(cloud)
        return roles

    @staticmethod
    def _sdk_command() -> list[str]:
        # Console-script launchers created by pip contain an absolute shebang
        # on Windows.  The module form works in both a venv and the portable
        # desktop runtime, and preserves normal multiprocessing semantics.
        #
        # The Agent's HTTP/worker processes are the python.exe that binds the
        # RTC media sockets, so the interpreter path must be stable across
        # upgrades for the firewall allow-rule to keep matching.  Normally
        # sys.executable is already the launcher's version-stable python (Core
        # is started from it), but when DESKBOT_STABLE_BIN_DIR names an existing
        # stable interpreter, prefer it explicitly so the agent never inherits a
        # per-version path even if Core itself was started differently.
        return [_stable_python_executable(), "-u", "-m", AGENT_SDK_ENTRY_MODULE]

    @staticmethod
    def _psutil():
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError(
                "psutil is required to inspect the RTC Agent SDK port"
            ) from exc
        return psutil

    @staticmethod
    def _process_is_current_user(process) -> bool:
        try:
            if hasattr(os, "getuid"):
                return int(process.uids().real) == os.getuid()
            process_user = process.username().replace("/", "\\").rsplit("\\", 1)[-1]
            current_user = getpass.getuser().replace("/", "\\").rsplit("\\", 1)[-1]
            return process_user.casefold() == current_user.casefold()
        except Exception:
            return False

    def _find_port_listener_pid(self) -> int | None:
        psutil = self._psutil()
        listeners: set[int] = set()
        unknown = False
        try:
            connections = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, OSError) as exc:
            raise RuntimeError(
                f"cannot inspect owner of TCP port {AGENT_SDK_PORT}"
            ) from exc
        for connection in connections:
            if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                continue
            local_port = getattr(connection.laddr, "port", None)
            if local_port is None and len(connection.laddr) >= 2:
                local_port = connection.laddr[1]
            if local_port != AGENT_SDK_PORT:
                continue
            if connection.pid is None:
                unknown = True
            else:
                listeners.add(int(connection.pid))
        if unknown or len(listeners) > 1:
            raise RuntimeError(
                f"cannot determine unique owner of TCP port {AGENT_SDK_PORT}"
            )
        if listeners:
            return next(iter(listeners))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", AGENT_SDK_PORT))
            except OSError as exc:
                raise RuntimeError(
                    f"TCP port {AGENT_SDK_PORT} is occupied by an uninspectable process"
                ) from exc
        return None

    def _identify_sdk_port_owner(self, listener_pid: int) -> _SDKPortOwner | None:
        psutil = self._psutil()
        try:
            current = psutil.Process(listener_pid)
            if (
                listener_pid in self._known_sdk_pids
                and self._process_is_current_user(current)
            ):
                return _SDKPortOwner(
                    listener_pid=listener_pid,
                    root_pid=listener_pid,
                    process_name=current.name(),
                )
            seen: set[int] = set()
            while current.pid not in seen and len(seen) < 10:
                seen.add(current.pid)
                if self._process_is_current_user(current):
                    try:
                        command = current.cmdline()
                    except psutil.Error:
                        command = []
                    if _command_runs_agent_sdk(command):
                        return _SDKPortOwner(
                            listener_pid=listener_pid,
                            root_pid=int(current.pid),
                            process_name=current.name(),
                        )
                parent = current.parent()
                if parent is None:
                    break
                current = parent
        except psutil.Error:
            return None
        return None

    def _describe_process(self, pid: int) -> str:
        psutil = self._psutil()
        try:
            return psutil.Process(pid).name()
        except psutil.Error:
            return "unknown"

    def _terminate_process_tree(self, pid: int, *, force: bool) -> None:
        psutil = self._psutil()
        try:
            root = psutil.Process(pid)
            processes = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            return
        for process in reversed(processes):
            try:
                process.kill() if force else process.terminate()
            except psutil.NoSuchProcess:
                continue

    async def _wait_for_port_free(self, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            try:
                owner = await asyncio.to_thread(self._find_port_listener_pid)
            except RuntimeError:
                return False
            if owner is None:
                return True
            await asyncio.sleep(0.1)
        return False

    async def _release_sdk_port(self) -> bool:
        try:
            listener_pid = await asyncio.to_thread(self._find_port_listener_pid)
        except RuntimeError as exc:
            self._set_last_error(str(exc))
            logger.error("[rtc] %s", self._last_error)
            return False
        if listener_pid is None:
            return True
        owner = await asyncio.to_thread(
            self._identify_sdk_port_owner,
            listener_pid,
        )
        if owner is None:
            process_name = await asyncio.to_thread(
                self._describe_process,
                listener_pid,
            )
            self._set_last_error(
                f"TCP port {AGENT_SDK_PORT} is occupied by an unknown process "
                f"(pid={listener_pid}, process={process_name}); refusing to terminate it"
            )
            logger.error("[rtc] %s", self._last_error)
            return False
        logger.warning(
            "[rtc] stopping stale Agent SDK pid=%s listener_pid=%s",
            owner.root_pid,
            owner.listener_pid,
        )
        await asyncio.to_thread(
            self._terminate_process_tree,
            owner.root_pid,
            force=False,
        )
        if await self._wait_for_port_free(3.0):
            return True
        await asyncio.to_thread(
            self._terminate_process_tree,
            owner.root_pid,
            force=True,
        )
        if await self._wait_for_port_free(2.0):
            return True
        self._set_last_error(
            f"stale {AGENT_SDK_PACKAGE} did not release TCP port {AGENT_SDK_PORT}"
        )
        return False

    @staticmethod
    def _merge_no_proxy(value: str | None) -> str:
        existing = [
            item.strip() for item in str(value or "").split(",") if item.strip()
        ]
        seen = {item.casefold() for item in existing}
        for host in _LOCAL_NO_PROXY_HOSTS:
            if host.casefold() not in seen:
                existing.append(host)
                seen.add(host.casefold())
        return ",".join(existing)

    def _create_runtime_files(self) -> None:
        fd, raw_path = tempfile.mkstemp(prefix="deskbot-roles-", suffix=".yaml")
        os.close(fd)
        self.roles_path = Path(raw_path)
        self.roles_path.write_text(
            yaml.safe_dump(self._roles(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.patch_dir = Path(tempfile.mkdtemp(prefix="deskbot-rtc-patch-"))
        (self.patch_dir / "sitecustomize.py").write_text(
            _SITECUSTOMIZE_CODE,
            encoding="utf-8",
        )

    def _cleanup_runtime_files(self) -> None:
        if self.roles_path is not None:
            try:
                self.roles_path.unlink()
            except OSError:
                pass
            self.roles_path = None
        if self.patch_dir is not None:
            try:
                shutil.rmtree(self.patch_dir)
            except OSError:
                pass
            self.patch_dir = None

    def _schedule_recovery(
        self, process: asyncio.subprocess.Process | None = None
    ) -> None:
        if asyncio.current_task() is self.recovery_task:
            return
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = asyncio.create_task(
                self._recover_worker(process),
                name="deskbot-rtc-agent-recovery",
            )

    async def start(self) -> bool:
        failed_process: asyncio.subprocess.Process | None = None
        async with self._start_lock:
            self._shutdown_requested = False
            if not self.settings.rtc.enabled:
                return False
            if self.is_ready:
                return True
            if self.process is not None:
                await self.stop()
                self._shutdown_requested = False
            if not self._can_start():
                self._schedule_recovery()
                return False
            if not await self._release_sdk_port():
                self._schedule_recovery()
                return False

            self._ready_event.clear()
            self._startup_failed_event.clear()
            try:
                self._create_runtime_files()
                assert self.roles_path is not None
                assert self.patch_dir is not None
                env = os.environ.copy()
                source_root = Path(__file__).resolve().parents[1]
                python_paths = [str(self.patch_dir), str(source_root)]
                existing_pythonpath = env.get("PYTHONPATH")
                if existing_pythonpath:
                    python_paths.append(existing_pythonpath)
                env["PYTHONPATH"] = os.pathsep.join(python_paths)
                env["DESKBOT_RTC_SPEECH_ADAPTER"] = self._speech_adapter
                env["DESKBOT_RTC_TOOL_BRIDGE_URL"] = self._tool_bridge_url
                env["DESKBOT_RTC_TOOL_BRIDGE_TOKEN"] = (
                    self._tool_bridge_token
                )
                env["LAMPGO_LIVEKIT_ALLOW_INTERRUPTIONS"] = (
                    "1"
                    if _allow_agent_interruptions(self.settings.rtc.call_mode)
                    else "0"
                )
                no_proxy = self._merge_no_proxy(
                    ",".join(
                        value
                        for value in (env.get("NO_PROXY"), env.get("no_proxy"))
                        if value
                    )
                )
                env["NO_PROXY"] = no_proxy
                env["no_proxy"] = no_proxy
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                )
                command = self._sdk_command()
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    "--config-file",
                    str(self.roles_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(AGENT_SDK_PORT),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    start_new_session=os.name != "nt",
                    creationflags=creationflags,
                )
                self._known_sdk_pids = {int(self.process.pid)}
                self.monitor_task = asyncio.create_task(
                    self._monitor(),
                    name="deskbot-rtc-agent-sdk",
                )
                self.process_watch_task = asyncio.create_task(
                    self._watch_process_exit(self.process),
                    name="deskbot-rtc-agent-process-watch",
                )
                if await self.wait_ready(_SDK_READY_TIMEOUT_SEC):
                    logger.info(
                        "[rtc] Agent SDK ready pid=%s speech_adapter=%s",
                        self.process.pid,
                        self._speech_adapter,
                    )
                    return True
                logger.error("[rtc] Agent SDK startup failed: %s", self._last_error)
            except Exception as exc:
                self._set_last_error(
                    f"failed to start {AGENT_SDK_PACKAGE}: {type(exc).__name__}"
                )
                logger.exception("[rtc] Agent SDK launch failed")
            failed_process = self.process
            await self.stop()
        # Initial startup failures used to leave RTC permanently offline.  A
        # recovery invocation already owns this loop; only the initial caller
        # needs to create it.
        self._shutdown_requested = False
        self._schedule_recovery(failed_process)
        return False

    async def _health_ready(self) -> bool:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", AGENT_SDK_PORT),
                timeout=_SDK_HEALTH_TIMEOUT_SEC,
            )
            writer.write(
                b"GET /healthz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            raw = await asyncio.wait_for(
                # The SDK replies with ``Connection: close``.  Reading to EOF
                # avoids treating a legal split between headers and the small
                # JSON body as a failed health check.
                reader.read(-1),
                timeout=_SDK_HEALTH_TIMEOUT_SEC,
            )
            head, _, body = raw.partition(b"\r\n\r\n")
            status_ok = b" 200 " in head.split(b"\r\n", 1)[0]
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            return status_ok and payload == {"status": "ok"}
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def wait_ready(self, timeout_s: float = _SDK_READY_TIMEOUT_SEC) -> bool:
        if self.is_ready:
            return True
        if not self.is_running or self._startup_failed_event.is_set():
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self._startup_failed_event.is_set() or not self.is_running:
                return False
            if self._ready_event.is_set() and await self._health_ready():
                return True
            await asyncio.sleep(0.1)
        self._set_last_error(
            f"voice Agent SDK did not become ready within {timeout_s:.1f}s"
        )
        return False

    async def _monitor(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            async for line in process.stdout:
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                logger.info("[rtc-agent] %s", text)
                lowered = text.casefold()
                if any(marker in lowered for marker in _BIND_FAILURE_MARKERS):
                    self._set_last_error(
                        f"TCP port {AGENT_SDK_PORT} became unavailable during startup"
                    )
                    self._startup_failed_event.set()
                if "registered worker" in lowered:
                    self._ready_event.set()
                    self._worker_registered_mono = time.monotonic()
                    self._recovery_attempt = 0
                if any(marker in lowered for marker in _WORKER_FATAL_MARKERS):
                    self._set_last_error(
                        "LiveKit worker connection task stopped; restarting Agent SDK"
                    )
                    self._ready_event.clear()
                    if self.recovery_task is None or self.recovery_task.done():
                        self.recovery_task = asyncio.create_task(
                            self._recover_worker(process),
                            name="deskbot-rtc-agent-recovery",
                        )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[rtc] Agent SDK output monitor failed")
        rc = await process.wait()
        if rc != 0:
            if not self._last_error:
                self._set_last_error(
                    f"{AGENT_SDK_PACKAGE} exited with status {rc}"
                )
            self._startup_failed_event.set()
            logger.error("[rtc] Agent SDK exited status=%d", rc)
        elif not self._ready_event.is_set():
            self._set_last_error(
                f"{AGENT_SDK_PACKAGE} exited before its worker registered"
            )
            self._startup_failed_event.set()
        if self.process is process:
            self.process = None
            self._ready_event.clear()
        if (
            not self._shutdown_requested
            and (self.recovery_task is None or self.recovery_task.done())
        ):
            self.recovery_task = asyncio.create_task(
                self._recover_worker(process),
                name="deskbot-rtc-agent-recovery",
            )

    async def _watch_process_exit(
        self, process: asyncio.subprocess.Process
    ) -> None:
        """Detect parent death even when an orphan child keeps stdout open."""

        def _scan_children() -> set[int]:
            psutil = self._psutil()
            try:
                root = psutil.Process(process.pid)
                return {
                    int(child.pid) for child in root.children(recursive=True)
                }
            except psutil.Error:
                return set()

        try:
            loop = asyncio.get_running_loop()
            next_scan_mono = 0.0
            while process.returncode is None:
                # The recursive children() walk scans the entire host process
                # table; run it on a worker thread and at a low cadence.
                # Parent-exit detection stays immediate via process.wait().
                if loop.time() >= next_scan_mono:
                    self._known_sdk_pids.update(
                        await asyncio.to_thread(_scan_children)
                    )
                    next_scan_mono = loop.time() + _SDK_CHILD_SCAN_INTERVAL_SEC
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    continue
            rc = await process.wait()
        except asyncio.CancelledError:
            return
        if self.process is not process or self._shutdown_requested:
            return
        self.process = None
        self._ready_event.clear()
        self._set_last_error(f"{AGENT_SDK_PACKAGE} parent exited with status {rc}")
        logger.error("[rtc] Agent SDK parent exited status=%d", rc)
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = asyncio.create_task(
                self._recover_worker(process),
                name="deskbot-rtc-agent-recovery",
            )

    async def _recover_worker(
        self, process: asyncio.subprocess.Process | None
    ) -> None:
        """Replace an SDK whose HTTP server survived a dead Worker task."""

        try:
            logger.error(
                "[rtc] restarting unhealthy Agent SDK pid=%s",
                process.pid if process is not None else "none",
            )
            if process is not None and process.returncode is None:
                await asyncio.to_thread(
                    self._terminate_process_tree, process.pid, force=False
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    await asyncio.to_thread(
                        self._terminate_process_tree, process.pid, force=True
                    )
                    await process.wait()
            if process is not None and self.process is process:
                self.process = None
            self._ready_event.clear()
            self._startup_failed_event.clear()
            self._cleanup_runtime_files()
            delay = _RECOVERY_INITIAL_DELAY_SEC
            while not self._shutdown_requested:
                self._recovery_attempt += 1
                logger.warning(
                    "[rtc] Agent SDK recovery attempt=%d delay=%.1fs",
                    self._recovery_attempt,
                    delay,
                )
                await asyncio.sleep(delay)
                if await self.start():
                    logger.info("[rtc] Agent SDK recovered after Worker disconnect")
                    return
                # start() performs a full cleanup through stop() on a failed
                # launch.  That sets the shutdown flag for ordinary callers;
                # this recovery task itself is still alive, so continue its
                # bounded backoff loop.  An external stop cancels this task.
                self._shutdown_requested = False
                logger.error("[rtc] Agent SDK recovery failed: %s", self._last_error)
                delay = min(_RECOVERY_MAX_DELAY_SEC, delay * 2.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[rtc] Agent SDK automatic recovery failed")

    async def stop(self) -> None:
        self._shutdown_requested = True
        current = asyncio.current_task()
        if self.recovery_task is not None and self.recovery_task is not current:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
            self.recovery_task = None
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                await asyncio.to_thread(
                    self._terminate_process_tree,
                    process.pid,
                    force=False,
                )
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                await asyncio.to_thread(
                    self._terminate_process_tree,
                    process.pid,
                    force=True,
                )
                await process.wait()
            except Exception:
                logger.exception(
                    "[rtc] failed to stop Agent SDK process tree pid=%s",
                    process.pid,
                )
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
            self.monitor_task = None
        if self.process_watch_task is not None:
            self.process_watch_task.cancel()
            await asyncio.gather(self.process_watch_task, return_exceptions=True)
            self.process_watch_task = None
        # When start() is called by the recovery loop, a failed attempt comes
        # through stop(). Keep the current task registered so start() cannot
        # spawn a second recovery loop alongside it.
        if self.recovery_task is current:
            self.recovery_task = current
        self._ready_event.clear()
        self._startup_failed_event.clear()
        self._cleanup_runtime_files()
        self._known_sdk_pids.clear()
