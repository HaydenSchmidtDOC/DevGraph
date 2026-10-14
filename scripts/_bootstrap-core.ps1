#Requires -Version 5.1
<#
.SYNOPSIS
    Shared bootstrap core: venv, editable install, Neo4j container, wait for Neo4j, doctor (schema init included).
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
            & $PodmanPath run -d --name devgraph-neo4j --restart=always `
                -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 `
                -e NEO4J_AUTH=neo4j/devgraph-local-dev `
                -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs `
                docker.io/library/neo4j:5.26-community | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Fail "podman run failed"; return $false }
            Write-Ok "created and started"
        }
    }

    # Wait for Neo4j, schema init and final verification in one step: a
    # fresh container accepts TCP at once (Podman's port forwarder) but
    # answers Bolt only after ~20 s, longer in a Podman machine, so doctor
    # --wait polls Bolt itself before it checks (and creates) the schema.
    Write-Step "Waiting for Neo4j, then running devgraph doctor"
    & $venvPython -P -m devgraph.cli.main doctor --wait 90 | Out-Host
    $doctorExit = $LASTEXITCODE

    if ($doctorExit -ne 0) {
        Write-Fail "bootstrap finished but 'devgraph doctor' reported failing checks (see above)"
        return $false
    }
    return $true
}
