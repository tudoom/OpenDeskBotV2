"""随包固件的清单读取与设备端 USB 烧录任务。

PC 安装包内置最新固件（``firmware_payload/``），控制台「固件更新」tab
对比设备 hello 上报的版本决定是否亮出「更新」。烧录只走 USB：先让串口
管理器挂起该 COM 口（关会话 + 关句柄），esptool 独占烧写 0x10000 应用
分区，完成后解除挂起，设备重启进新固件由扫描器自然重连。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("deskbot-server")

_PAYLOAD_DIR = Path(__file__).resolve().parent / "firmware_payload"
_FLASH_OFFSET = "0x10000"
_FLASH_BAUD = "460800"
# 单次烧录的硬超时：1.9MB @460800 实测约 60-90s，含进入 ROM loader 的重试。
_FLASH_TIMEOUT_S = 300


def parse_version(raw: str) -> tuple[int, ...]:
    """把 "0.0.17" 类版本号转成可比较元组；解析不出的段按 0。"""
    parts: list[int] = []
    for piece in str(raw or "").strip().split("."):
        m = re.search(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    while parts and parts[-1] == 0 and len(parts) > 3:
        parts.pop()
    return tuple(parts or [0])


def bundled_manifest() -> dict[str, Any] | None:
    """返回随包固件清单（含 bin 路径与大小）；缺文件时返回 None。"""
    manifest_path = _PAYLOAD_DIR / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    version = str(manifest.get("version") or "").strip()
    bin_name = str(manifest.get("file") or "").strip()
    if not version or not bin_name or "/" in bin_name or "\\" in bin_name:
        return None
    bin_path = _PAYLOAD_DIR / bin_name
    if not bin_path.is_file():
        return None
    return {
        "version": version,
        "path": bin_path,
        "size": bin_path.stat().st_size,
        "chip": str(manifest.get("chip") or "esp32s3"),
    }


class FirmwareUpdateJob:
    """进程内单例任务：同一时间最多一台设备在烧录。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {"state": "idle"}

    def status(self) -> dict[str, Any]:
        return dict(self._state)

    def _set(self, **fields: Any) -> None:
        self._state = {**fields, "t": time.time()}

    async def start(
        self,
        *,
        manager: Any,
        device_id: str,
        port: str,
        target_version: str,
        bin_path: Path,
        chip: str,
    ) -> dict[str, Any]:
        if self._lock.locked():
            return {"ok": False, "error": "update_already_running"}
        asyncio.get_running_loop().create_task(
            self._run(
                manager=manager,
                device_id=device_id,
                port=port,
                target_version=target_version,
                bin_path=bin_path,
                chip=chip,
            ),
            name=f"firmware-update:{device_id}",
        )
        return {"ok": True, "state": "starting"}

    async def _run(
        self,
        *,
        manager: Any,
        device_id: str,
        port: str,
        target_version: str,
        bin_path: Path,
        chip: str,
    ) -> None:
        async with self._lock:
            self._set(
                state="running",
                device_id=device_id,
                port=port,
                target_version=target_version,
                phase="detach",
            )
            logger.info(
                "[fw_update] start device_id=%s port=%s target=%s",
                device_id,
                port,
                target_version,
            )
            try:
                await manager.begin_port_maintenance(port)
                # 关口后给 Windows 一点时间释放句柄，否则 esptool 偶发
                # PermissionError(13)。
                await asyncio.sleep(1.0)
                self._set(
                    state="running",
                    device_id=device_id,
                    port=port,
                    target_version=target_version,
                    phase="flashing",
                )
                ok, detail = await asyncio.to_thread(
                    self._flash_blocking, port, bin_path, chip
                )
                if ok:
                    self._set(
                        state="success",
                        device_id=device_id,
                        target_version=target_version,
                        detail=detail,
                    )
                    logger.info(
                        "[fw_update] success device_id=%s target=%s",
                        device_id,
                        target_version,
                    )
                else:
                    self._set(
                        state="failed",
                        device_id=device_id,
                        target_version=target_version,
                        error=detail,
                    )
                    logger.warning(
                        "[fw_update] failed device_id=%s err=%s",
                        device_id,
                        detail,
                    )
            except Exception as exc:  # noqa: BLE001 - 状态必须落到 failed
                self._set(
                    state="failed",
                    device_id=device_id,
                    target_version=target_version,
                    error=str(exc),
                )
                logger.exception("[fw_update] crashed device_id=%s", device_id)
            finally:
                try:
                    manager.end_port_maintenance(port)
                except Exception:
                    logger.exception("[fw_update] end maintenance failed")

    @staticmethod
    def _flash_blocking(port: str, bin_path: Path, chip: str) -> tuple[bool, str]:
        def run(write_cmd: str) -> subprocess.CompletedProcess[str]:
            cmd = [
                sys.executable,
                "-m",
                "esptool",
                "--chip",
                chip,
                "--port",
                port,
                "--baud",
                _FLASH_BAUD,
                write_cmd,
                _FLASH_OFFSET,
                str(bin_path),
            ]
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_FLASH_TIMEOUT_S,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )

        try:
            # esptool v4 用下划线子命令，v5 改中划线；先试 v5 风格，
            # 参数解析失败（不会碰硬件）再回退 v4。
            completed = run("write-flash")
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode != 0 and (
                "invalid choice" in output or "unrecognized arguments" in output
            ):
                completed = run("write_flash")
                output = (completed.stdout or "") + (completed.stderr or "")
        except subprocess.TimeoutExpired:
            return False, f"esptool timeout after {_FLASH_TIMEOUT_S}s"
        except FileNotFoundError:
            return False, "python interpreter not found for esptool"
        tail = "\n".join(output.strip().splitlines()[-6:])
        if completed.returncode != 0:
            if "No module named esptool" in output:
                return False, "esptool 未随包安装（需要 1.9+ 安装包）"
            return False, tail or f"esptool exited {completed.returncode}"
        return True, tail


