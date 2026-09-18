"""DevGraph CLI: register repositories, manage watch settings, check status."""

import importlib.metadata
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from devgraph.agent import lifecycle
from devgraph.cli._env import resolve_podman, resolve_repo_root, resolve_venv_python
from devgraph.cli.exporters import export_cypher, export_dot, export_json
from devgraph.config import get_settings
from devgraph.dashboard import queries as dashboard_queries
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.git_history.extractor import sync_git_history
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
@app.command(name="register")
def add(
    path: str = typer.Argument(".", help="Path to a git repository (defaults to the current directory)."),
    full: bool = typer.Option(
        False, "--full", help="Also run incremental git-history indexing after the file scan (local-only, no network)."
    ),
) -> None:
    """Register a repository and run its initial full scan.

    No-op (with a status message) if this path is already registered.

    Args:
        path: Absolute or relative path to a git repository.
    """
    try:
        registry = _get_registry()
        try:
            resolved = Path(path).resolve()
            existing = next((r for r in registry.list_repos() if r.path == resolved), None)
            if existing is not None:
                console.print(f"[green][OK][/green] Already registered: {existing.repo_id} at {existing.path}")
                return
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
                    count = full_scan(engine, record.repo_id, record.path, docs_path=record.docs_path, mentions_enabled=record.mentions_enabled)
                    registry.mark_indexed(record.repo_id)
                    console.print(f"[green][OK][/green] Indexed {count} file(s)")

                    if full:
                        try:
                            result = sync_git_history(engine, registry, record.repo_id)
                            console.print(f"[green][OK][/green] Indexed {result['commits_indexed']} commit(s)")
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
            if registry.get(repo_id) is None:
                raise ValueError(f"no such repo_id: {repo_id}")

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                engine.delete_repository(repo_id)
            finally:
                engine.close()

            registry.remove_repo(repo_id)
            console.print(f"[green][OK][/green] Removed: {repo_id} (registry entry and graph data)")
        finally:
            registry.close()
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command(name="list")
def list_repos() -> None:
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

            # Load repo issues for display
            settings = get_settings()
            issues_path = settings.registry_db_path.parent / "repo_issues.json"
            repo_issues = {}
            if issues_path.exists():
                try:
                    import json
                    repo_issues = json.loads(issues_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

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
            
            # Show any issues
            if repo_issues:
                console.print("\n[bold]⚠️  Repository Issues[/bold]")
                for repo_id, error_msg in repo_issues.items():
                    console.print(f"  [yellow]{repo_id}:[/yellow] {error_msg}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def rescan(
    repo_id: str,
    full: bool = typer.Option(
        False, "--full", help="Also run git-history indexing after the file scan, forcing a full re-sync even if HEAD hasn't moved (local-only, no network)."
    ),
) -> None:
    """Run a full re-index of a registered repository.

    Walks every file under the repo's root and re-runs every extractor that
    recognizes it (Python, docs, containers, APIs, datastores). Idempotent —
    safe to run repeatedly; existing nodes are updated in place via MERGE.

    Reconciles the graph to disk: file nodes whose source file no longer
    exists are pruned, and git history is always re-synced (cheap no-op when
    HEAD hasn't moved; full reconcile when history was rewritten, e.g. a
    force-push or pruned branch). With --full, git history is force
    re-synced even when HEAD hasn't moved — the escape hatch for repairing
    a graph whose MODIFIES edges were destroyed by a bug.

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
                count = full_scan(engine, repo_id, repo.path, docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled)
                registry.mark_indexed(repo_id)
                console.print(f"[green][OK][/green] Rescanned {repo_id}: {count} file(s) indexed")

                # Always reconcile git history on a rescan — not just with
                # --full. sync_git_history is a cheap no-op when HEAD hasn't
                # moved, and it's the only path that heals a rewritten
                # history (force-push, pruned branch): without it, orphaned
                # Commit nodes linger forever. --full additionally forces a
                # full re-sync even when HEAD hasn't moved, repairing
                # MODIFIES edges destroyed by the old replace_file_nodes
                # blanket-delete bug.
                try:
                    result = sync_git_history(engine, registry, repo_id, force=full)
                    if result["commits_indexed"] or result["commits_deleted"]:
                        console.print(
                            f"[green][OK][/green] History: {result['commits_indexed']} commit(s) indexed, "
                            f"{result['commits_deleted']} pruned"
                        )
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
                result = sync_git_history(engine, registry, repo_id, max_count=max_count)
                console.print(f"[green][OK][/green] Indexed {result['commits_indexed']} new commit(s) for {repo_id}")
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


@app.command(name="mentions")
def mentions(repo_id: str, action: str) -> None:
    """Enable or disable mentions indexing for a repository.

    Off by default (Design Brief Principle 2). When enabled, Markdown files
    are scanned for references to entities in the graph.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "mentions_enabled", "Mentions")


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
            elif setter_flag == "issue_source_enabled":
                registry.set_issue_source_enabled(repo_id, enabled)
            elif setter_flag == "mentions_enabled":
                registry.set_mentions_enabled(repo_id, enabled)

            verb = "enabled" if enabled else "disabled"
            console.print(f"[green][OK][/green] {label} {verb} for {repo_id}")
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

    # Repo issues (missing paths, etc.)
    issues_path = settings.registry_db_path.parent / "repo_issues.json"
    if issues_path.exists():
        try:
            import json
            issues = json.loads(issues_path.read_text(encoding="utf-8"))
            if issues:
                console.print("[bold]⚠️  Repository Issues[/bold]")
                for repo_id, error_msg in issues.items():
                    console.print(f"  [yellow]{repo_id}:[/yellow] {error_msg}")
        except Exception:
            pass

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

    # 3b. Indexer extractors importability smoke check
    console.print("[bold]Indexer extractors[/bold]")
    try:
        import devgraph.indexer.dispatch  # noqa: F401  # eagerly imports every language extractor

        console.print("  [green][OK][/green] devgraph.indexer.dispatch imports cleanly (all language extractors)")
    except ModuleNotFoundError as e:
        console.print(f"  [red][X] Missing dependency:[/red] {e}")
        console.print("       Run `pip install -e '.[dev]'` to install all declared grammars.")
        any_failed = True
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


