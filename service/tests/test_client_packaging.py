from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_portable_path_overrides_are_resolved_in_fresh_process(tmp_path):
    profile = tmp_path / "profile"
    models = tmp_path / "runtime" / "models"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(SERVICE_ROOT / "src"),
            "DESKBOT_PROJECT_ROOT": str(profile),
            "DESKBOT_DATA_DIR": str(profile / "state"),
            "DESKBOT_MODELS_DIR": str(models),
            "DESKBOT_SERVER_CONFIG": str(profile / "client.yaml"),
            "DESKBOT_ENV_FILE": str(profile / "client.env"),
        }
    )
    script = (
        "from deskbot_server.paths import (PROJECT_ROOT, DATA_DIR, MODELS_DIR, "
        "DEFAULT_CONFIG_PATH, ENV_FILE); "
        "print('|'.join(map(str, (PROJECT_ROOT, DATA_DIR, MODELS_DIR, "
        "DEFAULT_CONFIG_PATH, ENV_FILE))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().split("|") == [
        str(profile.resolve()),
        str((profile / "state").resolve()),
        str(models.resolve()),
        str((profile / "client.yaml").resolve()),
        str((profile / "client.env").resolve()),
    ]


def test_rtc_agent_uses_portable_python_module_command():
    from deskbot_server.rtc_agent_sdk import (
        AGENT_SDK_ENTRY_MODULE,
        RtcAgentSdkManager,
        _command_runs_agent_sdk,
    )

    command = RtcAgentSdkManager._sdk_command()

    assert command == [sys.executable, "-u", "-m", AGENT_SDK_ENTRY_MODULE]
    assert _command_runs_agent_sdk(command) is True
    assert _command_runs_agent_sdk(
        ["python.exe", "-m", "deskbot_server.web"]
    ) is False


def test_client_payload_excludes_secrets_and_offline_asr_model():
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    assert '"mediapipe", "silero_vad"' in build_script
    assert "SenseVoiceSmall" not in build_script.split("$manifest =", 1)[0]
    assert "Copy-Item -LiteralPath (Join-Path $serviceRoot \".env\")" not in build_script
    assert "Install-CurrentMachineConfig" in build_script


def test_client_build_pins_webview2_hash_and_keeps_env_install_opt_in():
    import re

    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    # The pinned SDK hash must be a full SHA-256 and the download must be
    # verified before anything from the package is compiled into the EXE.
    assert re.search(
        r'\$webView2PinnedSha256 = "[0-9a-f]{64}"', build_script
    ), "WebView2 nupkg SHA-256 pin is missing"
    assert "Test-WebView2PackageHash" in build_script
    assert "refusing to build with an unverified SDK" in build_script

    # Copying the build machine's real .env is opt-in, never the default.
    assert "[switch]$InstallLocalConfig" in build_script
    assert "-not $InstallLocalConfig" in build_script


def test_client_launcher_blocks_non_http_urls_and_rotates_logs():
    launcher_source = (
        SERVICE_ROOT / "client" / "OpenDeskBotV2Launcher.cs"
    ).read_text(encoding="utf-8")

    # ShellExecute whitelist: only http/https ever reaches the shell.
    assert "blocked external open of non-http(s) url" in launcher_source
    assert "Uri.UriSchemeHttp" in launcher_source
    assert "Uri.UriSchemeHttps" in launcher_source

    # Startup log cleanup plus per-file size rollover.
    assert "class ClientLogRotation" in launcher_source
    assert "ClientLogRotation.CleanInBackground(logRoot)" in launcher_source
    assert "MaxLogBytes" in launcher_source


def test_client_build_precompiles_bytecode_with_hash_invalidation():
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    # app\src keeps its .py sources (field tracebacks need real source lines)
    # and is precompiled with the shipped interpreter using PEP 552 hash-based
    # pycs that stay valid after extraction rewrites file timestamps on the
    # user's machine. -f force-regenerates so stale timestamp-mode dev caches
    # can never ship.
    assert "Invoke-StageBytecodeCompilation" in build_script
    assert (
        "compileall -q -f -j 0 `\n        --invalidation-mode unchecked-hash "
        "$appSources" in build_script
    )

    # The two precompiled trees must no longer strip __pycache__ / *.pyc while
    # staging (the stdlib Lib/DLLs copies still may).
    site_packages_block = build_script.split("$sitePackagesSource", 1)[1]
    site_packages_block = site_packages_block.split("Get-ChildItem", 1)[0]
    assert "__pycache__" not in site_packages_block
    assert "*.pyc" not in site_packages_block
    app_src_block = build_script.split(
        "Staging service and Web frontend sources", 1
    )[1].split("$runtimeBin", 1)[0]
    assert '"/XF", "*.pyc"' not in app_src_block