firmware_update_job = FirmwareUpdateJob()


def manifest_response(
    manager: Any,
    device_id: str,
) -> dict[str, Any]:
    """给「固件更新」tab 的一站式状态：随包版本 vs 设备版本。"""
    bundled = bundled_manifest()
    session = (
        manager.session_for_device(device_id) if device_id and manager else None
    )
    hello = getattr(session, "hello_info", None) if session is not None else None
    device_version = str(getattr(hello, "firmware", "") or "")
    transport = str(getattr(session, "transport", "") or "")
    update_available = bool(
        bundled
        and device_version
        and parse_version(bundled["version"]) > parse_version(device_version)
    )
    return {
        "ok": True,
        "bundled_version": bundled["version"] if bundled else None,
        "bundled_size": bundled["size"] if bundled else 0,
        "device_version": device_version or None,
        "device_connected": session is not None,
        "device_transport": transport or None,
        "update_available": update_available,
        # 只有 USB 直连才能真的点下去烧。
        "update_ready": update_available and transport == "usb_cdc",
        "job": firmware_update_job.status(),
        "t": time.time(),
    }


async def start_update(
    manager: Any,
    device_id: str,
) -> tuple[int, dict[str, Any]]:
    """校验后启动烧录任务；返回 (http_status, body)。"""
    bundled = bundled_manifest()
    if not bundled:
        return 503, {"ok": False, "error": "no_bundled_firmware"}
    session = manager.session_for_device(device_id) if device_id else None
    if session is None:
        return 409, {"ok": False, "error": "device_not_connected"}
    hello = getattr(session, "hello_info", None)
    device_version = str(getattr(hello, "firmware", "") or "")
    if session.transport != "usb_cdc":
        return 409, {
            "ok": False,
            "error": "usb_required",
            "message": "固件升级需要 USB 连接，请插上 USB 线后重试",
        }
    if not (
        device_version
        and parse_version(bundled["version"]) > parse_version(device_version)
    ):
        return 409, {
            "ok": False,
            "error": "already_latest",
            "device_version": device_version or None,
            "bundled_version": bundled["version"],
        }
    result = await firmware_update_job.start(
        manager=manager,
        device_id=device_id,
        port=session.port,
        target_version=bundled["version"],
        bin_path=bundled["path"],
        chip=bundled["chip"],
    )
    if not result.get("ok"):
        return 409, result
    return 200, {**result, "target_version": bundled["version"]}
