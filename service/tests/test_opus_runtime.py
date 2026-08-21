from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from deskbot_server.pipeline import opus_runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_runtime_state(monkeypatch):
    # Do not close a real add_dll_directory handle created by an earlier test
    # in the same process; replace the containers and let monkeypatch restore
    # them instead.
    monkeypatch.setattr(opus_runtime, "_dll_directory_handles", [])
    monkeypatch.setattr(opus_runtime, "_prepared_dll_directories", set())


def test_windows_runtime_prepends_pyogg_dll_directory_and_keeps_handle(monkeypatch, tmp_path: Path):
    package_dir = tmp_path / "pyogg"
    package_dir.mkdir()
    dll_path = package_dir / "opus.dll"
    dll_path.write_bytes(b"fixture")

    monkeypatch.setattr(opus_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        opus_runtime,
        "_windows_opus_dll_candidates",
        lambda: [dll_path],
    )
    monkeypatch.setenv("PATH", str(tmp_path / "other"))
    handles: list[object] = []

    def _add_dll_directory(path: str):
        assert path == str(package_dir)
        handle = object()
        handles.append(handle)
        return handle

    monkeypatch.setattr(
        opus_runtime.os,
        "add_dll_directory",
        _add_dll_directory,
        raising=False,
    )

    assert opus_runtime._prepare_windows_opus_runtime() == dll_path
    assert os.environ["PATH"].split(os.pathsep)[0] == str(package_dir)
    assert opus_runtime._dll_directory_handles == handles

    # Repeated encoder/decoder construction must not leak handles or duplicate
    # PATH entries.
    assert opus_runtime._prepare_windows_opus_runtime() == dll_path
    assert opus_runtime._dll_directory_handles == handles
    assert os.environ["PATH"].split(os.pathsep).count(str(package_dir)) == 1


def test_windows_runtime_still_uses_path_when_add_dll_directory_fails(monkeypatch, tmp_path: Path):
    package_dir = tmp_path / "pyogg"
    package_dir.mkdir()
    dll_path = package_dir / "opus.dll"
    dll_path.write_bytes(b"fixture")

    monkeypatch.setattr(opus_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        opus_runtime,
        "_windows_opus_dll_candidates",
        lambda: [dll_path],
    )
    monkeypatch.setenv("PATH", "")

    def _fail_add_dll_directory(_path: str):
        raise OSError("simulated policy restriction")

    monkeypatch.setattr(
        opus_runtime.os,
        "add_dll_directory",
        _fail_add_dll_directory,
        raising=False,
    )

    assert opus_runtime._prepare_windows_opus_runtime() == dll_path
    assert os.environ["PATH"] == str(package_dir)
    assert not opus_runtime._dll_directory_handles


def test_windows_candidates_include_installed_and_frozen_layouts(monkeypatch, tmp_path: Path):
    installed = tmp_path / "site-packages" / "pyogg"
    frozen = tmp_path / "bundle"
    monkeypatch.setattr(opus_runtime.sys, "platform", "win32")
    monkeypatch.setattr(opus_runtime.sys, "_MEIPASS", str(frozen), raising=False)
    monkeypatch.setattr(
        opus_runtime.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(submodule_search_locations=[str(installed)]),
    )

    assert opus_runtime._windows_opus_dll_candidates() == [
        installed.resolve() / "opus.dll",
        frozen.resolve() / "pyogg" / "opus.dll",
        frozen.resolve() / "opus.dll",
    ]


def test_loader_prepares_native_runtime_before_import(monkeypatch):
    events: list[str] = []
    module = ModuleType("opuslib_next")
    monkeypatch.setattr(
        opus_runtime,
        "_prepare_windows_opus_runtime",
        lambda: events.append("prepare"),
    )

    def _import(name: str):
        assert name == "opuslib_next"
        events.append("import")
        return module

    monkeypatch.setattr(opus_runtime.importlib, "import_module", _import)

    assert opus_runtime.load_opuslib_next() is module
    assert events == ["prepare", "import"]


@pytest.mark.parametrize(
    ("error", "expected_hint"),
    [
        (
            ModuleNotFoundError("missing binding", name="opuslib_next"),
            "install the opuslib_next Python package",
        ),
        (OSError("bad image"), "same architecture"),
    ],
)
def test_loader_errors_are_actionable_on_windows(monkeypatch, error: Exception, expected_hint: str):
    monkeypatch.setattr(opus_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        opus_runtime,
        "_prepare_windows_opus_runtime",
        lambda: Path("pyogg") / "opus.dll",
    )
    monkeypatch.setattr(
        opus_runtime.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(error),
    )

    with pytest.raises(RuntimeError, match=expected_hint):
        opus_runtime.load_opuslib_next()


def test_windows_native_runtime_dependency_is_locked_and_platform_scoped():
    pyproject = (ROOT / "service" / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "service" / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "service" / "uv.lock").read_text(encoding="utf-8")

    assert "\"PyOgg==0.6.14a1; sys_platform == 'win32'\"" in pyproject
    assert 'PyOgg==0.6.14a1; sys_platform == "win32"' in requirements
    assert 'name = "pyogg"' in lock
    assert "PyOgg-0.6.14a1-py2.py3-none-win_amd64.whl" in lock