def _vscode_mcp_config_path() -> Path:
    """User-level VS Code MCP config path.

    Windows only today (matches the rest of the CLI's Windows-first scope);
    `%APPDATA%` is always set in a normal Windows user session.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("%APPDATA% is not set; cannot locate VS Code's user config directory")
    return Path(appdata) / "Code" / "User" / "mcp.json"


def _register_vscode(python_path: Path, repo_root: Path) -> bool:
    """Upsert a 'devgraph' entry into VS Code's user-level mcp.json.

    Merges into any existing file rather than overwriting it — other
    registered servers must survive this call untouched. Returns True on
    success; prints its own error and returns False on failure so callers
    (e.g. a multi-target loop) can report per-target status instead of
    aborting the whole run.
    """
    config_path = _vscode_mcp_config_path()
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            data = {}
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red][X] Error:[/red] could not read {config_path}: {exc}")
        return False

    data.setdefault("servers", {})
    data["servers"]["devgraph"] = {
        "type": "stdio",
        "command": str(python_path),
        "args": ["-m", "devgraph.mcp.server"],
        "cwd": str(repo_root),
    }

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        console.print(f"[red][X] Error:[/red] could not write {config_path}: {exc}")
        return False

    console.print(f"[green][OK][/green] VS Code: registered 'devgraph' in {config_path}")
    return True


def _run_claude_mcp_add(claude_path: str, python_path: Path, repo_root: Path) -> bool:
    """Register 'devgraph' with Claude Code via `claude mcp add`, skipping if already registered."""
    already_registered = subprocess.run(
        [claude_path, "mcp", "get", "devgraph"],
        cwd=str(repo_root),
        capture_output=True,
    ).returncode == 0
    if already_registered:
        console.print("[green][OK][/green] Claude Code: 'devgraph' already registered")
        return True
    mcp_add_line = f'claude mcp add devgraph -- "{python_path}" -m devgraph.mcp.server'
    console.print(f"\n[bold]Running:[/bold] {mcp_add_line}")
    result = subprocess.run(
        [claude_path, "mcp", "add", "devgraph", "--", str(python_path), "-m", "devgraph.mcp.server"],
        cwd=str(repo_root),
    )
    return result.returncode == 0


mcp_app = typer.Typer(help="Manage DevGraph's MCP registration with Claude Code.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command(name="add")
def mcp_add() -> None:
    """Register DevGraph as an MCP server with Claude Code (runs 'claude mcp add')."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X] Error:[/red] 'claude' not found on PATH")
        raise typer.Exit(code=1)
    if not _run_claude_mcp_add(claude_path, resolve_venv_python(), resolve_repo_root()):
        raise typer.Exit(code=1)


