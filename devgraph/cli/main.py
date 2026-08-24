"""DevGraph CLI: register repositories, manage watch settings, check status."""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

app = typer.Typer(help="DevGraph: local-first developer knowledge graph")
console = Console()


def _get_registry() -> RepoRegistry:
    """Get or create the registry from configured path."""
    settings = get_settings()
    return RepoRegistry(settings.registry_db_path)


@app.command()
def add(path: str) -> None:
    """Register a repository for DevGraph indexing.

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
    """Trigger a rescan of a repository.

    This is a placeholder: the indexer wiring is handled separately.

    Args:
        repo_id: The repository ID to rescan.
    """
    try:
        registry = _get_registry()
        try:
            # Verify the repo exists
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            console.print(f"[green][OK][/green] Rescan queued for {repo_id}")
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
