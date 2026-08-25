#Requires -Version 5.1
<#
.SYNOPSIS
    One-command bootstrap for DevGraph: venv, editable install, Neo4j container, schema init.
.DESCRIPTION
    Idempotent — safe to re-run. Never installs system software (Python/Git/Podman must
    already be present); never mutates the persistent PATH; every Podman call is scoped
    to the literal container name 'devgraph-neo4j'. Stops after Neo4j is up and schema
    exists — does not register any repo (explicit-registration-only per CLAUDE.md).
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

# 3. Venv create-or-reuse
Write-Step "Python virtual environment"
if (Test-Path ".venv") {
    Write-Ok ".venv already exists, reusing"
} else {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Fail "python -m venv failed"; exit 1 }
    Write-Ok ".venv created"
}
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# 4. Editable install
Write-Step "Installing devgraph (editable, with dev deps)"
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Write-Fail "pip install failed"; exit 1 }
Write-Ok "install complete"

# 5. Container check-or-start
Write-Step "Neo4j container (devgraph-neo4j)"
$running = & $podmanPath ps --filter "name=devgraph-neo4j" --format "{{.Names}}"
if ($running -match "devgraph-neo4j") {
    Write-Ok "already running"
} else {
    $existing = & $podmanPath ps -a --filter "name=devgraph-neo4j" --format "{{.Names}}"
    if ($existing -match "devgraph-neo4j") {
        Write-Host "  container exists but is stopped, starting it"
        & $podmanPath start devgraph-neo4j | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Fail "podman start devgraph-neo4j failed"; exit 1 }
        Write-Ok "started"
    } else {
        Write-Host "  container does not exist, creating it"
        & $podmanPath run -d --name devgraph-neo4j `
            -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 `
            -e NEO4J_AUTH=neo4j/devgraph-local-dev `
            -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs `
            docker.io/library/neo4j:5.26-community | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Fail "podman run failed"; exit 1 }
        Write-Ok "created and started"
    }
}

# 6. Health wait loop
Write-Step "Waiting for Neo4j Bolt to become reachable"
$maxAttempts = 15
$attempt = 0
$healthy = $false
while ($attempt -lt $maxAttempts) {
    $attempt++
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", 7687)
        $tcp.Close()
        $healthy = $true
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $healthy) {
    Write-Fail "Neo4j did not become reachable on 127.0.0.1:7687 after $($maxAttempts * 2)s"
    exit 1
}
Write-Ok "Neo4j Bolt reachable"

# 7. Schema init
Write-Step "Initializing schema"
& $venvPython -c "from devgraph.graph.engine import GraphEngine; e = GraphEngine('bolt://127.0.0.1:7687', 'neo4j', 'devgraph-local-dev'); e.init_schema(); e.close()"
if ($LASTEXITCODE -ne 0) { Write-Fail "schema init failed"; exit 1 }
Write-Ok "schema initialized"

# 8. Final verification
Write-Step "Running devgraph doctor"
& $venvPython -m devgraph.cli.main doctor
$doctorExit = $LASTEXITCODE

# 9. Next-steps message
Write-Host "`n==> Next steps" -ForegroundColor Cyan
Write-Host "  devgraph add <path-to-a-git-repo>"
Write-Host "  devgraph client-config          # get the MCP registration command for this machine"

if ($doctorExit -ne 0) {
    Write-Fail "bootstrap finished but 'devgraph doctor' reported failing checks (see above)"
    exit 1
}
Write-Host "`nBootstrap complete." -ForegroundColor Green
