"""Lifecycle management for the loopback-only LiveKit Server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from deskbot_server.atomic_store import atomic_write_json, atomic_write_text
from deskbot_server.core.settings import AppSettings
from deskbot_server.paths import DATA_DIR

logger = logging.getLogger("deskbot-server")

_DEFAULT_BINARY = DATA_DIR / "local" / "livekit" / "livekit-server.exe"
_DEFAULT_CREDENTIALS = DATA_DIR / "local" / "livekit" / "credentials.json"
_DEFAULT_CONFIG = DATA_DIR / "local" / "livekit" / "livekit.yaml"
_STARTUP_TIMEOUT_SEC = 15.0
_RECOVERY_INITIAL_DELAY_SEC = 1.0
_RECOVERY_MAX_DELAY_SEC = 15.0


@dataclass(frozen=True, slots=True)
class LocalLiveKitCredentials:
    api_key: str
    api_secret: str


class LocalLiveKitServerManager:
    """Own one local SFU process and its machine-local root credentials."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self.credentials: LocalLiveKitCredentials | None = None
        self._ready = False
        self._shutdown_requested = False
        self._start_lock = asyncio.Lock()
        self._last_error = ""
        self._recovery_attempt = 0

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.rtc.enabled
            and self.settings.rtc.local_server_enabled
        )

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def is_ready(self) -> bool:
        return self.is_running and self._ready

    def health_snapshot(self) -> dict[str, object]:
        return {
            "mode": "local" if self.enabled else "external",
            "status": (
                "ready"
                if self.is_ready
                else "recovering"
                if self.recovery_task is not None and not self.recovery_task.done()
                else "starting"
                if self.is_running
                else "offline"
                if self.enabled
                else "external"
            ),
            "process_running": self.is_running,
            "url": self.settings.rtc.livekit_url,
            "recovery_attempt": self._recovery_attempt,
            "last_error": self._last_error,
        }

    def _resolve_binary(self) -> Path | None:
        candidates: list[Path] = []
        # The desktop launcher materializes livekit-server.exe to a path that is
        # stable across upgrades and exports it here; preferring it keeps the
        # firewall allow-rule (keyed on the image path) matching after every
        # upgrade instead of re-prompting on each per-version path.
        stable_bin_dir = str(os.environ.get("DESKBOT_STABLE_BIN_DIR") or "").strip()
        if stable_bin_dir:
            candidates.append(Path(stable_bin_dir) / "livekit-server.exe")
        configured = str(
            os.environ.get("DESKBOT_LOCAL_LIVEKIT_BINARY")
            or self.settings.rtc.local_server_binary
            or ""
        ).strip()
        if configured:
            candidates.append(Path(configured))
        candidates.append(_DEFAULT_BINARY)
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        discovered = shutil.which("livekit-server")
        return Path(discovered).resolve() if discovered else None

    @staticmethod
    def _credentials_from_env() -> LocalLiveKitCredentials | None:
        api_key = str(os.environ.get("LIVEKIT_API_KEY") or "").strip()
        api_secret = str(os.environ.get("LIVEKIT_API_SECRET") or "").strip()
        if bool(api_key) != bool(api_secret):
            raise RuntimeError(
                "LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be configured together"
            )
        if api_key and api_secret:
            return LocalLiveKitCredentials(api_key, api_secret)
        return None

    def _load_or_create_credentials(self) -> LocalLiveKitCredentials:
        from_env = self._credentials_from_env()
        if from_env is not None:
            return from_env
        try:
            payload = json.loads(_DEFAULT_CREDENTIALS.read_text(encoding="utf-8"))
            api_key = str(payload.get("api_key") or "").strip()
            api_secret = str(payload.get("api_secret") or "").strip()
            if api_key and api_secret:
                return LocalLiveKitCredentials(api_key, api_secret)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning(
                "[rtc-local] invalid local credentials; replacing them",
                exc_info=True,
            )
        credentials = LocalLiveKitCredentials(
            api_key=f"deskbot_{secrets.token_hex(12)}",
            api_secret=secrets.token_urlsafe(48),
        )
        atomic_write_json(
            _DEFAULT_CREDENTIALS,
            {
                "api_key": credentials.api_key,
                "api_secret": credentials.api_secret,
            },
            mode=0o600,
        )
        return credentials

    def _server_port(self) -> int:
        parsed = urlparse(self.settings.rtc.livekit_url)
        host = (parsed.hostname or "").casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(
                "local LiveKit Server requires a loopback DESKBOT_LIVEKIT_URL"
            )
        return int(parsed.port or 7880)

    def _write_config(self, credentials: LocalLiveKitCredentials) -> Path:
        port = self._server_port()
        config = {
            "port": port,
            "bind_addresses": ["127.0.0.1"],
            "keys": {credentials.api_key: credentials.api_secret},
            "rtc": {
                # Windows LiveKit clients omit loopback host candidates even
                # when signalling uses 127.0.0.1. Keep ICE on the host's
                # ordinary interface so local processes can pair candidates;
                # no remote LiveKit or TURN service is involved.
                "tcp_port": port + 1,
                "port_range_start": 50000,
                "port_range_end": 50020,
                "use_external_ip": False,
                "node_ip": "127.0.0.1",
            },
            "logging": {"level": "info", "json": False},
            "room": {"empty_timeout": 300, "departure_timeout": 20},
        }
        atomic_write_text(
            _DEFAULT_CONFIG,
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            mode=0o600,
        )
        return _DEFAULT_CONFIG

    @staticmethod
    def _listener_pid(port: int) -> int | None:
        import psutil

        pids = {
            int(item.pid)
            for item in psutil.net_connections(kind="tcp")
            if item.status == psutil.CONN_LISTEN
            and item.laddr
            and int(getattr(item.laddr, "port", item.laddr[1])) == port
            and item.pid is not None
        }
        if len(pids) > 1:
            raise RuntimeError(f"multiple processes listen on local LiveKit port {port}")
        return next(iter(pids), None)

    @staticmethod
    def _is_livekit_process(pid: int) -> bool:
        import psutil

        try:
            process = psutil.Process(pid)
            values = [process.name(), *process.cmdline()]
        except psutil.Error:
            return False
        return any("livekit-server" in str(value).casefold() for value in values)

    @staticmethod
    def _terminate_process_tree(pid: int, *, force: bool) -> None:
        import psutil

        try:
            root = psutil.Process(pid)
            processes = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            return
        for process in reversed(processes):
            try:
                process.kill() if force else process.terminate()
            except psutil.NoSuchProcess:
                pass

    async def _release_port(self, port: int) -> bool:
        try:
            pid = await asyncio.to_thread(self._listener_pid, port)
        except Exception as exc:
            self._last_error = f"cannot inspect local LiveKit port {port}: {exc}"
            return False
        if pid is None:
            return True
        if not await asyncio.to_thread(self._is_livekit_process, pid):
            self._last_error = (
                f"local LiveKit port {port} is occupied by non-LiveKit pid={pid}"
            )
            return False
        logger.warning("[rtc-local] stopping stale LiveKit Server pid=%d", pid)
        await asyncio.to_thread(self._terminate_process_tree, pid, force=False)
        for _ in range(30):
            await asyncio.sleep(0.1)
            if await asyncio.to_thread(self._listener_pid, port) is None:
                return True
        await asyncio.to_thread(self._terminate_process_tree, pid, force=True)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if await asyncio.to_thread(self._listener_pid, port) is None:
                return True
        self._last_error = f"stale LiveKit Server did not release port {port}"
        return False

    async def _health_ready(self) -> bool:
        writer = None
        try:
            port = self._server_port()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1.0
            )
            writer.write(
                b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            return b" 200 " in response.split(b"\r\n", 1)[0]
        except (OSError, TimeoutError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _start_once(self) -> bool:
        try:
            credentials = self._load_or_create_credentials()
            self.credentials = credentials
            binary = self._resolve_binary()
            if binary is None:
                self._last_error = (
                    "local livekit-server binary is missing; run "
                    "tools/install_livekit_windows.ps1"
                )
                return False
            port = self._server_port()
            if not await self._release_port(port):
                return False
            config_path = self._write_config(credentials)
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            self.process = await asyncio.create_subprocess_exec(
                str(binary),
                "--config",
                str(config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            self.monitor_task = asyncio.create_task(
                self._monitor(self.process), name="deskbot-local-livekit-log"
            )
            self.watch_task = asyncio.create_task(
                self._watch(self.process), name="deskbot-local-livekit-watch"
            )
            deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_SEC
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    break
                if await self._health_ready():
                    self._ready = True
                    self._last_error = ""
                    self._recovery_attempt = 0
                    logger.info(
                        "[rtc-local] LiveKit Server ready pid=%d url=%s",
                        self.process.pid,
                        self.settings.rtc.livekit_url,
                    )
                    return True
                await asyncio.sleep(0.1)
            self._last_error = "local LiveKit Server did not become ready"
        except Exception as exc:
            self._last_error = f"failed to start local LiveKit Server: {type(exc).__name__}: {exc}"
            logger.exception("[rtc-local] LiveKit Server startup failed")
        await self._stop_process()
        return False

    async def start(self) -> bool:
        if not self.enabled:
            return True
        self._shutdown_requested = False
        async with self._start_lock:
            if self.is_ready:
                return True
            ready = await self._start_once()
        if not ready:
            self._schedule_recovery()
        return ready

    def _schedule_recovery(self) -> None:
        if self._shutdown_requested:
            return
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = asyncio.create_task(
                self._recover(), name="deskbot-local-livekit-recovery"
            )

    async def _recover(self) -> None:
        delay = _RECOVERY_INITIAL_DELAY_SEC
        try:
            while not self._shutdown_requested:
                self._recovery_attempt += 1
                logger.warning(
                    "[rtc-local] recovery attempt=%d delay=%.1fs error=%s",
                    self._recovery_attempt,
                    delay,
                    self._last_error,
                )
                await asyncio.sleep(delay)
                async with self._start_lock:
                    if await self._start_once():
                        return
                delay = min(_RECOVERY_MAX_DELAY_SEC, delay * 2.0)
        except asyncio.CancelledError:
            raise

    async def _monitor(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        try:
            async for raw in process.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    logger.info("[rtc-local] %s", line)
        except asyncio.CancelledError:
            return

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        try:
            rc = await process.wait()
        except asyncio.CancelledError:
            return
        if self.process is not process:
            return
        self.process = None
        self._ready = False
        if not self._shutdown_requested:
            self._last_error = f"local LiveKit Server exited with status {rc}"
            logger.error("[rtc-local] %s", self._last_error)
            self._schedule_recovery()

    async def _stop_process(self) -> None:
        current = asyncio.current_task()
        process = self.process
        self.process = None
        self._ready = False
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
        for task_name in ("monitor_task", "watch_task"):
            task = getattr(self, task_name)
            if task is not None and task is not current:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            setattr(self, task_name, None)

    async def stop(self) -> None:
        self._shutdown_requested = True
        current = asyncio.current_task()
        if self.recovery_task is not None and self.recovery_task is not current:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
        self.recovery_task = None
        async with self._start_lock:
            await self._stop_process()