@mcp_app.command(name="remove")
def mcp_remove() -> None:
    """Unregister DevGraph's MCP server from Claude Code (runs 'claude mcp remove')."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X] Error:[/red] 'claude' not found on PATH")
        raise typer.Exit(code=1)
    result = subprocess.run([claude_path, "mcp", "remove", "devgraph"])
    if result.returncode != 0:
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] Removed 'devgraph' from Claude Code")


@mcp_app.command(name="doctor")
def mcp_doctor() -> None:
    """Diagnose the DevGraph <-> Claude Code MCP registration."""
    any_failed = False

    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X][/red] 'claude' CLI not found on PATH")
        any_failed = True
    else:
        console.print(f"[green][OK][/green] claude CLI at {claude_path}")
        result = subprocess.run(
            [claude_path, "mcp", "get", "devgraph"], capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print("[green][OK][/green] 'devgraph' registered")
            console.print(result.stdout.strip())
        else:
            console.print("[red][X][/red] 'devgraph' not registered (run 'devgraph mcp add')")
            any_failed = True

    try:
        from devgraph.mcp.server import build_server  # noqa: F401

        console.print("[green][OK][/green] devgraph.mcp.server imports cleanly")
    except Exception as e:
        console.print(f"[red][X] Import failed:[/red] {e}")
        any_failed = True

    if any_failed:
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")


@app.command(name="client-config")
def client_config(
    claude_mcp_add_only: bool = typer.Option(
        False, "--claude-mcp-add-only", help="Print just the 'claude mcp add' one-liner."
    ),
    run: bool = typer.Option(
        False, "--run", help="Execute registration for the selected --target(s) (opt-in; print-only is the default)."
    ),
    target: str = typer.Option(
        "both", "--target", help="Which client(s) to print/run registration for: 'claude', 'vscode', or 'both'."
    ),
) -> None:
    """Print (or optionally run) the MCP registration command for this machine's checkout.

    Resolves sys.executable and the repo root so the output is portable across
    machines instead of hardcoding a literal path — copy/paste this into any
    client repo's docs instead of a fixed path that only works on one machine.
    """
    if target not in ("claude", "vscode", "both"):
        console.print(f"[red][X] Error:[/red] --target must be 'claude', 'vscode', or 'both' (got '{target}')")
        raise typer.Exit(code=1)

    python_path = resolve_venv_python()
    repo_root = resolve_repo_root()
    mcp_add_line = f'claude mcp add devgraph -- "{python_path}" -m devgraph.mcp.server'
    want_claude = target in ("claude", "both")
    want_vscode = target in ("vscode", "both")

    if claude_mcp_add_only:
        console.print(mcp_add_line, soft_wrap=True)
    else:
        console.print("## Connect DevGraph as an MCP server\n")
        console.print(f"- **command**: {python_path}")
        console.print("- **args**: -m devgraph.mcp.server")
        console.print(f"- **cwd**: {repo_root}\n")
        if want_claude:
            console.print("```bash")
            console.print(mcp_add_line, soft_wrap=True)
            console.print("```")
        if want_vscode:
            console.print(f"\nVS Code (user mcp.json at {_vscode_mcp_config_path()}):")
            console.print("```json")
            console.print(json.dumps(
                {"servers": {"devgraph": {
                    "type": "stdio",
                    "command": str(python_path),
                    "args": ["-m", "devgraph.mcp.server"],
                    "cwd": str(repo_root),
                }}},
                indent=2,
            ))
            console.print("```")

    if run:
        any_failed = False
        if want_claude:
            claude_path = shutil.which("claude")
            if claude_path is None:
                console.print("[red][X] Error:[/red] 'claude' not found on PATH; skipping Claude Code registration")
                any_failed = True
            elif not _run_claude_mcp_add(claude_path, python_path, repo_root):
                any_failed = True
        if want_vscode:
            if not _register_vscode(python_path, repo_root):
                any_failed = True
        if any_failed:
            raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed DevGraph version."""
    try:
        ver = importlib.metadata.version("devgraph")
    except importlib.metadata.PackageNotFoundError:
        # Fallback: read pyproject.toml
        try:
            repo_root = resolve_repo_root()
            pyproject = repo_root / "pyproject.toml"
            if pyproject.exists():
                import tomllib
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                ver = data.get("project", {}).get("version", "unknown")
            else:
                ver = "unknown"
        except Exception:
            ver = "unknown"
    console.print(ver)


