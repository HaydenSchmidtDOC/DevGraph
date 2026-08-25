"""DevGraph CLI: register repositories, manage watch settings, check status."""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.git_history.extractor import index_repo_history
from devgraph.registry.store import RepoRegistry

app = typer.Typer(help="DevGraph: local-first developer knowledge graph")
console = Console()


def _get_registry() -> RepoRegistry:
    """Get or create the registry from configured path."""
    settings = get_settings()
    return RepoRegistry(settings.registry_db_path)


@app.command()
def add(path: str) -> None:
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
def rescan(repo_id: str) -> None:
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

    console.print()


if __name__ == "__main__":
    app()
