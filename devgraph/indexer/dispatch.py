"""Indexer dispatch: routes a repo's changed/deleted files to the right
extractor and upserts (or removes) their graph output.

This is the orchestration layer the Implementation Plan's watcher/indexer
sections describe but that never got wired up: `devgraph add`/`rescan`, the
watcher's on_changes callback, and the tray app all now go through
`index_paths`/`remove_paths` here instead of leaving extractors as
importable-but-unwired Python functions.

Dispatch is purely by file name/extension — no path outside what the caller
passes in (already registry-scoped by construction: callers only ever pass
paths from RepoRegistry-backed watchers or a walk rooted at a registered
repo's own `record.path`).
"""

from __future__ import annotations

from pathlib import Path

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.datastores.extractor import DatastoreExtractor
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.python.extractor import index_file as index_python_file

_COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "podman-compose.yml", "podman-compose.yaml", "compose.yml", "compose.yaml"}
_CONTAINERFILE_NAMES = {"containerfile", "dockerfile"}


def index_paths(engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None = None) -> int:
    """Index a set of changed files, routing each to its extractor by name/extension.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID (already registry-scoped by the caller).
        repo_root: The repo's root path, used to resolve docs_path and to
            compute relative paths for provenance.
        paths: Files to (re)index. Paths outside repo_root are silently
            skipped — this function never indexes anything the caller didn't
            explicitly hand it, but the extra check guards against a caller
            bug passing an unrelated path.
        docs_path: The repo's configured docs folder (repo-relative), if any.

    Returns:
        Number of files actually indexed (skipped/unrecognized files don't count).
    """
    indexed = 0
    docs_root = (repo_root / docs_path).resolve() if docs_path else None

    for path in paths:
        path = Path(path)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not str(resolved).startswith(str(repo_root.resolve())):
            continue
        if not resolved.exists() or not resolved.is_file():
            continue

        name_lower = resolved.name.lower()

        if resolved.suffix == ".py":
            index_python_file(engine, repo_id, resolved)
            indexed += 1
        elif docs_root is not None and resolved.suffix in (".md", ".markdown") and str(resolved).startswith(str(docs_root)):
            index_doc_file(engine, repo_id, resolved)
            indexed += 1
        elif name_lower in _CONTAINERFILE_NAMES:
            _index_containerfile(engine, repo_id, resolved)
            indexed += 1
        elif name_lower in _COMPOSE_NAMES:
            _index_compose_file(engine, repo_id, resolved)
            indexed += 1

        # Datastore/API extraction reads the same .py files already routed
        # above, so it runs alongside the Python indexer rather than as a
        # separate dispatch branch.
        if resolved.suffix == ".py":
            _index_datastores(engine, repo_id, resolved)
            _index_apis(engine, repo_id, resolved)

    return indexed


def remove_paths(engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path]) -> int:
    """Remove graph nodes whose provenance is one of these now-deleted files.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID.
        repo_root: The repo's root path (paths outside it are skipped).
        paths: Files that were deleted (no longer expected to exist on disk).

    Returns:
        Number of files whose provenance was cleaned up.
    """
    cleaned = 0
    for path in paths:
        path = Path(path)
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if not str(resolved).startswith(str(repo_root.resolve())):
            continue

        if resolved.suffix == ".py":
            module_name = resolved.name
            engine.delete_nodes_by_source_file(repo_id, module_name)
            cleaned += 1
        elif resolved.suffix in (".md", ".markdown"):
            engine.delete_nodes_by_source_file(repo_id, resolved.name)
            cleaned += 1
    return cleaned


def full_scan(engine: GraphEngine, repo_id: str, repo_root: Path, docs_path: str | None = None) -> int:
    """Walk every file under repo_root and index it. Used by `devgraph add`/`rescan`."""
    all_files = {p for p in repo_root.rglob("*") if p.is_file() and ".git" not in p.parts}
    return index_paths(engine, repo_id, repo_root, all_files, docs_path=docs_path)


def _index_containerfile(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_containerfile(content)
    _upsert_container_result(engine, repo_id, result)


def _index_compose_file(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_compose_file(content)
    _upsert_container_result(engine, repo_id, result)


def _upsert_container_result(engine: GraphEngine, repo_id: str, result) -> None:
    for container in result.containers:
        engine.upsert_node("Container", repo_id, container.name, {**container.properties, "image": container.image})
    for service in result.services:
        engine.upsert_node("Service", repo_id, service.name, service.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label, rel.source_name, rel.relationship_type, rel.target_label, rel.target_name, repo_id
        )


def _index_datastores(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = DatastoreExtractor(repo_id).extract_from_source(content, path.name)
    for ds in result.datastores:
        engine.upsert_node(ds.datastore_type, repo_id, ds.name, ds.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label, rel.source_name, rel.relationship_type, rel.target_label, rel.target_name, repo_id
        )


def _index_apis(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = APIExtractor(repo_id).extract_from_source(content, path.name)
    for endpoint in result.endpoints:
        endpoint_id = f"{endpoint.method} {endpoint.path}"
        engine.upsert_node("Endpoint", repo_id, endpoint_id, endpoint.properties)
    for func in result.functions:
        engine.upsert_node("Function", repo_id, func.name, func.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label, rel.source_name, rel.relationship_type, rel.target_label, rel.target_name, repo_id
        )