@app.command()
def dashboard(
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the dashboard in the default browser."
    ),
    url_only: bool = typer.Option(
        False, "--url-only", help="Just print the dashboard URL, don't open."
    ),
) -> None:
    """Open the DevGraph dashboard in the default browser, or print its URL."""
    settings = get_settings()
    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}"

    # Check tray liveness
    liveness = _tray_liveness_text(settings)
    if liveness != "running":
        console.print("[yellow]Dashboard may not be running[/yellow] (tray app is not alive)")
        console.print("  Start it with: devgraph tray start")

    if url_only:
        console.print(url)
    elif open_browser:
        console.print(f"Opening {url} ...")
        webbrowser.open(url)
    else:
        console.print(url)


@app.command()
def stats(
    repo_id: Optional[str] = typer.Argument(
        None, help="Repository ID. Omit for aggregate stats across all repos."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON instead of a table."
    ),
) -> None:
    """Print summary statistics for one or all registered repositories."""
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        if repo_id:
            registry = _get_registry()
            try:
                if registry.get(repo_id) is None:
                    console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                    raise typer.Exit(code=1)
            finally:
                registry.close()

            data = dashboard_queries.summary_counts(engine, repo_id)
            total_nodes = sum(data["nodes_by_label"].values())
            total_rels = sum(data["relationships_by_type"].values())
        else:
            data = dashboard_queries.total_counts(engine)
            total_nodes = sum(data["nodes_by_label"].values())
            total_rels = sum(data["relationships_by_type"].values())

        if as_json:
            console.print_json(json.dumps({
                "total_nodes": total_nodes,
                "total_relationships": total_rels,
                **data,
            }))
        else:
            console.print(f"\n[bold]Stats{' for ' + repo_id if repo_id else ''}[/bold]")
            console.print(f"  Total nodes: {total_nodes}")
            console.print(f"  Total relationships: {total_rels}")

            if data["nodes_by_label"]:
                node_table = Table(title="Nodes by Label")
                node_table.add_column("Label", style="cyan")
                node_table.add_column("Count", style="green")
                for label, count in sorted(data["nodes_by_label"].items()):
                    node_table.add_row(label, str(count))
                console.print(node_table)

            if data["relationships_by_type"]:
                rel_table = Table(title="Relationships by Type")
                rel_table.add_column("Type", style="cyan")
                rel_table.add_column("Count", style="green")
                for rtype, count in sorted(data["relationships_by_type"].items()):
                    rel_table.add_row(rtype, str(count))
                console.print(rel_table)
    finally:
        engine.close()


