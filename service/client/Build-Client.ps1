[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [switch]$KeepBuildFiles,
    # Opt-in: copy the build machine's real .env into the LocalAppData
    # profile after the build. Off by default so a routine build never
    # spreads live credentials.
    [switch]$InstallLocalConfig,
    # Deprecated compatibility alias: skipping the .env install is now the
    # default behavior, so this switch is a no-op.
    [switch]$SkipLocalConfigInstall,
    # Expected SHA-256 (hex) of the pinned WebView2 SDK .nupkg. Overrides
    # the built-in value for the pinned version below.
    [string]$WebView2Sha256 = "",
    # Opt-in: after the portable EXE is built, wrap it in the standard NSIS
    # installer (service\dist\OpenDeskBotV2-Setup.exe). Off by default so a
    # routine build still produces only the green single-file EXE.
    [switch]$BuildInstaller,
    # makensis.exe used when -BuildInstaller is set. Defaults to the NSIS that
    # ships with Tauri's toolchain; a missing binary aborts with an install
    # hint. Ignored unless -BuildInstaller is set.
    [string]$MakeNsisPath = (Join-Path $env:LOCALAPPDATA "tauri\NSIS\makensis.exe"),
    # Opt-in: ship the face vision stack (mediapipe / cv2 / insightface and
    # their face-only transitive dependencies, plus the mediapipe model).
    # Off by default: the product no longer includes face features, and the
    # server degrades explicitly at runtime when the stack is absent (camera
    # preview and photo Q&A keep working; face detect/recognize/register are
    # reported as not installed). Matches pyproject's optional extra `face`.
    [switch]$IncludeFaceStack
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if ($SkipLocalConfigInstall) {
    Write-Warning (
        "-SkipLocalConfigInstall is deprecated and has no effect: the local " +
        ".env is no longer installed by default. Use -InstallLocalConfig to opt in."
    )
}

$clientRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$serviceRoot = (Resolve-Path -LiteralPath (Join-Path $clientRoot "..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $serviceRoot "dist"
}
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)

$shortTempRoot = "C:\tmp"
if (-not (Test-Path -LiteralPath $shortTempRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $shortTempRoot -Force | Out-Null
}
$buildRoot = [IO.Path]::GetFullPath(
    (Join-Path $shortTempRoot ("odv2-client-build-" + $PID))
)
$stageRoot = Join-Path $buildRoot "stage"
$stubPath = Join-Path $buildRoot "OpenDeskBotV2.stub.exe"
$payloadPath = Join-Path $buildRoot "runtime.zip"
$outputExe = Join-Path $outputRoot "OpenDeskBotV2.exe"
$webView2Version = "1.0.4129.50"
# SHA-256 of microsoft.web.webview2.1.0.4129.50.nupkg as served by
# api.nuget.org (verified 2026-08-18). The SDK assemblies from this package
# are compiled into the distributed EXE, so the download is pinned by hash:
# any mismatch aborts the build. Bumping $webView2Version requires updating
# this value (or passing -WebView2Sha256 for a one-off build).
$webView2PinnedSha256 = "d3934f482d484b89fb4825df720c710664e1143a1e90f7b3a60794ef33f473d2"

# Top-level site-packages entries (package directory or single-module name,
# without .py) that keep their .py sources in the payload. Everything else
# ships source-less - legacy same-dir .pyc only - because the target machines
# run an AV (ESET, no exclusions possible) whose synchronous per-file scan
# makes unpack/install time scale with the file count; dropping the .py twin
# of every shipped .pyc halves site-packages' file count. Add a name here
# whenever the source-less smoke test shows a package genuinely reads its own
# source at runtime (inspect.getsource at import/run, not just degraded
# tracebacks); staging then keeps that package's .py files and removes its
# redundant legacy .pyc twins instead (CPython ignores a same-dir .pyc while
# the .py exists).
$sourceRetainedPackages = @(
    # cv2: OpenCV's loader (cv2\__init__.py bootstrap) searches for literal
    # config*.py files by NAME next to __init__.py and execs them; deleting
    # the sources breaks `import cv2` outright (verified by the smoke gate).
    # (Only relevant with -IncludeFaceStack; the default build removes cv2.)
    "cv2"
)

# Face vision stack: top-level site-packages entries removed from the stage
# when -IncludeFaceStack is NOT set. Mirrors pyproject's optional extra
# `face` (mediapipe / opencv-contrib-python / insightface / onnx) plus the
# transitive dependencies that ONLY the face stack pulls in (verified via
# pip reverse-dependency walk, 2026-08-20).
# Intentionally NOT in this list:
#   - onnxruntime: required by livekit-plugins-silero (voice VAD) at runtime.
#   - flatbuffers / sounddevice: required by onnxruntime / livekit-agents.
#   - tqdm / requests / numpy / Pillow / python-dateutil / packaging: shared
#     with the voice, TTS (g2p-en -> nltk) and telemetry paths.
$faceStackSitePackages = @(
    "mediapipe", "mediapipe-*.dist-info",
    "cv2", "opencv_contrib_python-*.dist-info", "opencv_python-*.dist-info",
    "insightface", "insightface-*.dist-info",
    "onnx", "onnx-*.dist-info",
    # matplotlib chain (mediapipe is its only consumer here):
    "matplotlib", "matplotlib-*.dist-info", "matplotlib.libs",
    "mpl_toolkits", "pylab.py",
    "contourpy", "contourpy-*.dist-info",
    "cycler", "cycler-*.dist-info",
    "fontTools", "fonttools-*.dist-info",
    "kiwisolver", "kiwisolver-*.dist-info",
    "pyparsing", "pyparsing-*.dist-info",
    # scipy / scikit-image chain (insightface is the only consumer):
    "scipy", "scipy.libs", "scipy-*.dist-info",
    "skimage", "scikit_image-*.dist-info",
    "imageio", "imageio-*.dist-info",
    "tifffile", "tifffile-*.dist-info",
    "lazy_loader", "lazy_loader-*.dist-info",
    "networkx", "networkx-*.dist-info",
    # absl-py (mediapipe is the only consumer):
    "absl", "absl_py-*.dist-info"
)

function Remove-FaceStackFromStage {
    param([Parameter(Mandatory = $true)][string]$SitePackages)

    $removedFileCount = 0
    $removedByteCount = 0
    foreach ($pattern in $faceStackSitePackages) {
        # Get-Item, not Get-ChildItem: for a directory name the latter lists
        # the directory's CONTENTS instead of matching the directory itself,
        # which would delete the files but leave the (importable) package
        # directory behind.
        $matched = Get-Item `
            -Path (Join-Path $SitePackages $pattern) `
            -Force `
            -ErrorAction SilentlyContinue
        foreach ($entry in $matched) {
            if ($entry.PSIsContainer) {
                $measured = Get-ChildItem `
                    -LiteralPath $entry.FullName `
                    -Recurse `
                    -File `
                    -Force `
                    -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum
                $removedFileCount += [int]$measured.Count
                if ($measured.Sum) { $removedByteCount += $measured.Sum }
                Remove-Item -LiteralPath $entry.FullName -Recurse -Force
            } else {
                $removedFileCount++
                $removedByteCount += $entry.Length
                Remove-Item -LiteralPath $entry.FullName -Force
            }
        }
    }
    Write-Host (
        "  Face stack removed from stage: {0:N0} files, {1:N1} MB" -f
        $removedFileCount, ($removedByteCount / 1MB)
    )
}

