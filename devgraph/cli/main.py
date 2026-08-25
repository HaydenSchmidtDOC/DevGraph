"""DevGraph CLI: register repositories, manage watch settings, check status."""

import importlib.metadata
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from devgraph.agent import lifecycle
from devgraph.cli._env import resolve_podman, resolve_repo_root, resolve_venv_python
from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.git_history.extractor import index_repo_history
from devgraph.registry.store import RepoRegistry

app = typer.Typer(help="DevGraph: local-first developer knowledge graph")
tray_app = typer.Typer(help="Manage the DevGraph tray app (live watcher + incremental indexer) as a background process.")
app.add_typer(tray_app, name="tray")
console = Console()


def _get_registry() -> RepoRegistry:
    """Get or create the registry from configured path."""
    settings = get_settings()
    return RepoRegistry(settings.registry_db_path)


@app.command()
def add(
    path: str,
    full: bool = typer.Option(
        False, "--full", help="Also run incremental git-history indexing after the file scan (local-only, no network)."
    ),
) -> None:
    """Register a repository and run its initial full scan.

    Args:
        path: Absolute or relative path to a git repository.
    """
    try:
        registry = _get_registry()
        try:
            record = registry.add_repo(path)
            console.print(
                f"[green][OK][/green] Registered: {record.repo_id} at {record.path}"
            )

            try:
                settings = get_settings()
                engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
                try:
                    engine.init_schema()
                    engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
                    count = full_scan(engine, record.repo_id, record.path, docs_path=record.docs_path)
                    registry.mark_indexed(record.repo_id)
                    console.print(f"[green][OK][/green] Indexed {count} file(s)")

                    if full:
                        try:
                            history_count = index_repo_history(engine, registry, record.repo_id)
                            console.print(f"[green][OK][/green] Indexed {history_count} commit(s)")
                        except Exception as e:
                            console.print(
                                f"[yellow]Full scan complete but history indexing failed:[/yellow] {e}\n"
                                f"  Run 'devgraph index-history {record.repo_id}' to retry."
                            )
                finally:
                    engine.close()
            except Exception as e:
                # Registration already succeeded (SQLite committed above) — an
                # indexing failure (e.g. Neo4j unreachable) shouldn't undo that.
                # `devgraph rescan <repo_id>` retries the scan once Neo4j is up.
                console.print(
                    f"[yellow]Registered but initial scan failed:[/yellow] {e}\n"
                    f"  Run 'devgraph rescan {record.repo_id}' once Neo4j is reachable."
                )
        finally:
            registry.close()
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def remove(repo_id: str) -> None:
    """Unregister a repository.

    Args:
        repo_id: The repository ID (shown by 'devgraph list').
    """
    try:
        registry = _get_registry()
        try:
            registry.remove_repo(repo_id)
            console.print(f"[green][OK][/green] Removed: {repo_id}")
        finally:
            registry.close()
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def list() -> None:
    """List all registered repositories and their status."""
    try:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
            if not repos:
                console.print("No repositories registered.")
                return

            table = Table(title="Registered Repositories")
            table.add_column("Repo ID", style="cyan")
            table.add_column("Path", style="magenta")
            table.add_column("Active", style="green")
            table.add_column("Watch", style="blue")
            table.add_column("Last Indexed", style="yellow")

            for repo in repos:
                active_str = "[OK]" if repo.active else "[X]"
                watch_str = "[OK]" if repo.watch_enabled else "[X]"
                last_indexed = repo.last_indexed or "-"
                table.add_row(
                    repo.repo_id,
                    str(repo.path),
                    active_str,
                    watch_str,
                    last_indexed,
                )

            console.print(table)
        finally:
            registry.close()
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def rescan(
    repo_id: str,
    full: bool = typer.Option(
        False, "--full", help="Also run incremental git-history indexing after the file scan (local-only, no network)."
    ),
) -> None:
    """Run a full re-index of a registered repository.

    Walks every file under the repo's root and re-runs every extractor that
    recognizes it (Python, docs, containers, APIs, datastores). Idempotent —
    safe to run repeatedly; existing nodes are updated in place via MERGE.

    Args:
        repo_id: The repository ID to rescan.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                engine.init_schema()
                engine.upsert_repository(repo_id, repo_id, str(repo.path))
                count = full_scan(engine, repo_id, repo.path, docs_path=repo.docs_path)
                registry.mark_indexed(repo_id)
                console.print(f"[green][OK][/green] Rescanned {repo_id}: {count} file(s) indexed")

                if full:
                    try:
                        history_count = index_repo_history(engine, registry, repo_id)
                        console.print(f"[green][OK][/green] Indexed {history_count} commit(s)")
                    except Exception as e:
                        console.print(
                            f"[yellow]Rescan complete but history indexing failed:[/yellow] {e}\n"
                            f"  Run 'devgraph index-history {repo_id}' to retry."
                        )
            finally:
                engine.close()
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def watch(action: str, repo_id: str) -> None:
    """Enable or disable file watching for a repository.

    Args:
        action: 'enable' or 'disable'.
        repo_id: The repository ID.
    """
    try:
        if action not in ("enable", "disable"):
            console.print(f"[red][X] Error:[/red] action must be 'enable' or 'disable'")
            raise typer.Exit(code=1)

        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            if action == "enable":
                registry.enable_watch(repo_id)
                console.print(f"[green][OK][/green] Watch enabled for {repo_id}")
            else:
                registry.disable_watch(repo_id)
                console.print(f"[green][OK][/green] Watch disabled for {repo_id}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@tray_app.command("start")
def tray_start() -> None:
    """Launch the tray app (watcher + incremental indexer) as a detached background process.

    No-op if a tray process (per the PID file) is already running. This does
    not register the process to survive reboots/logout — rerun after each
    login, or wire it into your own Startup-folder/Task Scheduler entry.

    Note: connecting an MCP client (e.g. Claude Code) also auto-starts this
    the same way, so most workflows never need to run this by hand — it's
    here for manual control (checking in on it, or running it without an
    MCP client attached).
    """
    started_pid = lifecycle.start_tray_if_not_running()
    if started_pid is None:
        existing_pid = lifecycle.read_tray_pid()
        console.print(f"[yellow]Tray app already running[/yellow] (pid {existing_pid})")
        return

    console.print(f"[green][OK][/green] Tray app started (pid {started_pid})")
    console.print("  Run 'devgraph status' to confirm the heartbeat once it's up.")


@tray_app.command("stop")
def tray_stop() -> None:
    """Force-stop the background tray process, regardless of any connected MCP clients.

    Every connected MCP client's server process holds a "hold" on the tray
    (see devgraph/agent/lifecycle.py) so a single client disconnecting
    doesn't stop indexing for the others — this command overrides that and
    stops it outright, clearing all recorded holders in the process.
    """
    pid = lifecycle.read_tray_pid()
    lifecycle.clear_tray_holders()
    if pid is None or not lifecycle.pid_is_running(pid):
        console.print("[yellow]Tray app is not running[/yellow] (no live PID on record)")
        lifecycle.tray_pid_path().unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green][OK][/green] Sent stop signal to tray app (pid {pid})")
    except OSError as e:
        console.print(f"[red][X] Error:[/red] failed to stop pid {pid}: {e}")
        raise typer.Exit(code=1)
    finally:
        lifecycle.tray_pid_path().unlink(missing_ok=True)


@tray_app.command("status")
def tray_status() -> None:
    """Report whether the background tray process (per the PID file) is alive."""
    pid = lifecycle.read_tray_pid()
    if pid is not None and lifecycle.pid_is_running(pid):
        console.print(f"[green][OK] running[/green] (pid {pid})")
    else:
        console.print("[yellow]not running[/yellow] (start with 'devgraph tray start')")


@app.command()
def annotate(
    repo_id: str,
    docs_path: Optional[str] = typer.Option(
        None, "--docs-path", help="Repo-relative path to the docs folder (e.g. 'devgraph/docs')."
    ),
    note: Optional[str] = typer.Option(
        None, "--note", help="Index a single Markdown note file immediately (path relative to the repo root)."
    ),
) -> None:
    """Configure or use Phase 2 doc annotations for a repository.

    With --docs-path: register/update the repo's docs folder (registry-scoped
    only — never a path outside the repo).
    With --note: parse and upsert one Markdown note (Requirement /
    DesignDecision / ArchitectureNote front-matter) into the graph immediately.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            if docs_path is not None:
                registry.set_docs_path(repo_id, docs_path)
                console.print(f"[green][OK][/green] Docs path set for {repo_id}: {docs_path}")

            if note is not None:
                note_path = repo.path / note
                if not str(note_path.resolve()).startswith(str(repo.path.resolve())):
                    console.print("[red][X] Error:[/red] note path must be inside the repository")
                    raise typer.Exit(code=1)

                settings = get_settings()
                engine = GraphEngine(
                    settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
                )
                try:
                    engine.init_schema()
                    index_doc_file(engine, repo_id, note_path)
                    console.print(f"[green][OK][/green] Indexed note: {note}")
                finally:
                    engine.close()

            if docs_path is None and note is None:
                console.print(f"docs_path: {repo.docs_path or '(not set)'}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command(name="index-history")
def index_history(
    repo_id: str,
    max_count: Optional[int] = typer.Option(
        None, "--max-count", help="Cap the number of commits walked (useful for a first scan of a large repo)."
    ),
) -> None:
    """Incrementally index a repo's git commit history (Phase 3).

    Purely local — reads the repo's own .git directory, no network calls.
    Walks only commits newer than the last indexed one for this repo_id.

    Args:
        repo_id: The repository ID to index history for.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                engine.init_schema()
                count = index_repo_history(engine, registry, repo_id, max_count=max_count)
                console.print(f"[green][OK][/green] Indexed {count} new commit(s) for {repo_id}")
            finally:
                engine.close()
        finally:
            registry.close()
    except typer.Exit:
        raise
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command(name="pr-source")
def pr_source(repo_id: str, action: str) -> None:
    """Enable or disable PR ingestion opt-in for a repository (Phase 3).

    Off by default (Design Brief Principle 2). This command only flips the
    registry flag — it does not itself contact GitHub/GitLab/etc.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "pr_source_enabled", "PR")


