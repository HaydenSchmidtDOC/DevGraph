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
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from devgraph.config import get_settings
from devgraph.config.project_schema import (
    ABSENT_SCHEMA_HASH,
    LABEL_PATTERN,
    RELATIONSHIP_TYPE_PATTERN,
    EffectiveSchema,
    ProjectSchemaError,
    _user_constraint_name,
    resolve_effective_schema,
    schema_file_hash,
)
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.containers.extractor import ContainerExtractor, ExtractionResult
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
from devgraph.indexer.providers import docs, filesystem
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file
from devgraph.indexer.schema_constraints import (
    encode_keys,
    generated_objects,
    realign_keys,
    recorded_declarations,
    release_labels,
)
# Re-exported under their pre-walk.py names for the watcher and existing callers.
from devgraph.indexer.walk import IGNORED_DIR_NAMES as IGNORED_DIR_NAMES
from devgraph.indexer.walk import indexable_paths as _indexable_paths
from devgraph.indexer.walk import is_ignored_dir_name as is_ignored_dir_name
from devgraph.indexer.walk import is_ignored_path as is_ignored_path
from devgraph.indexer.walk import is_indexable_file as _is_indexable_file
from devgraph.indexer.walk import links_outside as _links_outside  # noqa: F401
from devgraph.indexer.walk import repo_relative as _repo_relative
from devgraph.paths import is_within

logger = logging.getLogger(__name__)

_CPP_SUFFIXES = {".cpp", ".cc", ".cxx", ".h", ".hpp"}

_COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "podman-compose.yml", "podman-compose.yaml", "compose.yml", "compose.yaml"}
_CONTAINERFILE_NAMES = {"containerfile", "dockerfile"}

_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}


def _provider_specs(repo_root: Path) -> tuple[bool, filesystem.FilesystemSpec | None, docs.DocsSpec | None]:
    """(schema usable, filesystem spec, docs spec), from one schema resolve.

    An invalid schema disables both providers for this call -- including any
    prune, edge write or relink -- so a bad edit never deletes good nodes.
    """
    try:
        effective = resolve_effective_schema(repo_root)
    except ProjectSchemaError as exc:
        logger.warning("project schema for %s is invalid; schema providers skipped: %s", repo_root, exc)
        return False, None, None
    return True, filesystem.filesystem_spec(effective), docs.docs_spec(effective)


def schema_pending(engine: GraphEngine, repo_id: str, repo_root: Path) -> bool:
    """True when the schema file differs from the one the graph was built with.

    A repository with no recorded state and no schema file has nothing to
    apply, so repositories registered before schema tracking are never
    pending just for lacking state.
    """
    current = schema_file_hash(repo_root)
    applied = engine.read_applied_schema(repo_id)
    if applied is None:
        return current != ABSENT_SCHEMA_HASH
    return current != applied["hash"]


def apply_project_schema(engine: GraphEngine, repo_id: str, repo_root: Path) -> bool:
    """Bring the graph in line with the repository's current schema file;
    see _apply_project_schema. True when applied."""
    return _apply_project_schema(engine, repo_id, repo_root)[0]


#: What `_apply_project_schema` applied for the docs provider: the spec and the front matter it read.
AppliedDocs = tuple[docs.DocsSpec, docs.Selected]


