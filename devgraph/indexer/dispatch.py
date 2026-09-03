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

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.datastores.extractor import DatastoreExtractor
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.mentions.extractor import index_file as index_mentions_file
from devgraph.indexer.python.extractor import extract_python_file

_COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "podman-compose.yml", "podman-compose.yaml", "compose.yml", "compose.yaml"}
_CONTAINERFILE_NAMES = {"containerfile", "dockerfile"}

# Mirrors this project's own .gitignore: directories no full_scan (and, via
# devgraph.watcher.manager, no live watch) should ever walk into. Without
# this, `devgraph add` on any Python repo with a local venv indexes thousands
# of third-party dependency files from .venv/site-packages alongside the
# repo's actual ~dozens of source files.
IGNORED_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", "build", "dist", ".pytest_cache", ".devgraph"}


def is_ignored_path(path: Path) -> bool:
    return any(part in IGNORED_DIR_NAMES or part.endswith(".egg-info") for part in path.parts)


def index_paths(engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None = None, mentions_enabled: bool = False) -> int:
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
        mentions_enabled: Whether to index mentions in Markdown files.

    Returns:
        Number of files actually indexed (skipped/unrecognized files don't count).
    """
    indexed = 0
    docs_root = (repo_root / docs_path).resolve() if docs_path else None
    py_files: list[tuple[str, str]] = []  # (rel_path, content), for the cross-link pass below
    # (rel_path -> (node dicts, rel dicts)) from pass 1's extraction, reused
    # by pass 2 so it re-upserts without re-parsing the file a second time.
    py_extractions: dict[str, tuple[list[dict], list[dict]]] = {}

    paths = _expand_with_reverse_dependents(engine, repo_id, repo_root, paths)

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
            content = resolved.read_text(encoding="utf-8", errors="replace")
            result = extract_python_file(content, rel_path, repo_id)
            nodes = [n.to_dict() for n in result.nodes]
            rels = [r.to_dict() for r in result.relationships]
            # Delete this file's previously-indexed nodes and write the
            # freshly-extracted ones in one transaction, not just
            # MERGE-upsert the current contents: a Function/Class removed
            # from the file (edited, not deleted) would otherwise survive in
            # the graph forever, since MERGE only ever adds/updates matching
            # nodes, never removes ones the current source no longer
            # produces. One transaction also means a reader never observes
            # this file's nodes as gone-but-not-yet-rebuilt.
            engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
            indexed += 1

            # Datastore/API extraction reads the same content, so it runs
            # alongside the Python indexer rather than as a separate dispatch
            # branch. Passed the repo-relative path (not bare filename) so
            # their 'source'/'file' provenance properties match what
            # delete_nodes_by_source_file looks up on file deletion.
            _index_datastores(engine, repo_id, rel_path, content)
            _index_apis(engine, repo_id, rel_path, content)
            py_files.append((rel_path, content))
            py_extractions[rel_path] = (nodes, rels)
        elif docs_root is not None and resolved.suffix in (".md", ".markdown") and str(resolved).startswith(str(docs_root)):
            index_doc_file(engine, repo_id, resolved)
            indexed += 1
        if mentions_enabled and resolved.suffix in (".md", ".markdown"):
            index_mentions_file(engine, repo_id, resolved, repo_root, ambiguous_mode=get_settings().mentions_ambiguous_mode)
            indexed += 1
        if name_lower in _CONTAINERFILE_NAMES:
            _index_containerfile(engine, repo_id, resolved)
            indexed += 1
        elif name_lower in _COMPOSE_NAMES:
            _index_compose_file(engine, repo_id, resolved)
            indexed += 1

    # Second pass: re-upsert every .py file's already-extracted nodes/edges
    # (no re-parse, no re-prune). Batch iteration order is unspecified (paths
    # is a set), so a CALLS/IMPORTS edge from file X to file Y within the
    # SAME batch can silently fail to materialize on the first pass if X
    # happens to be processed before Y — upsert_relationships only
    # MATCH-MATCHes existing endpoint nodes, it doesn't create them, so Y's
    # node isn't there yet when X's edges are upserted. Re-upserting (not
    # re-pruning) every file's cached extraction a second time is idempotent
    # (see test_index_file_creates_idempotent_nodes) and guarantees every
    # node in the batch exists before every file's edges are attempted at
    # least once, regardless of first-pass order.
    for rel_path, _content in py_files:
        nodes, rels = py_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Service cross-linking runs as a final pass, after every file in this
    # batch (including any compose file) has been indexed — Service nodes'
    # build_context properties must already be in the graph for this to find
    # anything, and paths/a compose file can be indexed in any order within
    # one batch (set iteration has no guaranteed order). The Service/
    # build_context lookup is loaded once for the whole batch rather than
    # once per file, since it can't have changed mid-batch (Services are
    # only written by the Containerfile/compose branches above, already run
    # by this point).
    if py_files:
        services = _load_services_with_build_context(engine, repo_id)
        service_rels: list[dict] = []
        for rel_path, content in py_files:
            service_rels.extend(_owning_service_relationships(repo_id, rel_path, content, services))
        engine.upsert_relationships(service_rels)

    return indexed


def _expand_with_reverse_dependents(
    engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path]
) -> set[Path]:
    """Widen a changed-files batch to also include direct importers of any
    changed .py file already in the graph.

    Without this, a CALLS/IMPORTS edge in some other file (e.g. a caller of
    a since-renamed/removed function) is only ever re-evaluated when that
    other file happens to be edited again, or a full rescan runs — it isn't
    a dangling edge (upsert_relationship only MATCH-MATCHes real endpoint
    nodes), but it silently goes stale/missing until then. One level of
    fan-out only (direct importers, not transitive) to keep this a cheap
    per-change lookup rather than a repo walk; transitive staleness is rare
    enough that `--full` remains the intended escape hatch for it.
    """
    root_resolved = repo_root.resolve()
    expanded = set(paths)
    original_py_rel_paths = set()

    for path in paths:
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if resolved.suffix != ".py" or not str(resolved).startswith(str(root_resolved)):
            continue
        try:
            original_py_rel_paths.add(resolved.relative_to(root_resolved).as_posix())
        except ValueError:
            continue

    for rel_path in original_py_rel_paths:
        for importer_rel_path in engine.find_importing_modules(repo_id, rel_path):
            if importer_rel_path in original_py_rel_paths:
                continue
            importer_path = (root_resolved / importer_rel_path).resolve()
            if importer_path.exists():
                expanded.add(importer_path)

    return expanded


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
            # Must match the same repo-relative key index_paths() writes
            # (Document nodes are keyed by path relative to repo_root via
            # mentions/extractor.py's index_file, not bare filename).
            try:
                rel_path = resolved.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                rel_path = resolved.name
            engine.delete_nodes_by_source_file(repo_id, rel_path)
            cleaned += 1
    return cleaned


def full_scan(engine: GraphEngine, repo_id: str, repo_root: Path, docs_path: str | None = None, mentions_enabled: bool = False) -> int:
    """Walk every file under repo_root and index it, skipping VCS/build/venv noise. Used by `devgraph add`/`rescan`."""
    all_files = {p for p in repo_root.rglob("*") if p.is_file() and not is_ignored_path(p)}
    return index_paths(engine, repo_id, repo_root, all_files, docs_path=docs_path, mentions_enabled=mentions_enabled)


def _index_containerfile(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_containerfile(content)
    _upsert_container_result(engine, repo_id, result)


def _index_compose_file(engine: GraphEngine, repo_id: str, path: Path) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_compose_file(content)
    _upsert_container_result(engine, repo_id, result)


def _relationship_dict(rel, repo_id: str) -> dict:
    """Canonical upsert_relationships dict from any extractor's
    source_label/source_name/relationship_type/target_label/target_name
    Relationship dataclass shape (docs/apis/containers/datastores all share
    it, distinct from the python extractor's from_/to_/rel_type naming)."""
    return {
        "from_label": rel.source_label,
        "from_name": rel.source_name,
        "rel_type": rel.relationship_type,
        "to_label": rel.target_label,
        "to_name": rel.target_name,
        "repo_id": repo_id,
        "properties": getattr(rel, "properties", None) or {},
    }


def _upsert_container_result(engine: GraphEngine, repo_id: str, result) -> None:
    nodes = [
        {"label": "Container", "repo_id": repo_id, "name": c.name, "properties": {**c.properties, "image": c.image}}
        for c in result.containers
    ] + [
        {"label": "Service", "repo_id": repo_id, "name": s.name, "properties": s.properties}
        for s in result.services
    ]
    engine.upsert_nodes(nodes)
    engine.upsert_relationships([_relationship_dict(rel, repo_id) for rel in result.relationships])


def _index_datastores(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> None:
    result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    nodes = [{"label": ds.datastore_type, "repo_id": repo_id, "name": ds.name, "properties": ds.properties} for ds in result.datastores]
    engine.upsert_nodes(nodes)
    engine.upsert_relationships([_relationship_dict(rel, repo_id) for rel in result.relationships])


def _load_services_with_build_context(engine: GraphEngine, repo_id: str) -> dict[str, str]:
    """{Service name -> build_context} for every repo Service that has one.

    Loaded once per index_paths batch (not once per file) since Services are
    only written earlier in the same batch by the Containerfile/compose
    branches, so the set can't change again mid-batch.
    """
    results = engine.run_cypher(
        "MATCH (s:Service {repo_id: $repo_id}) "
        "WHERE s.build_context IS NOT NULL "
        "RETURN s.name as name, s.build_context as build_context",
        {"repo_id": repo_id},
    )
    return {row["name"]: row["build_context"] for row in results}


def _match_owning_service(services: dict[str, str], rel_path: str) -> str | None:
    """Find the Service whose build_context is the longest prefix of rel_path's directory."""
    file_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""

    best_match: str | None = None
    best_match_len = -1
    for name, context in services.items():
        if file_dir == context or file_dir.startswith(context + "/"):
            if len(context) > best_match_len:
                best_match = name
                best_match_len = len(context)

    return best_match


def _owning_service_relationships(
    repo_id: str, rel_path: str, content: str, services: dict[str, str]
) -> list[dict]:
    """USES/CALLS relationship dicts linking this file's Database/
    VectorStore/Queue and Endpoint nodes back to the compose Service that
    owns it, via directory containment.

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
    owning_service = _match_owning_service(services, rel_path)
    if owning_service is None:
        return []

    rels: list[dict] = []

    datastore_result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    for ds in datastore_result.datastores:
        rels.append(
            {
                "from_label": "Service",
                "from_name": owning_service,
                "rel_type": "USES",
                "to_label": ds.datastore_type,
                "to_name": ds.name,
                "repo_id": repo_id,
                "properties": {},
            }
        )

    api_result = APIExtractor(repo_id).extract_from_source(content, rel_path)
    for endpoint in api_result.endpoints:
        endpoint_id = f"{endpoint.method} {endpoint.path}"
        rels.append(
            {
                "from_label": "Endpoint",
                "from_name": endpoint_id,
                "rel_type": "CALLS",
                "to_label": "Service",
                "to_name": owning_service,
                "repo_id": repo_id,
                "properties": {},
            }
        )

    return rels


def _index_apis(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> None:
    result = APIExtractor(repo_id).extract_from_source(content, rel_path)
    nodes = [
        {"label": "Endpoint", "repo_id": repo_id, "name": f"{e.method} {e.path}", "properties": e.properties}
        for e in result.endpoints
    ] + [
        {"label": "Function", "repo_id": repo_id, "name": f.name, "properties": f.properties}
        for f in result.functions
    ]
    engine.upsert_nodes(nodes)
    engine.upsert_relationships([_relationship_dict(rel, repo_id) for rel in result.relationships])