@app.command(name="issue-source")
def issue_source(repo_id: str, action: str) -> None:
    """Enable or disable issue ingestion opt-in for a repository (Phase 3).

    Off by default (Design Brief Principle 2). This command only flips the
    registry flag — it does not itself contact GitHub/GitLab/etc.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "issue_source_enabled", "Issue")


def _set_external_source_flag(repo_id: str, action: str, setter_flag: str, label: str) -> None:
    if action not in ("enable", "disable"):
        console.print("[red][X] Error:[/red] action must be 'enable' or 'disable'")
        raise typer.Exit(code=1)

    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            enabled = action == "enable"
            if setter_flag == "pr_source_enabled":
                registry.set_pr_source_enabled(repo_id, enabled)
            else:
                registry.set_issue_source_enabled(repo_id, enabled)

            verb = "enabled" if enabled else "disabled"
            console.print(f"[green][OK][/green] {label} source {verb} for {repo_id}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


def _tray_liveness_text(settings) -> str:
    """Read the tray heartbeat file (item 7) and classify liveness.

    Returns one of "running" / "stale (process may have crashed)" / "not running".
    Shared between `status` and `doctor` so the read/compare logic isn't duplicated.
    """
    heartbeat_path = settings.registry_db_path.parent / "tray_heartbeat.txt"
    if not heartbeat_path.exists():
        return "not running"
    try:
        raw = heartbeat_path.read_text(encoding="utf-8").strip()
        last_beat = datetime.fromisoformat(raw)
    except (ValueError, OSError):
        return "not running"
    age_s = (datetime.now(timezone.utc) - last_beat).total_seconds()
    if age_s <= 2 * settings.health_check_interval_s:
        return "running"
    return "stale (process may have crashed)"


@app.command()
def status() -> None:
    """Check DevGraph status: Neo4j connectivity and repository counts."""
    settings = get_settings()
    console.print()

    # Check Neo4j connectivity
    console.print("[bold]Neo4j Connection[/bold]")
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
        console.print(f"  [green][OK] Reachable[/green] at {settings.neo4j_uri}")
    except Exception as e:
        console.print(f"  [red][X] Not reachable:[/red] {e}")
    finally:
        engine.close()

    # Repository counts
    console.print("[bold]Registered Repositories[/bold]")
    try:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
            active_repos = [r for r in repos if r.active]
            console.print(f"  Total: {len(repos)}")
            console.print(f"  Active: {len(active_repos)}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"  [red]Error:[/red] {e}")

    # Live watcher (tray app) liveness
    console.print("[bold]Live Watcher[/bold]")
    liveness = _tray_liveness_text(settings)
    if liveness == "running":
        console.print("  [green][OK] running[/green]")
    elif liveness == "not running":
        console.print("  [yellow]not running[/yellow] (no heartbeat file — start with 'devgraph tray start')")
    else:
        console.print(f"  [red]{liveness}[/red]")

    console.print()


@app.command()
def doctor() -> None:
    """Run a heavier environment-drift diagnostic than `status`.

    Checks Python version, the installed `mcp` package, MCP server
    importability, Neo4j reachability + schema, Podman container state, the
    repo registry, and tray liveness — continuing past non-fatal failures so
    one run surfaces everything at once. Intended for bootstrap/troubleshooting
    moments; `status` stays the fast/lightweight command for quick glances.
    """
    settings = get_settings()
    any_failed = False
    console.print()

    # 1. Python version
    console.print("[bold]Python[/bold]")
    py_ok = sys.version_info >= (3, 13)
    marker = "[green][OK][/green]" if py_ok else "[red][X][/red]"
    console.print(f"  {marker} {sys.version.split()[0]} ({sys.executable})")
    any_failed = any_failed or not py_ok

    # 2. mcp package version
    console.print("[bold]mcp package[/bold]")
    try:
        mcp_version = importlib.metadata.version("mcp")
        console.print(f"  [green][OK][/green] mcp {mcp_version} (pyproject.toml requires >=2.0)")
    except importlib.metadata.PackageNotFoundError:
        console.print("  [red][X] Not installed[/red]")
        any_failed = True

    # 3. MCP server importability smoke check
    console.print("[bold]MCP server import[/bold]")
    try:
        from devgraph.mcp.server import build_server  # noqa: F401

        console.print("  [green][OK][/green] devgraph.mcp.server.build_server imports cleanly")
    except Exception as e:
        console.print(f"  [red][X] Import failed:[/red] {e}")
        any_failed = True

    # 4 & 5. Neo4j reachability + schema
    console.print("[bold]Neo4j[/bold]")
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
        console.print(f"  [green][OK] Reachable[/green] at {settings.neo4j_uri}")
        try:
            engine.init_schema()
            console.print("  [green][OK][/green] Schema present (init_schema is idempotent)")
        except Exception as e:
            console.print(f"  [red][X] Schema init failed:[/red] {e}")
            any_failed = True
    except Exception as e:
        console.print(f"  [red][X] Not reachable:[/red] {e}")
        any_failed = True
    finally:
        engine.close()

    # 6. Podman container state
    console.print("[bold]Podman[/bold]")
    podman_path = resolve_podman()
    if podman_path is None:
        console.print("  [red][X] podman not found[/red] on PATH or %LOCALAPPDATA%\\Programs\\Podman")
        any_failed = True
    else:
        try:
            result = subprocess.run(
                [str(podman_path), "ps", "-a", "--filter", "name=devgraph-neo4j", "--format", "{{.Names}}\t{{.State}}"],
                capture_output=True, text=True, timeout=15,
            )
            output = result.stdout.strip()
            if not output:
                console.print("  [yellow]devgraph-neo4j container not found[/yellow]")
            else:
                console.print(f"  [green][OK][/green] {output}")
        except Exception as e:
            console.print(f"  [red][X] podman ps failed:[/red] {e}")
            any_failed = True

    # 7. Registry reachability
    console.print("[bold]Registry[/bold]")
    try:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
            console.print(f"  [green][OK][/green] {len(repos)} repo(s) registered at {settings.registry_db_path}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"  [red][X] Registry error:[/red] {e}")
        any_failed = True

    # 8. Tray/watcher liveness
    console.print("[bold]Live Watcher[/bold]")
    liveness = _tray_liveness_text(settings)
    if liveness == "running":
        console.print("  [green][OK] running[/green]")
    elif liveness == "not running":
        console.print("  [yellow]not running[/yellow] (expected if the tray app isn't started)")
    else:
        console.print(f"  [red]{liveness}[/red]")

    console.print()
    if any_failed:
        console.print("[red]doctor found one or more failing checks above.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")


@app.command(name="client-config")
def client_config(
    claude_mcp_add_only: bool = typer.Option(
        False, "--claude-mcp-add-only", help="Print just the 'claude mcp add' one-liner."
    ),
    run: bool = typer.Option(
        False, "--run", help="Execute the constructed 'claude mcp add' command via the 'claude' CLI (opt-in; print-only is the default)."
    ),
) -> None:
    """Print (or optionally run) the MCP registration command for this machine's checkout.

    Resolves sys.executable and the repo root so the output is portable across
    machines instead of hardcoding a literal path — copy/paste this into any
    client repo's docs instead of a fixed path that only works on one machine.
    """
    python_path = resolve_venv_python()
    repo_root = resolve_repo_root()
    mcp_add_line = f'claude mcp add devgraph -- "{python_path}" -m devgraph.mcp.server'

    if claude_mcp_add_only:
        console.print(mcp_add_line, soft_wrap=True)
    else:
        console.print("## Connect DevGraph as an MCP server\n")
        console.print(f"- **command**: {python_path}")
        console.print("- **args**: -m devgraph.mcp.server")
        console.print(f"- **cwd**: {repo_root}\n")
        console.print("```bash")
        console.print(mcp_add_line, soft_wrap=True)
        console.print("```")

    if run:
        claude_path = shutil.which("claude")
        if claude_path is None:
            console.print("[red][X] Error:[/red] 'claude' not found on PATH; cannot run --run")
            raise typer.Exit(code=1)
        console.print(f"\n[bold]Running:[/bold] {mcp_add_line}")
        result = subprocess.run(
            [claude_path, "mcp", "add", "devgraph", "--", str(python_path), "-m", "devgraph.mcp.server"],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            raise typer.Exit(code=result.returncode)


if __name__ == "__main__":
    app()
