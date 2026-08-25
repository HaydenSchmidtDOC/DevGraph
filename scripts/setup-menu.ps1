#Requires -Version 5.1
<#
.SYNOPSIS
    Interactive DevGraph setup: detects Podman, bootstraps Neo4j/venv, and
    registers DevGraph as an MCP server with whichever AI clients are found
    on this machine.
.DESCRIPTION
    Safe to re-run on an existing checkout - every step below is idempotent.
    Never installs system software; if Podman is missing, this stops and
    points at the company software portal rather than attempting an install.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Yellow }

Write-Host "DevGraph setup" -ForegroundColor Cyan
Write-Host "==============" -ForegroundColor Cyan

# 1. Preflight (python, git) - same checks bootstrap.ps1 makes, kept here so
#    this script can run standalone (e.g. invoked directly by install.ps1)
#    without requiring a separate bootstrap.ps1 call first.
Write-Step "Preflight: python, git"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Fail "python not found on PATH. Install Python 3.13+ and re-run. See README.md Prerequisites."
    exit 1
}
$pyVersionOutput = & python --version 2>&1
if ($pyVersionOutput -notmatch "Python 3\.(1[3-9]|[2-9][0-9])") {
    Write-Fail "python found but not >= 3.13 ($pyVersionOutput). See README.md Prerequisites."
    exit 1
}
Write-Ok "$pyVersionOutput"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Fail "git not found on PATH. Install Git and re-run. See README.md Prerequisites."
    exit 1
}
Write-Ok "git found: $($git.Source)"

# 2. Podman detection
Write-Step "Checking for Podman"
$podmanCmd = Get-Command podman -ErrorAction SilentlyContinue
$podmanPath = $null
if ($podmanCmd) {
    $podmanPath = $podmanCmd.Source
} else {
    $fallback = Join-Path $env:LOCALAPPDATA "Programs\Podman\podman.exe"
    if (Test-Path $fallback) {
        $podmanPath = $fallback
    }
}
if (-not $podmanPath) {
    Write-Fail "Podman is not installed (or not on PATH)."
    Write-Info "Podman is required to run DevGraph's Neo4j database locally."
    Write-Info "Install it from the company software portal, then re-run this script."
    exit 1
}
Write-Ok "podman found: $podmanPath"

# 3. Shared bootstrap core (venv, install, container, health, schema, doctor)
. (Join-Path $PSScriptRoot "_bootstrap-core.ps1")
$coreSucceeded = Invoke-BootstrapCore -RepoRoot $RepoRoot -PodmanPath $podmanPath
if (-not $coreSucceeded) {
    Write-Fail "Setup could not complete - see errors above."
    exit 1
}
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# 4. Detect installed, MCP-compatible AI clients
Write-Step "Detecting AI clients on this machine"
$clients = @()

$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
    $clients += [PSCustomObject]@{ Name = "Claude Code"; Target = "claude" }
    Write-Ok "Claude Code found: $($claudeCmd.Source)"
} else {
    Write-Info "Claude Code not found on PATH"
}

$codeCmd = Get-Command code -ErrorAction SilentlyContinue
$vscodeUserDir = Join-Path $env:APPDATA "Code\User"
if ($codeCmd -or (Test-Path $vscodeUserDir)) {
    $clients += [PSCustomObject]@{ Name = "VS Code"; Target = "vscode" }
    Write-Ok "VS Code found"
} else {
    Write-Info "VS Code not found"
}

if ($clients.Count -eq 0) {
    Write-Step "No supported AI client detected"
    Write-Info "Run this to get manual registration instructions any time:"
    Write-Info "  $venvPython -m devgraph.cli.main client-config"
    Write-Host "`n==> Next steps" -ForegroundColor Cyan
    Write-Host "  devgraph add <path-to-a-git-repo>"
    Write-Host "`nSetup complete." -ForegroundColor Green
    exit 0
}

# 5. Multi-select menu
Write-Step "Select which client(s) to register DevGraph with"
for ($i = 0; $i -lt $clients.Count; $i++) {
    Write-Host "  [$($i + 1)] $($clients[$i].Name)"
}
Write-Host "  [A] All of the above"
$selection = Read-Host "Enter number(s) separated by commas, or 'A' for all"

$selectedTargets = @()
if ($selection.Trim().ToUpper() -eq "A") {
    $selectedTargets = $clients.Target
} else {
    $indices = $selection -split "," | ForEach-Object { $_.Trim() }
    foreach ($idx in $indices) {
        $n = 0
        if ([int]::TryParse($idx, [ref]$n) -and $n -ge 1 -and $n -le $clients.Count) {
            $selectedTargets += $clients[$n - 1].Target
        }
    }
}

if ($selectedTargets.Count -eq 0) {
    Write-Info "No valid selection made - skipping MCP registration."
} else {
    # 6. Register against each selected target
    Write-Step "Registering DevGraph"
    foreach ($target in $selectedTargets) {
        & $venvPython -m devgraph.cli.main client-config --target $target --run
    }
}

# 7. Final message
Write-Host "`n==> Next steps" -ForegroundColor Cyan
Write-Host "  devgraph add <path-to-a-git-repo>"
Write-Host "`nSetup complete." -ForegroundColor Green