function Assert-SafeBuildPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $safeParent = [IO.Path]::GetFullPath($shortTempRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    if (
        -not $resolved.StartsWith($safeParent, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($resolved)).StartsWith(
            "odv2-client-build-",
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Refusing to recursively modify unsafe build path: $resolved"
    }
}

function Remove-SafeBuildDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-SafeBuildPath -Path $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Copy-Tree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$ExtraArguments = @()
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Source directory was not found: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $arguments = @(
        $Source,
        $Destination,
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/R:2",
        "/W:1",
        "/XJ",
        "/MT:16",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    ) + $ExtraArguments
    & robocopy.exe @arguments | Out-Null
    $robocopyExit = $LASTEXITCODE
    if ($robocopyExit -ge 8) {
        throw "Robocopy failed ($robocopyExit): $Source -> $Destination"
    }
}

function Resolve-PythonHome {
    $venvConfigPath = Join-Path $serviceRoot ".venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $venvConfigPath -PathType Leaf)) {
        throw "Virtual environment configuration not found: $venvConfigPath"
    }
    $homeLine = Get-Content -LiteralPath $venvConfigPath |
        Where-Object { $_ -match "^\s*home\s*=" } |
        Select-Object -First 1
    if (-not $homeLine) {
        throw "Python home is missing from $venvConfigPath"
    }
    $pythonHome = [Environment]::ExpandEnvironmentVariables(
        (($homeLine -split "=", 2)[1]).Trim().Trim('"')
    )
    $pythonExe = Join-Path $pythonHome "python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "Base Python executable not found: $pythonExe"
    }
    return (Resolve-Path -LiteralPath $pythonHome).Path
}

