#Requires -Version 5.1
<#
.SYNOPSIS
    One-command update for an existing DevGraph checkout: pulls latest master,
    reinstalls dependencies, and restarts the tray app if it was running.
.DESCRIPTION
    Safe to re-run. Refuses to pull over local changes (stashes nothing for
    you - if the working tree is dirty, it stops and tells you to commit or
    stash first). Does not touch the Neo4j container or any registered repo.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Yellow }

Write-Step "Checking working tree"
$dirty = git status --porcelain
if ($dirty) {
    Write-Fail "Local changes present - commit or stash before updating."
    git status --short | Out-Host
    exit 1
}
Write-Ok "clean"

Write-Step "Was the tray app running?"
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$wasRunning = $false
if (Test-Path $venvPython) {
    & $venvPython -m devgraph.cli.main tray status | Out-Null
    $wasRunning = ($LASTEXITCODE -eq 0)
}
if ($wasRunning) { Write-Ok "running - will restart after update" } else { Write-Info "not running" }

Write-Step "Pulling latest master"
git pull --ff-only origin master | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Fail "git pull --ff-only failed - your branch may have diverged from origin/master."
    exit 1
}
Write-Ok "up to date"

if ($wasRunning) {
    Write-Step "Stopping tray app for reinstall"
    & $venvPython -m devgraph.cli.main tray stop | Out-Host
}

Write-Step "Reinstalling dependencies and verifying environment"
$podmanCmd = Get-Command podman -ErrorAction SilentlyContinue
$podmanPath = $null
if ($podmanCmd) {
    $podmanPath = $podmanCmd.Source
} else {
    $fallback = Join-Path $env:LOCALAPPDATA "Programs\Podman\podman.exe"
    if (Test-Path $fallback) { $podmanPath = $fallback }
}
if (-not $podmanPath) {
    Write-Fail "podman not found on PATH. Can't verify the Neo4j container - re-run scripts/bootstrap.ps1 once Podman is available."
    exit 1
}

. (Join-Path $PSScriptRoot "_bootstrap-core.ps1")
$coreSucceeded = Invoke-BootstrapCore -RepoRoot $RepoRoot -PodmanPath $podmanPath

if ($wasRunning) {
    Write-Step "Restarting tray app"
    & $venvPython -m devgraph.cli.main tray start | Out-Host
}

if (-not $coreSucceeded) {
    Write-Fail "Update finished but 'devgraph doctor' reported failing checks - see above."
    exit 1
}
Write-Host "`nUpdate complete." -ForegroundColor Green
