#Requires -Version 5.1
<#
.SYNOPSIS
    One-command bootstrap for DevGraph: venv, editable install, Neo4j container, schema init.
.DESCRIPTION
    Idempotent - safe to re-run. Never installs system software (Python/Git/Podman must
    already be present); never mutates the persistent PATH; every Podman call is scoped
    to the literal container name 'devgraph-neo4j'. Stops after Neo4j is up and schema
    exists - does not register any repo (explicit-registration-only per CLAUDE.md) and
    does not register any MCP client (see scripts/setup-menu.ps1 for the interactive,
    client-registering install flow this script is also called from).
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

# 1. Preflight
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

# 2. Podman resolution
Write-Step "Resolving podman"
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
    Write-Fail "podman not found on PATH or at $env:LOCALAPPDATA\Programs\Podman. Install Podman and re-run. See README.md Prerequisites."
    exit 1
}
Write-Ok "podman found: $podmanPath"

# 3-8. Shared bootstrap core (venv, install, container, health, schema, doctor)
. (Join-Path $PSScriptRoot "_bootstrap-core.ps1")
$coreSucceeded = Invoke-BootstrapCore -RepoRoot $RepoRoot -PodmanPath $podmanPath

# 9. Next-steps message
Write-Host "`n==> Next steps" -ForegroundColor Cyan
Write-Host "  devgraph add <path-to-a-git-repo>"
Write-Host "  devgraph client-config          # get the MCP registration command for this machine"
Write-Host "  .\scripts\setup-menu.ps1         # or: run the interactive menu to register an AI client for you"

if (-not $coreSucceeded) {
    exit 1
}
Write-Host "`nBootstrap complete." -ForegroundColor Green
