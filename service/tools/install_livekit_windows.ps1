[CmdletBinding()]
param(
    [string]$Version = "1.13.5",
    [string]$ExpectedSha256 = "3ec7eaa76ef64063bf21f78364733703e0969612cb92ffd60661ed45fa4a8906"
)

$ErrorActionPreference = "Stop"

$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$installRoot = Join-Path $serviceRoot "data\local\livekit"
$binaryPath = Join-Path $installRoot "livekit-server.exe"
$downloadUrl = (
    "https://github.com/livekit/livekit/releases/download/v{0}/" +
    "livekit_{0}_windows_amd64.zip"
) -f $Version
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "deskbot-livekit-install-" + [guid]::NewGuid().ToString("N")
)
$zipPath = Join-Path $tempRoot "livekit.zip"
$extractRoot = Join-Path $tempRoot "extract"

try {
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
    $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
    if ($actualSha256 -ine $ExpectedSha256) {
        throw "LiveKit archive checksum mismatch: expected=$ExpectedSha256 actual=$actualSha256"
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
    $downloadedBinary = Join-Path $extractRoot "livekit-server.exe"
    if (-not (Test-Path -LiteralPath $downloadedBinary -PathType Leaf)) {
        throw "Downloaded archive does not contain livekit-server.exe"
    }
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    Copy-Item -LiteralPath $downloadedBinary -Destination $binaryPath -Force
    & $binaryPath --version
    Write-Host "Installed and verified: $binaryPath"
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