def _apply_project_schema(engine: GraphEngine, repo_id: str, repo_root: Path) -> tuple[bool, AppliedDocs | None]:
    """Bring the graph in line with the repository's current schema file.

    Provisions constraints/indexes, deletes nodes and relationships of user
    types the previously applied schema declared but this one doesn't
    (built-ins are never touched), re-syncs the filesystem and docs providers
    (docs edges are rebuilt by full_scan once every node exists), records
    the applied state, then reconciles the generated constraints/indexes with
    every repository's recorded state (see schema_constraints). An invalid
    schema or a provisioning failure returns False with the graph untouched;
    errors from the deletion or reconcile steps propagate, while a failed
    constraint reconcile (or the re-provisioning after the record) is only logged.

    Both providers prune before either upserts: an upsert MERGEs onto the
    node of that label and name whichever provider made it, and retags it,
    so a type switching providers would otherwise keep the old provider's
    properties.

    Returns (applied, the docs spec and front matter applied, or None), so
    full_scan's edge pass reuses them instead of resolving the schema and
    reading every docs file again.
    """
    current_hash = schema_file_hash(repo_root)
    try:
        effective = resolve_effective_schema(repo_root)
    except ProjectSchemaError as exc:
        logger.warning("project schema for %s is invalid; not applied: %s", repo_root, exc)
        return False, None
    try:
        provision_repository_schema(engine, repo_root)
    except Exception as exc:
        logger.warning("could not provision the project schema for %s; not applied: %s", repo_root, exc)
        return False, None

    labels = [node_type.label for node_type in effective.node_types]
    rel_types = list(dict.fromkeys(r.type for r in effective.relationships if r.type not in RELATIONSHIP_TYPES))
    previous = engine.read_applied_schema(repo_id) or {}
    # Re-validated: these names come back from the graph and are interpolated.
    removed_labels = [
        label for label in previous.get("labels") or []
        if label not in labels and label not in NODE_LABELS and LABEL_PATTERN.fullmatch(label or "")
    ]
    for label in removed_labels:
        engine.delete_label_nodes(repo_id, label)
    for rel_type in previous.get("relationship_types") or []:
        if rel_type not in rel_types and rel_type not in RELATIONSHIP_TYPES and RELATIONSHIP_TYPE_PATTERN.fullmatch(rel_type or ""):
            engine.delete_relationship_type(repo_id, rel_type)

    spec = filesystem.filesystem_spec(effective)
    disk = {rel: p for p in _indexable_paths(repo_root) if (rel := _repo_relative(repo_root, p)) is not None}
    on_disk = set(disk)
    docs_spec = docs.docs_spec(effective)
    docs_nodes, selected = _prune_docs(engine, repo_id, docs_spec, disk)
    filesystem.reconcile(engine, repo_id, spec, on_disk)
    if spec is not None:
        filesystem.sync_present(engine, repo_id, spec, on_disk)
    deferred = _deferred_docs_labels(engine, effective, docs_spec)
    engine.upsert_nodes([node for node in docs_nodes if node["label"] not in deferred])
    engine.record_applied_schema(repo_id, current_hash, labels, rel_types, encode_keys(effective.node_types))
    # After recording, so this repository's new state is part of what every
    # other repository's declarations are weighed against. Provisioning is
    # re-run first: another repository's apply may have released a label
    # between this one's provisioning and its record, and nothing else would
    # re-create the constraint (the schema is no longer pending).
    try:
        engine.init_schema(effective)
        release_labels(engine, removed_labels)
        realign_keys(engine, effective.node_types)
    except Exception as exc:
        logger.warning("could not reconcile generated constraints/indexes for %s: %s", repo_id, exc)
    for label in sorted(deferred):
        _upsert_deferred_label(engine, repo_id, effective, label, [node for node in docs_nodes if node["label"] == label])
    return True, (docs_spec, selected) if docs_spec is not None and selected is not None else None


def _deferred_docs_labels(engine: GraphEngine, effective: EffectiveSchema, spec: docs.DocsSpec | None) -> set[str]:
    """Docs labels whose generated constraint in the database is keyed differently
    from their declaration (addendum K7): a key switch, or a first apply under a
    constraint another repository created. Their entries are written after
    realign_keys, which may replace the constraint."""
    if spec is None:
        return set()
    docs_labels = {docs_type.label for docs_type in spec.types}
    existing = generated_objects(engine)
    deferred = set()
    for node_type in effective.node_types:
        constraint = existing.get(_user_constraint_name(node_type.label))
        if node_type.label in docs_labels and constraint is not None and (constraint.label, constraint.properties) != (
            node_type.label, ("repo_id", *node_type.key)
        ):
            deferred.add(node_type.label)
    return deferred