function Find-CSharpCompiler {
    $command = Get-Command csc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $frameworkRoot = Join-Path $env:WINDIR "Microsoft.NET\Framework64"
    $candidate = Get-ChildItem `
        -LiteralPath $frameworkRoot `
        -Filter csc.exe `
        -File `
        -Recurse `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw ".NET Framework C# compiler (csc.exe) was not found."
    }
    return $candidate.FullName
}

function Test-WebView2PackageHash {
    param([Parameter(Mandatory = $true)][string]$PackagePath)

    $expectedSha256 = $WebView2Sha256
    if (-not $expectedSha256) {
        $expectedSha256 = $webView2PinnedSha256
    }
    if (-not $expectedSha256) {
        Write-Warning (
            "*** SUPPLY CHAIN WARNING: no expected SHA-256 is pinned for " +
            "Microsoft.Web.WebView2 $webView2Version. The downloaded SDK " +
            "will be compiled into the distributed EXE WITHOUT integrity " +
            "verification. Pin `$webView2PinnedSha256 or pass -WebView2Sha256. ***"
        )
        return
    }
    if ($expectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "The expected WebView2 SHA-256 is not a 64-digit hex string: $expectedSha256"
    }
    $actualSha256 = (Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash
    if (-not $actualSha256.Equals($expectedSha256, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $PackagePath -Force -ErrorAction SilentlyContinue
        throw (
            "Microsoft.Web.WebView2 $webView2Version failed SHA-256 verification. " +
            "Expected $($expectedSha256.ToLowerInvariant()) but the package " +
            "hashed to $($actualSha256.ToLowerInvariant()). The file has been " +
            "deleted; refusing to build with an unverified SDK."
        )
    }
}

function Resolve-WebView2Sdk {
    $cacheRoot = [IO.Path]::GetFullPath(
        (Join-Path $shortTempRoot "odv2-webview2-sdk\$webView2Version")
    )
    $packagePath = Join-Path $cacheRoot (
        "microsoft.web.webview2.$webView2Version.nupkg"
    )
    $packageRoot = Join-Path $cacheRoot "package"
    $coreAssembly = Join-Path $packageRoot "lib\net462\Microsoft.Web.WebView2.Core.dll"
    $formsAssembly = Join-Path $packageRoot "lib\net462\Microsoft.Web.WebView2.WinForms.dll"
    $nativeLoader = Join-Path $packageRoot "runtimes\win-x64\native\WebView2Loader.dll"
    $required = @($coreAssembly, $formsAssembly, $nativeLoader)

    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
        $temporaryPackage = $packagePath + ".download-" + $PID
        if (Test-Path -LiteralPath $temporaryPackage) {
            Remove-Item -LiteralPath $temporaryPackage -Force
        }
        $url = (
            "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/" +
            "$webView2Version/microsoft.web.webview2.$webView2Version.nupkg"
        )
        & curl.exe `
            -L `
            --fail `
            --retry 3 `
            --connect-timeout 30 `
            -o $temporaryPackage `
            $url
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporaryPackage)) {
            throw "Failed to download Microsoft.Web.WebView2 $webView2Version"
        }
        Move-Item -LiteralPath $temporaryPackage -Destination $packagePath -Force
    }

    # Verify the archive on every build - also when a cached copy exists -
    # and always re-extract from the verified archive, so nothing in a
    # possibly tampered cache can slip an unverified SDK into the EXE.
    # A hash mismatch deletes the file and aborts the build.
    Test-WebView2PackageHash -PackagePath $packagePath

    if (Test-Path -LiteralPath $packageRoot) {
        $resolvedPackageRoot = [IO.Path]::GetFullPath($packageRoot)
        $safeCachePrefix = [IO.Path]::GetFullPath($cacheRoot).TrimEnd(
            [IO.Path]::DirectorySeparatorChar
        ) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedPackageRoot.StartsWith(
            $safeCachePrefix,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to clear unsafe WebView2 package path: $resolvedPackageRoot"
        }
        Remove-Item -LiteralPath $resolvedPackageRoot -Recurse -Force
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($packagePath, $packageRoot)
    if (@($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -ne 0) {
        throw "Microsoft.Web.WebView2 package is incomplete: $packageRoot"
    }
    return $packageRoot
}

function Copy-PortablePython {
    param(
        [Parameter(Mandatory = $true)][string]$PythonHome,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $PythonHome -File | Where-Object {
        $_.Name -match '^(python(|w)\.exe|python3\.dll|python\d+\.dll|vcruntime.*\.dll|LICENSE\.txt)$'
    } | Copy-Item -Destination $Destination -Force

    Copy-Tree `
        -Source (Join-Path $PythonHome "DLLs") `
        -Destination (Join-Path $Destination "DLLs") `
        -ExtraArguments @("/XD", "__pycache__", "/XF", "*.pyc", "*.pyo")
    Copy-Tree `
        -Source (Join-Path $PythonHome "Lib") `
        -Destination (Join-Path $Destination "Lib") `
        -ExtraArguments @(
            "/XD",
            (Join-Path $PythonHome "Lib\site-packages"),
            "__pycache__",
            "/XF",
            "*.pyc",
            "*.pyo"
        )

    # __pycache__ / *.pyc are intentionally NOT excluded any more: the stage
    # is re-compiled with unchecked-hash pycs (Invoke-StageBytecodeCompilation
    # runs with -f, so any stale timestamp-mode cache copied from the dev venv
    # is overwritten) and the payload ships them so no user machine ever
    # compiles site-packages at first launch.
    $sitePackagesSource = Join-Path $serviceRoot ".venv\Lib\site-packages"
    $sitePackagesDestination = Join-Path $Destination "Lib\site-packages"
    Copy-Tree `
        -Source $sitePackagesSource `
        -Destination $sitePackagesDestination `
        -ExtraArguments @(
            "/XD",
            ".pytest_cache",
            ".ruff_cache"
        )

    Get-ChildItem `
        -LiteralPath $sitePackagesDestination `
        -Filter "__editable__.deskbot_server*.pth" `
        -File `
        -ErrorAction SilentlyContinue |
        Remove-Item -Force
    foreach ($virtualenvArtifact in @("_virtualenv.pth", "_virtualenv.py")) {
        $artifactPath = Join-Path $sitePackagesDestination $virtualenvArtifact
        if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
            Remove-Item -LiteralPath $artifactPath -Force
        }
    }
}

function Invoke-StageBytecodeCompilation {
    param([Parameter(Mandatory = $true)][string]$StageRoot)

    $stagedPython = Join-Path $StageRoot "python\python.exe"
    $sitePackages = Join-Path $StageRoot "python\Lib\site-packages"
    $appSources = Join-Path $StageRoot "app\src"
    foreach ($required in @($stagedPython, $sitePackages, $appSources)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Bytecode precompilation target is missing: $required"
        }
    }

    # app\src keeps its .py sources: field debugging needs tracebacks with
    # real source lines from our own business code. It is precompiled with
    # the staged (= shipped) interpreter into __pycache__ so the launcher's
    # PYTHONDONTWRITEBYTECODE=1 never forces an in-memory recompile.
    #
    # --invalidation-mode unchecked-hash (PEP 552): the .pyc header stores a
    # source hash that the interpreter never re-validates at import time, so
    # the cache always hits even though first-launch extraction rewrites file
    # timestamps (zip DOS times have 2-second granularity, so timestamp-mode
    # pycs would practically always mismatch after unpacking into a fresh
    # directory and be recompiled in memory on every start). checked-hash
    # would also survive the unpack, but re-reads and re-hashes each source
    # file on import; unchecked-hash is safe here because the runtime tree is
    # immutable - versioned by payload SHA-256 and replaced wholesale on
    # upgrade. -f forces regeneration so a stale timestamp-mode cache copied
    # in from the dev venv can never ship (compileall's up-to-date probe
    # would otherwise skip files whose dev pyc still matches its mtime).
    & $stagedPython -E -s -m compileall -q -f -j 0 `
        --invalidation-mode unchecked-hash $appSources
    if ($LASTEXITCODE -ne 0) {
        throw "Bytecode precompilation failed for app\src (exit $LASTEXITCODE)"
    }

    # numpy.f2py is the Fortran-wrapper developer tool (own CLI, tests and
    # source templates, ~200 files); numpy never imports it at runtime, so it
    # is removed wholesale before compilation.
    $f2pyRoot = Join-Path $sitePackages "numpy\f2py"
    if (Test-Path -LiteralPath $f2pyRoot -PathType Container) {
        Remove-Item -LiteralPath $f2pyRoot -Recurse -Force
    }

    # site-packages ships source-less to halve its file count (see the
    # $sourceRetainedPackages note at the top): -b compiles to legacy
    # same-dir .pyc instead of __pycache__, then every .py that gained a
    # compiled twin is deleted. A source-less legacy .pyc is loaded without
    # any header validation at import, so the extraction rewriting file
    # timestamps on the user machine is irrelevant for these.
    & $stagedPython -E -s -m compileall -q -f -b -j 0 $sitePackages
    if ($LASTEXITCODE -ne 0) {
        # Vendored third-party trees can contain files that intentionally do
        # not compile on this interpreter (version-gated syntax, test
        # fixtures). Importing those would fail at runtime regardless, so a
        # partial failure only means the affected files keep their .py and
        # ship without a .pyc.
        Write-Warning (
            "compileall reported failures inside site-packages " +
            "(exit $LASTEXITCODE); uncompilable files keep their .py."
        )
    }

    # The dev venv's __pycache__ trees (copied verbatim while staging) are
    # superseded by the legacy-location pycs; drop them before pruning.
    Get-ChildItem -LiteralPath $sitePackages -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force

    $sitePackagesPrefix = [IO.Path]::GetFullPath($sitePackages).TrimEnd(
        [IO.Path]::DirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    $removedSourceCount = 0
    $retainedSourceCount = 0
    Get-ChildItem -LiteralPath $sitePackages -Recurse -Filter "*.py" -File |
        Where-Object { $_.Extension -eq ".py" } |
        ForEach-Object {
            $relativePath = $_.FullName.Substring($sitePackagesPrefix.Length)
            $topLevelName = ($relativePath -split '[\\/]', 2)[0] -replace '\.py$', ''
            $legacyPyc = [IO.Path]::ChangeExtension($_.FullName, ".pyc")
            if ($sourceRetainedPackages -contains $topLevelName) {
                # Keep the source; its same-dir .pyc would be dead weight
                # (CPython ignores it while the .py exists).
                if (Test-Path -LiteralPath $legacyPyc -PathType Leaf) {
                    Remove-Item -LiteralPath $legacyPyc -Force
                }
                $retainedSourceCount++
            } elseif (Test-Path -LiteralPath $legacyPyc -PathType Leaf) {
                Remove-Item -LiteralPath $_.FullName -Force
                $removedSourceCount++
            }
        }
    Write-Host (
        ("  site-packages made source-less: {0:N0} .py removed, " +
        "{1:N0} kept via `$sourceRetainedPackages") -f
        $removedSourceCount, $retainedSourceCount
    )

    $pycBytes = 0
    foreach ($measuredRoot in @($sitePackages, $appSources)) {
        $measured = Get-ChildItem `
            -LiteralPath $measuredRoot `
            -Recurse `
            -Filter "*.pyc" `
            -File `
            -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum
        if ($measured.Sum) {
            $pycBytes += $measured.Sum
        }
    }
    Write-Host ("  Precompiled bytecode in stage: {0:N1} MB (uncompressed)" -f ($pycBytes / 1MB))
}

function Invoke-StdlibZipCompression {
    param([Parameter(Mandatory = $true)][string]$StageRoot)

    $pythonRoot = Join-Path $StageRoot "python"
    $stagedPython = Join-Path $pythonRoot "python.exe"
    $libRoot = Join-Path $pythonRoot "Lib"
    foreach ($required in @($stagedPython, $libRoot)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Stdlib zip target is missing: $required"
        }
    }

    # Stdlib directories that keep their loose files on disk (never zipped):
    # site-packages obviously, plus stdlib packages that locate sibling data
    # files via __file__ at runtime (a module imported from the zip gets a
    # zip-internal __file__, so on-disk data next to it would be unreachable).
    $looseStdlibDirs = @(
        "site-packages", "tkinter", "idlelib", "turtledemo",
        "lib2to3", "ensurepip", "venv"
    )

    # pythonXY.zip directly beside python.exe is a getpath landmark that
    # CPython appends to sys.path on its own, with the normal site machinery
    # left fully intact. That is also exactly why NO pythonXY._pth file is
    # written: the mere presence of a ._pth switches the interpreter to its
    # isolated path mode, which ignores PYTHONPATH and PYTHONHOME - and the
    # launcher feeds app\src to the interpreter through PYTHONPATH from a
    # directory whose position relative to python.exe is not fixed (stable
    # bin materialization copies python\ away from the versioned runtime),
    # so a ._pth file must never ship.
    $zipName = (
        & $stagedPython -E -s -c "import sys; print('python%d%d.zip' % sys.version_info[:2])"
    ) | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $zipName -or $zipName -notmatch '^python\d+\.zip$') {
        throw "Could not resolve the stdlib zip name from the staged interpreter."
    }
    $zipName = $zipName.Trim()
    $zipPath = Join-Path $pythonRoot $zipName
    Write-Host "  Zipping the pure-Python standard library into $zipName..."

    # 1) Mirror the zip-bound sources into a scratch tree and compile them to
    #    legacy-location pycs there (-b). Only .pyc files enter the zip; a
    #    source-less pyc is loaded by zipimport without any header check.
    $zipBuildRoot = Join-Path $buildRoot "stdlib-zip"
    if (Test-Path -LiteralPath $zipBuildRoot) {
        Remove-Item -LiteralPath $zipBuildRoot -Recurse -Force
    }
    $excludedDirPaths = @()
    foreach ($looseName in $looseStdlibDirs) {
        $excludedDirPaths += (Join-Path $libRoot $looseName)
    }
    Copy-Tree `
        -Source $libRoot `
        -Destination $zipBuildRoot `
        -ExtraArguments (@("*.py", "/XD") + $excludedDirPaths + @("__pycache__"))
    & $stagedPython -E -s -m compileall -q -f -b -j 0 $zipBuildRoot
    if ($LASTEXITCODE -ne 0) {
        # Lib\test ships intentional bad-syntax fixtures; anything that does
        # not compile stays out of the zip and keeps its loose .py instead.
        Write-Warning (
            "compileall reported failures while building $zipName " +
            "(exit $LASTEXITCODE); the affected files stay outside the zip."
        )
    }
    Get-ChildItem -LiteralPath $zipBuildRoot -Recurse -Filter "*.py" -File |
        Where-Object { $_.Extension -eq ".py" } |
        Remove-Item -Force
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $zipBuildRoot,
        $zipPath,
        [IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    # 2) Rebuild Lib with only what must stay loose: the excluded directories,
    #    every non-.py data file of zipped packages (kept at its old path for
    #    tools that open them as plain files), and any .py that failed to
    #    compile into the zip. The pure-py originals are parked in Lib._prezip
    #    until the smoke gates pass, so a failure can restore them verbatim.
    $libBackup = Join-Path $pythonRoot "Lib._prezip"
    if (Test-Path -LiteralPath $libBackup) {
        Remove-Item -LiteralPath $libBackup -Recurse -Force
    }
    Rename-Item -LiteralPath $libRoot -NewName "Lib._prezip"
    New-Item -ItemType Directory -Path $libRoot -Force | Out-Null
    foreach ($looseName in $looseStdlibDirs) {
        $looseSource = Join-Path $libBackup $looseName
        if (Test-Path -LiteralPath $looseSource) {
            Move-Item -LiteralPath $looseSource -Destination (Join-Path $libRoot $looseName)
        }
    }
    $backupPrefix = [IO.Path]::GetFullPath($libBackup).TrimEnd(
        [IO.Path]::DirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    $backupFiles = @(Get-ChildItem -LiteralPath $libBackup -Recurse -File)
    $backupFiles |
        ForEach-Object {
            $relativePath = $_.FullName.Substring($backupPrefix.Length)
            if ($relativePath -match '(^|\\)__pycache__(\\|$)') {
                # Side-effect caches written by the staging interpreter runs;
                # orphaned (and never loaded) once the sources live in the zip.
                return
            }
            if ($_.Extension -eq ".py") {
                $zippedPyc = Join-Path $zipBuildRoot (
                    $relativePath -replace '\.py$', '.pyc'
                )
                if (Test-Path -LiteralPath $zippedPyc -PathType Leaf) {
                    return  # lives in the zip now
                }
            }
            $target = Join-Path $libRoot $relativePath
            $targetParent = Split-Path -Path $target -Parent
            if (-not (Test-Path -LiteralPath $targetParent)) {
                New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            }
            Move-Item -LiteralPath $_.FullName -Destination $target
        }

    # 3) Hard gate: the staged interpreter must actually run from the zip
    #    (encodings is needed at interpreter boot, so gate 1 proves the zip
    #    is on the default path), the binary deps must import, and the app
    #    package must import through PYTHONPATH exactly like the launcher
    #    passes it. Any failure reverts to the plain loose Lib - a slower
    #    unpack is acceptable, a broken package is not.
    $failedGate = Invoke-StageSmokeGates -StageRoot $StageRoot

    if ($failedGate) {
        # Full rollback: put every loose file back and drop the zip, so the
        # payload ships the previous known-good plain Lib layout.
        Write-Warning (
            "Reverting the stdlib zip (gate '$failedGate' failed); " +
            "the payload ships the plain Lib tree instead."
        )
        & robocopy.exe $libRoot $libBackup /E /MOVE /R:2 /W:1 /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "Failed to restore the plain Lib tree after a gate failure."
        }
        if (Test-Path -LiteralPath $libRoot) {
            Remove-Item -LiteralPath $libRoot -Recurse -Force
        }
        Rename-Item -LiteralPath $libBackup -NewName "Lib"
        Remove-Item -LiteralPath $zipPath -Force

        # The gates also cover the source-less site-packages prune, and a bad
        # prune is not the zip's fault. Re-run against the restored plain
        # Lib: if a gate still fails, the stage itself is broken - abort the
        # build rather than ship it, and add the offending package to
        # $sourceRetainedPackages.
        $stillFailing = Invoke-StageSmokeGates -StageRoot $StageRoot
        if ($stillFailing) {
            throw (
                "Stage smoke gate '$stillFailing' still fails with the plain " +
                "Lib tree; the source-less site-packages prune likely broke " +
                "a package - add it to `$sourceRetainedPackages and rebuild."
            )
        }
        return $false
    }

    Remove-Item -LiteralPath $libBackup -Recurse -Force
    $zipInfo = Get-Item -LiteralPath $zipPath
    Write-Host ("  {0}: {1:N1} MB; loose Lib now holds only site-packages, " -f $zipName, ($zipInfo.Length / 1MB))
    Write-Host "  data files and __file__-dependent stdlib packages."
    return $true
}

function Invoke-StageSmokeGates {
    param([Parameter(Mandatory = $true)][string]$StageRoot)

    # Runs the three import smoke gates with the staged interpreter under the
    # same environment shape the launcher builds (BuildPythonStartInfo in
    # OpenDeskBotV2Launcher.cs). Returns the name of the first failing gate,
    # or $null when all pass.
    $pythonRoot = Join-Path $StageRoot "python"
    $stagedPython = Join-Path $pythonRoot "python.exe"
    $libRoot = Join-Path $pythonRoot "Lib"
    # The 'deps' gate is tiered by -IncludeFaceStack. The default (faceless)
    # tier verifies the retained runtime deps AND asserts the face stack is
    # really gone: a child-process import of mediapipe/cv2/insightface must
    # raise ImportError, otherwise the stage prune leaked face packages.
    if ($IncludeFaceStack) {
        $depsGateCode = (
            "import flask,sqlalchemy,numpy,cv2,mediapipe,aiohttp,livekit; " +
            "print('deps ok')"
        )
    } else {
        $depsGateCode = @'
import flask, sqlalchemy, numpy, aiohttp, livekit
leaked = []
for name in ("mediapipe", "cv2", "insightface"):
    try:
        __import__(name)
    except ImportError:
        pass
    else:
        leaked.append(name)
if leaked:
    raise SystemExit(
        "face stack packages leaked into the faceless stage: %r" % (leaked,)
    )
print('deps ok')
'@
    }
    $gateChecks = @(
        @{
            Name = "stdlib"
            Code = "import encodings,ssl,sqlite3,ctypes,asyncio,json,zipfile; print('stdlib ok')"
        },
        @{
            Name = "deps"
            Code = $depsGateCode
        },
        @{
            Name = "app"
            Code = "import deskbot_server.main; print('app ok')"
        }
    )
    $gateProfileRoot = Join-Path $buildRoot "stdlib-zip-gate-profile"
    New-Item -ItemType Directory -Path $gateProfileRoot -Force | Out-Null
    $gateEnvironment = @{
        # Mirrors BuildPythonStartInfo in OpenDeskBotV2Launcher.cs.
        PYTHONHOME = $pythonRoot
        PYTHONPATH = (
            (Join-Path $StageRoot "app\src") + [IO.Path]::PathSeparator +
            (Join-Path $libRoot "site-packages")
        )
        PYTHONNOUSERSITE = "1"
        PYTHONDONTWRITEBYTECODE = "1"
        PYTHONUTF8 = "1"
        # Keep the app import from touching the real LocalAppData profile.
        DESKBOT_PROJECT_ROOT = $gateProfileRoot
        DESKBOT_DATA_DIR = (Join-Path $gateProfileRoot "state")
        DESKBOT_MODELS_DIR = (Join-Path $gateProfileRoot "models")
    }
    $savedEnvironment = @{}
    foreach ($name in $gateEnvironment.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
        [Environment]::SetEnvironmentVariable($name, $gateEnvironment[$name])
    }
    $failedGate = $null
    try {
        foreach ($gate in $gateChecks) {
            $stdoutPath = Join-Path $buildRoot ("gate-" + $gate.Name + ".out")
            $stderrPath = Join-Path $buildRoot ("gate-" + $gate.Name + ".err")
            # Gate code runs from a file, not -c: the faceless 'deps' gate is
            # multi-line (try/except negative import assertions) which cannot
            # survive command-line quoting.
            $gateScriptPath = Join-Path $buildRoot ("gate-" + $gate.Name + ".py")
            [IO.File]::WriteAllText(
                $gateScriptPath,
                $gate.Code + [Environment]::NewLine,
                (New-Object Text.UTF8Encoding($false))
            )
            $gateProcess = Start-Process `
                -FilePath $stagedPython `
                -ArgumentList @('"' + $gateScriptPath + '"') `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath `
                -NoNewWindow `
                -Wait `
                -PassThru
            $gateStdout = ""
            if (Test-Path -LiteralPath $stdoutPath) {
                $gateStdout = (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue)
                if ($null -eq $gateStdout) { $gateStdout = "" }
            }
            $expectedToken = $gate.Name + " ok"
            if ($gateProcess.ExitCode -ne 0 -or -not $gateStdout.Contains($expectedToken)) {
                $gateStderr = ""
                if (Test-Path -LiteralPath $stderrPath) {
                    $gateStderr = (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
                }
                $failedGate = $gate.Name
                Write-Warning (
                    "Stdlib zip smoke gate '$($gate.Name)' failed " +
                    "(exit $($gateProcess.ExitCode)). stderr:`n$gateStderr"
                )
                break
            }
            Write-Host ("  Gate '{0}' passed: {1}" -f $gate.Name, $gateStdout.Trim())
        }
    } finally {
        foreach ($name in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name])
        }
    }

    return $failedGate
}

function Build-LauncherStub {
    param(
        [Parameter(Mandatory = $true)][string]$Compiler,
        [Parameter(Mandatory = $true)][string]$WebView2Sdk,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $sourcePath = Join-Path $clientRoot "OpenDeskBotV2Launcher.cs"
    $manifestPath = Join-Path $clientRoot "OpenDeskBotV2Launcher.xml"
    $iconPath = Join-Path $clientRoot "app.ico"
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
        throw "Client icon not found: $iconPath"
    }
    $webView2Core = Join-Path $WebView2Sdk "lib\net462\Microsoft.Web.WebView2.Core.dll"
    $webView2Forms = Join-Path $WebView2Sdk "lib\net462\Microsoft.Web.WebView2.WinForms.dll"
    # app.ico is wired in twice on purpose: /win32icon paints the EXE's shell
    # icon, /resource embeds the same multi-size .ico so the tray icon and the
    # window title bars render identical artwork without any external file.
    & $Compiler `
        /nologo `
        /target:winexe `
        /optimize+ `
        /platform:x64 `
        "/out:$Destination" `
        "/win32manifest:$manifestPath" `
        "/win32icon:$iconPath" `
        "/resource:$iconPath,app.ico" `
        /reference:System.dll `
        /reference:System.Core.dll `
        /reference:System.Drawing.dll `
        /reference:System.Windows.Forms.dll `
        /reference:System.IO.Compression.dll `
        /reference:System.IO.Compression.FileSystem.dll `
        "/reference:$webView2Core" `
        "/reference:$webView2Forms" `
        "/resource:$webView2Core,Microsoft.Web.WebView2.Core.dll" `
        "/resource:$webView2Forms,Microsoft.Web.WebView2.WinForms.dll" `
        $sourcePath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Destination)) {
        throw "C# launcher compilation failed with exit code $LASTEXITCODE"
    }
}

function Add-PayloadToExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Stub,
        [Parameter(Mandatory = $true)][string]$Payload,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $payloadInfo = Get-Item -LiteralPath $Payload
    $payloadHashHex = (Get-FileHash -LiteralPath $Payload -Algorithm SHA256).Hash
    $payloadHash = New-Object byte[] 32
    for ($index = 0; $index -lt $payloadHash.Length; $index++) {
        $payloadHash[$index] = [Convert]::ToByte(
            $payloadHashHex.Substring($index * 2, 2),
            16
        )
    }
    $magic = [Text.Encoding]::ASCII.GetBytes("ODBV2PKG")

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }
    $destinationStream = [IO.File]::Open(
        $Destination,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        foreach ($sourcePath in @($Stub, $Payload)) {
            $sourceStream = [IO.File]::OpenRead($sourcePath)
            try {
                $sourceStream.CopyTo($destinationStream, 1MB)
            } finally {
                $sourceStream.Dispose()
            }
        }
        $writer = New-Object IO.BinaryWriter(
            $destinationStream,
            (New-Object Text.UTF8Encoding($false)),
            $true
        )
        try {
            $writer.Write([long]$payloadInfo.Length)
            $writer.Write([byte[]]$payloadHash)
            $writer.Write([byte[]]$magic)
            $writer.Flush()
        } finally {
            $writer.Dispose()
        }
    } finally {
        $destinationStream.Dispose()
    }
}

function Install-CurrentMachineConfig {
    # Copying the build machine's real .env (live cloud credentials) into
    # the LocalAppData profile is strictly opt-in via -InstallLocalConfig;
    # a default build never touches local secrets.
    if (-not $InstallLocalConfig) {
        return
    }
    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    $profileRoot = Join-Path $localAppData "OpenDeskBotV2"
    New-Item -ItemType Directory -Path $profileRoot -Force | Out-Null
    $sourceEnv = Join-Path $serviceRoot ".env"
    $profileEnv = Join-Path $profileRoot ".env"
    if (
        (Test-Path -LiteralPath $sourceEnv -PathType Leaf) -and
        -not (Test-Path -LiteralPath $profileEnv -PathType Leaf)
    ) {
        [IO.File]::Copy($sourceEnv, $profileEnv, $false)
        Write-Host "Installed the current .env into the private LocalAppData profile."
    }
}

function Resolve-InstallerVersion {
    # The installer version is taken from the launcher's AssemblyVersion - the
    # very number stamped into the EXE the installer wraps - so the Add/Remove
    # Programs "DisplayVersion" always matches the shipped client. It is a
    # proper 4-part Windows version (2.0.0.0), which NSIS's VIProductVersion
    # also requires; the Python package version (pyproject 0.1.0) is a 3-part
    # PEP 440 string that would fail that check and does not track the client.
    $launcherSource = Join-Path $clientRoot "OpenDeskBotV2Launcher.cs"
    if (Test-Path -LiteralPath $launcherSource -PathType Leaf) {
        $match = Select-String `
            -LiteralPath $launcherSource `
            -Pattern 'AssemblyVersion\("([0-9]+(?:\.[0-9]+){3})"\)' `
            -List
        if ($match) {
            return $match.Matches[0].Groups[1].Value
        }
    }
    $manifest = Join-Path $clientRoot "OpenDeskBotV2Launcher.xml"
    if (Test-Path -LiteralPath $manifest -PathType Leaf) {
        $match = Select-String `
            -LiteralPath $manifest `
            -Pattern 'version="([0-9]+(?:\.[0-9]+){3})"' `
            -List
        if ($match) {
            return $match.Matches[0].Groups[1].Value
        }
    }
    throw "Could not resolve the client version from the launcher sources."
}

function Build-Installer {
    param(
        [Parameter(Mandatory = $true)][string]$SourceExe,
        [Parameter(Mandatory = $true)][string]$OutputRoot
    )

    if (-not (Test-Path -LiteralPath $MakeNsisPath -PathType Leaf)) {
        throw (
            "makensis.exe was not found at '$MakeNsisPath'. Install NSIS " +
            "(e.g. 'winget install NSIS.NSIS') or pass -MakeNsisPath to point " +
            "at an existing makensis.exe."
        )
    }
    $scriptPath = Join-Path $clientRoot "installer.nsi"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Installer script not found: $scriptPath"
    }
    $iconPath = Join-Path $clientRoot "app.ico"
    $licensePath = Join-Path $serviceRoot "LICENSE"
    $setupExe = Join-Path $OutputRoot "OpenDeskBotV2-Setup.exe"
    $version = Resolve-InstallerVersion

    Write-Host "Building the Windows installer (NSIS)..."
    Write-Host "  makensis: $MakeNsisPath"
    Write-Host "  Version:  $version"
    Write-Host "  Source:   $SourceExe"
    Write-Host "  Output:   $setupExe"

    if (Test-Path -LiteralPath $setupExe) {
        Remove-Item -LiteralPath $setupExe -Force
    }

    # All paths flow in as /D defines so installer.nsi never hardcodes a build
    # machine layout; its !ifndef fallbacks keep it hand-compilable too.
    & $MakeNsisPath `
        "/DODB_VERSION=$version" `
        "/DODB_SOURCE_EXE=$SourceExe" `
        "/DODB_ICON=$iconPath" `
        "/DODB_LICENSE=$licensePath" `
        "/DODB_OUTFILE=$setupExe" `
        $scriptPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setupExe)) {
        throw "makensis failed to build the installer (exit $LASTEXITCODE)."
    }

    $setupHash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(
        $setupExe + ".sha256",
        $setupHash + "  " + [IO.Path]::GetFileName($setupExe) + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )
    $setup = Get-Item -LiteralPath $setupExe
    Write-Host "Installer build complete."
    Write-Host ("  Size:   {0:N2} MB" -f ($setup.Length / 1MB))
    Write-Host "  SHA256: $setupHash"
    Write-Host "  File:   $($setup.FullName)"
}

$pythonHome = Resolve-PythonHome
$compiler = Find-CSharpCompiler
$webView2Sdk = Resolve-WebView2Sdk
Write-Host "Building Open Desk Bot V2 client"
Write-Host "  Service root: $serviceRoot"
Write-Host "  Python home:  $pythonHome"
Write-Host "  Compiler:     $compiler"
Write-Host "  WebView2 SDK: $webView2Sdk"
Write-Host "  Output:       $outputExe"

Remove-SafeBuildDirectory -Path $buildRoot
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

try {
    Write-Host "Compiling the Windows tray launcher..."
    Build-LauncherStub `
        -Compiler $compiler `
        -WebView2Sdk $webView2Sdk `
        -Destination $stubPath

    Write-Host "Staging portable Python and native dependencies..."
    Copy-PortablePython `
        -PythonHome $pythonHome `
        -Destination (Join-Path $stageRoot "python")

    if (-not $IncludeFaceStack) {
        # Default build ships without the face vision stack. Pruning happens
        # before bytecode precompilation so the removed trees are neither
        # compiled nor shipped; the faceless 'deps' smoke gate then asserts
        # the prune actually removed the imports.
        Write-Host "Removing the face vision stack from the stage (default)..."
        Remove-FaceStackFromStage `
            -SitePackages (Join-Path $stageRoot "python\Lib\site-packages")
    } else {
        Write-Host "Keeping the face vision stack in the stage (-IncludeFaceStack)."
    }

    Write-Host "Staging service and Web frontend sources..."
    # No __pycache__ / *.pyc exclusion: see Invoke-StageBytecodeCompilation.
    Copy-Tree `
        -Source (Join-Path $serviceRoot "src") `
        -Destination (Join-Path $stageRoot "app\src")

    $runtimeBin = Join-Path $stageRoot "app\bin"
    New-Item -ItemType Directory -Path $runtimeBin -Force | Out-Null
    $liveKitBinary = Join-Path $serviceRoot "data\local\livekit\livekit-server.exe"
    if (-not (Test-Path -LiteralPath $liveKitBinary -PathType Leaf)) {
        throw "LiveKit Server binary not found: $liveKitBinary"
    }
    Copy-Item -LiteralPath $liveKitBinary -Destination $runtimeBin -Force

    $webView2NativeRoot = Join-Path $stageRoot "client\x64"
    New-Item -ItemType Directory -Path $webView2NativeRoot -Force | Out-Null
    Copy-Item `
        -LiteralPath (Join-Path $webView2Sdk "runtimes\win-x64\native\WebView2Loader.dll") `
        -Destination $webView2NativeRoot `
        -Force

    # silero_vad always ships (voice VAD); the mediapipe face model only ships
    # with -IncludeFaceStack.
    $stagedModelNames = @("silero_vad")
    if ($IncludeFaceStack) {
        $stagedModelNames = @("mediapipe", "silero_vad")
    }
    foreach ($modelName in $stagedModelNames) {
        Copy-Tree `
            -Source (Join-Path $serviceRoot "models\$modelName") `
            -Destination (Join-Path $stageRoot "app\models\$modelName") `
            -ExtraArguments @("/XD", "__pycache__", "/XF", "*.pyc", "*.pyo")
    }

    Write-Host "Staging non-secret initial configuration..."
    $seedRoot = Join-Path $stageRoot "seed"
    New-Item -ItemType Directory -Path $seedRoot -Force | Out-Null
    Copy-Item `
        -LiteralPath (Join-Path $serviceRoot "config.yaml") `
        -Destination (Join-Path $seedRoot "config.yaml") `
        -Force
    Copy-Item `
        -LiteralPath (Join-Path $serviceRoot ".env.example") `
        -Destination (Join-Path $seedRoot ".env.example") `
        -Force
    Copy-Tree `
        -Source (Join-Path $serviceRoot "data\global") `
        -Destination (Join-Path $seedRoot "data\global")

    $commit = (& git -C $serviceRoot rev-parse HEAD).Trim()
    $manifest = [ordered]@{
        product = "Open Desk Bot V2"
        built_at = (Get-Date).ToUniversalTime().ToString("o")
        git_commit = $commit
        python = (& (Join-Path $pythonHome "python.exe") --version 2>&1).ToString()
        face_stack = [bool]$IncludeFaceStack
        excludes = @(".env", "databases", "runtime logs", "SenseVoiceSmall")
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText(
        (Join-Path $stageRoot "runtime-manifest.json"),
        $manifest + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )

    Write-Host "Precompiling Python bytecode into the stage..."
    Invoke-StageBytecodeCompilation -StageRoot $stageRoot

    # File-count matters more than bytes here: the target machines run an AV
    # whose synchronous per-file scan makes unpack time scale with the file
    # count, so the stdlib is collapsed into one pythonXY.zip. The step is
    # self-verifying and reverts itself (keeping the plain Lib) if the staged
    # interpreter cannot pass the import smoke gates from the zip.
    Write-Host "Zipping the Python standard library..."
    Invoke-StdlibZipCompression -StageRoot $stageRoot | Out-Null

    $stageFileCount = 0
    $stageByteCount = 0
    foreach ($stagedFile in [IO.Directory]::EnumerateFiles(
        $stageRoot, "*", [IO.SearchOption]::AllDirectories
    )) {
        $stageFileCount++
        $stageByteCount += ([IO.FileInfo]::new($stagedFile)).Length
    }
    Write-Host (
        "  Stage contents: {0:N0} files, {1:N1} MB (uncompressed)" -f
        $stageFileCount, ($stageByteCount / 1MB)
    )

    Write-Host "Packing the runtime payload (stored, no compression)..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $payloadPath) {
        Remove-Item -LiteralPath $payloadPath -Force
    }
    # Stored, not Deflate: extraction on the user's machine was measured to be
    # CPU-bound on per-entry decompression (launcher at ~4 cores, AV idle).
    # Storing turns first-run/install-time extraction into a plain disk copy
    # (minutes -> seconds) at the cost of a larger single-file EXE, which is
    # acceptable for LAN distribution. The stdlib python zip inside the stage
    # keeps its own compression, so the size increase is bounded by
    # site-packages/native binaries.
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $stageRoot,
        $payloadPath,
        [IO.Compression.CompressionLevel]::NoCompression,
        $false
    )

    Write-Host "Creating the single-file client executable..."
    Add-PayloadToExecutable `
        -Stub $stubPath `
        -Payload $payloadPath `
        -Destination $outputExe

    $artifactHash = (Get-FileHash -LiteralPath $outputExe -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(
        $outputExe + ".sha256",
        $artifactHash + "  " + [IO.Path]::GetFileName($outputExe) + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )
    Install-CurrentMachineConfig

    $artifact = Get-Item -LiteralPath $outputExe
    Write-Host "Build complete."
    Write-Host ("  Size:   {0:N2} MB" -f ($artifact.Length / 1MB))
    Write-Host "  SHA256: $artifactHash"
    Write-Host "  File:   $($artifact.FullName)"

    if ($BuildInstaller) {
        Build-Installer -SourceExe $outputExe -OutputRoot $outputRoot
    }
} finally {
    if (-not $KeepBuildFiles) {
        Remove-SafeBuildDirectory -Path $buildRoot
    } else {
        Write-Host "Intermediate build files kept at: $buildRoot"
    }
}
