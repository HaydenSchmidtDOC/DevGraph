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

import logging
import re
from pathlib import Path

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.datastores.extractor import DatastoreExtractor
from devgraph.indexer.docs.extractor import DocsExtractor
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.go.extractor import _find_module_path, extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.mentions.extractor import index_file as index_mentions_file
from devgraph.indexer.mentions.extractor import mentions_any, upsert_document_node
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file

logger = logging.getLogger(__name__)

_CPP_SUFFIXES = {".cpp", ".cc", ".cxx", ".h", ".hpp"}

_COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "podman-compose.yml", "podman-compose.yaml", "compose.yml", "compose.yaml"}
_CONTAINERFILE_NAMES = {"containerfile", "dockerfile"}

# Mirrors this project's own .gitignore: directories no full_scan (and, via
# devgraph.watcher.manager, no live watch) should ever walk into. Without
# this, `devgraph add` on any Python repo with a local venv indexes thousands
# of third-party dependency files from .venv/site-packages alongside the
# repo's actual ~dozens of source files.
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".devgraph",
    "node_modules",
    "bin",
    "obj",
    "target",
    "vendor",
    # Kotlin/Gradle build + tool scratch (Kotlin extractor, "motonav" plan):
    # .gradle is the venv-equivalent (build cache + expanded AAR dependency
    # sources); .kotlin is the compiler session cache; the rest are IDE/MCP
    # tool scratch that's never source.
    ".gradle",
    ".kotlin",
    ".idea",
    ".serena",
    ".playwright-mcp",
    # C++ build-directory conventions (Implementation Plan #8, C++ row):
    # CLion/CMake's default out-of-source build dir names.
    "cmake-build-debug",
    "cmake-build-release",
    # Tool-generated scratch caches that can land inside a registered repo's
    # working tree (e.g. a research skill's local cache dir) rather than a
    # true temp directory. Never source, never worth graphing.
    ".firecrawl",
    # Agent worktrees (.worktrees/ at the repo root, .claude/worktrees/ for
    # ones a coding agent spawns). Each is a full checkout of the repo it
    # lives inside, so walking them indexes the entire repo again per live
    # worktree — the same function then legitimately exists at N paths and
    # becomes N nodes under the file-scoped MERGE key, silently multiplying
    # the graph by however many worktrees happen to be open at scan time.
    ".worktrees",
    "worktrees",
}

