[CmdletBinding()]
param(
    [string]$Distro = 'WifiProxyBuild',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'WSL is unavailable. This script only uses an existing build distribution.'
}
$distributions = @(& wsl.exe --list --quiet) | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($LASTEXITCODE -ne 0 -or $Distro -notin $distributions) {
    throw "Existing WSL distribution '$Distro' was not found. No distribution or Windows feature was installed."
}
$portableRoot = $repoRoot.Replace('\', '/')
$linuxRoot = (& wsl.exe -d $Distro --exec wslpath -a -u $portableRoot)
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the repository path in WSL.' }
$linuxRoot = ($linuxRoot -join "`n").Trim()
if (-not $linuxRoot.StartsWith('/')) { throw 'WSL returned an invalid repository path.' }

$dependencyCheck = 'for tool in python3 curl sha256sum tar zstd rsync make realpath stat find install; do command -v $tool >/dev/null || exit 1; done'
& wsl.exe -d $Distro --exec bash -c $dependencyCheck
if ($LASTEXITCODE -ne 0) { throw 'The existing WSL distribution is missing build dependencies. No packages were installed.' }
& wsl.exe -d $Distro --exec bash -n "$linuxRoot/scripts/build-firmware.sh"
if ($LASTEXITCODE -ne 0) { throw 'Build script syntax validation failed.' }
Write-Host "Build environment checked: $Distro. Firmware builds under /opt/wifi-proxy on Linux ext4."
if ($CheckOnly) { return }

& wsl.exe -d $Distro --exec bash "$linuxRoot/scripts/build-firmware.sh"
if ($LASTEXITCODE -ne 0) { throw "Firmware build failed (exit $LASTEXITCODE)." }