def _upsert_deferred_label(
    engine: GraphEngine, repo_id: str, effective: EffectiveSchema, label: str, nodes: list[dict]
) -> None:
    """Write one deferred label's entries in their own transaction. When another
    repository's key keeps the old constraint and the entries break it, none of
    them is written: warn, naming the repositories that disagree."""
    try:
        engine.upsert_nodes(nodes)
    except Exception as exc:
        key = next(tuple(node_type.key) for node_type in effective.node_types if node_type.label == label)
        others = sorted({
            other for other, other_label, other_key in recorded_declarations(engine).get(label.casefold(), [])
            if other != repo_id and (other_label, other_key) != (label, key)
        })
        logger.warning(
            "%s: %s entries were not written: %s %s %s keyed differently, so its constraint keeps "
            "the old key and these entries break it; align the key or rename one label (%s)",
            repo_id, label, ", ".join(others) or "another repository",
            "declares" if len(others) <= 1 else "declare", label, exc,
        )


def _is_provider_file(repo_root: Path, path: Path) -> bool:
    """A file the filesystem provider represents: what a full scan would index."""
    rel = _repo_relative(repo_root, path)
    return rel is not None and _is_indexable_file(path) and not is_ignored_path(Path(rel))


def _prune_docs(
    engine: GraphEngine, repo_id: str, spec: docs.DocsSpec | None, files: dict[str, Path]
) -> tuple[list[dict], docs.Selected | None]:
    """Clear the docs provider's graph for a rebuild from the whole repository (spec §4).

    Every docs edge goes first, of any type, current or former: full_scan's
    final pass rebuilds them once every target exists. Then properties the
    schema no longer declares are cleared and nodes the mapping no longer
    produces are pruned (all of them when no type is docs-sourced). Returns
    the nodes for the caller to upsert, and the front matter read (None
    without docs types).

    `files` is every file on disk, so the owners of field-keyed entries are
    worked out from all of them (`docs.keyed_owners`). The nodes are built
    before anything is written, so a failure leaves the graph untouched.
    """
    nodes: list[dict] = []
    selected = None
    if spec is not None:
        selected = docs.read_selected(spec, files)
        owners = docs.keyed_owners(docs.keyed_claims(spec, selected))
        nodes, problems = docs.build_nodes(spec, repo_id, selected, owners)
        _log_docs_problems(repo_id, problems)
    engine.delete_extracted_edges(repo_id, docs.EXTRACTOR, None)
    if spec is not None:
        for docs_type in spec.types:
            engine.clear_extracted_properties(
                repo_id, docs.EXTRACTOR, docs_type.label, [field.name for field in docs_type.fields]
            )
    engine.prune_extracted_nodes(repo_id, docs.EXTRACTOR, [f"{n['label']}:{n['name']}" for n in nodes])
    return nodes, selected


def _log_docs_problems(repo_id: str, problems: list[docs.Problem]) -> None:
    """One warning per pass, naming the first problem (repr: file names are untrusted)."""
    if problems:
        first = problems[0]
        logger.warning(
            "%s: %d Markdown front-matter problem(s), first %r for %s: %r; `devgraph doctor` lists them",
            repo_id, len(problems), first.path, first.label, first.reason,
        )


def _read_reusing(spec: docs.DocsSpec, files: Mapping[str, Path], seen: Mapping[str, tuple]) -> docs.Selected:
    """`docs.read_selected`, except that front matter already in `seen` is not read again."""
    return docs.Selected(
        (rel, seen[rel] if rel in seen else docs.read_front_matter(files[rel]))
        for rel in sorted(files)
        if any(docs.selects(docs_type, rel) for docs_type in spec.types)
    )


def _keyed_view(repo_root: Path, spec: docs.DocsSpec, seen: Mapping[str, tuple] | None = None) -> docs.KeyedView:
    """Every field-keyed docs file on disk and who owns each key (addendum §4).

    One walk and one read of the files a field-keyed type selects, reusing the
    batch's front matter (`seen`). Owners always come from all of them, never
    from the batch alone or from the graph.
    """
    files = {rel: p for p in _indexable_paths(repo_root) if (rel := _repo_relative(repo_root, p)) is not None}
    keyed = docs.DocsSpec(tuple(docs_type for docs_type in spec.types if docs_type.key is not None), ())
    selected = _read_reusing(keyed, files, seen or {})
    claims = docs.keyed_claims(spec, selected)
    return docs.KeyedView(files, selected, claims, docs.keyed_owners(claims))