_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}


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
            skipped. Besides these, the batch also re-indexes files that
            refer to what these files contain: direct importers (see
            _expand_with_reverse_dependents) and files whose by-name edges
            target a node the batch adds (see _find_referrers), and relinks
            Markdown mentioning an added name (see _mention_referrers).
        docs_path: The repo's configured docs folder (repo-relative), if any.
        mentions_enabled: Whether to index mentions in Markdown files.

    Returns:
        Number of files actually indexed, including those referrers
        (skipped/unrecognized files and Markdown relinks don't count).
    """
    indexed = 0
    docs_root = (repo_root / docs_path).resolve() if docs_path else None
    py_files: list[tuple[str, str]] = []  # (rel_path, content), for the cross-link pass below
    # (rel_path -> (node dicts, rel dicts)) from pass 1's extraction, reused
    # by pass 2 so it re-upserts without re-parsing the file a second time.
    py_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    # Same two-pass cross-link machinery as py_files/py_extractions above,
    # kept as parallel lists rather than merged with the Python one:
    # datastore/API extraction (_index_datastores/_index_apis/
    # _owning_service_relationships below) is still keyed to Python source
    # conventions (regex/AST patterns tuned for Python), so non-Python files
    # don't participate in those passes — only in their own node/edge
    # re-upsert pass.
    js_files: list[str] = []  # rel_path, for the cross-link pass below
    js_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    cs_files: list[str] = []
    cs_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    cpp_files: list[str] = []
    cpp_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    java_files: list[tuple[str, str]] = []
    java_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    rs_files: list[str] = []
    rs_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    kt_files: list[str] = []
    kt_extractions: dict[str, tuple[list[dict], list[dict]]] = {}

    # Same cross-link-on-second-pass pattern as the .py branch, kept as a
    # parallel list rather than folded into py_files/py_extractions since Go
    # extraction needs go.mod's module path (resolved once per batch, not
    # per file) that the Python branch has no equivalent of.
    go_extractions: dict[str, tuple[list[dict], list[dict]]] = {}
    module_path = _find_module_path(repo_root)
    # Markdown files whose cross-file edges are resolved after every other
    # node in the batch exists (see the docs/mentions passes below).
    docs_files: list[Path] = []
    mention_files: list[Path] = []

    # Process files in a fixed order, not set order. Shared nodes
    # (Datastore/Endpoint) keep the last writer's `source`/`library` and
    # accumulate `sources` in claim order -- so iterating the set directly
    # made the graph depend on PYTHONHASHSEED. (Cross-file edges no longer
    # depend on order: they are re-resolved in the passes after this loop.)
    # Each path is resolved once and the batch is sorted by its
    # repo-relative POSIX path, so the order is the same however the caller
    # spelled a path (relative, absolute, through a symlink).
    root_resolved = repo_root.resolve()
    by_rel_path: dict[str, Path] = {}
    for path in _expand_with_reverse_dependents(engine, repo_id, repo_root, paths):
        try:
            resolved = Path(path).resolve()
            rel_path = resolved.relative_to(root_resolved).as_posix()
        except (OSError, ValueError):
            continue
        by_rel_path[rel_path] = resolved

    def index_one(rel_path: str, resolved: Path) -> None:
        nonlocal indexed
        if not resolved.exists() or not resolved.is_file():
            return
        # One unparseable/locked file (or a transient Neo4j error mid-batch)
        # must not abort the whole batch: a full scan or watcher batch would
        # otherwise silently lose every file after the failure point. Log and
        # skip the offending file so the rest of the batch still indexes.
        try:
            indexed += _index_single_path(
                engine, repo_id, repo_root, resolved, rel_path, resolved.name.lower(),
                docs_root, mentions_enabled, module_path,
                py_files, py_extractions, js_files, js_extractions,
                cs_files, cs_extractions, cpp_files, cpp_extractions,
                java_files, java_extractions, rs_files, rs_extractions,
                kt_files, kt_extractions, go_extractions,
                docs_files, mention_files,
            )
        except Exception:
            logger.warning(
                "indexing failed for %s (%s); skipping file", repo_id, rel_path, exc_info=True
            )

    # What the batch's files produced before this run, so the referrer step
    # below can tell which nodes the batch adds. Docs notes record a bare
    # filename as their source_file, hence the extra keys.
    provenance_keys = set(by_rel_path)
    if docs_root is not None:
        provenance_keys |= {p.name for p in by_rel_path.values() if p.is_relative_to(docs_root)}
    batch_keys = sorted(provenance_keys)
    previous_nodes = engine.list_file_nodes(repo_id, batch_keys)

    for rel_path in sorted(by_rel_path):
        index_one(rel_path, by_rel_path[rel_path])

    # Files outside the batch that refer by name to a node the batch just
    # added were indexed before that node existed, so their edge to it was
    # skipped. Index them now as part of this batch (one level, no
    # recursion); the passes below then link them like any batch file.
    code_extractions = [
        py_extractions, js_extractions, cs_extractions, cpp_extractions,
        java_extractions, rs_extractions, kt_extractions, go_extractions,
    ]
    added_nodes = _batch_nodes(repo_id, root_resolved, code_extractions, docs_files, mention_files) - previous_nodes
    referrers = _find_referrers(
        engine, repo_id, root_resolved, docs_root, added_nodes, set(by_rel_path),
        {**java_extractions, **kt_extractions},
    )
    for rel_path in sorted(referrers):
        index_one(rel_path, root_resolved / rel_path)
    # Markdown that only mentions an added name is unchanged itself, so it
    # just gains edges to the added names (in the mentions pass below)
    # instead of a full re-index that re-matches every name in the repo.
    mention_relinks: list[Path] = []
    if mentions_enabled and added_nodes:
        mention_relinks = [
            root_resolved / rel_path
            for rel_path in sorted(
                _mention_referrers(engine, repo_id, root_resolved, added_nodes, set(by_rel_path) | referrers, batch_keys)
            )
        ]

    # Second pass: re-upsert every .py file's already-extracted nodes/edges
    # (no re-parse, no re-prune). Batch order is path order, not dependency
    # order, so a CALLS/IMPORTS edge from file X to file Y within the
    # SAME batch can silently fail to materialize on the first pass if X
    # happens to sort before Y — upsert_relationships only
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

    # Same same-batch cross-link guarantee as the .py re-upsert pass above,
    # for JS/TS files.
    for rel_path in js_files:
        nodes, rels = js_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same re-upsert pass for C# files, same rationale as above.
    for rel_path in cs_files:
        nodes, rels = cs_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same second-pass re-upsert, for the same reason, for this batch's C++
    # files (a stub Class node from an out-of-class method definition — see
    # cpp/extractor.py — is exactly the kind of same-batch endpoint an
    # IMPORTS/CONTAINS edge could otherwise miss on the first pass).
    for rel_path in cpp_files:
        nodes, rels = cpp_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same cross-link re-upsert as above, for the Java files in this batch.
    for rel_path, _content in java_files:
        nodes, rels = java_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same second-pass re-upsert for Rust files, for the same reason.
    for rel_path in rs_files:
        nodes, rels = rs_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same second-pass re-upsert for Kotlin files, for the same reason.
    for rel_path in kt_files:
        nodes, rels = kt_extractions[rel_path]
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Same second-pass rationale as the .py loop above, for Go's CALLS/
    # IMPORTS edges within this batch.
    for rel_path, (nodes, rels) in go_extractions.items():
        engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Service cross-linking runs as a final pass, after every file in this
    # batch (including any compose file) has been indexed — Service nodes'
    # build_context properties must already be in the graph for this to find
    # anything, and a compose file can sort after the files it owns within
    # one batch. The Service/
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

    # Docs notes get the same node-then-edge treatment as the source files
    # above: pass 1 (in the loop) created every note's node, but a note's
    # SUPERSEDES/DECIDED_BY target note or `links` Module may come from a
    # file that sorted after it. Re-indexing the (tiny) note now that every
    # node in the batch exists materializes those edges.
    for path in docs_files:
        try:
            index_doc_file(engine, repo_id, path)
        except Exception:
            logger.warning("docs edge pass failed for %s (%s); skipping file", repo_id, path, exc_info=True)

    # Mentions run last: unlike every other extractor, mention extraction
    # reads the graph (the set of known entity names) to decide what a
    # Markdown file mentions, so it needs every node from this batch --
    # code symbols, docs notes, and the Document nodes pass 1 created --
    # to already exist.
    for path in mention_files:
        try:
            index_mentions_file(engine, repo_id, path, repo_root, ambiguous_mode=get_settings().mentions_ambiguous_mode)
        except Exception:
            logger.warning("mentions pass failed for %s (%s); skipping file", repo_id, path, exc_info=True)
    added_names = {name for _label, name in added_nodes}
    for path in mention_relinks:
        try:
            index_mentions_file(
                engine, repo_id, path, repo_root, ambiguous_mode=get_settings().mentions_ambiguous_mode, names=added_names
            )
        except Exception:
            logger.warning("mentions relink failed for %s (%s); skipping file", repo_id, path, exc_info=True)

    return indexed


def _index_single_path(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    resolved: Path,
    rel_path: str,
    name_lower: str,
    docs_root: Path | None,
    mentions_enabled: bool,
    module_path: str | None,
    py_files: list[tuple[str, str]],
    py_extractions: dict[str, tuple[list[dict], list[dict]]],
    js_files: list[str],
    js_extractions: dict[str, tuple[list[dict], list[dict]]],
    cs_files: list[str],
    cs_extractions: dict[str, tuple[list[dict], list[dict]]],
    cpp_files: list[str],
    cpp_extractions: dict[str, tuple[list[dict], list[dict]]],
    java_files: list[tuple[str, str]],
    java_extractions: dict[str, tuple[list[dict], list[dict]]],
    rs_files: list[str],
    rs_extractions: dict[str, tuple[list[dict], list[dict]]],
    kt_files: list[str],
    kt_extractions: dict[str, tuple[list[dict], list[dict]]],
    go_extractions: dict[str, tuple[list[dict], list[dict]]],
    docs_files: list[Path],
    mention_files: list[Path],
) -> int:
    """Extract and upsert one file's graph output, routing by name/extension.

    Extracted into its own function so `index_paths` can wrap each file in a
    try/except: one unparseable/locked file (or a transient Neo4j error
    mid-batch) must not abort the whole batch. The per-language accumulator
    lists are passed in so the second-pass cross-linking below still sees
    every file that *did* extract successfully.

    Returns the number of indexing actions performed (0 for a file that
    matched no extractor, e.g. a Markdown file outside docs_path with
    mentions disabled) -- mirrors the original loop's `indexed += 1` count.
    """
    indexed = 0
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
        api_rels = _index_apis(engine, repo_id, rel_path, content)
        py_files.append((rel_path, content))
        # API edges ride along in the re-upsert pass: an Endpoint's
        # IMPLEMENTS target can be a handler defined in a later file (e.g.
        # a Django urls.py naming a view from views.py).
        py_extractions[rel_path] = (nodes, rels + api_rels)
    elif resolved.suffix in _JS_SUFFIXES:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_js_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-reupsert rationale as the .py branch above:
        # prune this file's previously-indexed nodes/edges and write the
        # freshly-extracted ones in one transaction.
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        js_files.append(rel_path)
        js_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix == ".cs":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_csharp_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above:
        # prune this file's stale nodes/edges in one transaction, then
        # re-upsert in pass 2 once every file in the batch has a node.
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        cs_files.append(rel_path)
        cs_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix in _CPP_SUFFIXES:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_cpp_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        cpp_files.append(rel_path)
        cpp_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix == ".java":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_java_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-cross-link-reupsert pattern as the .py
        # branch above (see its comment for why replace_file_nodes runs
        # first, in one transaction, rather than plain upsert).
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        java_files.append((rel_path, content))
        java_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix == ".rs":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_rust_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-not-merely-upsert reasoning as the .py branch
        # above: a removed Function/Class must not survive in the graph
        # forever just because MERGE never deletes.
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        rs_files.append(rel_path)
        rs_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix == ".kt":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_kotlin_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above: a
        # class/function removed from the file must not survive in the graph
        # as a stale node.
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        kt_files.append(rel_path)
        kt_extractions[rel_path] = (nodes, rels)
    elif resolved.suffix == ".go":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        result = extract_go_file(content, rel_path, repo_id, module_path)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above:
        # a struct/func/method removed from the file must not survive in
        # the graph as a stale node.
        engine.replace_file_nodes(repo_id, rel_path, nodes, rels)
        indexed += 1
        go_extractions[rel_path] = (nodes, rels)
    elif docs_root is not None and resolved.suffix in (".md", ".markdown") and str(resolved).startswith(str(docs_root)):
        index_doc_file(engine, repo_id, resolved)
        indexed += 1
        docs_files.append(resolved)
    if mentions_enabled and resolved.suffix in (".md", ".markdown"):
        # Only the Document node here; its MENTIONS edges are resolved in
        # index_paths' final pass, once every node in the batch exists.
        upsert_document_node(engine, repo_id, resolved, repo_root)
        indexed += 1
        mention_files.append(resolved)
    if name_lower in _CONTAINERFILE_NAMES:
        _index_containerfile(engine, repo_id, resolved, rel_path)
        indexed += 1
    elif name_lower in _COMPOSE_NAMES:
        _index_compose_file(engine, repo_id, resolved, rel_path)
        indexed += 1
    return indexed


def _expand_with_reverse_dependents(
    engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path]
) -> set[Path]:
    """Widen a changed-files batch to also include direct importers of any
    changed .py or .java file already in the graph.

    Without this, a CALLS/IMPORTS edge in some other file (e.g. a caller of
    a since-renamed/removed function) is only ever re-evaluated when that
    other file happens to be edited again, or a full rescan runs — it isn't
    a dangling edge (upsert_relationship only MATCH-MATCHes real endpoint
    nodes), but it silently goes stale/missing until then. One level of
    fan-out only (direct importers, not transitive) to keep this a cheap
    per-change lookup rather than a repo walk; transitive staleness is rare
    enough that `--full` remains the intended escape hatch for it.

    This finds referrers only through edges that already exist. A referrer
    indexed before its target existed has no edge to follow; index_paths
    finds those separately, after pass 1, via _find_referrers.
    """
    root_resolved = repo_root.resolve()
    expanded = set(paths)
    original_rel_paths = set()

    for path in paths:
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if resolved.suffix not in (".py", ".java") or not str(resolved).startswith(str(root_resolved)):
            continue
        try:
            original_rel_paths.add(resolved.relative_to(root_resolved).as_posix())
        except ValueError:
            continue

    for rel_path in original_rel_paths:
        for importer_rel_path in engine.find_importing_modules(repo_id, rel_path):
            if importer_rel_path in original_rel_paths:
                continue
            importer_path = (root_resolved / importer_rel_path).resolve()
            if importer_path.exists():
                expanded.add(importer_path)

    return expanded


def _batch_nodes(
    repo_id: str,
    root: Path,
    code_extractions: list[dict[str, tuple[list[dict], list[dict]]]],
    docs_files: list[Path],
    mention_files: list[Path],
) -> set[tuple[str, str]]:
    """(label, name) of every file-provenance node this batch wrote: code
    symbols and Modules, docs notes, and Markdown Document nodes -- the same
    provenance list_file_nodes snapshots. File-less nodes (a C++ out-of-class
    method's Class stub, API handler stubs, other `source`-keyed nodes) are
    left out: they have no provenance to compare against, so they would
    look newly added on every save."""
    nodes = {
        (node["label"], node["name"])
        for extractions in code_extractions
        for file_nodes, _rels in extractions.values()
        for node in file_nodes
        if node["label"] == "Module" or node["properties"].get("file")
    }
    for path in docs_files:
        result = DocsExtractor(repo_id).extract_from_source(_read_text(path), path.name)
        nodes |= {(doc.label, doc.name) for doc in result.docs}
    nodes |= {("Document", path.relative_to(root).as_posix()) for path in mention_files}
    return nodes


def _find_referrers(
    engine: GraphEngine,
    repo_id: str,
    root: Path,
    docs_root: Path | None,
    added: set[tuple[str, str]],
    batch: set[str],
    jvm_extractions: dict[str, tuple[list[dict], list[dict]]],
) -> set[str]:
    """Repo-relative paths outside `batch` whose by-name edges can target a
    node in `added` -- files indexed before that node existed, so their edge
    to it was skipped (an edge whose endpoint is missing is never created).

    Unlike _expand_with_reverse_dependents, nothing in the graph points from
    these files to the new node, so each kind is found by its own lookup,
    and each is bounded to stay cheap: it only runs when the batch added a
    node of a label it can target, and it only reads files outside the
    batch. A full scan has no files outside the batch, and a batch that
    only re-saves files adds no nodes, so either way this is close to free.
    (Markdown mentions are found separately, by _mention_referrers.)
    """
    if not added:
        return set()
    found = _docs_note_referrers(repo_id, root, docs_root, added, batch)
    found |= _handler_stub_referrers(engine, repo_id, added)
    found |= _same_package_subtype_referrers(root, added, batch, jvm_extractions)
    return found - batch


# Labels a docs note's SUPERSEDES/DECIDED_BY/`links` edges can point at.
_DOCS_NOTE_TARGET_LABELS = {"DesignDecision", "ArchitectureNote", "Module"}


def _docs_note_referrers(
    repo_id: str, root: Path, docs_root: Path | None, added: set[tuple[str, str]], batch: set[str]
) -> set[str]:
    """Docs notes whose SUPERSEDES/DECIDED_BY/`links` edge names an added
    note or Module. Re-parses each note outside the batch (docs folders are
    small; no graph access) and keeps only those with an edge endpoint in
    `added`."""
    if docs_root is None or not any(label in _DOCS_NOTE_TARGET_LABELS for label, _name in added):
        return set()
    found = set()
    for path in sorted(docs_root.rglob("*")):
        if path.suffix not in (".md", ".markdown") or not _is_indexable_file(path) or is_ignored_path(path):
            continue
        rel_path = path.relative_to(root).as_posix()
        if rel_path in batch:
            continue
        result = DocsExtractor(repo_id).extract_from_source(_read_text(path), path.name)
        if any(
            (rel.source_label, rel.source_name) in added or (rel.target_label, rel.target_name) in added
            for rel in result.relationships
        ):
            found.add(rel_path)
    return found


def _handler_stub_referrers(engine: GraphEngine, repo_id: str, added: set[tuple[str, str]]) -> set[str]:
    """Route files whose Endpoint IMPLEMENTS a handler that only existed as
    a file-less stub until the batch added a real Function of that name.
    One graph lookup, only when the batch added Functions."""
    functions = sorted(name for label, name in added if label == "Function")
    if not functions:
        return set()
    return engine.find_handler_stub_sources(repo_id, functions)


# At most this many Markdown files are relinked per batch to pick up
# mentions of the batch's new names, so adding a common name (`get`, `run`)
# mentioned across a large docs tree can't stall a watcher save. The rest
# wait for the next rescan.
_MAX_MENTION_RELINKS = 25


def _mention_referrers(
    engine: GraphEngine,
    repo_id: str,
    root: Path,
    added: set[tuple[str, str]],
    batch: set[str],
    batch_keys: list[str],
) -> set[str]:
    """Markdown files (already indexed for mentions) that mention an added
    node's name, at most _MAX_MENTION_RELINKS of them.

    A name some node outside the batch already has is answered from the
    graph: the Documents already MENTIONing that (label, name) are exactly
    the ones that mention it. Only names new to the whole graph need a text
    check of each Markdown file (the extractor's own matching), which stops
    once the cap is exceeded.
    """
    candidates = [
        rel_path
        for rel_path in sorted(engine.list_indexed_files(repo_id))
        if rel_path.endswith((".md", ".markdown")) and rel_path not in batch
    ]
    if not candidates:
        return set()
    existing = engine.find_mentioning_documents(repo_id, sorted(added), batch_keys)
    found = {doc for docs in existing.values() for doc in docs} & set(candidates)
    new_names = {name for label, name in added if (label, name) not in existing}
    if new_names:
        for rel_path in candidates:
            if len(found) > _MAX_MENTION_RELINKS:
                break
            path = root / rel_path
            if rel_path not in found and _is_indexable_file(path) and mentions_any(_read_text(path), new_names):
                found.add(rel_path)
    if len(found) > _MAX_MENTION_RELINKS:
        logger.warning(
            "%s: more than %d Markdown files mention names this batch added; relinking the first %d, "
            "the rest link on the next rescan",
            repo_id, _MAX_MENTION_RELINKS, _MAX_MENTION_RELINKS,
        )
        found = set(sorted(found)[:_MAX_MENTION_RELINKS])
    return found


def _same_package_subtype_referrers(
    root: Path,
    added: set[tuple[str, str]],
    batch: set[str],
    jvm_extractions: dict[str, tuple[list[dict], list[dict]]],
) -> set[str]:
    """Java/Kotlin files in the same directory (package) as an added Class
    that name it -- a same-package subtype needs no import, so there is no
    IMPORTS edge for _expand_with_reverse_dependents to follow. A word
    match over sibling files with the same extension."""
    found = set()
    for rel_path, (nodes, _rels) in jvm_extractions.items():
        classes = {node["name"] for node in nodes if node["label"] == "Class" and ("Class", node["name"]) in added}
        if not classes:
            continue
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in sorted(classes)) + r")\b")
        source = root / rel_path
        for sibling in sorted(source.parent.iterdir()):
            sibling_rel = sibling.relative_to(root).as_posix()
            if sibling.suffix != source.suffix or sibling_rel in batch or not _is_indexable_file(sibling):
                continue
            if pattern.search(_read_text(sibling)):
                found.add(sibling_rel)
    return found


def _read_text(path: Path) -> str:
    """A referrer candidate's text, or "" if it can't be read (it is then
    simply not a referrer)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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

        if resolved.suffix in (".py", ".cs", ".java", ".rs", ".go", ".kt"):
            # Must match the same repo-relative key index_paths() writes
            # (Module nodes are keyed by path relative to repo_root, not
            # bare filename — see the corresponding extractor's index_file
            # for each language).
            try:
                module_name = resolved.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                module_name = resolved.name
            engine.delete_nodes_by_source_file(repo_id, module_name)
            cleaned += 1
        elif resolved.suffix in _JS_SUFFIXES:
            # Same repo-relative-key rationale as the .py branch above,
            # matching what index_paths()/extract_js_file() writes.
            try:
                module_name = resolved.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                module_name = resolved.name
            engine.delete_nodes_by_source_file(repo_id, module_name)
            cleaned += 1
        elif resolved.suffix in _CPP_SUFFIXES:
            # Must match the same repo-relative key index_paths() writes
            # (Module nodes are keyed by path relative to repo_root — see
            # cpp/extractor.py's index_file).
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
        else:
            # Any other file that has a bare Module node keyed on its
            # repo-relative path (docs/mentions extractors' shape, or a
            # file indexed before its extension was routed). The delete
            # Cypher matches Module.name == path, so no extension routing
            # is needed here — just clean up whatever names this path.
            try:
                module_name = resolved.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                module_name = resolved.name
            engine.delete_nodes_by_source_file(repo_id, module_name)
            cleaned += 1
    return cleaned


def _is_indexable_file(path: Path) -> bool:
    """p.is_file(), but a file the OS can't even stat (locked, broken
    symlink, Windows reparse point) is skipped rather than aborting the
    whole scan."""
    try:
        return path.is_file()
    except OSError:
        return False


def _indexable_paths(repo_root: Path) -> set[Path]:
    """Every file under repo_root that a full scan would index: a regular
    file, not under an ignored directory. Shared by full_scan (which indexes
    them) and prune_stale_files (which diffs them against the graph)."""
    return {p for p in repo_root.rglob("*") if _is_indexable_file(p) and not is_ignored_path(p)}


def prune_stale_files(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    docs_path: str | None = None,
    mentions_enabled: bool = False,
) -> int:
    """Delete graph nodes whose file no longer exists on disk.

    The reconcile half of a rescan: full_scan re-indexes everything that's
    currently on disk, but nothing ever removed nodes for files that were
    deleted while the watcher was down (or that a watcher event missed).
    Without this, a rescan is additive-only and stale nodes linger forever.

    Conservative by construction: only file-provenance nodes
    (`source_file`/`file` keys) are reconciled — `Commit`/`Repository` nodes
    and `source`-keyed nodes (Container/Service/API, co-produced by several
    files) are never touched here. The diff is disk-vs-graph: every path the
    graph believes it has indexed that is no longer an indexable file on
    disk is routed through remove_paths, which handles the
    delete-vs-unclaim distinction per node.

    Returns the number of files pruned.
    """
    on_disk = {p.resolve().relative_to(repo_root.resolve()).as_posix() for p in _indexable_paths(repo_root)}
    in_graph = engine.list_indexed_files(repo_id)
    stale = in_graph - on_disk
    if not stale:
        return 0
    stale_paths = {repo_root / p for p in stale}
    return remove_paths(engine, repo_id, repo_root, stale_paths)


def full_scan(engine: GraphEngine, repo_id: str, repo_root: Path, docs_path: str | None = None, mentions_enabled: bool = False) -> int:
    """Walk every file under repo_root and index it, skipping VCS/build/venv noise. Used by `devgraph add`/`rescan`.

    Reconciles the graph to disk before indexing: any file the graph has
    nodes for that no longer exists on disk is pruned first (see
    prune_stale_files), so a rescan heals stale nodes left by a watcher
    that was down or missed events — not just adds/updates what's current.
    """
    prune_stale_files(engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled)
    all_files = _indexable_paths(repo_root)
    return index_paths(engine, repo_id, repo_root, all_files, docs_path=docs_path, mentions_enabled=mentions_enabled)


def _index_containerfile(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_containerfile(content, rel_path)
    _upsert_container_result(engine, repo_id, result)


def _index_compose_file(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_compose_file(content, rel_path)
    _upsert_container_result(engine, repo_id, result)


def _relationship_dict(rel, repo_id: str) -> dict:
    """Canonical upsert_relationships dict from any extractor's
    source_label/source_name/relationship_type/target_label/target_name
    Relationship dataclass shape (docs/apis/containers/datastores all share
    it, distinct from the python extractor's from_/to_/rel_type naming).

    from_file/to_file use getattr with a None default since only
    containers/extractor.py's Relationship carries them (Service is the one
    label from this family that's file-scoped) -- every other module's
    Relationship dataclass predates that field and stays bare-name matched.
    """
    return {
        "from_label": rel.source_label,
        "from_name": rel.source_name,
        "rel_type": rel.relationship_type,
        "to_label": rel.target_label,
        "to_name": rel.target_name,
        "repo_id": repo_id,
        "properties": getattr(rel, "properties", None) or {},
        "from_file": getattr(rel, "from_file", None),
        "to_file": getattr(rel, "to_file", None),
    }


def _upsert_container_result(engine: GraphEngine, repo_id: str, result) -> None:
    # Container nodes deliberately stay keyed on (repo_id, name) alone, no
    # `file` -- a Container node represents a shared base image (e.g.
    # "python"), and two compose/Containerfiles both building FROM the same
    # image should merge into that one shared node. Service nodes are the
    # opposite: two different compose files declaring a service named "api"
    # are two distinct services that happen to share a name, and merging
    # them today (both keyed on bare (repo_id, name)) is the same
    # cross-file collision bug Function/Class nodes had -- ServiceNode
    # already carries a `file` property (see ContainerExtractor), which
    # _upsert_nodes_tx picks up automatically to key Service on
    # (repo_id, name, file) instead.
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


def _load_services_with_build_context(engine: GraphEngine, repo_id: str) -> dict[str, tuple[str, str]]:
    """{Service name -> (build_context, file)} for every repo Service that has one.

    `file` (the compose file the Service was declared in) is carried
    alongside build_context so _owning_service_relationships can set
    from_file/to_file on the edges it emits -- Service is now file-scoped
    (see schema.py's _FILE_SCOPED_LABELS), so a MATCH by bare name alone
    would hit every same-named Service across every compose file again.

    Loaded once per index_paths batch (not once per file) since Services are
    only written earlier in the same batch by the Containerfile/compose
    branches, so the set can't change again mid-batch.
    """
    results = engine.run_cypher(
        "MATCH (s:Service {repo_id: $repo_id}) "
        "WHERE s.build_context IS NOT NULL "
        "RETURN s.name as name, s.build_context as build_context, s.file as file",
        {"repo_id": repo_id},
    )
    # Sorted so the result doesn't depend on the order Neo4j returns rows:
    # _match_owning_service keeps the first of several Services sharing a
    # build_context, and a name declared in two compose files keeps the last.
    results = sorted(results, key=lambda row: (row["name"], row["file"] or ""))
    return {row["name"]: (row["build_context"], row["file"]) for row in results}


def _match_owning_service(services: dict[str, tuple[str, str]], rel_path: str) -> str | None:
    """Find the Service whose build_context is the longest prefix of rel_path's directory."""
    file_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""

    best_match: str | None = None
    best_match_len = -1
    for name, (context, _file) in services.items():
        if file_dir == context or file_dir.startswith(context + "/"):
            if len(context) > best_match_len:
                best_match = name
                best_match_len = len(context)

    return best_match


def _owning_service_relationships(
    repo_id: str, rel_path: str, content: str, services: dict[str, tuple[str, str]]
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
    _owning_context, owning_service_file = services[owning_service]

    rels: list[dict] = []

    datastore_result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    for ds in datastore_result.datastores:
        rels.append(
            {
                "from_label": "Service",
                "from_name": owning_service,
                "from_file": owning_service_file,
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
                "to_file": owning_service_file,
                "repo_id": repo_id,
                "properties": {},
            }
        )

    return rels


def _index_apis(engine: GraphEngine, repo_id: str, rel_path: str, content: str) -> list[dict]:
    result = APIExtractor(repo_id).extract_from_source(content, rel_path)
    nodes = [
        {"label": "Endpoint", "repo_id": repo_id, "name": f"{e.method} {e.path}", "properties": e.properties}
        for e in result.endpoints
    ] + [
        {"label": "Function", "repo_id": repo_id, "name": f.name, "properties": f.properties}
        for f in result.functions
    ]
    engine.upsert_nodes(nodes)
    rels = [_relationship_dict(rel, repo_id) for rel in result.relationships]
    engine.upsert_relationships(rels)
    return rels
