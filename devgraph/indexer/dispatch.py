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
    py_files: list[tuple[str, str]] = []  # (rel_path, content), for the cross-link pass below

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
        rel_path = resolved.relative_to(repo_root.resolve()).as_posix()

        if resolved.suffix == ".py":
            index_python_file(engine, repo_id, resolved, repo_root=repo_root)
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
        # separate dispatch branch. Passed the repo-relative path (not bare
        # filename) so their 'source'/'file' provenance properties match
        # what delete_nodes_by_source_file looks up on file deletion.
        if resolved.suffix == ".py":
            content = resolved.read_text(encoding="utf-8", errors="replace")
            _index_datastores(engine, repo_id, rel_path, content)
            _index_apis(engine, repo_id, rel_path, content)
            py_files.append((rel_path, content))

    # Service cross-linking runs as a final pass, after every file in this
    # batch (including any compose file) has been indexed — Service nodes'
    # build_context properties must already be in the graph for this to find
    # anything, and paths/a compose file can be indexed in any order within
    # one batch (set iteration has no guaranteed order).
    for rel_path, content in py_files:
        _link_to_owning_service(engine, repo_id, rel_path, content)

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
            # Must match the same repo-relative key index_paths() writes
            # (Module nodes are keyed by path relative to repo_root, not
            # bare filename — see python/extractor.py's index_file).
            try:
                module_name = resolved.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
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


def _index_datastores(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> None:
    result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    for ds in result.datastores:
        engine.upsert_node(ds.datastore_type, repo_id, ds.name, ds.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label, rel.source_name, rel.relationship_type, rel.target_label, rel.target_name, repo_id
        )


def _link_to_owning_service(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> None:
    """Link this file's Database/VectorStore/Queue and Endpoint nodes back to
    the compose Service that owns it, via directory containment.

    Closes the previously-documented gap where the container extractor
    (compose-derived Service nodes) and the datastore/API extractors
    (per-file Database/Endpoint nodes) never cross-referenced each other, so
    explain_architecture's Service 'uses'/'calls' output stayed empty even
    on a fully-scanned repo. Ownership is determined by matching rel_path's
    directory against each repo Service's build_context (see
    containers/extractor.py's _extract_build_context) — the longest matching
    prefix wins, so a service at 'services/api' isn't shadowed by an
    unrelated top-level Service with no build_context.
    """
    owning_service = _find_owning_service(engine, repo_id, rel_path)
    if owning_service is None:
        return

    datastore_result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    for ds in datastore_result.datastores:
        engine.upsert_relationship("Service", owning_service, "USES", ds.datastore_type, ds.name, repo_id)

    api_result = APIExtractor(repo_id).extract_from_source(content, rel_path)
    for endpoint in api_result.endpoints:
        endpoint_id = f"{endpoint.method} {endpoint.path}"
        engine.upsert_relationship("Endpoint", endpoint_id, "CALLS", "Service", owning_service, repo_id)


def _find_owning_service(engine: GraphEngine, repo_id: str, rel_path: str) -> str | None:
    """Find the Service whose build_context is the longest prefix of rel_path's directory."""
    file_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""

    results = engine.run_cypher(
        "MATCH (s:Service {repo_id: $repo_id}) "
        "WHERE s.build_context IS NOT NULL "
        "RETURN s.name as name, s.build_context as build_context",
        {"repo_id": repo_id},
    )

    best_match: str | None = None
    best_match_len = -1
    for row in results:
        context = row["build_context"]
        if file_dir == context or file_dir.startswith(context + "/"):
            if len(context) > best_match_len:
                best_match = row["name"]
                best_match_len = len(context)

    return best_match


def _index_apis(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> None:
    result = APIExtractor(repo_id).extract_from_source(content, rel_path)
    for endpoint in result.endpoints:
        endpoint_id = f"{endpoint.method} {endpoint.path}"
        engine.upsert_node("Endpoint", repo_id, endpoint_id, endpoint.properties)
    for func in result.functions:
        engine.upsert_node("Function", repo_id, func.name, func.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label, rel.source_name, rel.relationship_type, rel.target_label, rel.target_name, repo_id
        )