def test_client_build_ships_site_packages_source_less():
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    # AV-bound unpack time scales with the file count, so site-packages is
    # compiled to legacy same-dir pycs (-b) and every .py with a compiled
    # twin is deleted afterwards.
    assert "compileall -q -f -b -j 0 $sitePackages" in build_script
    assert "site-packages made source-less" in build_script

    # Escape hatch: packages proven to read their own source at runtime keep
    # their .py files via an easily extendable name list. cv2 is the known
    # member: its loader searches for a literal config.py at import time.
    assert "$sourceRetainedPackages = @(" in build_script
    retained_block = build_script.split("$sourceRetainedPackages = @(", 1)[1]
    retained_block = retained_block.split("\n)", 1)[0]
    assert '"cv2"' in retained_block
    assert "$sourceRetainedPackages -contains $topLevelName" in build_script

    # The superseded dev-venv __pycache__ trees are dropped from the payload,
    # and numpy's unused Fortran tooling is removed wholesale.
    assert 'Recurse -Directory -Filter "__pycache__"' in build_script
    assert 'Join-Path $sitePackages "numpy\\f2py"' in build_script


def test_client_build_zips_stdlib_with_smoke_gates_and_rollback():
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    # The pure-Python stdlib collapses into one pythonXY.zip beside
    # python.exe - a getpath landmark CPython adds to sys.path by itself, so
    # the normal PYTHONHOME/PYTHONPATH mechanics the launcher depends on stay
    # intact.
    assert "function Invoke-StdlibZipCompression" in build_script
    assert "python%d%d.zip" in build_script

    # No ._pth may ever ship: its mere presence switches the interpreter to
    # isolated path mode, which ignores the PYTHONPATH the launcher uses to
    # pass app\src (whose location is not fixed relative to python.exe after
    # stable-bin materialization).
    assert "a ._pth file must never ship" in build_script
    assert "python311._pth" not in build_script

    # Hard gates run the staged interpreter from the zip: stdlib boot
    # (encodings proves the zip is on the default path), binary deps, and the
    # app package via launcher-style PYTHONPATH. Any failure reverts to the
    # plain loose Lib - a broken package must never ship.
    assert "import encodings,ssl,sqlite3,ctypes,asyncio,json,zipfile" in build_script
    assert "print('stdlib ok')" in build_script
    assert "import flask,sqlalchemy,numpy,cv2,mediapipe,aiohttp,livekit" in build_script
    assert "print('deps ok')" in build_script
    assert "import deskbot_server.main; print('app ok')" in build_script
    assert "Reverting the stdlib zip" in build_script

    # __file__-dependent stdlib packages stay loose on disk.
    assert '"site-packages", "tkinter", "idlelib", "turtledemo"' in build_script
    assert '"lib2to3", "ensurepip", "venv"' in build_script


def test_client_launcher_extracts_payload_in_parallel_with_progress():
    launcher_source = (
        SERVICE_ROOT / "client" / "OpenDeskBotV2Launcher.cs"
    ).read_text(encoding="utf-8")

    # Multi-threaded extraction: one ZipArchive reader per worker. The cap is
    # 16 (not the CPU-bound 8) because target machines run an AV that scans
    # each created file synchronously - workers overlap those scan stalls.
    assert "Parallel.ForEach" in launcher_source
    assert "MaxDegreeOfParallelism" in launcher_source
    assert "Math.Min(Environment.ProcessorCount, 16)" in launcher_source
    # Visible first-launch progress with a remaining-time estimate.
    assert "首次启动正在展开运行时（一次性，约" in launcher_source
    # The safety semantics of the sequential extractor are retained.
    assert "运行时压缩包包含不安全路径" in launcher_source  # Zip-Slip guard
    assert "sha.ComputeHash(payload)" in launcher_source  # payload SHA-256
    assert "Directory.Move(temporaryRuntime, runtimeRoot)" in launcher_source


def test_client_branding_icon_is_compiled_into_the_exe():
    icon_path = SERVICE_ROOT / "client" / "app.ico"
    assert icon_path.is_file()
    # Multi-size .ico, transparent background, roughly 83 KB.
    assert icon_path.read_bytes()[:4] == b"\x00\x00\x01\x00"

    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )
    assert "/win32icon:$iconPath" in build_script
    assert "/resource:$iconPath,app.ico" in build_script

    launcher_source = (
        SERVICE_ROOT / "client" / "OpenDeskBotV2Launcher.cs"
    ).read_text(encoding="utf-8")
    assert 'GetManifestResourceStream("app.ico")' in launcher_source
    assert "AppIcon.Tray" in launcher_source
    assert "AppIcon.Window" in launcher_source


