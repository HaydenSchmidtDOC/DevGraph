#Requires -Version 5.1
<#
.SYNOPSIS
    Shared bootstrap core: venv, editable install, Neo4j container, schema init, doctor.
.DESCRIPTION
    Dot-sourced by both bootstrap.ps1 and setup-menu.ps1 so this logic exists
    in exactly one place. Not meant to be run directly. Expects $RepoRoot and
    $PodmanPath to already be set by the caller (Podman resolution stays in
    each entry point since setup-menu.ps1 needs to react differently to a
    missing Podman than bootstrap.ps1 does).

    Idempotent - safe to re-run. Never installs system software; never
    mutates the persistent PATH; every Podman call is scoped to the literal
    container name 'devgraph-neo4j'.
#>

function Invoke-BootstrapCore {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$PodmanPath
    )

    # Venv create-or-reuse
    Write-Step "Python virtual environment"
    if (Test-Path (Join-Path $RepoRoot ".venv")) {
        Write-Ok ".venv already exists, reusing"
    } else {
        python -m venv (Join-Path $RepoRoot ".venv") | Out-Host
        if ($LASTEXITCODE -ne 0) { Write-Fail "python -m venv failed"; return $false }
        Write-Ok ".venv created"
    }
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

    # Editable install
    Write-Step "Installing devgraph (editable, with dev deps)"
    & $venvPython -m pip install -e "$RepoRoot[dev]" | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Fail "pip install failed"; return $false }
    Write-Ok "install complete"

    # Container check-or-start
    Write-Step "Neo4j container (devgraph-neo4j)"
    $running = & $PodmanPath ps --filter "name=devgraph-neo4j" --format "{{.Names}}"
    if ($running -match "devgraph-neo4j") {
        Write-Ok "already running"
    } else {
        $existing = & $PodmanPath ps -a --filter "name=devgraph-neo4j" --format "{{.Names}}"
        if ($existing -match "devgraph-neo4j") {
            Write-Host "  container exists but is stopped, starting it"
            & $PodmanPath start devgraph-neo4j | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Fail "podman start devgraph-neo4j failed"; return $false }
            Write-Ok "started"
        } else {
            Write-Host "  container does not exist, creating it"
            & $PodmanPath run -d --name devgraph-neo4j `
                -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 `
                -e NEO4J_AUTH=neo4j/devgraph-local-dev `
                -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs `
                docker.io/library/neo4j:5.26-community | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Fail "podman run failed"; return $false }
            Write-Ok "created and started"
        }
    }

    # Health wait loop
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
        return $false
    }
    Write-Ok "Neo4j Bolt reachable"

    # Schema init
    Write-Step "Initializing schema"
    & $venvPython -c "from devgraph.graph.engine import GraphEngine; e = GraphEngine('bolt://127.0.0.1:7687', 'neo4j', 'devgraph-local-dev'); e.init_schema(); e.close()" | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Fail "schema init failed"; return $false }
    Write-Ok "schema initialized"

    # Final verification
    Write-Step "Running devgraph doctor"
    & $venvPython -m devgraph.cli.main doctor | Out-Host
    $doctorExit = $LASTEXITCODE

    if ($doctorExit -ne 0) {
        Write-Fail "bootstrap finished but 'devgraph doctor' reported failing checks (see above)"
        return $false
    }
    return $true
}
