[CmdletBinding()]
param(
    # Empty means auto-discover Espressif native USB CDC (VID 303A).  Keep an
    # explicit override only for diagnostics; COM numbers are not identities.
    [string]$SerialPorts = "",
    [ValidateRange(1, 65535)]
    [int]$CorePort = 9000,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 5050,
    [ValidateRange(1, 300)]
    [int]$StartupTimeoutSeconds = 75
)

$ErrorActionPreference = "Stop"

# Some desktop launchers can inject both ``Path`` and ``PATH`` into the same
# Windows process environment.  Windows itself tolerates that environment
# block, but PowerShell's Start-Process materializes it into a
# case-insensitive dictionary and fails before the child is created.  Keep the
# ordinary ``Path`` entry and remove only the duplicate uppercase variant.
$pathKeys = @(
    [Environment]::GetEnvironmentVariables("Process").Keys |
        Where-Object { [string]$_ -ieq "Path" }
)
if ($pathKeys -ccontains "Path" -and $pathKeys -ccontains "PATH") {
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
}

$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvRoot = Join-Path $serviceRoot ".venv"
$venvConfigPath = Join-Path $venvRoot "pyvenv.cfg"
$venvScripts = Join-Path $venvRoot "Scripts"
$sitePackages = Join-Path $venvRoot "Lib\site-packages"
$srcRoot = Join-Path $serviceRoot "src"
$logRoot = Join-Path $serviceRoot "runtime_logs"

if ($CorePort -eq $WebPort) {
    throw "CorePort and WebPort must be different."
}

foreach ($requiredPath in @($venvConfigPath, $sitePackages, $srcRoot)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Deskbot runtime path not found: $requiredPath"
    }
}

# .venv\Scripts\python.exe can be a short-lived Windows redirector. Starting
# and monitoring that file makes the coordinator believe the service exited
# even though the real interpreter is still running. Resolve the base
# interpreter recorded by venv and monitor that process directly instead.
$venvConfig = @{}
foreach ($line in Get-Content -LiteralPath $venvConfigPath) {
    if ($line -match "^\s*([^#][^=]*?)\s*=\s*(.*?)\s*$") {
        $key = $Matches[1].Trim().ToLowerInvariant()
        $value = $Matches[2].Trim().Trim('"')
        $venvConfig[$key] = $value
    }
}

$pythonCandidates = New-Object System.Collections.Generic.List[string]
if ($venvConfig.ContainsKey("executable") -and $venvConfig["executable"]) {
    $pythonCandidates.Add(
        [Environment]::ExpandEnvironmentVariables($venvConfig["executable"])
    )
}
if ($venvConfig.ContainsKey("home") -and $venvConfig["home"]) {
    $pythonHome = [Environment]::ExpandEnvironmentVariables($venvConfig["home"])
    $pythonCandidates.Add((Join-Path $pythonHome "python.exe"))
}

$python = $null
foreach ($candidate in $pythonCandidates) {
    $candidatePath = $candidate
    if (-not [IO.Path]::IsPathRooted($candidatePath)) {
        $candidatePath = Join-Path (Split-Path -Parent $venvConfigPath) $candidatePath
    }
    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        $python = (Resolve-Path -LiteralPath $candidatePath).Path
        break
    }
}
if (-not $python) {
    throw "Base Python from $venvConfigPath was not found. Checked: $($pythonCandidates -join ', ')"
}

New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

foreach ($port in @($CorePort, $WebPort)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $listener) {
        throw "TCP port $port is already in use by PID $($listener.OwningProcess)"
    }
}

$secret = [string]$env:DESKBOT_WEB_SECRET_KEY
if ($secret.Length -lt 32) {
    $randomBytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($randomBytes)
    } finally {
        $rng.Dispose()
    }
    $secret = [Convert]::ToBase64String($randomBytes)
}

$env:DESKBOT_WEB_SECRET_KEY = $secret
$env:DESKBOT_SERIAL_PORTS = $SerialPorts
$env:DESKBOT_USB_SERIAL_ENABLED = "1"
$env:DESKBOT_SERVER_HOST = "127.0.0.1"
$env:DESKBOT_SERVER_PORT = [string]$CorePort
$env:DESKBOT_WEB_HOST = "127.0.0.1"
$env:DESKBOT_WEB_PORT = [string]$WebPort
$env:DESKBOT_WEB_DEBUG = "0"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONPATH = [string]::Join(
    [IO.Path]::PathSeparator,
    @($srcRoot, $sitePackages)
)
$env:VIRTUAL_ENV = $venvRoot
$env:PATH = [string]::Join(
    [IO.Path]::PathSeparator,
    @($venvScripts, [string]$env:PATH)
)