class _DocsBatch(NamedTuple):
    """What a batch writes for the docs provider."""

    #: The batch's front matter plus that of the owner of each affected key.
    selected: docs.Selected
    nodes: list[dict]
    owners: Mapping[tuple[str, str], str]
    #: The batch's view of the field-keyed files, or None if it touches none.
    view: docs.KeyedView | None
    #: Affected (label, key)s whose entry is already in the graph, wherever it was.
    existing: set[tuple[str, str]]


def _read_docs_batch(
    engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec, files: dict[str, Path],
    previous: set[tuple[str, str]],
) -> _DocsBatch | None:
    """The batch's docs front matter and nodes, or None if that failed (the
    docs pass is then skipped for this batch).

    For each field-keyed type the batch touches (one of its files is selected,
    or `previous`, the nodes at its paths, holds one), the affected keys are
    those its files claim plus those of its previous nodes, and the owner on
    disk of each joins the batch. Losers never do.
    """
    try:
        selected = docs.read_selected(spec, files)
        view, owners, existing = None, {}, set()
        keyed = [docs_type for docs_type in spec.types if docs_type.key is not None]
        touched = {
            docs_type.label for docs_type in keyed
            if any(docs.selects(docs_type, rel) for rel in files) or any(label == docs_type.label for label, _ in previous)
        }
        if touched:
            view = _keyed_view(repo_root, spec, selected)
            owners = view.owners
            batch = set(files)
            keys = {key for key, paths in view.claims.items() if key[0] in touched and not batch.isdisjoint(paths)}
            keys |= {(label, name) for label, name in previous if label in touched}
            selected = docs.expand_to_owners(view, selected, keys)
            for label in sorted(touched):
                names = sorted(name for claimed, name in keys if claimed == label)
                existing |= {(label, name) for name in engine.existing_node_names(repo_id, label, names)}
        nodes, problems = docs.build_nodes(spec, repo_id, selected, owners)
    except Exception:
        logger.warning("docs node pass failed for %s; skipping it for this batch", repo_id, exc_info=True)
        return None
    _log_docs_problems(repo_id, problems)
    return _DocsBatch(selected, nodes, owners, view, existing)


def _sync_docs(engine: GraphEngine, repo_id: str, spec: docs.DocsSpec, batch: _DocsBatch) -> None:
    """Rewrite the docs nodes and outgoing edges of the batch's selected files.

    Nodes MERGE in place, so edges into them from other docs files survive.
    They are upserted first: an entry whose owner changed moves onto its new
    `path` before the outgoing edges at the batch's paths (its old owner's
    among them) are deleted and rebuilt.
    """
    paths = sorted(batch.selected)
    try:
        engine.upsert_nodes(batch.nodes)
        engine.delete_extracted_edges(repo_id, docs.EXTRACTOR, paths)
        engine.prune_extracted_at(repo_id, docs.EXTRACTOR, paths, [f"{n['label']}:{n['name']}" for n in batch.nodes])
    except Exception:
        logger.warning("docs node pass failed for %s; skipping it for this batch", repo_id, exc_info=True)
        return
    try:
        engine.upsert_relationships(docs.build_edges(spec, repo_id, batch.selected, owners=batch.owners))
    except Exception:
        logger.warning("docs edge pass failed for %s; skipping it for this batch", repo_id, exc_info=True)


def _relink_docs(
    engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec,
    added: set[tuple[str, str]], batch: set[str], view: docs.KeyedView | None = None,
) -> None:
    """Link docs files outside the batch to the nodes the batch added.

    A docs edge to a node that didn't exist yet was skipped when its file was
    indexed. Only added nodes can be such a target: a re-indexed node MERGEs
    in place and keeps its incoming edges. Re-reads the files the docs types
    select (never the graph), and only when a docs relationship targets an
    added node's label. The batch's `view` supplies the walk, the field-keyed
    front matter and the owners (built here when a type is field-keyed and
    the batch had none), so only the other files are read.
    """
    target_labels = {relationship.to_label for relationship in spec.relationships}
    targets = {(label, name) for label, name in added if label in target_labels}
    if not targets:
        return
    try:
        if view is None and any(docs_type.key is not None for docs_type in spec.types):
            view = _keyed_view(repo_root, spec)
        if view is None:
            files = {rel: p for p in _indexable_paths(repo_root) if (rel := _repo_relative(repo_root, p)) is not None}
        else:
            files = dict(view.files)
        outside = {rel: p for rel, p in files.items() if rel not in batch}
        selected = _read_reusing(spec, outside, view.selected if view else {})
        owners = view.owners if view else {}
        engine.upsert_relationships(docs.build_edges(spec, repo_id, selected, targets, owners=owners))
    except Exception:
        logger.warning("docs relink failed for %s; the next rescan links them", repo_id, exc_info=True)