def test_installer_script_declares_uninstall_registry_and_shortcuts():
    installer = (SERVICE_ROOT / "client" / "installer.nsi").read_text(
        encoding="utf-8-sig"
    )

    # MUI2 wizard, bilingual, x64 default location, elevated.
    assert '!include "MUI2.nsh"' in installer
    assert '!insertmacro MUI_LANGUAGE "SimpChinese"' in installer
    assert '!insertmacro MUI_LANGUAGE "English"' in installer
    assert "RequestExecutionLevel admin" in installer
    assert 'InstallDir "$PROGRAMFILES64\\${PRODUCT_DIRNAME}"' in installer

    # The wrapped portable EXE is the single released File.
    assert 'File "/oname=${PRODUCT_EXE}" "${ODB_SOURCE_EXE}"' in installer

    # Standard Add/Remove Programs uninstall registration.
    uninst_key = (
        "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenDeskBotV2"
    )
    assert uninst_key in installer
    for value in (
        "DisplayName",
        "DisplayVersion",
        "DisplayIcon",
        "Publisher",
        "UninstallString",
        "EstimatedSize",
        "NoModify",
        "NoRepair",
    ):
        assert f'"{value}"' in installer, f"missing uninstall value {value}"

    # Start Menu shortcut is unconditional; desktop is its own component.
    assert 'CreateShortcut "$SMPROGRAMS\\${PRODUCT_NAME}.lnk"' in installer
    assert 'CreateShortcut "$DESKTOP\\${PRODUCT_NAME}.lnk"' in installer

    # Optional auto-start writes an HKCU Run value; uninstall removes it.
    assert 'WriteRegStr HKCU "${RUNKEY}" "${RUNVALUE}"' in installer
    assert 'DeleteRegValue HKCU "${RUNKEY}" "${RUNVALUE}"' in installer


def test_installer_hardens_host_silently_and_reverses_on_uninstall():
    installer = (SERVICE_ROOT / "client" / "installer.nsi").read_text(
        encoding="utf-8-sig"
    )

    # (a) Defender exclusion on install, removed on uninstall.
    assert "Add-MpPreference -ExclusionPath" in installer
    assert "Remove-MpPreference -ExclusionPath" in installer
    # (b) The installer intentionally does NOT create firewall rules: the images
    # that bind a non-loopback socket live under per-user %LOCALAPPDATA%\bin,
    # which does not exist at install time. The launcher provisions those
    # path-based rules once on first run. The installer must document this
    # rather than add a wrong-image rule on $INSTDIR.
    assert "firewall allow rules are intentionally NOT created here" in installer
    assert "netsh advfirewall firewall add rule" not in installer
    # Best-effort: Defender uses -ErrorAction SilentlyContinue and hardening
    # goes through nsExec without failing the install.
    assert "-ErrorAction SilentlyContinue" in installer
    assert "nsExec::ExecToLog" in installer

    # Running client is stopped before an in-place upgrade overwrites the EXE.
    assert 'taskkill /F /T /IM "${PRODUCT_EXE}"' in installer

    # Uninstall keeps user data by default and asks before deleting it.
    assert "MB_DEFBUTTON2" in installer  # data-delete prompt defaults to No
    assert 'RMDir /r "${PRODUCT_DATADIR}"' in installer
    assert "$LOCALAPPDATA\\OpenDeskBotV2" in installer


def test_build_script_exposes_installer_switch_and_version_source():
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    # Opt-in installer build, off by default, with a discoverable makensis path.
    assert "[switch]$BuildInstaller" in build_script
    assert "[string]$MakeNsisPath" in build_script
    assert "winget install NSIS.NSIS" in build_script

    # The installer version is taken from the launcher AssemblyVersion so the
    # DisplayVersion matches the wrapped EXE.
    assert "function Resolve-InstallerVersion" in build_script
    assert "AssemblyVersion" in build_script

    # Build inputs are passed to makensis as /D defines.
    for define in (
        "/DODB_VERSION=",
        "/DODB_SOURCE_EXE=",
        "/DODB_ICON=",
        "/DODB_LICENSE=",
        "/DODB_OUTFILE=",
    ):
        assert define in build_script, f"missing makensis define {define}"

    # Installer artifact is emitted next to the EXE with a SHA-256 sidecar.
    assert "OpenDeskBotV2-Setup.exe" in build_script
    assert "Build-Installer -SourceExe $outputExe" in build_script


def test_client_hosts_frontend_in_embedded_webview2_window():
    launcher_source = (
        SERVICE_ROOT / "client" / "OpenDeskBotV2Launcher.cs"
    ).read_text(encoding="utf-8")
    build_script = (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )

    assert "internal sealed class RobotWindow" in launcher_source
    assert "new WebView2()" in launcher_source
    assert "CoreWebView2Environment.CreateAsync" in launcher_source
    assert "Program.OpenUrl(Program.WebUrl)" not in launcher_source
    assert "Microsoft.Web.WebView2.WinForms.dll" in build_script
    assert "WebView2Loader.dll" in build_script
