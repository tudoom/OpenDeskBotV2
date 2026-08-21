"""Load ``opuslib_next`` with a deterministic native runtime.

``opuslib_next`` relies on ``ctypes.util.find_library("opus")``. On Windows
that only searches ``PATH`` and therefore does not discover the ``opus.dll``
bundled by PyOgg after a normal wheel install. Keep the platform workaround in
one place so uplink and downlink cannot drift.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

_dll_directory_handles: list[Any] = []
_prepared_dll_directories: set[str] = set()
_windows_runtime_lock = threading.Lock()


def _windows_opus_dll_candidates() -> list[Path]:
    """Return installed/frozen PyOgg DLL candidates without importing PyOgg."""

    candidates: list[Path] = []
    try:
        spec = importlib.util.find_spec("pyogg")
    except (ImportError, AttributeError, ValueError):
        spec = None
    locations = list(spec.submodule_search_locations or ()) if spec else []
    candidates.extend(Path(location).resolve() / "opus.dll" for location in locations)

    # PyInstaller extracts collected binaries below ``sys._MEIPASS``. Support
    # both common layouts so a future standalone PC build does not regress.
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        root = Path(frozen_root).resolve()
        candidates.extend((root / "pyogg" / "opus.dll", root / "opus.dll"))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _prepare_windows_opus_runtime() -> Path | None:
    if sys.platform != "win32":
        return None

    with _windows_runtime_lock:
        for dll_path in _windows_opus_dll_candidates():
            if not dll_path.is_file():
                continue
            package_dir = dll_path.parent
            package_dir_text = str(package_dir)

            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if package_dir_text.casefold() not in {part.casefold() for part in path_parts if part}:
                current_path = os.environ.get("PATH", "")
                os.environ["PATH"] = (
                    package_dir_text
                    if not current_path
                    else package_dir_text + os.pathsep + current_path
                )

            # Python 3.8+ restricts dependent-DLL lookup on Windows. Keep each
            # directory handle alive for the process lifetime. PATH is still
            # required because opuslib_next calls ctypes.util.find_library.
            add_dll_directory = getattr(os, "add_dll_directory", None)
            directory_key = package_dir_text.casefold()
            if add_dll_directory is not None and directory_key not in _prepared_dll_directories:
                try:
                    handle = add_dll_directory(package_dir_text)
                except OSError:
                    # The absolute opus.dll path remains discoverable through
                    # PATH; opuslib_next will raise a precise load error if a
                    # transitive DLL is genuinely unavailable.
                    pass
                else:
                    _dll_directory_handles.append(handle)
                    _prepared_dll_directories.add(directory_key)
            return dll_path
    return None


def _native_runtime_hint(dll_path: Path | None) -> str:
    if sys.platform != "win32":
        return "install the operating system libopus package"
    if dll_path is None:
        return "install the Windows PyOgg wheel, which bundles opus.dll"
    return (
        "PyOgg's opus.dll was found but could not be loaded; verify that "
        "Python and the PyOgg wheel use the same architecture"
    )


def load_opuslib_next() -> ModuleType:
    """Return a ready-to-use Opus binding or raise an actionable error."""

    dll_path = _prepare_windows_opus_runtime()
    try:
        return importlib.import_module("opuslib_next")
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "opuslib_next":
            hint = "install the opuslib_next Python package"
        else:
            hint = _native_runtime_hint(dll_path)
        raise RuntimeError(
            "Opus audio is configured but its runtime is unavailable; " + hint
        ) from exc