def _take_over_keys(engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec, gone: list[str]) -> None:
    """Before the docs nodes at deleted paths go: move each field-keyed entry
    among them to the file that now owns its key, and rebuild that file's
    outgoing edges. An entry whose key no file claims is left to the delete.
    A failure falls through to the delete; the next rescan restores the entry.
    """
    keyed = {docs_type.label for docs_type in spec.types if docs_type.key is not None}
    if not keyed:
        return
    try:
        keys = {key for key in engine.extracted_nodes_at(repo_id, docs.EXTRACTOR, gone) if key[0] in keyed}
        if not keys:
            return
        view = _keyed_view(repo_root, spec)
        selected = docs.expand_to_owners(view, docs.Selected(), keys)
        if not selected:
            return
        nodes, _problems = docs.build_nodes(spec, repo_id, selected, view.owners)
        engine.upsert_nodes(nodes)
        engine.delete_extracted_edges(repo_id, docs.EXTRACTOR, sorted(selected))
        engine.upsert_relationships(docs.build_edges(spec, repo_id, selected, owners=view.owners))
    except Exception:
        logger.warning("docs takeover failed for %s; the next rescan restores the entries", repo_id, exc_info=True)


def _sync_docs_edges(engine: GraphEngine, repo_id: str, repo_root: Path, applied: AppliedDocs | None) -> None:
    """Write every docs edge in the repository from the spec and front matter
    apply used: full_scan's last step, once every target node exists. Skipped
    when the schema file changed since (it is then pending)."""
    if applied is None or schema_pending(engine, repo_id, repo_root):
        return
    spec, selected = applied
    try:
        owners = docs.keyed_owners(docs.keyed_claims(spec, selected))
        engine.upsert_relationships(docs.build_edges(spec, repo_id, selected, owners=owners))
    except Exception:
        logger.warning("docs edge pass failed for %s; the next rescan retries it", repo_id, exc_info=True)