$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$coreOut = Join-Path $logRoot "core-local-$stamp.stdout.log"
$coreErr = Join-Path $logRoot "core-local-$stamp.stderr.log"
$webOut = Join-Path $logRoot "web-local-$stamp.stdout.log"
$webErr = Join-Path $logRoot "web-local-$stamp.stderr.log"
$statePath = Join-Path $logRoot "local-windows-current.json"

$state = [ordered]@{
    status = "starting"
    coordinator_pid = $PID
    core_pid = $null
    web_pid = $null
    python = $python
    pythonpath = $env:PYTHONPATH
    serial_ports = $SerialPorts
    core_url = "http://127.0.0.1:$CorePort"
    web_url = "http://127.0.0.1:$WebPort"
    started_at = (Get-Date).ToString("o")
    ready_at = $null
    stopped_at = $null
    failure = $null
    core_stdout = $coreOut
    core_stderr = $coreErr
    web_stdout = $webOut
    web_stderr = $webErr
}

function Write-CurrentState {
    # Readers (the console and diagnostics) poll this file while startup is
    # updating it.  Writing the destination in place lets a short-lived reader
    # make Set-Content fail on Windows and tears down otherwise healthy child
    # processes.  Publish a complete same-directory temp file atomically and
    # retry only the brief sharing violation window.
    $json = $state | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $tempPath = Join-Path $logRoot (
        "local-windows-current.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    )
    $lastWriteError = $null
    try {
        [IO.File]::WriteAllText($tempPath, $json, $utf8NoBom)
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                Move-Item `
                    -LiteralPath $tempPath `
                    -Destination $statePath `
                    -Force `
                    -ErrorAction Stop
                return
            } catch {
                $lastWriteError = $_
                Start-Sleep -Milliseconds 50
            }
        }
        throw $lastWriteError
    } finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-LoopbackListener {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        $properties = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
        foreach ($endpoint in $properties.GetActiveTcpListeners()) {
            if (
                $endpoint.Port -eq $Port -and
                [Net.IPAddress]::IsLoopback($endpoint.Address)
            ) {
                return $true
            }
        }
        return $false
    } catch {
        return $false
    }
}

$core = $null
$web = $null
try {
    $core = Start-Process `
        -FilePath $python `
        -ArgumentList @("-u", "-m", "deskbot_server") `
        -WorkingDirectory $serviceRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $coreOut `
        -RedirectStandardError $coreErr `
        -PassThru

    $state.core_pid = $core.Id
    Write-CurrentState

    $web = Start-Process `
        -FilePath $python `
        -ArgumentList @("-u", "-m", "deskbot_server.web") `
        -WorkingDirectory $serviceRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $webOut `
        -RedirectStandardError $webErr `
        -PassThru

    $state.web_pid = $web.Id
    Write-CurrentState

    $coreReady = $false
    $webReady = $false
    $startupDeadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while (-not ($coreReady -and $webReady)) {
        $core.Refresh()
        $web.Refresh()
        if ($core.HasExited) {
            throw "Deskbot core exited during startup with code $($core.ExitCode); see $coreErr"
        }
        if ($web.HasExited) {
            throw "Deskbot web console exited during startup with code $($web.ExitCode); see $webErr"
        }

        if (-not $coreReady) {
            $coreReady = Test-LoopbackListener -Port $CorePort
        }
        if (-not $webReady) {
            $webReady = Test-LoopbackListener -Port $WebPort
        }
        if ($coreReady -and $webReady) {
            break
        }
        if ((Get-Date) -ge $startupDeadline) {
            throw "Deskbot services did not listen within $StartupTimeoutSeconds seconds; see $coreErr and $webErr"
        }
        Start-Sleep -Milliseconds 200
    }

    $state.status = "running"
    $state.ready_at = (Get-Date).ToString("o")
    Write-CurrentState

    while (-not $core.HasExited -and -not $web.HasExited) {
        Start-Sleep -Seconds 1
        $core.Refresh()
        $web.Refresh()
    }

    if ($core.HasExited) {
        throw "Deskbot core exited with code $($core.ExitCode); see $coreErr"
    }
    throw "Deskbot web console exited with code $($web.ExitCode); see $webErr"
} catch {
    $state.status = "failed"
    $state.failure = $_.Exception.Message
    try {
        Write-CurrentState
    } catch {
        # Preserve the original service failure when state persistence fails.
    }
    throw
} finally {
    foreach ($process in @($core, $web)) {
        if ($null -ne $process) {
            try {
                $process.Refresh()
                if (-not $process.HasExited) {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                }
            } catch {
                # Best-effort cleanup when one child has already exited.
            }
        }
    }
    if ($state.status -ne "failed") {
        $state.status = "stopped"
    }
    $state.stopped_at = (Get-Date).ToString("o")
    try {
        Write-CurrentState
    } catch {
        # Best-effort final state update during shutdown.
    }
}