@app.command()
def info(
    repo_id: str,
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON instead of a table."
    ),
) -> None:
    """Show detailed information about a registered repository."""
    settings = get_settings()
    registry = _get_registry()
    try:
        repo = registry.get(repo_id)
        if not repo:
            console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
            raise typer.Exit(code=1)

        # Node count from Neo4j
        node_count = 0
        engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            node_count = dashboard_queries.count_nodes(engine, repo_id)
        except Exception:
            pass
        finally:
            engine.close()

        # Git status
        git_status: dict[str, Any] = {}
        try:
            from devgraph.dashboard.git_info import get_git_status
            git_status = get_git_status(repo.path)
        except Exception:
            pass

        # Issues
        issues_path = settings.registry_db_path.parent / "repo_issues.json"
        repo_issues: dict[str, str] = {}
        if issues_path.exists():
            try:
                repo_issues = json.loads(issues_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if as_json:
            console.print_json(json.dumps({
                "repo_id": repo.repo_id,
                "path": str(repo.path),
                "active": repo.active,
                "watch_enabled": repo.watch_enabled,
                "last_indexed": repo.last_indexed,
                "last_indexed_commit": repo.last_indexed_commit,
                "docs_path": repo.docs_path,
                "mentions_enabled": repo.mentions_enabled,
                "pr_source_enabled": repo.pr_source_enabled,
                "issue_source_enabled": repo.issue_source_enabled,
                "node_count": node_count,
                "git_branch": git_status.get("branch"),
                "uncommitted_changes": len(git_status.get("uncommitted", [])),
                "issue": repo_issues.get(repo_id),
            }, default=str))
        else:
            console.print(f"\n[bold]Repository: {repo.repo_id}[/bold]")
            console.print(f"  Path: {repo.path}")
            console.print(f"  Active: {'[OK]' if repo.active else '[X]'}")
            console.print(f"  Watch: {'[OK]' if repo.watch_enabled else '[X]'}")
            console.print(f"  Last indexed: {repo.last_indexed or '-'}")
            console.print(f"  Last indexed commit: {repo.last_indexed_commit or '-'}")
            console.print(f"  Docs path: {repo.docs_path or '(not set)'}")
            console.print(f"  Mentions: {'[OK]' if repo.mentions_enabled else '[X]'}")
            console.print(f"  PR source: {'[OK]' if repo.pr_source_enabled else '[X]'}")
            console.print(f"  Issue source: {'[OK]' if repo.issue_source_enabled else '[X]'}")
            console.print(f"  Nodes in graph: {node_count}")
            console.print(f"  Git branch: {git_status.get('branch', '-')}")
            uncommitted = git_status.get("uncommitted", [])
            console.print(f"  Uncommitted changes: {len(uncommitted)}")
            if uncommitted:
                for entry in uncommitted[:10]:
                    console.print(f"    {entry['state']:>10}  {entry['path']}")
                if len(uncommitted) > 10:
                    console.print(f"    ... and {len(uncommitted) - 10} more")
            issue = repo_issues.get(repo_id)
            if issue:
                console.print(f"  [yellow]Issue:[/yellow] {issue}")
    finally:
        registry.close()


@app.command()
def update(
    force: bool = typer.Option(
        False, "--force", help="Allow update with a dirty working tree (stash first)."
    ),
    branch: str = typer.Option(
        "origin/master", "--branch", help="Remote branch to pull from."
    ),
) -> None:
    """Pull latest from git, reinstall dependencies, and verify.

    Port of scripts/update.ps1 into Python. Runs git pull --ff-only,
    reinstalls the editable package, runs doctor, and restarts the tray
    if it was running.
    """
    repo_root = resolve_repo_root()
    python_path = resolve_venv_python()

    # 1. Check working tree
    if not force:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if result.stdout.strip():
            console.print("[red][X] Local changes present[/red] — commit, stash, or use --force.")
            console.print(result.stdout)
            raise typer.Exit(code=1)

    # 2. Check tray liveness
    settings = get_settings()
    was_running = _tray_liveness_text(settings) == "running"
    if was_running:
        console.print("[yellow]Tray app is running — will restart after update.[/yellow]")

    # 3. Pull
    console.print("[bold]Pulling latest...[/bold]")
    result = subprocess.run(
        ["git", "pull", "--ff-only", branch],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red][X] git pull failed:[/red] {result.stderr.strip()}")
        raise typer.Exit(code=1)
    console.print(f"[green][OK][/green] {result.stdout.strip()}")

    # 4. Stop tray if running
    if was_running:
        console.print("[bold]Stopping tray app...[/bold]")
        subprocess.run(
            [str(python_path), "-m", "devgraph.cli.main", "tray", "stop"],
            cwd=str(repo_root),
        )

    # 5. Reinstall
    console.print("[bold]Reinstalling dependencies...[/bold]")
    result = subprocess.run(
        [str(python_path), "-m", "pip", "install", "-e", ".[dev]"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red][X] pip install failed:[/red] {result.stderr.strip()}")
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] Dependencies reinstalled.")

    # 6. Doctor
    console.print("[bold]Verifying environment...[/bold]")
    result = subprocess.run(
        [str(python_path), "-m", "devgraph.cli.main", "doctor"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print("[red]doctor reported issues:[/red]")
        console.print(result.stdout)
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] All checks passed.")

    # 7. Restart tray
    if was_running:
        console.print("[bold]Restarting tray app...[/bold]")
        subprocess.run(
            [str(python_path), "-m", "devgraph.cli.main", "tray", "start"],
            cwd=str(repo_root),
        )

    console.print("[green]Update complete.[/green]")


@app.command()
def config(
    key: Optional[str] = typer.Argument(
        None, help="Setting key to show (e.g. 'neo4j_uri'). Omit to show all."
    ),
    show_defaults: bool = typer.Option(
        False, "--show-defaults", help="Also show the default value for each setting."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON."
    ),
) -> None:
    """View or validate the current DevGraph configuration."""
    settings = get_settings()

    # Build a list of (field_name, value, default) tuples
    fields: list[tuple[str, Any, Any]] = []
    for field_name in settings.model_fields:
        field_info = settings.model_fields[field_name]
        value = getattr(settings, field_name)
        default = field_info.default
        fields.append((field_name, value, default))

    if key:
        matched = [(n, v, d) for n, v, d in fields if n == key]
        if not matched:
            console.print(f"[red][X] Unknown setting:[/red] {key}")
            raise typer.Exit(code=1)
        fields = matched

    # Mask passwords
    def _display_value(v: Any) -> str:
        if isinstance(v, str) and any(kw in (key or "").lower() or kw in str(key).lower() for kw in ("password", "secret", "token")):
            return "****" if v else "(empty)"
        return str(v)

    if as_json:
        data = {n: getattr(settings, n) for n, _, _ in fields}
        console.print_json(json.dumps(data, default=str))
    else:
        table = Table(title="DevGraph Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        if show_defaults:
            table.add_column("Default", style="yellow")
        for field_name, value, default in fields:
            display = _display_value(value)
            if show_defaults:
                table.add_row(field_name, display, str(default))
            else:
                table.add_row(field_name, display)
        console.print(table)


@app.command()
def logs(
    lines: int = typer.Option(
        50, "--lines", "-n", help="Number of recent lines to show."
    ),
    level: str = typer.Option(
        "INFO", "--level", "-l", help="Minimum log level filter (DEBUG, INFO, WARNING, ERROR)."
    ),
) -> None:
    """Show recent log output from the DevGraph tray/dashboard process."""
    settings = get_settings()
    log_path = settings.log_file
    if not log_path or not log_path.exists():
        console.print("[yellow]No log file found.[/yellow]")
        console.print(f"  Expected at: {log_path}")
        console.print("  The tray app must be started with file logging enabled.")
        return

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    min_level = level_map.get(level.upper(), logging.INFO)

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError as e:
        console.print(f"[red][X] Error reading log file:[/red] {e}")
        raise typer.Exit(code=1)

    # Parse log lines, filter by level, take last N
    filtered: list[str] = []
    for line in text.splitlines():
        # Simple level detection from common log formats
        line_upper = line.upper()
        line_level = logging.INFO
        if "ERROR" in line_upper:
            line_level = logging.ERROR
        elif "WARNING" in line_upper or "WARN" in line_upper:
            line_level = logging.WARNING
        elif "DEBUG" in line_upper:
            line_level = logging.DEBUG
        if line_level >= min_level:
            filtered.append(line)

    tail = filtered[-lines:] if lines > 0 else filtered
    for line in tail:
        console.print(line)


@app.command()
def prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without actually deleting."
    ),
) -> None:
    """Remove orphaned graph data for repos no longer in the registry."""
    settings = get_settings()
    registry = _get_registry()
    try:
        registered_ids = {r.repo_id for r in registry.list_repos()}
    finally:
        registry.close()

    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        # Find all Repository nodes in Neo4j
        repo_rows = engine.run_cypher(
            "MATCH (n:Repository) RETURN n.repo_id AS repo_id"
        )
        neo4j_ids = {row["repo_id"] for row in repo_rows if row.get("repo_id")}
        orphaned = neo4j_ids - registered_ids

        if not orphaned:
            console.print("[green]No orphaned repos found.[/green]")
            return

        console.print(f"Found {len(orphaned)} orphaned repo(s):")
        for rid in sorted(orphaned):
            console.print(f"  {rid}")

        if dry_run:
            console.print("[yellow]Dry run — no data deleted.[/yellow]")
            return

        for rid in sorted(orphaned):
            engine.delete_repository(rid)
            console.print(f"[green][OK][/green] Deleted: {rid}")
    finally:
        engine.close()


@app.command(name="self-test")
def self_test(
    repo_id: Optional[str] = typer.Argument(
        None, help="Repository ID to test. Omit to test all registered repos."
    ),
) -> None:
    """Run internal consistency checks against the graph data."""
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    registry = _get_registry()
    try:
        if repo_id:
            if registry.get(repo_id) is None:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)
            repo_ids = [repo_id]
        else:
            repo_ids = [r.repo_id for r in registry.list_repos()]
    finally:
        registry.close()

    all_passed = True

    for rid in repo_ids:
        console.print(f"\n[bold]Checking: {rid}[/bold]")

        # 1. Every Module node has a file property
        try:
            bad = engine.run_cypher(
                "MATCH (n:Module {repo_id: $rid}) WHERE n.file IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} Module(s) missing 'file' property[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All Module nodes have 'file'")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 2. No dangling CONTAINS edges
        try:
            bad = engine.run_cypher(
                "MATCH (a {repo_id: $rid})-[r:CONTAINS]->(b) "
                "WHERE b.repo_id <> $rid OR b IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} dangling CONTAINS edge(s)[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All CONTAINS edges valid")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 3. No dangling CALLS edges
        try:
            bad = engine.run_cypher(
                "MATCH (a {repo_id: $rid})-[r:CALLS]->(b) "
                "WHERE b.repo_id <> $rid OR b IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} dangling CALLS edge(s)[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All CALLS edges valid")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 4. Registry ↔ Neo4j consistency
        try:
            repo_nodes = engine.run_cypher(
                "MATCH (n:Repository) RETURN n.repo_id AS rid"
            )
            neo4j_ids = {r["rid"] for r in repo_nodes if r.get("rid")}
            registry = _get_registry()
            try:
                reg_ids = {r.repo_id for r in registry.list_repos()}
            finally:
                registry.close()
            orphaned = neo4j_ids - reg_ids
            missing = reg_ids - neo4j_ids
            if orphaned:
                console.print(f"  [red][X] {len(orphaned)} orphaned repo(s) in Neo4j: {', '.join(sorted(orphaned))}[/red]")
                all_passed = False
            if missing:
                console.print(f"  [yellow]{len(missing)} repo(s) in registry but not in Neo4j: {', '.join(sorted(missing))}[/yellow]")
            if not orphaned and not missing:
                console.print(f"  [green][OK][/green] Registry ↔ Neo4j consistent")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

    console.print()
    if all_passed:
        console.print("[green]All checks passed.[/green]")
    else:
        console.print("[red]One or more checks failed.[/red]")
        raise typer.Exit(code=1)


