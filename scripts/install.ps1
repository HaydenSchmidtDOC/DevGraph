#Requires -Version 5.1
<#
.SYNOPSIS
    Distribution entry point: clones DevGraph (if not already in a checkout)
    and hands off to the interactive setup menu.
.DESCRIPTION
    Intended to be run via the documented one-liner in README.md:
        irm https://raw.githubusercontent.com/HaydenSchmidtDOC/DevGraph/master/scripts/install.ps1 | iex
    A developer with an existing local clone can skip this and run
    scripts/setup-menu.ps1 (or scripts/bootstrap.ps1) directly instead.
.PARAMETER Path
    Where to clone DevGraph if this isn't already run from inside a checkout.
    Defaults to $env:USERPROFILE\devgraph.
#>

param(
    [string]$Path = (Join-Path $env:USERPROFILE "devgraph")
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

$RepoUrl = "https://github.com/HaydenSchmidtDOC/DevGraph.git"

# Already inside a DevGraph checkout (e.g. a developer who cloned manually
# and is running this script directly) -- use that checkout, don't re-clone.
$currentRoot = $null
$probe = Get-Location
while ($probe) {
    if (Test-Path (Join-Path $probe "pyproject.toml")) {
        $marker = Get-Content (Join-Path $probe "pyproject.toml") -Raw -ErrorAction SilentlyContinue
        if ($marker -match "devgraph") {
            $currentRoot = $probe.Path
            break
        }
    }
    $parent = Split-Path -Parent $probe
    if (-not $parent -or $parent -eq $probe) { break }
    $probe = $parent
}

if ($currentRoot) {
    Write-Step "Already inside a DevGraph checkout"
    Write-Ok "$currentRoot"
    $RepoRoot = $currentRoot
} else {
    Write-Step "Cloning DevGraph"
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Fail "git not found on PATH. Install Git and re-run."
        exit 1
    }

    if (Test-Path $Path) {
        if (Test-Path (Join-Path $Path ".git")) {
            Write-Ok "$Path already exists and is a git checkout, reusing"
        } else {
            Write-Fail "$Path already exists and is not a git checkout. Pass -Path to choose a different location."
            exit 1
        }
    } else {
        git clone $RepoUrl $Path
        if ($LASTEXITCODE -ne 0) { Write-Fail "git clone failed"; exit 1 }
        Write-Ok "cloned to $Path"
    }
    $RepoRoot = (Resolve-Path $Path).Path
}

Write-Step "Launching setup"
& (Join-Path $RepoRoot "scripts\setup-menu.ps1")
exit $LASTEXITCODE