def index_paths(engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None = None, mentions_enabled: bool = False, sync_provider: bool = True) -> int:
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
        sync_provider: Write schema-provider nodes and edges for `paths`
            and relink docs edges to added nodes (skipped while the schema
            is pending or invalid). False when the caller has just applied
            the schema.

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
    # (label, name) of the Service nodes the batch's compose files wrote.
    batch_services: set[tuple[str, str]] = set()

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
        except OSError:
            continue
        if not is_within(resolved, root_resolved):
            continue
        by_rel_path[resolved.relative_to(root_resolved).as_posix()] = resolved

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
                docs_files, mention_files, batch_services,
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

    # The schema providers' specs, resolved once for the batch. The docs
    # nodes are read now, so the relink below can tell which ones are new;
    # they are written after the filesystem sync.
    providers_ok, fs_spec, docs_spec = (False, None, None)
    if sync_provider and not schema_pending(engine, repo_id, repo_root):
        providers_ok, fs_spec, docs_spec = _provider_specs(repo_root)
    present: set[str] = set()
    if providers_ok and fs_spec is not None:
        present = {
            rel for p in paths
            if _is_provider_file(repo_root, Path(p)) and (rel := _repo_relative(repo_root, Path(p))) is not None
        }
    docs_batch = None
    if providers_ok and docs_spec is not None:
        docs_batch = _read_docs_batch(
            engine, repo_id, repo_root, docs_spec,
            {rel: p for rel, p in by_rel_path.items() if _is_provider_file(repo_root, p)}, previous_nodes,
        )
    if docs_batch:
        # A field-keyed entry the batch writes that was already in the graph
        # (moving between files) is not added.
        previous_nodes |= docs_batch.existing
    # Provider nodes the batch writes that a docs edge can target: docs nodes
    # and filesystem files. Folders are left out: a folder isn't one of the
    # batch's files, so the snapshot above can't tell a new one from an old one.
    provider_nodes = {(node["label"], node["name"]) for node in (docs_batch.nodes if docs_batch else [])}
    if fs_spec is not None and fs_spec.file_label:
        provider_nodes |= {(fs_spec.file_label, rel) for rel in present}

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
    added_nodes = _batch_nodes(
        repo_id, root_resolved, code_extractions, docs_files, mention_files, batch_services, provider_nodes,
    ) - previous_nodes
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

    if providers_ok and fs_spec is not None:
        filesystem.sync_present(engine, repo_id, fs_spec, present)
    if docs_batch:
        _sync_docs(engine, repo_id, docs_spec, docs_batch)
        _relink_docs(engine, repo_id, repo_root, docs_spec, added_nodes, set(docs_batch.selected), docs_batch.view)

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
    batch_services: set[tuple[str, str]],
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
    elif docs_root is not None and resolved.suffix in (".md", ".markdown") and is_within(resolved, docs_root):
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
        result = _index_containerfile(engine, repo_id, resolved, rel_path)
        batch_services |= {("Service", service.name) for service in result.services}
        indexed += 1
    elif name_lower in _COMPOSE_NAMES:
        result = _index_compose_file(engine, repo_id, resolved, rel_path)
        batch_services |= {("Service", service.name) for service in result.services}
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
        if resolved.suffix not in (".py", ".java") or not is_within(resolved, root_resolved):
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
    services: set[tuple[str, str]],
    provider_nodes: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """(label, name) of every file-provenance node this batch wrote: code
    symbols and Modules, docs notes, Markdown Document nodes, compose
    Services and schema-provider nodes -- the same provenance list_file_nodes
    snapshots. File-less nodes (a C++ out-of-class
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
    nodes |= services
    nodes |= provider_nodes
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
        if not is_within(resolved, repo_root):
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

    if not schema_pending(engine, repo_id, repo_root):
        ok, fs_spec, docs_spec = _provider_specs(repo_root)
        gone = {rel for p in paths if (rel := _repo_relative(repo_root, Path(p))) is not None and rel != "."}
        if ok and fs_spec is not None:
            filesystem.sync_absent(
                engine, repo_id, repo_root, fs_spec, gone,
                is_indexable=lambda p: _is_provider_file(repo_root, p),
                is_ignored_dir=is_ignored_dir_name,
            )
        if ok and docs_spec is not None:
            _take_over_keys(engine, repo_id, repo_root, docs_spec, sorted(gone))
            # Nodes (still) at or below the deleted paths, with all their edges.
            engine.delete_extracted_nodes(repo_id, docs.EXTRACTOR, sorted(gone))

    return cleaned


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
    A full scan also applies the project schema (see apply_project_schema),
    which reconciles filesystem- and docs-provider nodes (see providers/),
    and then writes every docs edge once every target node exists. A schema
    that cannot be applied leaves the repository pending, with no docs edge
    written; callers that care check schema_pending afterwards.
    """
    prune_stale_files(engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled)
    all_files = _indexable_paths(repo_root)
    applied, applied_docs = _apply_project_schema(engine, repo_id, repo_root)
    indexed = index_paths(
        engine, repo_id, repo_root, all_files, docs_path=docs_path, mentions_enabled=mentions_enabled,
        sync_provider=False,  # applied just above
    )
    if applied:
        _sync_docs_edges(engine, repo_id, repo_root, applied_docs)
    return indexed


def _index_containerfile(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> ExtractionResult:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_containerfile(content, rel_path)
    _upsert_container_result(engine, repo_id, result)
    return result


def _index_compose_file(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> ExtractionResult:
    content = path.read_text(encoding="utf-8", errors="replace")
    result = ContainerExtractor(repo_id).extract_from_compose_file(content, rel_path)
    _upsert_container_result(engine, repo_id, result)
    return result


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