@app.command()
def export(
    repo_id: str,
    fmt: str = typer.Option(
        "json", "--format", "-f", help="Output format: json, cypher, or dot."
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="File path to write to (default: stdout)."
    ),
    limit: int = typer.Option(
        500, "--limit", "-n", help="Maximum number of nodes to export."
    ),
) -> None:
    """Export graph data for a repository in a portable format."""
    if fmt not in ("json", "cypher", "dot"):
        console.print("[red][X] Error:[/red] --format must be 'json', 'cypher', or 'dot'")
        raise typer.Exit(code=1)

    settings = get_settings()
    registry = _get_registry()
    try:
        if registry.get(repo_id) is None:
            console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
            raise typer.Exit(code=1)
    finally:
        registry.close()

    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        nodes, edges = dashboard_queries.graph_slice(engine, repo_id, None, limit)

        if fmt == "json":
            result = export_json(nodes, edges)
        elif fmt == "cypher":
            result = export_cypher(nodes, edges)
        else:
            result = export_dot(nodes, edges)

        if output:
            Path(output).write_text(result, encoding="utf-8")
            console.print(f"[green][OK][/green] Exported {len(nodes)} nodes, {len(edges)} edges to {output}")
        else:
            console.print(result)
    finally:
        engine.close()


if __name__ == "__main__":
    app()
