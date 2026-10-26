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
import os
import pickle
import posixpath
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
import zlib
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import NamedTuple

from devgraph.config import get_settings
from devgraph.config.settings import devgraph_home
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
from devgraph.graph.engine import (
    ABORTS_BATCH,
    OWNING_SERVICE_TARGETS,
    SERVICE_API_SUFFIXES,
    EngineClosed,
    GraphEngine,
    provision_repository_schema,
)
from devgraph.graph.schema import FILE_SCOPED_LABELS, HEIR_KEY, NODE_LABELS, RELATIONSHIP_TYPES, pin_kind, split_pin
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.apis.routes import MountTable, mount_routes
from devgraph.indexer.common import (
    best_pin,
    name_ref_properties,
    owner_matches,
    owners_by_pin,
    parse_name_ref,
    pin_confidence,
    pin_excludes,
    tiered,
    visible_to,
)
from devgraph.indexer.containers.extractor import ContainerExtractor, ExtractionResult
from devgraph.indexer.datastores.extractor import DatastoreExtractor, client_datastores
from devgraph.indexer.docs.extractor import DocsExtractor
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.mentions.extractor import index_file as index_mentions_file
from devgraph.indexer.mentions.extractor import mentions_any, upsert_document_node
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.providers import docs, docs_cache, filesystem
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.resolver_config import LANGUAGE_SUFFIXES, ResolverConfig, fingerprints, touches_config
from devgraph.indexer.rust.extractor import extract_rust_file
from devgraph.indexer.schema_constraints import (
    encode_keys,
    generated_objects,
    realign_keys,
    recorded_declarations,
    release_labels,
)
from devgraph.indexer.gitignore import is_gitignored
from devgraph.indexer.source_text import read_source
# Re-exported under their pre-walk.py names for the watcher and existing callers.
from devgraph.indexer.walk import IGNORED_DIR_NAMES as IGNORED_DIR_NAMES
from devgraph.indexer.walk import GITIGNORED, RepoRootEmpty, RepoRootUnavailable, check_repo_root, content_skip_reason
from devgraph.indexer.walk import indexable_paths as _indexable_paths
from devgraph.indexer.walk import forget_manifests, gitignore_hides_files, indexable_paths_under
from devgraph.indexer.walk import is_ignored_dir_name as is_ignored_dir_name
from devgraph.indexer.walk import is_ignored_path as is_ignored_path
from devgraph.indexer.walk import is_indexable_file as _is_indexable_file
from devgraph.indexer.walk import keyed_indexable_paths as _keyed_indexable_paths
from devgraph.indexer.walk import links_outside as _links_outside  # noqa: F401
from devgraph.indexer.walk import repo_relative as _repo_relative
from devgraph.paths import is_within

logger = logging.getLogger(__name__)


class BatchAborted(Exception):
    """The indexer gives a whole batch or scan up, as it does a shutdown's
    (`ABORTS_BATCH`): nothing is stamped, and the work is redone."""


class ExtractionSpillFailed(BatchAborted):
    """A batch's extractions could not be written to, or read back from, its
    spill file (`_Extractions`): a full disk, say. Not one file's failure, so
    not skipped as one: the batch's second pass would miss that file's edges."""


class FullScanCancelled(BatchAborted):
    """A full scan waiting its turn (`_FULL_SCAN_LOCK`) was called off: the
    watcher is stopping, or the repository is no longer watched."""


ABORTS_BATCH = (*ABORTS_BATCH, BatchAborted)

_CPP_SUFFIXES = {".cpp", ".cc", ".cxx", ".h", ".hpp"}

_COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "podman-compose.yml", "podman-compose.yaml", "compose.yml", "compose.yaml"}
_CONTAINERFILE_NAMES = {"containerfile", "dockerfile"}

_JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}


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


#: The graph index format a full scan produces (2: edge `origins`, `name_refs`;
#: 3: FastAPI/Flask IMPLEMENTS pinned to the route's file; 4: Python IMPORTS
#: to every candidate file and CALLS resolved through scope and imports; 5: JS/TS
#: `.mjs`/`.cjs`/`.mts`/`.cts` files, explicit-extension imports such as `./x.js`
#: resolved to their TypeScript source, compose `depends_on`/`links` as Service
#: DEPENDS_ON edges; 6: path pins, Module `dir`/`basename`, the resolver
#: configuration fingerprint, JS/TS CALLS resolved through scope, imports,
#: tsconfig aliases and types, Kotlin declarations recovered after one-line
#: class bodies; 7: Go, Java and Kotlin CALLS and IMPORTS resolved through
#: modules, source roots and types, path pin flags, Module `dir_name`; 8:
#: Rust, C# and C++ CALLS and IMPORTS resolved through crate roots and
#: Cargo crate names, referenced types and quoted includes; 9: a C++
#: out-of-class method's Class stub claimed by its file, and the unclaimed
#: stubs an older index left deleted; 10: C# Class and Function match keys
#: (`mkeys`) and C# CALLS and IMPORTS narrowed by namespace through them;
#: 11: Class and Function keyed by their `owner` within a file, so two
#: classes' same-named methods are two nodes, `name_refs` recording the
#: source's owner; C++ templates, Java records, TS class-field arrow
#: functions and Go methods on a pointer to a generic type extracted; 12:
#: `name_refs` entries of one marked layout recording both ends' owners, a
#: nested function's owner including its functions, and calls to another
#: file's top-level function narrowed to it; 13: routes beyond Python as
#: `Endpoint` nodes, with their handlers' IMPLEMENTS and owning-service CALLS
#: (apis/routes.py), and a code Module's `shared_claims`); 14: C# match keys
#: in shadow tiers (an enclosing namespace's type hides a using's), a
#: tiered group's every pin recorded in `name_refs`; 15: C# Class and
#: Function `proj` and C# path-pinned rows limited to the projects their
#: file's project references (`visible`, a Module's `name_ref_visible`);
#: 16: Python absolute imports also tried under a `src/` directory beside
#: an ancestor and the source roots a pyproject.toml declares; Rust calls and imports
#: through `pub use` re-exports (resolver configuration); a Kotlin
#: anonymous companion object's members (owner `Class.Companion`); JSX
#: component elements as calls; 17: JS/TS imports of a workspace package
#: (package.json `workspaces`, pnpm-workspace.yaml) resolved to its entry
#: point or subpath and its directory (resolver configuration); 18:
#: datastore clients of JS/TS, Java, Kotlin, C# and Go files as the shared
#: datastore nodes their files claim, with their owning service's USES
#: (datastores/extractor.py `client_datastores`), and Express routers
#: mounted from another file named by those mounts (apis/routes.py
#: MountTable); build
#: output folder names (`build`, `dist`, `bin`, `obj`, `target`) ignored only
#: at the root or beside a project manifest (walk.is_build_output), so Rust's
#: `src/bin/` and source packages named `build` are indexed; a
#: method called on another file's Python, JS/TS or Java class narrowed to
#: that class's member, its own or one it inherits from a class of its
#: file (heir keys in `mkeys`, a Module's `heir_open`); JSX rendered from
#: callbacks, class components and wrappers; Rust module re-exports and
#: inline-module `pub use`; C# ImplicitUsings; and Go dot imports; 19:
#: Python definitions under compound statements (`if`, `try`, `with`, ...)
#: and classes defined in functions, same-named definitions of one scope
#: merged into one node, and Java enum members; Python routes read from the
#: syntax tree (one framework per decorator, Flask `methods` lists, no
#: route from a comment or docstring); JS/TS object-literal methods,
#: CommonJS exports and functions declared in blocks. The datastore
#: extractor also skips more test folders (`spec/`, `e2e/`, `__mocks__/`,
#: `testdata/`, `fixtures/`, .NET `UnitTests`/`IntegrationTests`;
#: devgraph/test_paths.py `TEST_DIR`); 20: Python routes under their
#: router's or blueprint's prefix (FastAPI `APIRouter(prefix=)` and
#: `include_router(prefix=)`, Flask `Blueprint(url_prefix=)` and
#: `register_blueprint(url_prefix=)`), a router another file mounts named by
#: those mounts (apis/routes.py MountTable) and one mounted nowhere the file
#: shows an Endpoint of its own file (`unmounted_endpoint`); and a compose
#: service's build context resolved from the compose file's folder, the
#: repository root (`build: .` at the root, `context: ..` one level down)
#: owning every file no deeper context claims; and JS/TS calls inside an
#: anonymous callback (`useEffect(() => ...)`, an `onClick` arrow, `.then`)
#: attributed to the named function holding it (jsts `_call_functions`).
#: An index stamped lower, or not at all, is rescanned automatically.
INDEX_FORMAT = 20


def index_outdated(engine: GraphEngine, repo_id: str) -> bool:
    """True when the repository's last full scan predates `INDEX_FORMAT`."""
    return (engine.index_format(repo_id) or 1) < INDEX_FORMAT


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


#: What `_apply_project_schema` applied for the docs provider: the spec, the
#: front matter it read and the owner of each field-keyed entry.
AppliedDocs = tuple[docs.DocsSpec, docs.Selected, Mapping[tuple[str, str], str]]


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

    Returns (applied, the docs spec, front matter and owners applied, or
    None), so full_scan's edge pass reuses them instead of resolving the
    schema, reading every docs file and working out the owners again.
    """
    current_hash = schema_file_hash(repo_root)
    try:
        effective = resolve_effective_schema(repo_root)
    except ProjectSchemaError as exc:
        logger.warning("project schema for %s is invalid; not applied: %s", repo_root, exc)
        return False, None
    try:
        provision_repository_schema(engine, repo_root)
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
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
    # Recorded before anything is written: a crash part-way then never leaves
    # a label's nodes without a record naming it, so the next apply deletes
    # them if the schema has dropped the label. The final record below
    # replaces it with `labels`.
    engine.record_schema_labels(repo_id, list(dict.fromkeys([*(previous.get("labels") or []), *labels])))
    for label in removed_labels:
        engine.delete_label_nodes(repo_id, label)
    for rel_type in previous.get("relationship_types") or []:
        if rel_type not in rel_types and rel_type not in RELATIONSHIP_TYPES and RELATIONSHIP_TYPE_PATTERN.fullmatch(rel_type or ""):
            engine.delete_relationship_type(repo_id, rel_type)

    spec = filesystem.filesystem_spec(effective)
    disk = _disk_files(repo_root)
    on_disk = set(disk)
    docs_spec = docs.docs_spec(effective)
    docs_nodes, selected, owners = _prune_docs(engine, repo_id, repo_root, docs_spec, disk)
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
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception as exc:
        logger.warning("could not reconcile generated constraints/indexes for %s: %s", repo_id, exc)
    for label in sorted(deferred):
        _upsert_deferred_label(engine, repo_id, effective, label, [node for node in docs_nodes if node["label"] == label])
    return True, (docs_spec, selected, owners) if docs_spec is not None and selected is not None else None


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
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception as exc:
        key = next(tuple(node_type.key) for node_type in effective.node_types if node_type.label == label)
        # A repository with the same key spells the label differently (case only).
        reasons = [
            f"{other} spells the type '{other_label}'" if other_key == key
            else f"{other} declares {label} keyed differently"
            for other, other_label, other_key in sorted(recorded_declarations(engine).get(label.casefold(), []), key=lambda entry: entry[0])
            if other != repo_id and (other_label, other_key) != (label, key)
        ]
        logger.warning(
            "%s: %s entries were not written: %s, so its constraint keeps "
            "the old key and these entries break it; align the key or rename one label (%s)",
            repo_id, label, " and ".join(reasons) or f"another repository declares {label} keyed differently", exc,
        )


def _disk_files(repo_root: Path) -> dict[str, Path]:
    """Every file a full scan indexes, by repo-relative path. A symlink is keyed
    by its target, so one into an ignored directory is left out, as
    `_is_provider_file` leaves it out of a batch."""
    return {rel: p for p, rel in _keyed_indexable_paths(repo_root)}


def _is_provider_file(repo_root: Path, path: Path) -> bool:
    """A file the filesystem provider represents: what a full scan would index.
    A .gitignore judges the file by its own path, as the walk and git do."""
    rel = _repo_relative(repo_root, path)
    return (
        rel is not None and _is_indexable_file(path) and not is_ignored_path(Path(rel), repo_root)
        and not is_gitignored(repo_root, _own_rel(repo_root, path) or rel)
    )


def _own_rel(repo_root: Path, path: Path) -> str | None:
    """`path`'s repo-relative POSIX path without resolving links (a symlink
    keeps its own name), or None when it lies lexically outside the root."""
    try:
        return Path(os.path.abspath(path)).relative_to(os.path.abspath(repo_root)).as_posix()
    except ValueError:
        return None


def _max_file_bytes() -> int:
    """The `max_file_bytes` setting: larger files are not extracted."""
    return get_settings().max_file_bytes


def _skip_reason(
    root_resolved: Path, spellings: list[str], resolved: Path, docs_root: Path | None, mentions_enabled: bool,
    max_bytes: int,
) -> str | None:
    """Why `index_paths` leaves this file out, or None: a .gitignore ignores
    every one of the caller's `spellings` of it (a link is judged by its own
    path, not its target's; none, for a file the batch pulled in), or an extractor would read it but it is too large, binary, or minified
    or generated (`walk.content_skip_reason`). A file no extractor reads (one
    only a schema provider represents, such as an image) is never judged by
    its content. Raises OSError when the file can't be read."""
    if spellings and all(is_gitignored(root_resolved, spelling) for spelling in spellings):
        return GITIGNORED
    if not _routes(resolved, docs_root, mentions_enabled):
        return None
    return content_skip_reason(resolved, max_bytes)


def _prune_docs(
    engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec | None, files: dict[str, Path]
) -> tuple[list[dict], docs.Selected | None, Mapping[tuple[str, str], str]]:
    """Clear the docs provider's graph for a rebuild from the whole repository (spec §4).

    Every docs edge goes first, of any type, current or former: full_scan's
    final pass rebuilds them once every target exists. Then properties the
    schema no longer declares are cleared and nodes the mapping no longer
    produces are pruned (all of them when no type is docs-sourced). Returns
    the nodes for the caller to upsert, the front matter read (None without
    docs types) and the owners.

    `files` is every file on disk, so the owners of field-keyed entries are
    worked out from all of them (`docs.keyed_owners`). The nodes are built
    before anything is written, so a failure leaves the graph untouched.
    They are read through the read cache, which full_scan has just emptied
    for this repository, so the reads refill it.
    """
    nodes: list[dict] = []
    selected, owners = None, {}
    if spec is not None:
        selected = docs.read_selected(spec, files, read=partial(docs_cache.read, repo_root))
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
    return nodes, selected, owners


def _log_cache_stats() -> None:
    """The docs read cache's process totals, after a batch's docs pass."""
    if logger.isEnabledFor(logging.DEBUG):
        stats = docs_cache.stats()
        logger.debug(
            "docs read cache: %d hits, %d misses, %d fresh, %d entries, %d bytes",
            stats["hits"], stats["misses"], stats["fresh"], stats["entries"], stats["bytes"],
        )


def _log_docs_problems(repo_id: str, problems: list[docs.Problem]) -> None:
    """One warning per pass, naming the first problem (repr: file names are untrusted)."""
    if problems:
        first = problems[0]
        logger.warning(
            "%s: %d Markdown front-matter problem(s), first %r for %s: %r; `devgraph doctor` lists them",
            repo_id, len(problems), first.path, first.label, first.reason,
        )


def _read_reusing(
    spec: docs.DocsSpec, files: Mapping[str, Path], seen: Mapping[str, tuple], read: Callable[[str, Path], tuple]
) -> docs.Selected:
    """`docs.read_selected` through `read`, except that front matter already in `seen` is not read again."""
    return docs.Selected(
        (rel, seen[rel] if rel in seen else read(rel, files[rel]))
        for rel in sorted(files)
        if any(docs.selects(docs_type, rel) for docs_type in spec.types)
    )


def _keyed_view(repo_root: Path, spec: docs.DocsSpec, seen: Mapping[str, tuple] | None = None) -> docs.KeyedView:
    """Every field-keyed docs file on disk and who owns each key (addendum §4).

    One walk and one read of the files a field-keyed type selects, reusing the
    batch's front matter (`seen`). Owners always come from all of them, never
    from the batch alone or from the graph. The reads go through the read
    cache, so only files changed since it last read them are parsed again.
    """
    files = _disk_files(repo_root)
    keyed = docs.DocsSpec(tuple(docs_type for docs_type in spec.types if docs_type.key is not None), ())
    selected = _read_reusing(keyed, files, seen or {}, partial(docs_cache.read, repo_root))
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
    #: Affected (label, key)s whose entry is already in the graph, wherever it was,
    #: and the entries already at the owners pulled in.
    existing: set[tuple[str, str]]


def _read_docs_batch(
    engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec, files: dict[str, Path],
    previous: set[tuple[str, str]],
) -> _DocsBatch | None:
    """The batch's docs front matter and nodes, or None if that failed (the
    docs pass is then skipped for this batch).

    For each field-keyed type the batch touches (one of its files is selected,
    or `previous`, the nodes at its paths, holds one), the affected keys K are
    those its files claim plus those of its previous nodes, and the owner on
    disk of each joins the batch. Losers never do.

    An owner pulled in may still hold, in the graph, an entry its file no
    longer claims (its own event is pending). The batch prunes at its path,
    so that entry's key joins K too and its owner on disk is pulled in, until
    K stops growing. K only grows and every path added owns a key in it, so
    this ends, without reading any file or querying the graph again (the
    field-keyed entries are fetched once), and every engine path list holds
    at most |batch| + |K| paths.

    The watcher has just reported the batch's files as changed, so they are
    read fresh (`docs_cache.read_fresh`), never served from the read cache.
    """
    try:
        selected = docs.read_selected(spec, files, read=partial(docs_cache.read_fresh, repo_root))
        view, owners, existing = None, {}, set()
        keyed = {docs_type.label for docs_type in spec.types if docs_type.key is not None}
        touched = {
            docs_type.label for docs_type in spec.types if docs_type.label in keyed
            and (any(docs.selects(docs_type, rel) for rel in files) or any(label == docs_type.label for label, _ in previous))
        }
        if touched:
            view = _keyed_view(repo_root, spec, selected)
            owners = view.owners
            batch = set(files)
            keys = {key for key, paths in view.claims.items() if key[0] in touched and not batch.isdisjoint(paths)}
            keys |= {(label, name) for label, name in previous if label in touched}
            # Every field-keyed entry in the graph, fetched once: the chase below
            # runs in memory however long the chain of stale entries is.
            in_graph: dict[str, set[tuple[str, str]]] = {}
            for label, name, path in engine.extracted_entries(repo_id, docs.EXTRACTOR, sorted(keyed)):
                in_graph.setdefault(path, set()).add((label, name))
            batch_selected, checked = selected, set(batch)
            while True:
                selected = docs.expand_to_owners(view, batch_selected, keys)
                pulled_in = set(selected) - checked
                checked |= pulled_in
                stale = {key for path in pulled_in for key in in_graph.get(path, ())} - keys
                if not stale:
                    break
                keys |= stale
            existing = keys & {key for entries in in_graph.values() for key in entries}
            # An owner pulled in also rewrites its other entries (a path-keyed
            # type's, say); those already there are not added either.
            pulled_in = sorted(set(selected) - batch)
            if pulled_in:
                existing |= {node[:2] for node in engine.list_file_nodes(repo_id, pulled_in)}
        nodes, problems = docs.build_nodes(spec, repo_id, selected, owners)
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
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
    among them) are deleted and rebuilt. If that delete or the prune fails
    after the upsert, a moved entry keeps its old owner's links and stale
    entries stay until the owner's next save or a rescan rewrites them.
    """
    paths = sorted(batch.selected)
    try:
        engine.upsert_nodes(batch.nodes)
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning("docs node pass failed for %s; skipping it for this batch", repo_id, exc_info=True)
        return
    try:
        engine.delete_extracted_edges(repo_id, docs.EXTRACTOR, paths)
        engine.prune_extracted_at(repo_id, docs.EXTRACTOR, paths, [f"{n['label']}:{n['name']}" for n in batch.nodes])
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning(
            "docs pass for %s wrote its nodes but could not clear their old links and entries; "
            "a moved entry may keep its previous file's links until that file's next save or a rescan",
            repo_id, exc_info=True,
        )
        return
    try:
        engine.upsert_relationships(docs.build_edges(spec, repo_id, batch.selected, owners=batch.owners))
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
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
    select, and only when a docs relationship targets an added node's label.
    The batch's `view` supplies the walk and the field-keyed front matter
    (built here when a type is field-keyed and the batch had none), so only
    the other files are read, through the read cache. A field-keyed entry is linked from the file it
    sits at in the graph (one query), whichever file owns its key on disk.
    """
    target_labels = {relationship.to_label for relationship in spec.relationships}
    targets = {(label, name) for label, name in added if label in target_labels}
    if not targets:
        return
    try:
        if view is None and any(docs_type.key is not None for docs_type in spec.types):
            view = _keyed_view(repo_root, spec)
        if view is None:
            files = _disk_files(repo_root)
        else:
            files = dict(view.files)
        outside = {rel: p for rel, p in files.items() if rel not in batch}
        selected = _read_reusing(spec, outside, view.selected if view else {}, partial(docs_cache.read, repo_root))
        owners: Mapping[tuple[str, str], str] = {}
        if view is not None:
            # Each field-keyed entry is linked from the file it sits at in the
            # graph, not from its owner on disk: a claimant whose event is
            # pending may outrank that file, and give the id up again before its
            # event (which would rebuild the entry's links) ever arrives.
            keyed = sorted(docs_type.label for docs_type in spec.types if docs_type.key is not None)
            owners = {
                (label, name): path
                for label, name, path in engine.extracted_entries(repo_id, docs.EXTRACTOR, keyed)
            }
        engine.upsert_relationships(docs.build_edges(spec, repo_id, selected, targets, owners=owners))
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning("docs relink failed for %s; the next rescan links them", repo_id, exc_info=True)


def _take_over_keys(engine: GraphEngine, repo_id: str, repo_root: Path, spec: docs.DocsSpec, gone: list[str]) -> None:
    """Before the docs nodes at deleted paths go: move each field-keyed entry
    among them to the file that now owns its key, and rebuild that file's
    outgoing edges. Nothing else the owner claims is written: that is its
    own event's work. An entry whose key no file claims is left to the delete.

    A failure before the move falls through to the delete, and the next
    rescan restores the entry. A failure after it leaves the entry at its new
    owner (the delete matches paths, so it no longer reaches it) with the
    deleted file's links, until the owner's next save or a rescan.
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
        # Only the entries taken over move. An owner whose own event is still
        # pending may claim more on disk; writing those here would make its
        # event see them as already there, so nothing would relink them.
        nodes, _problems = docs.build_nodes(spec, repo_id, selected, view.owners)
        nodes = [node for node in nodes if (node["label"], node["name"]) in keys]
        # The edge delete below clears every outgoing edge at the owner paths,
        # so those of the entries already there are rebuilt as well.
        sources = keys | engine.extracted_nodes_at(repo_id, docs.EXTRACTOR, sorted(selected))
        edges = [
            edge for edge in docs.build_edges(spec, repo_id, selected, owners=view.owners)
            if (edge["from_label"], edge["from_name"]) in sources
        ]
        engine.upsert_nodes(nodes)
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning("docs takeover failed for %s; the next rescan restores the entries", repo_id, exc_info=True)
        return
    try:
        engine.delete_extracted_edges(repo_id, docs.EXTRACTOR, sorted(selected))
        engine.upsert_relationships(edges)
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning(
            "docs takeover for %s moved entries to the files that now own their ids but could not rebuild "
            "their links; they keep the deleted file's links until the owner's next save or a rescan",
            repo_id, exc_info=True,
        )


def _sync_docs_edges(engine: GraphEngine, repo_id: str, repo_root: Path, applied: AppliedDocs | None) -> None:
    """Write every docs edge in the repository from the spec and front matter
    apply used: full_scan's last step, once every target node exists. Skipped
    when the schema file changed since (it is then pending)."""
    if applied is None or schema_pending(engine, repo_id, repo_root):
        return
    spec, selected, owners = applied
    try:
        engine.upsert_relationships(docs.build_edges(spec, repo_id, selected, owners=owners))
    except ABORTS_BATCH:
        raise  # shutdown or an index still building: leave the work, and its stamp, for later
    except Exception:
        logger.warning("docs edge pass failed for %s; the next rescan retries it", repo_id, exc_info=True)


class _Mounts:
    """The repository's Express, FastAPI and Flask mounts across files (apis/routes.py
    MountTable) for one batch: read from the graph when a file first needs
    them, then kept current with each JS/TS or Python file the batch writes."""

    def __init__(self, engine: GraphEngine, repo_id: str):
        self.engine, self.repo_id = engine, repo_id
        self.table: MountTable | None = None
        self.pending: dict[str, list[str] | None] = {}  # the batch's files' mounts before the read
        self.touched = False  # whether a batch file has mounts or routes another file mounts

    def written(self, rel_path: str, mounts: list[str] | None, exports: list[str] | None) -> None:
        """Record a JS/TS or Python file's `route_mounts` as the batch writes it."""
        self.touched |= bool(mounts or exports)
        if self.table is None:
            self.pending[rel_path] = mounts
        else:
            self.table.set(rel_path, mounts)

    def prefixes(self, rel_path: str, exports: list[str] | None) -> list[str] | None:
        """The `route_prefixes` of a file with these `route_exports`."""
        if not exports:
            return None
        if self.table is None:
            self.reload()
        return self.table.route_prefixes(rel_path, exports)

    def reload(self) -> dict[str, tuple[list[str] | None, list[str] | None, list[str] | None]]:
        """Read every Module's mounts from the graph again (the batch's
        pending ones over them) and return the state read."""
        state = self.engine.route_mount_state(self.repo_id)
        mounts = {module: entries for module, (entries, _exports, _prefixes) in state.items() if entries}
        self.table = MountTable({**mounts, **self.pending})
        self.pending = {}
        return state


def _mounted(nodes: list[dict], rels: list[dict], rel_path: str, mounts: _Mounts) -> tuple[list[dict], list[dict]]:
    """A JS/TS or Python file's extraction with its exported routers' routes named by
    the mounts that reach them (apis/routes.py `mount_routes`), its Module
    recording the prefixes used (`route_prefixes`)."""
    module = next(node for node in nodes if node["label"] == "Module" and node["name"] == rel_path)
    props = module["properties"]
    mounts.written(rel_path, props.get("route_mounts"), props.get("route_exports"))
    prefixes = mounts.prefixes(rel_path, props.get("route_exports"))
    nodes, rels = mount_routes(nodes, rels, rel_path, props.get("route_exports"), prefixes)
    props["route_prefixes"] = prefixes
    return nodes, rels


def _stale_mounts(state: dict, table: MountTable) -> set[str]:
    """The Modules whose `route_prefixes` differ from what `table` gives their `route_exports`."""
    return {
        module for module, (_mounts, exports, prefixes) in state.items()
        if (prefixes or None) != table.route_prefixes(module, exports)
    }


def index_paths(
    engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None = None,
    mentions_enabled: bool = False, sync_provider: bool = True, relink_outside: bool = True,
    skipped: dict[str, str] | None = None, resolver: dict[str, dict] | None = None,
) -> int:
    """Index a set of changed files, routing each to its extractor by name/extension.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID (already registry-scoped by the caller).
        repo_root: The repo's root path, used to resolve docs_path and to
            compute relative paths for provenance.
        paths: Files to (re)index. Paths outside repo_root are silently
            skipped. Besides these, the batch also re-indexes files whose
            by-name edges target a node the batch adds (see
            _find_referrers), relinks Markdown mentioning an added name (see
            _mention_referrers), and relinks the by-name edges other code
            files recorded to an added node (see _relink_name_refs). No
            importer is re-indexed: every code edge is a function of its
            writer's text, its resolver configuration and whether its target
            exists (a tiered pin's, which nodes of its target's name exist:
            other files' tiered groups to a name whose nodes the batch adds,
            removes or rekeys are written again). An Express, FastAPI or
            Flask router file whose mounts in other files changed under it (apis/routes.py)
            is indexed again with the batch (`_stale_mounts`).
        docs_path: The repo's configured docs folder (repo-relative), if any.
        mentions_enabled: Whether to index mentions in Markdown files.
        sync_provider: Write schema-provider nodes and edges for `paths`
            and relink docs edges to added nodes (skipped while the schema
            is pending or invalid). False when the caller has just applied
            the schema.
        relink_outside: Relink other code files' by-name edges to the
            nodes the batch adds. False for a full scan, which has no file
            outside the batch.
        skipped: When given, collects each file left out, by repo-relative
            path, with why (`_skip_reason`). Its graph nodes are deleted, so
            a file that grows past the size limit or becomes ignored leaves
            the graph, as a full scan would leave it out.
        resolver: The resolver configuration's fingerprints to extract
            under (resolver_config.fingerprints: the go.mod, go.work,
            Cargo.toml, re-exporting .rs, C# and pyproject.toml files it
            found); None reads the
            ones the graph was built with.

    Returns:
        Number of files actually indexed, including those referrers
        (skipped/unrecognized files and Markdown relinks don't count).
    """
    with _Extractions() as extractions:
        return _index_paths(
            engine, repo_id, repo_root, paths, docs_path, mentions_enabled, sync_provider, relink_outside,
            skipped, resolver, extractions,
        )


def _index_paths(
    engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None,
    mentions_enabled: bool, sync_provider: bool, relink_outside: bool, skipped: dict[str, str] | None,
    resolver: dict[str, dict] | None, extractions: _Extractions,
) -> int:
    """index_paths, holding the code files' pass-1 extractions in `extractions`
    for the passes after it (see _Extractions)."""
    indexed = 0
    docs_root = (repo_root / docs_path).resolve() if docs_path else None

    # The resolver configuration (tsconfig path aliases, go.mod module
    # paths, Cargo crate names, C# projects, pyproject source roots), read once for the batch under the fingerprint it was taken for
    # (the graph's, unless the caller has a newer one); see
    # sync_resolver_config for when a change to it re-indexes.
    def config_files(lang: str, names: tuple[str, ...] | None = None) -> list[str] | None:
        stored = resolver if resolver is not None else engine.read_resolver_config(repo_id)
        entry = stored.get(lang)
        if entry is None:
            return None
        return [rel for rel in entry.get("inputs", []) if names is None or posixpath.basename(rel) in names]

    config = ResolverConfig(
        repo_root, go_files=lambda: config_files("go", ("go.mod", "go.work")),
        rust_files=lambda: config_files("rust"), cs_files=lambda: config_files("csharp"),
        python_files=lambda: config_files("python"), ts_files=lambda: config_files("ts"),
    )
    # Markdown files whose cross-file edges are resolved after every other
    # node in the batch exists (see the docs/mentions passes below).
    docs_files: list[Path] = []
    mention_files: list[Path] = []
    # (label, name, compose file) of the Service nodes the batch's compose
    # files and Containerfiles wrote, and (label, name, "") of the Containers
    # they claim.
    batch_services: set[tuple[str, str, str]] = set()

    # Process files in a fixed order, not set order, so a batch never
    # depends on PYTHONHASHSEED. Shared nodes (Datastore/Endpoint) no longer
    # depend on it: they take `source`/`library` from the claim of the
    # alphabetically first file in `sources`, kept sorted, whatever order the
    # files claim them in (see engine._claim_nodes_tx). Cross-file edges don't
    # either: they are re-resolved in the passes after this loop.
    # Each path is resolved once and the batch is sorted by its
    # repo-relative POSIX path, so the order is the same however the caller
    # spelled a path (relative, absolute, through a symlink).
    root_resolved = repo_root.resolve()
    by_rel_path: dict[str, Path] = {}
    # rel -> the caller's own spellings of it, unresolved (`_own_rel`). Only
    # these are judged by .gitignore: referrers come from graph keys the walk
    # already let through (a .gitignore change is followed by a catch-up,
    # whose prune handles them).
    supplied: dict[str, list[str]] = {}
    for path in paths:
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if not is_within(resolved, root_resolved):
            continue
        rel = resolved.relative_to(root_resolved).as_posix()
        by_rel_path[rel] = resolved
        supplied.setdefault(rel, []).append(_own_rel(repo_root, Path(path)) or rel)

    max_bytes = _max_file_bytes()
    mounts = _Mounts(engine, repo_id)
    # Files left out by their content, for catch_up (see `_skip_marks`).
    judged: dict[str, list] = {}
    read: set[str] = set()  # files extracted, whose old marks go

    def index_one(rel_path: str, resolved: Path) -> None:
        nonlocal indexed
        if not resolved.exists() or not resolved.is_file():
            return
        # One unparseable/locked file (or a transient Neo4j error mid-batch)
        # must not abort the whole batch: a full scan or watcher batch would
        # otherwise silently lose every file after the failure point. Log and
        # skip the offending file so the rest of the batch still indexes.
        saved = _UNREAD
        try:
            spellings = sorted(supplied.get(rel_path, []))
            reason = _skip_reason(root_resolved, spellings, resolved, docs_root, mentions_enabled, max_bytes)
            if reason is not None:
                extractions.discard(rel_path)  # a router file the mounts pass found changed since
                shown = spellings[0] if spellings else rel_path
                logger.debug("%s: skipping %s (%s)", repo_id, shown, reason)
                if skipped is not None:
                    skipped[shown] = reason
                if reason != GITIGNORED:
                    judged[rel_path] = [reason, max_bytes, _change_stamp_ns(os.stat(resolved))]
                # Only the file's own path being ignored clears its nodes: an
                # ignored link leaves its target's alone.
                if reason != GITIGNORED or rel_path in spellings:
                    engine.delete_nodes_by_source_file(repo_id, rel_path)
                return
            # A router file the mounts pass indexes again counts once.
            single = partial(
                _index_single_path,
                engine, repo_id, repo_root, resolved, rel_path,
                docs_root, mentions_enabled, config, extractions,
                docs_files, mention_files, batch_services, mounts,
            )
            saved = extractions.saved(rel_path)
            try:
                count = single()
            except RecursionError:
                # Extraction raises before it writes, so this is a clean retry.
                count = _run_deep(single)
            indexed += 0 if rel_path in read else count
            read.add(rel_path)
        except ABORTS_BATCH:
            raise  # shutdown or an index still building: leave the work, and its stamp, for later
        except RecursionError as exc:
            # Deeper than even `_run_deep` allows: left out of the graph and
            # reported, as a fresh scan leaves it, not kept as it was.
            _log_file_failure("indexing failed", repo_id, rel_path, exc)
            shown = min(supplied.get(rel_path, []), default=rel_path)
            if skipped is not None:
                skipped[shown] = TOO_DEEP
            stamp = _stamp_or_none(resolved)  # gone or locked since: no mark, the next catch-up looks again
            if stamp is not None:
                judged[rel_path] = [TOO_DEEP, max_bytes, stamp]
            extractions.discard(rel_path)
            engine.delete_nodes_by_source_file(repo_id, rel_path)
        except Exception as exc:
            _log_file_failure("indexing failed", repo_id, rel_path, exc)
            if saved is not _UNREAD:
                extractions.restore(rel_path, saved)  # its graph write failed: pass 2 writes what the graph holds

    # What the batch's files produced before this run, so the referrer step
    # below can tell which nodes the batch adds.
    batch_keys = sorted(by_rel_path)
    previous_nodes: set = set()
    previous_keys: dict = {}
    previous_projects: dict = {}
    stale: set = set()
    claims_before: dict = {}

    def snapshot(keys: list[str]) -> None:
        previous = engine.list_file_nodes(repo_id, keys)
        nodes = {node[:4] for node in previous}
        previous_nodes.update(nodes)
        previous_keys.update({node[:4]: node[4] for node in previous})
        previous_projects.update({node[:4]: node[6] for node in previous})
        stale.update(node[:4] for node in previous if node[5])
        # The claims on the shared nodes the batch's files claim (see
        # _written_back_shared).
        if any(not n[2] for n in nodes):
            for key, sources in engine.list_shared_claims(repo_id, keys).items():
                claims_before.setdefault(key, set()).update(sources)

    snapshot(batch_keys)
    # Whether a batch file held Express mounts across files before (see _settle below).
    mounts.touched = bool(engine.route_mount_state(repo_id, batch_keys))

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
            {rel: p for rel, p in by_rel_path.items() if _is_provider_file(repo_root, p)},
            {node[:2] for node in previous_nodes},
        )
    # Provider nodes the batch writes that a docs edge can target: docs nodes
    # and filesystem files. Folders are left out: a folder isn't one of the
    # batch's files, so the snapshot above can't tell a new one from an old one.
    provider_nodes = {
        (node["label"], node["name"], node["properties"]["path"]) for node in (docs_batch.nodes if docs_batch else [])
    }
    if fs_spec is not None and fs_spec.file_label:
        provider_nodes |= {(fs_spec.file_label, rel, rel) for rel in present}

    total = len(by_rel_path)
    for done, rel_path in enumerate(sorted(by_rel_path), 1):
        _check_full_scan_claim(repo_id)  # a full scan another process took over stops
        index_one(rel_path, by_rel_path[rel_path])
        if done % PROGRESS_EVERY == 0:
            logger.info("%s: indexed %d / %d files", repo_id, done, total)
    # A router file another file mounts is named by those mounts
    # (apis/routes.py), which the batch may have changed (or written after
    # it, in path order): every router file whose `route_prefixes` no
    # longer match is indexed again, joining the batch.
    if mounts.touched:
        settle = _stale_mounts(mounts.reload(), mounts.table)
        outside = sorted(settle - set(by_rel_path))
        if outside:
            snapshot(outside)
            by_rel_path.update({rel: root_resolved / rel for rel in outside})
            batch_keys = sorted(by_rel_path)
        for rel_path in sorted(settle):
            index_one(rel_path, by_rel_path[rel_path])

    # Files outside the batch that refer by name to a node the batch just
    # added were indexed before that node existed, so their edge to it was
    # skipped. Index them now as part of this batch (one level, no
    # recursion); the passes below then link them like any batch file.
    batch_nodes = _batch_nodes(
        repo_id, root_resolved, extractions, docs_files, mention_files, batch_services, provider_nodes,
    )
    # A node is added when no node of its label, name and owner was at its
    # file before, so one moving between two of the batch's files, or to
    # another class of its file, is added too.
    # A field-keyed docs entry already in the graph (moving between files)
    # is not: its edges move with it.
    added = batch_nodes - previous_nodes
    if claims_before:
        written_back = _written_back_shared(claims_before, engine.list_shared_claims(repo_id, batch_keys), batch_keys)
        added |= {(label, name, "", "") for label, name in written_back} & batch_nodes
    if docs_batch:
        added -= {node for node in batch_nodes if node[:2] in docs_batch.existing}
    added_nodes = {node[:2] for node in added}
    # Match keys and C# projects, which pass 1 left as they were, are
    # written now, before pass 2 tests them. A kept node whose keys or
    # project changed (no keys and none, no project and "", being the
    # same) was linked by `@` pins under its old keys, and by rows that
    # could see its old project: its CALLS/IMPORTS/IMPLEMENTS edges go with them, and
    # pass 2 and the relink below write them again under the new ones, as
    # for an added node; one a failed batch left stale goes again. It is
    # not added otherwise: no referrer, docs or mention depends on them.
    match_keys = _match_keys(extractions)
    projects = _projects(extractions)
    rekeyed = {
        node for node, keys in match_keys.items()
        if node in stale or (node in previous_keys and (
            (previous_keys[node] or ()) != (keys or ()) or (previous_projects[node] or "") != (projects[node] or "")
        ))
    }
    # A file's open classes (its Module's `heir_open`, common.add_heir_keys)
    # decide which of its Functions an owned end links too, as keys do.
    new_open = _heir_open(extractions)
    old_open = engine.file_heir_open(repo_id, sorted(new_open)) if new_open else {}
    rekeyed |= _open_rekeyed(old_open, new_open, previous_keys, match_keys)
    rewritten = sorted(
        (*node, keys, projects[node], node in rekeyed) for node, keys in match_keys.items()
        if node in rekeyed or previous_keys.get(node) != keys or previous_projects.get(node) != projects[node]
    )
    if rewritten:
        engine.write_match_keys(repo_id, rewritten)
    # Written after the retraction marks (`mkeys_stale`), so a batch cut
    # short in between finds the old list and retracts again.
    if opened := {f: o for f, o in new_open.items() if old_open.get(f) != o}:
        engine.write_heir_open(repo_id, opened)
    # The match keys, old and new, of the nodes the batch adds, removes or
    # rekeys, by name: a tiered pin's edges to such a name depend on all its
    # nodes, so the relink writes the groups whose keys meet them again (see
    # _relink_name_refs). A node with no keys (any other language's) can
    # neither link nor shadow under an `@` pin, so it touches none.
    shadow_names = _shadow_keys(
        (node, previous_keys.get(node), match_keys.get(node))
        for node in added | rekeyed | (previous_nodes - batch_nodes)
    )
    referrers = _find_referrers(engine, repo_id, root_resolved, docs_root, added_nodes, set(by_rel_path))
    extractions.late = True  # written after the match keys above: pass 2 writes their nodes again
    for rel_path in sorted(referrers):
        index_one(rel_path, root_resolved / rel_path)
    if judged or read:
        engine.update_skipped_files(repo_id, add=judged, drop=read)
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

    # Second pass: write every code file's already-extracted edges again (no
    # re-parse, no re-prune). Batch order is path order, not dependency
    # order, so a CALLS/IMPORTS edge from file X to file Y within the
    # SAME batch can silently fail to materialize on the first pass if X
    # happens to sort before Y — upsert_relationships only
    # MATCH-MATCHes existing endpoint nodes, it doesn't create them, so Y's
    # node isn't there yet when X's edges are upserted. Upserting every
    # file's edges a second time is idempotent and guarantees every node in
    # the batch exists before every file's edges are attempted at least
    # once, regardless of first-pass order (a C# row with tiered match keys
    # or a project limit is only written here).
    # A file's nodes are not written again: its pass-1 replace wrote them,
    # nothing since deleted one (another file's replace deletes only that
    # file's nodes, and a shared node keeps this file's claim), and the
    # properties pass 1 left out (`mkeys`, `proj`, `heir_open`) were written
    # just above, as these nodes hold them (sorted, without repeats). Only a
    # referrer, written after those, has its nodes written again here.
    for _lang, _rel_path, entry in extractions.entries():
        nodes, rels, _content = extractions.load(entry)
        if entry.late:
            engine.upsert_nodes(nodes)
        engine.upsert_relationships(rels)

    # Code files outside the batch whose by-name edges meet an added node
    # were indexed before it existed. Their Modules' name_refs re-write those
    # edges straight from the graph, with no file read. A full scan has no
    # file outside the batch.
    if relink_outside:
        _relink_name_refs(
            engine, repo_id, added | rekeyed, set(by_rel_path) | referrers, match_keys, shadow_names, projects,
            _open_info(new_open, match_keys),
        )
    if rekeyed:
        engine.settle_match_keys(repo_id, sorted(rekeyed))

    # Service cross-linking runs as a final pass, after every file in this
    # batch (including any compose file) has been indexed — Service nodes'
    # build_context properties must already be in the graph for this to find
    # anything, and a compose file can sort after the files it owns within
    # one batch. The Service/
    # build_context lookup is loaded once for the whole batch rather than
    # once per file, since it can't have changed mid-batch (Services are
    # only written by the Containerfile/compose branches above, already run
    # by this point).
    # Another language's routes and datastores are its extraction's
    # Endpoint and datastore nodes.
    others = [(rel_path, entry) for lang, rel_path, entry in extractions.entries() if lang != "py"]
    route_endpoints = {
        rel_path: [node[1] for node in entry.nodes if node[0] == "Endpoint"] for rel_path, entry in others
    }
    route_endpoints = {rel_path: names for rel_path, names in route_endpoints.items() if names}
    client_stores = {
        rel_path: [node[:2] for node in entry.nodes if node[0] in DATASTORE_LABELS] for rel_path, entry in others
    }
    client_stores = {rel_path: stores for rel_path, stores in client_stores.items() if stores}
    py_files = extractions.files["py"]
    if py_files or route_endpoints or client_stores:
        services = _load_services_with_build_context(engine, repo_id)
        service_rels: list[dict] = []
        for rel_path, entry in py_files.items():
            # Read back only for a file a Service owns (the function's own first test).
            if _match_owning_service(services, rel_path) is not None:
                content = extractions.load(entry)[2]
                endpoints = [node[1] for node in entry.nodes if node[0] == "Endpoint"]
                owned = partial(_owning_service_relationships, repo_id, rel_path, content, services, endpoints)
                try:
                    service_rels.extend(owned())
                except RecursionError:
                    # As pass 1 read it: again on a deep stack, and a file
                    # deeper still writes none, never ending the batch.
                    found: list[dict] = []
                    try:
                        _run_deep(lambda: len(found.extend(owned()) or found))
                    except RecursionError as exc:
                        _log_file_failure("owning-service edges skipped", repo_id, rel_path, exc)
                        found = []
                    service_rels.extend(found)
        for rel_path, names in sorted(route_endpoints.items()):
            service_rels.extend(_endpoint_service_relationships(repo_id, rel_path, names, services))
        for rel_path, stores in sorted(client_stores.items()):
            service_rels.extend(_datastore_service_relationships(repo_id, rel_path, stores, services))
        engine.upsert_relationships(service_rels)
    # A compose file changed which Service owns which folder: the files
    # outside the batch that wrote owning-service edges are owned anew.
    if relink_outside and any(_is_compose(rel_path) for rel_path in by_rel_path):
        _relink_owning_services(engine, repo_id, set(by_rel_path) | referrers)

    if providers_ok and fs_spec is not None:
        filesystem.sync_present(engine, repo_id, fs_spec, present)
    if docs_batch:
        _sync_docs(engine, repo_id, docs_spec, docs_batch)
        _relink_docs(engine, repo_id, repo_root, docs_spec, added_nodes, set(docs_batch.selected), docs_batch.view)
        _log_cache_stats()

    # Docs notes get the same node-then-edge treatment as the source files
    # above: pass 1 (in the loop) created every note's node, but a note's
    # SUPERSEDES/DECIDED_BY target note or `links` Module may come from a
    # file that sorted after it. Re-indexing the (tiny) note now that every
    # node in the batch exists materializes those edges.
    for path in docs_files:
        try:
            index_doc_file(engine, repo_id, path, repo_root)
        except ABORTS_BATCH:
            raise  # shutdown or an index still building: leave the work, and its stamp, for later
        except Exception as exc:
            _log_file_failure("docs edge pass failed", repo_id, path, exc)

    # Mentions run last: unlike every other extractor, mention extraction
    # reads the graph (the set of known entity names) to decide what a
    # Markdown file mentions, so it needs every node from this batch --
    # code symbols, docs notes, and the Document nodes pass 1 created --
    # to already exist.
    for path in mention_files:
        try:
            index_mentions_file(engine, repo_id, path, repo_root, ambiguous_mode=get_settings().mentions_ambiguous_mode)
        except ABORTS_BATCH:
            raise  # shutdown or an index still building: leave the work, and its stamp, for later
        except Exception as exc:
            _log_file_failure("mentions pass failed", repo_id, path, exc)
    added_names = {name for _label, name in added_nodes}
    for path in mention_relinks:
        try:
            index_mentions_file(
                engine, repo_id, path, repo_root, ambiguous_mode=get_settings().mentions_ambiguous_mode, names=added_names
            )
        except ABORTS_BATCH:
            raise  # shutdown or an index still building: leave the work, and its stamp, for later
        except Exception as exc:
            _log_file_failure("mentions relink failed", repo_id, path, exc)

    return indexed


# Deeply nested code (a long `else if` chain, a generated `a + b + ...`
# expression) is one tree level per branch or term, and the extractors walk
# the tree recursively: about 500 branches or 1,000 terms pass the default
# recursion limit. Such a file is extracted again on a thread of its own with
# a large stack and the limit raised, so it is indexed in full; one deeper
# still is skipped as TOO_DEEP.
_DEEP_RECURSION_LIMIT = 200_000
# Under 256 MiB: CPython on Windows refuses a larger thread stack.
_DEEP_STACK_BYTES = 255 * 1024 * 1024
TOO_DEEP = "too deeply nested"
_deep_lock = threading.Lock()


def _run_deep(fn: Callable[[], int]) -> int:
    """`fn()` on a thread with `_DEEP_STACK_BYTES` of stack, the recursion
    limit raised to `_DEEP_RECURSION_LIMIT` meanwhile. One at a time: the
    limit is the interpreter's, not the thread's. A platform that can't give
    the thread that stack raises RecursionError, so the file takes the
    TOO_DEEP path as it would on any other failure of the retry."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # re-raised on the calling thread
            outcome["error"] = exc

    with _deep_lock:
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, _DEEP_RECURSION_LIMIT))
        try:
            try:
                stack = threading.stack_size(_DEEP_STACK_BYTES)
                try:
                    thread = threading.Thread(target=run, name="devgraph-deep-extract")
                    thread.start()
                finally:
                    threading.stack_size(stack)
            except (ValueError, RuntimeError, MemoryError) as exc:
                raise RecursionError(f"no thread with a {_DEEP_STACK_BYTES >> 20} MiB stack: {exc}") from exc
            thread.join()
        finally:
            sys.setrecursionlimit(limit)
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]  # type: ignore[return-value]


def _log_file_failure(what: str, repo_id: str, path: object, exc: Exception) -> None:
    """One line for a file the batch skips (a locked or unparseable file is
    not a crash); the traceback only at debug level."""
    logger.warning("%s for %s (%s); skipping file: %s: %s", what, repo_id, path, type(exc).__name__, exc)
    logger.debug("%s for %s (%s)", what, repo_id, path, exc_info=exc)


_MARKDOWN_SUFFIXES = (".md", ".markdown")
#: The labels a datastore node takes (datastores/extractor.py DatastoreType).
DATASTORE_LABELS = frozenset(OWNING_SERVICE_TARGETS)
_CODE_ROUTES = {
    ".py": "py", ".cs": "cs", ".java": "java", ".rs": "rs", ".kt": "kt", ".go": "go",
    **{suffix: "js" for suffix in _JS_SUFFIXES},
    **{suffix: "cpp" for suffix in _CPP_SUFFIXES},
}


def _routes(resolved: Path, docs_root: Path | None, mentions_enabled: bool) -> list[str]:
    """The built-in extractors `_index_single_path` runs on a file, by its
    resolved path (a symlink is routed by its target, as it is keyed): one
    code language or a docs note, then Markdown mentions, then a
    Containerfile or a compose file. Empty when none would.
    `_would_index` asks the same question for catch-up."""
    routes = []
    markdown = resolved.suffix in _MARKDOWN_SUFFIXES
    code = _CODE_ROUTES.get(resolved.suffix)
    if code is not None:
        routes.append(code)
    elif docs_root is not None and markdown and is_within(resolved, docs_root):
        routes.append("docs")
    if mentions_enabled and markdown:
        routes.append("mentions")
    name_lower = resolved.name.lower()
    if name_lower in _CONTAINERFILE_NAMES:
        routes.append("containerfile")
    elif name_lower in _COMPOSE_NAMES:
        routes.append("compose")
    return routes


def _with_name_refs(nodes: list[dict], rels: list[dict], rel_path: str) -> list[dict]:
    """A copy of a code file's `nodes` whose Module carries the file's
    by-name edges (see name_ref_properties), for its pass-1 replace only:
    pass 2 writes no file's nodes but a referrer's, the plain `nodes`. Its
    symbols' match keys are left out: index_paths writes them after pass 1
    (engine.write_match_keys), with the edges they decide, and so is their
    C# project (`proj`), and its Module's open classes (`heir_open`). A
    file-less claimed node (an ASP.NET handler stub) keeps both: no match-key
    write reaches it, and pass 2 no longer writes it again."""
    refs = name_ref_properties(rels)
    return [
        {**node, "properties": {**{k: v for k, v in node["properties"].items() if k != "heir_open"}, **refs}}
        if node["label"] == "Module" and node["name"] == rel_path
        else {**node, "properties": {k: v for k, v in node["properties"].items() if k not in ("mkeys", "proj")}}
        if node["properties"].get("file") and ("mkeys" in node["properties"] or "proj" in node["properties"])
        else node
        for node in nodes
    ]


def _index_single_path(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    resolved: Path,
    rel_path: str,
    docs_root: Path | None,
    mentions_enabled: bool,
    config: ResolverConfig,
    extractions: _Extractions,
    docs_files: list[Path],
    mention_files: list[Path],
    batch_services: set[tuple[str, str, str]],
    mounts: _Mounts,
) -> int:
    """Extract and upsert one file's graph output, routing by name/extension.

    Extracted into its own function so `index_paths` can wrap each file in a
    try/except: one unparseable/locked file (or a transient Neo4j error
    mid-batch) must not abort the whole batch. The batch's extraction
    store is passed in so the second-pass cross-linking below still sees
    every file that *did* extract successfully.

    Returns the number of indexing actions performed (0 for a file that
    matched no extractor, e.g. a Markdown file outside docs_path with
    mentions disabled) -- mirrors the original loop's `indexed += 1` count.
    """
    indexed = 0
    routes = _routes(resolved, docs_root, mentions_enabled)
    if "py" in routes:
        content = read_source(resolved)
        result = extract_python_file(content, rel_path, repo_id, config)
        # Datastore/API extraction reads the same content, so it runs
        # alongside the Python indexer rather than as a separate dispatch
        # branch. Passed the repo-relative path (not bare filename) so
        # their 'source'/'file' provenance properties match what
        # delete_nodes_by_source_file looks up on file deletion.
        api_nodes, api_rels, route_props = _api_nodes_and_rels(
            repo_id, rel_path, content, config.python_roots(rel_path) if config is not None else (),
        )
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content) + api_nodes
        rels = [r.to_dict() for r in result.relationships] + api_rels
        # Its routers' mounts across files, as a JS/TS file's (apis/routes.py).
        next(node for node in nodes if node["label"] == "Module" and node["name"] == rel_path)["properties"].update(
            route_props
        )
        nodes, rels = _mounted(nodes, rels, rel_path, mounts)
        # Delete this file's previously-indexed nodes and write the
        # freshly-extracted ones in one transaction, not just
        # MERGE-upsert the current contents: a Function/Class removed
        # from the file (edited, not deleted) would otherwise survive in
        # the graph forever, since MERGE only ever adds/updates matching
        # nodes, never removes ones the current source no longer
        # produces. One transaction also means a reader never observes
        # this file's nodes as gone-but-not-yet-rebuilt. The Datastore/
        # Endpoint/handler claims go through the same call, so the ones
        # the file still makes are re-claimed in place, not recreated. A
        # handler stub claims only a file-less Function (_claim_nodes_tx),
        # never the file-scoped one the extractor writes for the same def.
        # API edges ride along in the re-upsert pass: an Endpoint's
        # IMPLEMENTS target can be a handler defined in a later file (e.g.
        # a Django urls.py naming a view from views.py). The content is
        # kept for the owning-service pass. Kept before the graph write, so
        # a spill that fails aborts the batch with nothing written.
        extractions.add("py", rel_path, nodes, rels, content)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels, service_api=True)
        indexed += 1
    elif "js" in routes:
        content = read_source(resolved)
        result = extract_js_file(content, rel_path, repo_id, config)
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content, "js")
        rels = [r.to_dict() for r in result.relationships]
        nodes, rels = _mounted(nodes, rels, rel_path, mounts)
        # Same replace-then-reupsert rationale as the .py branch above:
        # prune this file's previously-indexed nodes/edges and write the
        # freshly-extracted ones in one transaction. Its routes' Endpoint
        # edges go as a Python file's do (`service_api`).
        extractions.add("js", rel_path, nodes, rels)  # once, though the mounts pass may index it again
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels, service_api=True)
        indexed += 1
    elif "cs" in routes:
        content = read_source(resolved)
        result = extract_csharp_file(content, rel_path, repo_id, config)
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content, "cs")
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above:
        # prune this file's stale nodes/edges in one transaction, then
        # re-upsert in pass 2 once every file in the batch has a node. A
        # row with tiered match keys waits for pass 2: whether a nearer
        # node shadows its target depends on nodes a later file of the
        # batch may still add, remove or rekey, and pass 2 only adds edges.
        # So does a row limited to its project's references (`visible`):
        # a node's `proj` is written with its match keys, after pass 1.
        extractions.add("cs", rel_path, nodes, rels)
        engine.replace_file_nodes(
            repo_id, rel_path, _with_name_refs(nodes, rels, rel_path),
            [rel for rel in rels if not tiered(rel.get("to_file")) and not rel.get("visible")], service_api=True,
        )
        indexed += 1
    elif "cpp" in routes:
        content = read_source(resolved)
        result = extract_cpp_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        extractions.add("cpp", rel_path, nodes, rels)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels)
        indexed += 1
    elif "java" in routes:
        content = read_source(resolved)
        result = extract_java_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content, "java")
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-cross-link-reupsert pattern as the .py
        # branch above (see its comment for why replace_file_nodes runs
        # first, in one transaction, rather than plain upsert).
        extractions.add("java", rel_path, nodes, rels)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels, service_api=True)
        indexed += 1
    elif "rs" in routes:
        content = read_source(resolved)
        result = extract_rust_file(content, rel_path, repo_id, config)
        nodes = [n.to_dict() for n in result.nodes]
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-not-merely-upsert reasoning as the .py branch
        # above: a removed Function/Class must not survive in the graph
        # forever just because MERGE never deletes.
        extractions.add("rs", rel_path, nodes, rels)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels)
        indexed += 1
    elif "kt" in routes:
        content = read_source(resolved)
        result = extract_kotlin_file(content, rel_path, repo_id)
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content, "kt")
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above: a
        # class/function removed from the file must not survive in the graph
        # as a stale node.
        extractions.add("kt", rel_path, nodes, rels)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels, service_api=True)
        indexed += 1
    elif "go" in routes:
        content = read_source(resolved)
        result = extract_go_file(content, rel_path, repo_id, config)
        nodes = [n.to_dict() for n in result.nodes] + _datastore_nodes(repo_id, rel_path, content, "go")
        rels = [r.to_dict() for r in result.relationships]
        # Same replace-then-re-upsert rationale as the .py branch above:
        # a struct/func/method removed from the file must not survive in
        # the graph as a stale node.
        extractions.add("go", rel_path, nodes, rels)
        engine.replace_file_nodes(repo_id, rel_path, _with_name_refs(nodes, rels, rel_path), rels, service_api=True)
        indexed += 1
    elif "docs" in routes:
        index_doc_file(engine, repo_id, resolved, repo_root)
        indexed += 1
        docs_files.append(resolved)
    if "mentions" in routes:
        # Only the Document node here; its MENTIONS edges are resolved in
        # index_paths' final pass, once every node in the batch exists.
        upsert_document_node(engine, repo_id, resolved, repo_root)
        indexed += 1
        mention_files.append(resolved)
    if "containerfile" in routes:
        result = _index_containerfile(engine, repo_id, resolved, rel_path)
        batch_services |= {("Service", service.name, rel_path) for service in result.services}
        batch_services |= {("Container", container.name, "") for container in result.containers}
        indexed += 1
    elif "compose" in routes:
        result = _index_compose_file(engine, repo_id, resolved, rel_path)
        batch_services |= {("Service", service.name, rel_path) for service in result.services}
        batch_services |= {("Container", container.name, "") for container in result.containers}
        indexed += 1
    return indexed


#: `index_paths`' marker for a file whose extraction entry was not yet looked up.
_UNREAD = object()

#: index_paths logs its progress every this many files of a batch.
PROGRESS_EVERY = 5_000


class _Extracted(NamedTuple):
    """One code file's pass-1 extraction as `_Extractions` holds it."""

    spill: tuple[int, int]  # (offset, length) of its pickled (nodes, rels, content) in the spill file
    # (label, name, file, owner, mkeys, proj, source) of each node, its
    # properties' values or None: what the steps between the passes read.
    nodes: tuple[tuple, ...]
    heir_open: tuple[tuple[str, tuple[str, ...]], ...]  # (Module, its open classes) of each Module with `heir_open`
    late: bool  # written after the batch's match keys (a referrer)


class _Extractions:
    """The code files' pass-1 extractions of one index_paths batch, for the passes after it.

    Pass 2 writes every file's edges again once every node exists, so each
    extraction is needed until then. Held as node and edge dicts, a full
    scan kept every file's (about 120 KB a file) to its end. Each one is
    pickled and compressed into an anonymous temporary file instead (a few
    KB a file), read back by pass 2, and only what the steps between the
    passes read stays in memory: each node's key fields (`_Extracted.nodes`).
    The file is in DevGraph's own folder (`devgraph_home()/tmp`), not the
    system's temporary folder, which may be memory (tmpfs); it has no name
    (deleted on close, on Windows too), and a write or read that fails
    aborts the batch (`ExtractionSpillFailed`).

    `files` is per language in pass 2's order, and per language in the order
    files were first added: a file added again (a router file the mounts
    pass indexes again) keeps its place and its latest extraction.
    """

    LANGUAGES = ("py", "js", "cs", "cpp", "java", "rs", "kt", "go")

    def __init__(self) -> None:
        self.files: dict[str, dict[str, _Extracted]] = {lang: {} for lang in self.LANGUAGES}
        self.late = False  # set once the batch's match keys are written
        self._spill = None

    def __enter__(self) -> _Extractions:
        return self

    def __exit__(self, *exc) -> None:
        if self._spill is not None:
            self._spill.close()

    def add(self, lang: str, rel_path: str, nodes: list[dict], rels: list[dict], content: str | None = None) -> None:
        data = zlib.compress(pickle.dumps((nodes, rels, content), pickle.HIGHEST_PROTOCOL), 1)
        try:
            if self._spill is None:
                folder = devgraph_home() / "tmp"
                folder.mkdir(parents=True, exist_ok=True)
                self._spill = tempfile.TemporaryFile(dir=folder)
            offset = self._spill.seek(0, os.SEEK_END)
            self._spill.write(data)
            self._spill.flush()  # a full disk fails this file, not a later one
        except OSError as exc:
            raise ExtractionSpillFailed(f"could not keep the extraction of {rel_path} for the batch: {exc}") from exc
        keys = []
        heir_open = []
        for node in nodes:
            props = node["properties"]
            mkeys = props.get("mkeys")
            keys.append((
                node["label"], node["name"], props.get("file"), props.get("owner"),
                None if mkeys is None else tuple(mkeys), props.get("proj"), props.get("source"),
            ))
            if node["label"] == "Module" and "heir_open" in props:
                heir_open.append((node["name"], tuple(sorted(props["heir_open"]))))
        self.files[lang][rel_path] = _Extracted((offset, len(data)), tuple(keys), tuple(heir_open), self.late)

    def load(self, entry: _Extracted) -> tuple[list[dict], list[dict], str | None]:
        """(nodes, rels, content) of a file as `add` was given them."""
        offset, length = entry.spill
        try:
            self._spill.seek(offset)
            return pickle.loads(zlib.decompress(self._spill.read(length)))
        except (OSError, EOFError, zlib.error, pickle.UnpicklingError) as exc:
            raise ExtractionSpillFailed(f"could not read back a kept extraction: {exc}") from exc

    def saved(self, rel_path: str) -> tuple[str, _Extracted] | None:
        """The file's current entry, for `restore`."""
        return next(((lang, files[rel_path]) for lang, files in self.files.items() if rel_path in files), None)

    def restore(self, rel_path: str, saved: tuple[str, _Extracted] | None) -> None:
        """Put back what `saved` returned: a file whose graph write failed
        after its extraction was added keeps its earlier one, or none."""
        if saved is None:
            self.discard(rel_path)
        else:
            self.files[saved[0]][rel_path] = saved[1]  # a file's language never changes: its place is kept

    def discard(self, rel_path: str) -> None:
        """Forget a file left out after it was extracted, so pass 2 writes nothing for it."""
        for files in self.files.values():
            files.pop(rel_path, None)

    def entries(self):
        """(language, rel_path, entry) of every file, in pass 2's order."""
        for lang, files in self.files.items():
            for rel_path, entry in files.items():
                yield lang, rel_path, entry


def _batch_nodes(
    repo_id: str,
    root: Path,
    extractions: _Extractions,
    docs_files: list[Path],
    mention_files: list[Path],
    services: set[tuple[str, str, str]],
    provider_nodes: set[tuple[str, str, str]],
) -> set[tuple[str, str, str, str]]:
    """(label, name, file, owner) of every file-provenance node this batch wrote:
    code symbols and Modules, docs notes, Markdown Document nodes, compose
    Services and schema-provider nodes -- the same provenance, with the same
    `file`, that list_file_nodes snapshots. A shared node a batch file claims
    (a `Datastore`, `Endpoint`, handler stub or C++ out-of-class method's
    Class stub, or a compose/Containerfile `Container`) has `file` "", as
    list_file_nodes gives it, so one whose last claim went and came back is
    added. Only a code symbol has an owner (common.member_owner); every
    other node's is ""."""
    nodes = {
        (label, name, rel_path if label == "Module" else file, owner or "")
        for _lang, rel_path, entry in extractions.entries()
        for label, name, file, owner, _mkeys, _proj, _source in entry.nodes
        if label == "Module" or file
    }
    nodes |= {
        (label, name, "", "")
        for _lang, rel_path, entry in extractions.entries()
        for label, name, file, _owner, _mkeys, _proj, source in entry.nodes
        if not file and source == rel_path
    }
    for path in docs_files:
        # The extraction is keyed by the file name, but index_doc_file
        # writes the repo-relative path as the note's `source_file`.
        result = DocsExtractor(repo_id).extract_from_source(_read_text(path), path.name)
        rel = path.relative_to(root).as_posix()
        nodes |= {(doc.label, doc.name, rel, "") for doc in result.docs}
    nodes |= {("Document", rel, rel, "") for rel in (path.relative_to(root).as_posix() for path in mention_files)}
    nodes |= {(*node, "") for node in services | provider_nodes}
    return nodes


def _match_keys(extractions: _Extractions) -> dict[tuple[str, str, str, str], tuple[str, ...] | None]:
    """(label, name, file, owner) -> sorted `mkeys` (graph/schema.py `split_pin`'s
    `@` flag tests them) of every code symbol this batch wrote, None for
    one written without them, as list_file_nodes gives them."""
    return {
        (label, name, file, owner or ""): None if mkeys is None else tuple(sorted(set(mkeys)))
        for _lang, _rel_path, entry in extractions.entries()
        for label, name, file, owner, mkeys, _proj, _source in entry.nodes
        if file
    }


def _projects(extractions: _Extractions) -> dict[tuple[str, str, str, str], str | None]:
    """(label, name, file, owner) -> the C# project (`proj`, which a row's
    `visible` tests, see common.visible_to) of every code symbol this batch
    wrote, None for one written without one, as list_file_nodes gives it."""
    return {
        (label, name, file, owner or ""): proj
        for _lang, _rel_path, entry in extractions.entries()
        for label, name, file, owner, _mkeys, proj, _source in entry.nodes
        if file
    }


def _heir_open(extractions: _Extractions) -> dict[str, tuple[str, ...]]:
    """File -> its Module's open classes (`heir_open`, common.add_heir_keys)
    for every code file of the batch whose extractor writes them."""
    return {module: opens for _lang, _rel_path, entry in extractions.entries() for module, opens in entry.heir_open}


def _open_rekeyed(
    old_open: dict[str, tuple[str, ...]], new_open: dict[str, tuple[str, ...]],
    previous_keys: dict[tuple[str, str, str, str], tuple[str, ...] | None],
    match_keys: dict[tuple[str, str, str, str], tuple[str, ...] | None],
) -> set[tuple[str, str, str, str]]:
    """The batch's Functions whose incoming owned edges an open class of
    their file (graph/engine.py `_owner_test`) may have linked otherwise:
    every Function of a file whose open classes changed, and of a file with
    open classes, every Function of a name whose Functions an open class
    now has or inherits otherwise than before (one added, removed, or
    rekeyed). They are retracted and relinked as rekeyed nodes are."""
    def functions(keys, file):
        return [(node, each or ()) for node, each in keys.items() if node[0] == "Function" and node[2] == file]

    found: set[tuple[str, str, str, str]] = set()
    for file in set(old_open) | set(new_open):
        owners_old, owners_new = set(old_open.get(file, ())), set(new_open.get(file, ()))
        new = functions(match_keys, file)
        if owners_old != owners_new:
            found.update(node for node, _keys in new)
            continue
        if not owners_new:
            continue
        old = functions(previous_keys, file)

        def has(nodes, name, owner):
            return any(node[1] == name and (node[3] == owner or HEIR_KEY + owner in keys) for node, keys in nodes)

        for name in {node[1] for node, _keys in new} | {node[1] for node, _keys in old}:
            if any(has(old, name, owner) != has(new, name, owner) for owner in owners_new):
                found.update(node for node, _keys in new if node[1] == name)
    return found


def _open_info(
    new_open: dict[str, tuple[str, ...]], match_keys: dict[tuple[str, str, str, str], tuple[str, ...] | None],
) -> dict[str, tuple[tuple[str, ...], dict[str, list]]]:
    """File -> (its open classes, name -> its Functions' (owner, mkeys)),
    which common.owner_matches reads, for the batch's files with open classes."""
    info: dict[str, tuple[tuple[str, ...], dict[str, list]]] = {
        file: (owners, {}) for file, owners in new_open.items() if owners
    }
    for (label, name, file, owner), keys in match_keys.items():
        if label == "Function" and file in info:
            info[file][1].setdefault(name, []).append((owner, keys or ()))
    return info


def _shadow_keys(nodes) -> dict[tuple[str, str], set[str]]:
    """(label, name) -> the match keys of these (node, old mkeys, new mkeys)
    of a file-scoped label, old and new together, heir keys
    (graph/schema.py `HEIR_KEY`) aside, which no `@` pin holds; a name
    whose nodes hold none is left out."""
    found: dict[tuple[str, str], set[str]] = {}
    for node, old, new in nodes:
        keys = {key for key in (*(old or ()), *(new or ())) if not key.startswith(HEIR_KEY)}
        if keys and node[0] in FILE_SCOPED_LABELS:
            found.setdefault(node[:2], set()).update(keys)
    return found


#: The most tiered groups `_relink_name_refs` writes again per transaction.
RERUN_GROUPS = 200


def _relink_name_refs(
    engine: GraphEngine, repo_id: str, added: set[tuple[str, str, str, str]], skip: set[str],
    match_keys: dict[tuple[str, str, str, str], tuple[str, ...] | None] | None = None,
    shadow_names: Mapping[tuple[str, str], set[str]] | None = None,
    projects: dict[tuple[str, str, str, str], str | None] | None = None,
    open_info: dict[str, tuple[tuple[str, ...], dict[str, list]]] | None = None,
) -> None:
    """Re-write the by-name edges that code files outside `skip` recorded in
    their Modules' `name_refs` (see name_ref_properties) and that meet a node
    in `added` ((label, name, file, owner)): by their target, or by an
    unpinned non-Module source (a Rust `impl Trait for Foo` whose `Foo` was
    just added). Those files were indexed before the node existed, so the
    edge was skipped then (or, for a node whose match keys changed, its
    edges were retracted). One graph read and one upsert, no file read; each
    edge's origin is its file.

    The end that met an added node is pinned to it when its label is keyed
    by file: to its file and its owner, so a sibling of its name in that
    file is never linked by it. Its edges to same-named nodes that were
    already there exist (they were written when the file was indexed, or
    relinked when that node was added), and a bare-name match can't use the
    file-keyed index. An entry with target pins relinks only an added node a
    pin of its links (common.best_pin: the strongest pin that matches its
    file, as the writer's rows leave out what a stronger pin matches, tested
    against the node's `match_keys`; an owned pin's owner as
    common.owner_matches reads it, with `open_info`, the batch files' open
    classes and their Functions), with that pin's confidence, when a
    path pin's node is visible to the entry's file's project (its
    Module's `name_ref_visible` against the node's `projects`,
    common.visible_to): such an edge is pinned to the node's file, so the
    test is made here, not by the engine. The rows a tiered group's re-run
    and an added unpinned source write keep the entry's path pins, and
    carry that `visible` for the engine to test (`_visible`). A pinned
    Module target is found by an added Module's directory, directory name or
    basename (the row's `to_name`, see common.module_pin_name) and relinked
    by its path. Each edge gets back the entry's `caller_class`, and its
    source the entry's owner.

    A group (one source's entries to one name) with a tiered pin
    (graph/schema.py `tiered_pin`) is never relinked edge by edge: whether
    it links a node depends on every node of the name. When one of its
    pins' keys is among `shadow_names`' keys for its (to_label, to_name)
    (the old and new match keys of the nodes of that name the batch added,
    removed or rekeyed) it is written again whole, as its writer wrote it
    (engine.rerun_edges: its file's edges of the group go first),
    RERUN_GROUPS groups per transaction; else it stays as it is: a node
    holding none of a pin's keys neither links nor shadows under it.
    """
    shadow_names = shadow_names or {}
    if not added and not shadow_names:
        return
    pinned: dict[tuple[str, str], set[tuple[str | None, str | None]]] = {}
    modules: set[str] = set()
    for label, name, file, owner in added:
        pinned.setdefault((label, name), set()).add((file, owner) if label in FILE_SCOPED_LABELS else (None, None))
        if label == "Module":
            modules.add(name)
    rels = []
    rerun: list[tuple[dict, list[dict]]] = []

    def relink(origin, fields, from_file, to_file, confidence, exclude=(), to_name=None, from_owner=None,
               to_owner=None, visible=None):
        rel_type, from_label, from_name, _from_file, to_label, field_to_name, caller_class, _owner = fields
        properties = {key: value for key, value in (("caller_class", caller_class), ("confidence", confidence)) if value}
        rels.append({
            "from_label": from_label, "from_name": from_name, "rel_type": rel_type,
            "to_label": to_label, "to_name": to_name or field_to_name, "repo_id": repo_id,
            "properties": properties or None,
            "from_file": from_file or None, "to_file": to_file, "origin": origin, "exclude": list(exclude),
            "from_owner": from_owner, "to_owner": to_owner, "visible": visible or None,
        })

    names = {name for _label, name in pinned} | {name for _label, name in shadow_names}
    names |= {key for module in modules for key in _module_pin_keys(module)}
    for origin, refs, visible in engine.find_name_refs(repo_id, sorted(names), sorted(skip)):
        parsed = _with_scopes_merged(_parsed_name_refs(origin, refs))
        # Every Module a source imports, by path or by pin, as calls.import_rows
        # collapsed them: a pinned Module target leaves out what a stronger
        # one of them matches.
        imported: dict[tuple, set[str]] = {}
        for fields, pins, _confidence in parsed:
            if fields[4] == "Module":
                imported.setdefault(tuple(fields[:4]), set()).update(pins if pins is not None else [fields[5]])
        for fields, pins, confidence in parsed:
            _rel_type, from_label, from_name, from_file, to_label, to_name, _caller_class, from_owner = fields
            # A source pinned to its file keeps the entry's owner (one of the
            # file's same-named siblings); one whose owner was not known
            # (an entry written before owners) matches them all.
            owner = from_owner if from_file else None
            owners = owners_by_pin(pins) if pins is not None else {}
            if pins is not None and any(tiered(pin) for pin in owners):
                touched = shadow_names.get((to_label, to_name), set())
                if any(touched & set(split_pin(pin).scope) for pin in owners):
                    group = {
                        "rel_type": fields[0], "from_label": from_label, "from_name": from_name,
                        "from_file": from_file or None, "from_owner": owner, "to_label": to_label,
                        "to_name": to_name, "origin": origin,
                    }
                    # The writer's rows (calls.call_rows).
                    start = len(rels)
                    for pin, exclude in pin_excludes(owners, origin).items():
                        for to_owner in [None] if None in owners[pin] else sorted(owners[pin]):
                            relink(
                                origin, fields, from_file, pin, pin_confidence(pin, confidence), exclude,
                                from_owner=owner, to_owner=to_owner, visible=visible,
                            )
                    rerun.append((group, rels[start:]))
                    del rels[start:]
                continue
            if pins is None:
                for to_file, to_owner in pinned.get((to_label, to_name), ()):
                    relink(origin, fields, from_file, to_file, confidence, from_owner=owner, to_owner=to_owner)
            elif to_label == "Module":
                # A directory or suffix pin: to_name is what it seeks.
                for module in sorted(modules):
                    if to_name in _module_pin_keys(module):
                        pin = best_pin(imported[tuple(fields[:4])], module, origin)
                        if pin in pins:
                            relink(
                                origin, fields, from_file, None, pin_confidence(pin, confidence), to_name=module,
                                from_owner=owner,
                            )
            else:
                for to_file, to_owner in pinned.get((to_label, to_name), ()):
                    node = (to_label, to_name, to_file, to_owner)
                    keys = (match_keys or {}).get(node) or ()
                    pin = best_pin(owners, to_file or "", origin, keys) if to_file is not None else None
                    # A path pin's row sees only its project's references (graph/engine.py `_visible`).
                    if pin is not None and pin_kind(pin) not in ("file", "fileless"):
                        pin = pin if visible_to((projects or {}).get(node), visible) else None
                    # A pin that named its target's owner links only that owner's member,
                    # its own or one it inherits from a class of its file.
                    opened, siblings = (open_info or {}).get(to_file or "", ((), {})) if to_label == "Function" \
                        else ((), {})
                    if pin is not None and owner_matches(owners[pin], to_owner, keys, opened, siblings.get(to_name, ())):
                        relink(
                            origin, fields, from_file, to_file, pin_confidence(pin, confidence), from_owner=owner,
                            to_owner=to_owner,
                        )
            if not from_file and from_label != "Module":
                excludes = pin_excludes(owners, origin) if pins is not None else {None: ()}
                for source_file, source_owner in pinned.get((from_label, from_name), ()):
                    for to_file, exclude in excludes.items():
                        conf = confidence if to_file is None else pin_confidence(to_file, confidence)
                        pin_owners = owners.get(to_file, {None})
                        for to_owner in [None] if None in pin_owners else sorted(pin_owners):
                            relink(
                                origin, fields, source_file, to_file, conf, exclude, from_owner=source_owner,
                                to_owner=to_owner, visible=visible,
                            )
    for start in range(0, len(rerun), RERUN_GROUPS):
        chunk = rerun[start:start + RERUN_GROUPS]
        engine.rerun_edges(repo_id, [group for group, _rows in chunk], [row for _group, rows in chunk for row in rows])
    if rels:
        engine.upsert_relationships(rels)


def _parsed_name_refs(origin: str, refs: list[str]) -> list[tuple[list, list[str] | None, str]]:
    """`refs` (a Module's `name_refs` entries) parsed, leaving out, with a
    debug line, any entry no layout reads: one an index older than owners
    wrote in a shape `parse_name_ref` does not know never stops a batch."""
    parsed = []
    for entry in refs:
        try:
            parsed.append(parse_name_ref(entry))
        except ValueError:
            logger.debug("skipping an unreadable name_refs entry of %s: %r", origin, entry)
    return parsed


def _with_scopes_merged(
    parsed: list[tuple[list[str], list[str] | None, str]],
) -> list[tuple[list[str], list[str] | None, str]]:
    """`name_refs` entries (parse_name_ref's) with the pinned entries of one
    source and name taken together: its pins with different match keys are
    entries of their own (name_ref_properties), and each of one call's pins
    leaves out what a stronger one of them matches, as the writer's rows
    did. Every other entry is as it was."""
    merged: dict[tuple[str, ...], tuple[set[str], set[str]]] = {}
    unpinned = []
    for fields, pins, confidence in parsed:
        if pins is None:
            unpinned.append((fields, pins, confidence))
        else:
            all_pins, confidences = merged.setdefault(tuple(fields), (set(), set()))
            all_pins.update(pins)
            confidences.add(confidence)
    return unpinned + [
        (list(fields), sorted(pins), min(confidences)) for fields, (pins, confidences) in merged.items()
    ]


def _module_pin_keys(module: str) -> tuple[str, str, str]:
    """What a pinned IMPORTS row can seek a Module by (common.module_pin_name):
    its directory, its directory's last name and its basename."""
    folder = posixpath.dirname(module)
    return folder, posixpath.basename(folder), posixpath.basename(module)


def _find_referrers(
    engine: GraphEngine,
    repo_id: str,
    root: Path,
    docs_root: Path | None,
    added: set[tuple[str, str]],
    batch: set[str],
) -> set[str]:
    """Repo-relative paths outside `batch` whose by-name edges can target a
    node in `added` -- files indexed before that node existed, so their edge
    to it was skipped (an edge whose endpoint is missing is never created).

    Nothing in the graph points from these files to the new node, so each
    kind is found by its own lookup,
    and each is bounded to stay cheap: it only runs when the batch added a
    node of a label it can target, and it only reads files outside the
    batch. A full scan has no files outside the batch, and a batch that
    only re-saves files adds no nodes, so either way this is close to free.
    (Markdown mentions are found separately, by _mention_referrers, and the
    by-name edges of other code files are relinked from the graph, without
    re-indexing them, by _relink_name_refs.)
    """
    if not added:
        return set()
    found = _docs_note_referrers(repo_id, root, docs_root, added, batch)
    found |= _handler_stub_referrers(engine, repo_id, added)
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
        if path.suffix not in (".md", ".markdown") or not _is_indexable_file(path):
            continue
        rel_path = path.relative_to(root).as_posix()
        if is_ignored_path(Path(rel_path), root):
            continue
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


def _written_back_shared(
    before: dict[tuple[str, str], set[str]], after: dict[tuple[str, str], set[str]], batch: list[str]
) -> set[tuple[str, str]]:
    """The shared nodes (label, name) the batch may have deleted and written
    back: every claim on one came from the batch's files, and one of those
    files dropped its claim. Files go in path order, so the dropping file can
    delete the node, with every edge into it (a Document's MENTIONS), before
    a later file claims it again; it was there before and after the batch,
    so the snapshot diff alone would not count it as added."""
    return {
        node for node, sources in before.items()
        if sources <= set(batch) and not sources <= after.get(node, set())
    }


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


def _read_text(path: Path) -> str:
    """A referrer candidate's text, or "" if it can't be read (it is then
    simply not a referrer)."""
    try:
        return read_source(path)
    except OSError:
        return ""


def _applied_provider_specs(
    engine: GraphEngine, repo_id: str, repo_root: Path
) -> tuple[bool, filesystem.FilesystemSpec | None, docs.DocsSpec | None]:
    """`_provider_specs`, or (False, None, None) while the schema is pending:
    the provider passes run only against the schema the graph was built with."""
    if schema_pending(engine, repo_id, repo_root):
        return False, None, None
    return _provider_specs(repo_root)


def _graph_files(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    specs: tuple[bool, filesystem.FilesystemSpec | None, docs.DocsSpec | None] | None = None,
) -> set[str]:
    """Every repo-relative file the graph has nodes for: the language keys
    (`list_indexed_files`), the sources claiming shared nodes
    (`list_claim_sources`, e.g. a Dockerfile's Container), plus the docs
    provider's paths and the filesystem provider's file-label paths. Folder
    nodes are never included. The provider paths are left out while the
    schema is pending, as the provider passes are. `specs` is
    `_applied_provider_specs`, when the caller already has it.
    """
    files = engine.list_indexed_files(repo_id) | engine.list_claim_sources(repo_id)
    ok, fs_spec, docs_spec = _applied_provider_specs(engine, repo_id, repo_root) if specs is None else specs
    if ok and docs_spec is not None:
        files |= engine.list_extracted_paths(repo_id, docs.EXTRACTOR, None)
    if ok and fs_spec is not None and fs_spec.file_label:
        files |= engine.list_extracted_paths(repo_id, filesystem.EXTRACTOR, [fs_spec.file_label])
    return files


def _gone_key(repo_root: Path, path: Path) -> str | None:
    """The repo-relative key of a deleted path, or None outside the repository.

    Only the parent is resolved. The missing leaf never is: on Windows,
    resolving a missing `Foo.py` can return an existing `foo.py`.
    """
    path = Path(path)
    try:
        candidate = path.parent.resolve() / path.name
    except OSError:
        candidate = path
    root = repo_root.resolve()
    if not is_within(candidate, root):
        return None
    return candidate.relative_to(root).as_posix()


def _listed(repo_root: Path, rel: str, listings: dict[str, set[str]]) -> bool:
    """Whether every component of `rel` appears exactly in its parent's
    `os.listdir` names. Listings are memoised in `listings`, keyed by the
    path string: a Windows `Path` compares case-insensitively."""
    current = repo_root
    for part in Path(rel).parts:
        names = listings.get(str(current))
        if names is None:
            try:
                names = set(os.listdir(current))
            except OSError:
                names = set()
            listings[str(current)] = names
        if part not in names:
            return False
        current = current / part
    return True


def _present(repo_root: Path, rel: str, listings: dict[str, set[str]] | None = None) -> bool:
    """Whether `rel` is an indexable file on disk, spelt case-exactly.

    Every component is checked, not just the leaf: after a Windows folder
    rename `Pkg` -> `pkg`, `Pkg/a.py` still opens.
    """
    listings = {} if listings is None else listings
    return _listed(repo_root, rel, listings) and _is_provider_file(repo_root, repo_root / rel)


def _holds_present_file(repo_root: Path, rel: str, listings: dict[str, set[str]]) -> bool:
    """Whether `rel` is, or is a folder holding, an indexable file on disk."""
    if not _listed(repo_root, rel, listings):
        return False
    folder = repo_root / rel
    if not folder.is_dir():
        return _is_provider_file(repo_root, folder)
    return bool(indexable_paths_under(repo_root, folder))


def _folders_above(rel: str) -> list[str]:
    """`a/b/c.py` -> [`a`, `a/b`]."""
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def remove_paths(
    engine: GraphEngine, repo_id: str, repo_root: Path, paths: set[Path], docs_path: str | None = None,
    mentions_enabled: bool = False,
) -> int:
    """Remove graph nodes whose provenance is one of these now-deleted paths.

    A path may be a file or a folder: each is keyed lexically (`_gone_key`)
    and expanded to every file the graph has at or below it (`_graph_files`,
    one query per call; the repository root itself expands to nothing).
    Anything present on disk, spelt case-exactly, is kept: a folder deleted
    and recreated in one batch keeps its recreated files. Language nodes are
    deleted per exact path; the provider passes get the exact paths, plus
    each deleted path that holds no indexable file on disk, so that its now
    empty folder nodes go. The tiered groups of other files to the deleted
    symbols' names are written again (`_relink_name_refs`): a farther node
    the deleted one hid is linked again. Deleting a file that mounted
    another file's Express router re-indexes that router file
    (`_stale_mounts`), with `docs_path` and `mentions_enabled` as
    index_paths takes them; a compose file's deletion re-owns the files
    under it (`_relink_owning_services`).

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID.
        repo_root: The repo's root path (paths outside it are skipped).
        paths: Files or folders that were deleted.

    Returns:
        Number of paths whose provenance was cleaned up.

    Raises:
        RepoRootUnavailable: The root itself is missing or unreadable (an
            unmount can report every file gone); nothing is removed.
    """
    check_repo_root(repo_root)
    keys = {key for p in paths if (key := _gone_key(repo_root, Path(p))) is not None and key != "."}
    if not keys:
        return 0
    specs = _applied_provider_specs(engine, repo_id, repo_root)
    graph_files = _graph_files(engine, repo_id, repo_root, specs)
    listings: dict[str, set[str]] = {}
    below = {f for f in graph_files if f in keys or any(folder in keys for folder in _folders_above(f))}
    removed = {f for f in below if not _present(repo_root, f, listings)}
    removed |= {key for key in keys if not _holds_present_file(repo_root, key, listings)}

    # The names and match keys of the symbols that go: a tiered pin's edges
    # to such a name depend on all its nodes (see _relink_name_refs). Only
    # C# writes the keys a tiered pin tests (other languages' heir keys
    # shadow nothing, _shadow_keys), so only a deleted .cs file is looked at.
    gone_cs = sorted(rel for rel in removed if rel.endswith(".cs"))
    shadow_names = _shadow_keys(
        (node[:4], node[4], None) for node in (engine.list_file_nodes(repo_id, gone_cs) if gone_cs else ())
    )
    # Router mounts the deleted files held (apis/routes.py).
    mounted = engine.route_mount_state(repo_id, sorted(removed)) if removed else {}
    # Must match the repo-relative key index_paths() writes for every
    # extractor (Module/Class/Function/Document nodes, and shared nodes'
    # `sources`).
    for rel in sorted(removed):
        engine.delete_nodes_by_source_file(repo_id, rel)
    if removed:
        engine.update_skipped_files(repo_id, drop=removed)
    if shadow_names:
        _relink_name_refs(engine, repo_id, set(), removed, shadow_names=shadow_names)
    if any(_is_compose(rel) for rel in removed):
        _relink_owning_services(engine, repo_id, set())
    if mounted:
        mounts = _Mounts(engine, repo_id)
        settle = _stale_mounts(mounts.reload(), mounts.table)
        if settle:
            index_paths(
                engine, repo_id, repo_root, {repo_root / rel for rel in settle}, docs_path=docs_path,
                mentions_enabled=mentions_enabled, sync_provider=False,
            )

    ok, fs_spec, docs_spec = specs
    if removed:
        if ok and fs_spec is not None:
            filesystem.sync_absent(
                engine, repo_id, repo_root, fs_spec, removed,
                is_indexable=lambda p: _is_provider_file(repo_root, p),
                is_ignored_dir=is_ignored_dir_name,
            )
        if ok and docs_spec is not None:
            _take_over_keys(engine, repo_id, repo_root, docs_spec, sorted(removed))
            # Nodes (still) at the deleted paths, with all their edges.
            engine.delete_extracted_nodes(repo_id, docs.EXTRACTOR, sorted(removed))
            _log_cache_stats()

    return len(removed)


def _docs_folder(repo_root: Path, docs_path: str | None) -> str | None:
    """The docs path as a repo-relative POSIX folder, or None when unset,
    outside the repository, or the repository root itself."""
    if not docs_path:
        return None
    root = repo_root.resolve()
    folder = (root / docs_path).resolve()
    if folder == root or not is_within(folder, root):
        return None
    return folder.relative_to(root).as_posix()


def prune_stale_files(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    docs_path: str | None = None,
    mentions_enabled: bool = False,
    force: bool = False,
) -> int:
    """Delete graph nodes whose file no longer exists on disk.

    The reconcile half of a rescan: full_scan re-indexes everything that's
    currently on disk, but nothing ever removed nodes for files that were
    deleted while the watcher was down (or that a watcher event missed).
    Without this, a rescan is additive-only and stale nodes linger forever.

    The diff is disk-vs-graph, with `_graph_files` as the graph side: file
    provenance (`source_file`/`file`), the sources claiming shared nodes
    (Container/Datastore/Endpoint, co-produced by several files), and schema
    providers' `path`, so a file only a provider represents (a `File` node
    for `logo.png`) is pruned too. `Commit`/`Repository` nodes are never
    touched. Every path the graph has that is no longer an indexable file on
    disk is routed through remove_paths, which handles the delete-vs-unclaim
    distinction per node.

    Module nodes with no file key at all, left by an older git-history sync,
    are deleted first, and so are Class nodes with no `file` or `source` (a
    C++ out-of-class method's stub before index format 9) and docs notes whose `source_file` is not under
    the docs path, left by an older scan that keyed a note by its bare
    filename (upgrade cleanups; a scan never makes either). The notes are
    written again under their repo-relative path by the scan or catch-up
    that follows.

    Files below a subfolder the walk couldn't list are kept, with a warning.
    A root that is missing or unreadable raises `RepoRootUnavailable`, and
    one holding no indexable file while the graph has files for it (a mount
    point with nothing mounted) raises `RepoRootEmpty` unless `force`; either
    way before anything is deleted.

    Returns the number of files pruned.
    """
    check_repo_root(repo_root)
    unreadable: list[str] = []
    on_disk = {rel for _, rel in _keyed_indexable_paths(repo_root, keep_ignored_targets=True, unreadable=unreadable)}
    # Checked again after the walk: a drive unmounted mid-walk lists as nothing.
    if "" in unreadable:
        raise RepoRootUnavailable(repo_root, "not readable")
    check_repo_root(repo_root)
    if not on_disk and not force:
        graph_files = _graph_files(engine, repo_id, repo_root)
        if graph_files:
            raise RepoRootEmpty(repo_root, repo_id, len(graph_files), gitignored=gitignore_hides_files(repo_root))
    bare = engine.delete_bare_modules(repo_id)
    if bare:
        logger.info("removed %d leftover module nodes with no file behind them from %s", bare, repo_id)
    stubs = engine.delete_bare_class_stubs(repo_id)
    if stubs:
        logger.info("removed %d unclaimed C++ class stubs from %s", stubs, repo_id)
    docs_folder = _docs_folder(repo_root, docs_path)
    # Skipped when the docs path is the repository root: every path is under
    # it, so nothing tells a bare filename key from a root-level note's path.
    if docs_folder is not None:
        misplaced = engine.delete_docs_notes_outside(repo_id, docs_folder)
        if misplaced:
            logger.info("removed %d docs notes keyed outside %s from %s", misplaced, docs_folder, repo_id)
    stale = _graph_files(engine, repo_id, repo_root) - on_disk
    if unreadable:
        # A folder that couldn't be listed says nothing about its files: keep them.
        logger.warning(
            "%s: %s not readable; files the graph has below %s kept until they can be read",
            repo_id, ", ".join(sorted(unreadable)), "them" if len(unreadable) > 1 else "it",
        )
        stale = {f for f in stale if not f.startswith(tuple(unreadable))}
    if not stale:
        return 0
    stale_paths = {repo_root / p for p in stale}
    return remove_paths(engine, repo_id, repo_root, stale_paths, docs_path, mentions_enabled)


#: How far before `since` a change stamp still makes a file due: FAT and SMB
#: timestamp granularity, and the gap between an edit and its event.
CATCH_UP_MARGIN_NS = 5_000_000_000


class CatchUp(NamedTuple):
    """What a catch-up did: files `index_paths` indexed (referrers included),
    files `prune_stale_files` pruned, files walked, files offered to
    `index_paths`, and how many of those the graph had no file for."""

    indexed: int
    pruned: int
    checked: int
    offered: int = 0
    unknown: int = 0


def _would_index(
    path: Path,
    rel: str,
    docs_root: Path | None,
    mentions_enabled: bool,
    specs: tuple[bool, filesystem.FilesystemSpec | None, docs.DocsSpec | None],
    resolved: Path | None = None,
) -> bool:
    """Whether `index_paths` would write anything for this file: a built-in
    extractor routes it (`_routes`, as `_index_single_path` does), or a
    declared schema provider represents it. `resolved` is the path resolved,
    when the caller already has it."""
    if resolved is None:
        try:
            resolved = path.resolve()
        except OSError:
            return False  # index_paths skips it too
    if _routes(resolved, docs_root, mentions_enabled):
        return True
    return _provider_represents(rel, specs)


def _provider_represents(
    rel: str, specs: tuple[bool, filesystem.FilesystemSpec | None, docs.DocsSpec | None]
) -> bool:
    """Whether a declared schema provider writes a node for this file."""
    ok, fs_spec, docs_spec = specs
    if ok and fs_spec is not None and fs_spec.file_label:
        return True
    return ok and docs_spec is not None and any(docs.selects(t, rel) for t in docs_spec.types)


def _extracted_files(engine: GraphEngine, repo_id: str) -> set[str]:
    """The files the graph has extraction nodes for: `_graph_files` without the
    schema providers' paths."""
    return engine.list_indexed_files(repo_id) | engine.list_claim_sources(repo_id)


def _skip_marks(engine: GraphEngine, repo_id: str) -> dict[str, list]:
    """The files `index_paths` left out for their content, by repo-relative
    path: [reason, the size limit, the file's change stamp]. A full scan sets
    them afresh; a batch adds the ones it skips. Catch-up does not offer such
    a file again (nor read it) until the limit or the stamp differs."""
    return engine.read_skipped_files(repo_id)


def _stamp_or_none(path: Path) -> int | None:
    """`_change_stamp_ns` of the file, or None when it can't be stat'ed."""
    try:
        return _change_stamp_ns(os.stat(path))
    except OSError:
        return None


def _docs_note_files(engine: GraphEngine, repo_id: str) -> set[str]:
    return engine.list_docs_note_files(repo_id)


def _is_unindexed_note(path: Path, docs_root: Path | None, mentions_enabled: bool) -> bool:
    """Whether a file no docs note holds the key of is a docs note: routed to
    the docs extractor and with note front matter. Read only for Markdown
    under the docs path that isn't a note in the graph, so a plain page there
    is read each catch-up and a note never is."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if "docs" not in _routes(resolved, docs_root, mentions_enabled):
        return False
    return bool(DocsExtractor("").extract_from_source(_read_text(resolved), resolved.name).docs)


def _change_stamp_ns(st: os.stat_result) -> int:
    """When a file last changed, by any stamp. The second term catches a file
    whose mtime was preserved (`cp -p`, tar, unzip, `rsync -a`): its ctime on
    POSIX, its creation time on Windows (where `st_ctime` is deprecated)."""
    second = st.st_birthtime_ns if sys.platform == "win32" else st.st_ctime_ns
    return max(st.st_mtime_ns, second)


def catch_up(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    since: datetime,
    docs_path: str | None = None,
    mentions_enabled: bool = False,
    force: bool = False,
    while_waiting: Callable[[], bool] | None = None,
) -> CatchUp:
    """Bring the graph up to date with changes made since `since`, while
    nothing was watching: an incremental `full_scan`.

    Files gone from disk are pruned first (provider-only ones included, and
    through `remove_paths`, so docs key takeover applies). Then a file that
    `index_paths` would write something for is indexed when the graph has no
    file for its key, or when any of its change stamps is at or after `since`
    less `CATCH_UP_MARGIN_NS`. A file nothing would index (a `.txt` with no
    filesystem type declared) is never offered.

    An index older than `INDEX_FORMAT` gets a `full_scan` instead, which
    upgrades it. It waits for its turn (`_take_full_scan_lock`, calling
    `while_waiting`) before it checks the format again, so a scan that
    another job (the schema rescan, or another process's `devgraph rescan`:
    see `_claim_full_scan`) ran meanwhile is not repeated. Last, a changed resolver configuration re-indexes its
    language's files (sync_resolver_config).

    A missing, unreadable or apparently unmounted root is refused as
    `prune_stale_files` refuses it (`force` as there), before any change.
    """
    check_repo_root(repo_root)
    if index_outdated(engine, repo_id):
        # The repository is registered (the agent checks): a graph that lost
        # its Repository node (wiped) gets it back, or full_scan would take
        # it for removed and scan nothing.
        engine.upsert_repository(repo_id, repo_id, str(repo_root))
        with full_scan_turn(repo_id, while_waiting, engine):
            if index_outdated(engine, repo_id):
                indexed = full_scan(
                    engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled, force=force
                )
                return CatchUp(indexed=indexed, pruned=0, checked=0, offered=0, unknown=0)
    forget_manifests()  # build manifests may have come or gone while nothing watched
    pruned = prune_stale_files(
        engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled, force=force
    )
    specs = _applied_provider_specs(engine, repo_id, repo_root)
    known = _graph_files(engine, repo_id, repo_root, specs)
    docs_root = (repo_root / docs_path).resolve() if docs_path else None
    # A file the docs extractor routes counts as known only through a note
    # holding its key: its mentions Document holds the same key, and a note
    # an upgrade cleanup just deleted must be written again.
    notes = _docs_note_files(engine, repo_id) if docs_root is not None else set()
    cutoff = int(since.timestamp() * 1_000_000_000) - CATCH_UP_MARGIN_NS
    walked = _keyed_indexable_paths(repo_root)
    due: set[Path] = set()
    unknown = 0
    # A file skipped for its content keeps a provider's File node, so for a
    # file an extractor reads and that has a skip mark, only extraction nodes
    # make it known. It is not offered again while its mark still holds (see
    # `_skip_marks`).
    extracted = _extracted_files(engine, repo_id)
    marks = _skip_marks(engine, repo_id)
    max_bytes = _max_file_bytes()
    walked_rels = set()
    for path, rel in walked:
        walked_rels.add(rel)
        try:
            resolved = path.resolve()
        except OSError:
            continue  # index_paths skips it too
        if not _would_index(path, rel, docs_root, mentions_enabled, specs, resolved):
            continue
        routed = bool(_routes(resolved, docs_root, mentions_enabled))
        marked = routed and rel in marks and rel not in extracted
        if rel not in known or marked or (
            rel not in notes and _is_unindexed_note(path, docs_root, mentions_enabled)
        ):
            mark = marks.get(rel)
            if mark is not None and rel not in extracted and mark[1] == max_bytes and _stamp_or_none(path) == mark[2]:
                continue  # left out under this limit, and unchanged since
            due.add(path)
            unknown += 1
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue  # gone since the walk; the next catch-up prunes it
        # A file extracted under a higher limit is offered so it can be left out.
        if _change_stamp_ns(st) >= cutoff or (routed and rel in extracted and st.st_size > max_bytes):
            due.add(path)
    if marks.keys() - walked_rels:
        engine.update_skipped_files(repo_id, drop=marks.keys() - walked_rels)
    indexed = (
        index_paths(engine, repo_id, repo_root, due, docs_path=docs_path, mentions_enabled=mentions_enabled)
        if due
        else 0
    )
    indexed += sync_resolver_config(
        engine, repo_id, repo_root, None, docs_path=docs_path, mentions_enabled=mentions_enabled
    )
    return CatchUp(indexed=indexed, pruned=pruned, checked=len(walked), offered=len(due), unknown=unknown)


def sync_resolver_config(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    touched: set[Path] | None,
    docs_path: str | None = None,
    mentions_enabled: bool = False,
) -> int:
    """Re-index every graph file of a language whose resolver configuration
    (tsconfig/jsconfig, go.mod/go.work, Cargo.toml, C# projects and
    `global using`s, pyproject.toml; see resolver_config.fingerprints)
    changed since the graph was built, and record the new fingerprints.

    `touched` are a live batch's changed and deleted paths: nothing is
    checked unless one of them can change the configuration (named like a
    configuration file, read by the last fingerprint, a folder holding
    one, or a .cs file whose head now holds a `global using`;
    resolver_config.touches_config). None (a catch-up) always checks.
    The fingerprints are taken before the re-index reads any file and
    recorded only once it has succeeded, so a batch cut short is redone.
    Returns the number of files re-indexed.
    """
    stored = engine.read_resolver_config(repo_id)
    if touched is not None:
        inputs = {rel for entry in stored.values() for rel in entry.get("inputs", [])}
        rels = {rel for path in touched if (rel := _repo_relative(repo_root, Path(path))) is not None}
        if not any(touches_config(rel, inputs, repo_root) for rel in rels):
            return 0
    current = fingerprints(repo_root)
    changed = [
        lang for lang, entry in sorted(current.items())
        if entry["fingerprint"] != (stored.get(lang) or {}).get("fingerprint")
    ]
    indexed = 0
    if changed:
        suffixes = tuple(suffix for lang in changed for suffix in LANGUAGE_SUFFIXES[lang])
        files = {repo_root / rel for rel in engine.list_indexed_files(repo_id) if rel.endswith(suffixes)}
        logger.info("%s: resolver configuration changed (%s); re-indexing %d files", repo_id,
                    ", ".join(changed), len(files))
        if files:
            indexed = index_paths(
                engine, repo_id, repo_root, files, docs_path=docs_path, mentions_enabled=mentions_enabled,
                resolver=current,
            )
    if current != stored:
        engine.write_resolver_config(repo_id, current)
    return indexed


#: Held for each full scan, so a process runs one at a time (see full_scan).
#: Reentrant: catch_up takes it before it checks the index format again,
#: then full_scan takes it once more.
_FULL_SCAN_LOCK = threading.RLock()

#: How often a full scan waiting its turn calls its `while_waiting`.
FULL_SCAN_POLL_S = 1.0


#: This process's name on a repository's full-scan claim (see
#: `_claim_full_scan`): pid and host, and a random part so a reused pid is not
#: taken for it.
_SCAN_OWNER = f"{os.getpid()}@{socket.gethostname()}/{uuid.uuid4().hex[:8]}"
_SCAN_OWNER_PARTS = re.compile(r"^(\d+)@(.+)/[0-9a-f]+$")

#: How often a held full-scan claim's heartbeat is refreshed, and how old a
#: heartbeat may be before another process takes the claim over (the holder
#: hung, or crashed on another host).
FULL_SCAN_HEARTBEAT_S = 15.0
FULL_SCAN_STALE_S = 90.0


class FullScanClaimLost(BatchAborted):
    """Another process took a running full scan's claim over (its heartbeat
    looked stale): the scan stops before it stamps the index format."""


class _Claim:
    """A full-scan claim this process holds: how deeply it is taken, the
    heartbeat's stop, set once the claim is lost, and the scanning thread."""

    def __init__(self) -> None:
        self.depth = 1
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.get_ident()


#: repo_id -> the claim this process holds; changed only while holding
#: `_FULL_SCAN_LOCK`.
_scan_claims: dict[str, _Claim] = {}

#: `_claim_full_scan` outcomes besides another process's name.
_HELD = object()
_NO_REPOSITORY = object()


def _acquire_full_scan_lock(repo_id: str, while_waiting: Callable[[], bool] | None, log: bool) -> None:
    if _FULL_SCAN_LOCK.acquire(blocking=False):
        return
    if log:
        logger.info("%s: waiting for another repository's full scan to finish", repo_id)
    while not _FULL_SCAN_LOCK.acquire(timeout=FULL_SCAN_POLL_S):
        if while_waiting is not None and not while_waiting():
            raise FullScanCancelled(f"{repo_id}: the full scan waiting its turn was called off")


def _take_full_scan_lock(
    repo_id: str, while_waiting: Callable[[], bool] | None, engine: GraphEngine | None = None
) -> bool:
    """Take `_FULL_SCAN_LOCK`, logging a wait. While another scan holds it,
    `while_waiting` runs every `FULL_SCAN_POLL_S` (the watcher's
    `yield_batch_lock`: the repository's live batches run meanwhile); False
    from it calls the scan off (`FullScanCancelled`).

    With `engine`, then also the repository's claim across processes
    (`_claim_full_scan`). While another process holds that, the lock is let
    go between tries, so at the outermost take this process's scans of other
    repositories run meanwhile. False, holding nothing, when the repository
    has no Repository node (removed): nothing to scan."""
    first = True
    while True:
        _acquire_full_scan_lock(repo_id, while_waiting, log=first)
        if engine is None:
            return True
        try:
            outcome = _claim_full_scan(engine, repo_id)
        except BaseException:
            _FULL_SCAN_LOCK.release()
            raise
        if outcome is _HELD:
            return True
        _FULL_SCAN_LOCK.release()
        if outcome is _NO_REPOSITORY:
            return False
        if first:
            logger.info("%s: waiting for the full scan process %s is running", repo_id, outcome)
            first = False
        if while_waiting is not None and not while_waiting():
            raise FullScanCancelled(f"{repo_id}: the full scan waiting for another process's was called off")
        time.sleep(FULL_SCAN_POLL_S)


def _release_full_scan_lock(repo_id: str, engine: GraphEngine | None = None) -> None:
    """Undo one `_take_full_scan_lock` that returned True."""
    try:
        if engine is not None:
            _release_full_scan_claim(engine, repo_id)
    finally:
        _FULL_SCAN_LOCK.release()


def _claim_full_scan(engine: GraphEngine, repo_id: str) -> object:
    """Try once to hold the repository's full-scan claim on its Repository
    node, so one full scan of it runs at a time across processes: `devgraph
    rescan` and the agent's catch-up or index upgrade no longer scan it
    together (their writes deadlocked, and the work doubled). The claim is
    apart from the index format, which a scan unstamps while it runs (see
    full_scan).

    Reentrant in this process. Returns `_HELD`, `_NO_REPOSITORY`, or the
    name of the other process holding it with a fresh heartbeat. A holder
    on this host whose process is gone, or whose heartbeat is older than
    `FULL_SCAN_STALE_S`, is taken over. A thread refreshes the heartbeat
    every `FULL_SCAN_HEARTBEAT_S` until release, and marks the claim lost
    if another process took it over (`_check_full_scan_claim`)."""
    held = _scan_claims.get(repo_id)
    if held is not None:
        held.depth += 1
        return _HELD
    granted, holder = engine.claim_full_scan(repo_id, _SCAN_OWNER, FULL_SCAN_STALE_S)
    if not granted and holder is not None and _holder_gone(holder):
        logger.warning("%s: taking over the full scan of %s, whose process has ended", repo_id, holder)
        granted, holder = engine.claim_full_scan(repo_id, _SCAN_OWNER, FULL_SCAN_STALE_S, replace=holder)
    if not granted:
        return holder if holder is not None else _NO_REPOSITORY
    claim = _Claim()
    threading.Thread(
        target=_beat_full_scan, args=(engine, repo_id, claim), name=f"devgraph-scan-heartbeat-{repo_id}", daemon=True
    ).start()
    _scan_claims[repo_id] = claim
    return _HELD


def _holder_gone(holder: str) -> bool:
    """Whether a claim's holder is a process on this host that has ended."""
    from devgraph.agent.lifecycle import pid_is_running  # the agent package imports dispatch

    parts = _SCAN_OWNER_PARTS.match(holder)
    if parts is None or parts.group(2) != socket.gethostname():
        return False
    pid = int(parts.group(1))
    return pid != os.getpid() and not pid_is_running(pid)


def _beat_full_scan(engine: GraphEngine, repo_id: str, claim: _Claim) -> None:
    while not claim.stop.wait(FULL_SCAN_HEARTBEAT_S):
        try:
            held = engine.beat_full_scan(repo_id, _SCAN_OWNER)
        except EngineClosed:  # closing: it released the claim (GraphEngine.close)
            return
        except Exception:
            logger.warning(
                "%s: the full scan's heartbeat failed; another process may take the scan over after %.0f s",
                repo_id, FULL_SCAN_STALE_S, exc_info=True,
            )
            continue
        if not held:
            claim.lost.set()
            logger.warning(
                "%s: another process took this full scan over (its heartbeat looked stale), or the repository "
                "was removed; stopping it before it stamps the index", repo_id,
            )
            return


def _check_full_scan_claim(repo_id: str) -> None:
    """Raise `FullScanClaimLost` in the thread running the repository's full
    scan once its claim is lost; anywhere else (a live batch), nothing."""
    claim = _scan_claims.get(repo_id)
    if claim is not None and claim.thread == threading.get_ident() and claim.lost.is_set():
        raise FullScanClaimLost(f"{repo_id}: the full scan's claim was taken over by another process")


def _release_full_scan_claim(engine: GraphEngine, repo_id: str) -> None:
    claim = _scan_claims[repo_id]
    if claim.depth > 1:
        claim.depth -= 1
        return
    del _scan_claims[repo_id]
    claim.stop.set()  # a beat still running only refreshes a claim of this process's
    try:
        engine.release_full_scan(repo_id, _SCAN_OWNER)
    except EngineClosed:
        pass  # it released the claim as it closed
    except Exception:
        logger.warning(
            "%s: could not release the full-scan claim; it lapses in %.0f s", repo_id, FULL_SCAN_STALE_S,
            exc_info=True,
        )


def full_scan_elsewhere(engine: GraphEngine, repo_id: str) -> str | None:
    """The other process running a full scan of the repository now (its
    claim's heartbeat is fresh), or None."""
    holder = engine.full_scan_holder(repo_id, FULL_SCAN_STALE_S)
    return holder if holder != _SCAN_OWNER else None


@contextmanager
def full_scan_turn(
    repo_id: str, while_waiting: Callable[[], bool] | None = None, engine: GraphEngine | None = None
):
    """Hold this process's full-scan turn (`_take_full_scan_lock`) for the
    block, and with `engine` the repository's claim across processes: a
    caller that scans only if the index is still outdated checks that inside
    it, after the wait, and its full_scan then runs at once. A repository
    with no Repository node holds nothing; its full_scan scans nothing."""
    taken = _take_full_scan_lock(repo_id, while_waiting, engine)
    try:
        yield
    finally:
        if taken:
            _release_full_scan_lock(repo_id, engine)


def full_scan_running() -> bool:
    """Whether another thread of this process is running a full scan now."""
    if not _FULL_SCAN_LOCK.acquire(blocking=False):
        return True
    _FULL_SCAN_LOCK.release()
    return False


def full_scan(
    engine: GraphEngine,
    repo_id: str,
    repo_root: Path,
    docs_path: str | None = None,
    mentions_enabled: bool = False,
    force: bool = False,
    skipped: dict[str, str] | None = None,
    while_waiting: Callable[[], bool] | None = None,
) -> int:
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

    The docs read cache forgets the repository first: the scan reads every
    docs file again, and a schema change is applied this way.

    A missing, unreadable or apparently unmounted root is refused as
    `prune_stale_files` refuses it (`force` as there), before any change.

    Files a .gitignore ignores are never walked; files too large, binary, or
    minified or generated are not extracted (see `index_paths`). Those are
    logged in one line and, when `skipped` is given, collected there by
    repo-relative path with why.

    The resolver configuration's fingerprints (see sync_resolver_config) are
    taken before any file is read and recorded at the end.

    The index format is unstamped (0) for the scan's duration and restamped
    at its end, so a scan cut short (the agent shutting down mid-rescan, say)
    leaves the index outdated and the next catch-up redoes it in full: the
    schema is recorded as applied before the files are written.

    One full scan runs at a time in this process (`_FULL_SCAN_LOCK`): after
    an `INDEX_FORMAT` change every repository's start catch-up is a full
    scan, and run together their memory peaks added up. A scan waits for
    the one running, calling `while_waiting` meanwhile (see
    `_take_full_scan_lock`); live batches never take the lock. Across
    processes, one full scan of the repository runs at a time
    (`_claim_full_scan`): a `devgraph rescan` and the agent wait for each
    other's. A repository with no Repository node (removed) is not scanned,
    and not created again: callers that register one create it first.
    """
    if not _take_full_scan_lock(repo_id, while_waiting, engine):
        logger.info("%s: not in the graph (removed); nothing to scan", repo_id)
        return 0
    try:
        check_repo_root(repo_root)
        docs_cache.forget(repo_root)
        forget_manifests()
        # The shared-claim records the writes below unclaim through are rebuilt
        # from the shared nodes first, so a full scan heals any that drifted.
        engine.rebuild_claim_records(repo_id)
        prune_stale_files(engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled, force=force)
        engine.set_index_format(repo_id, 0)
        resolver = fingerprints(repo_root)  # before extraction reads the configuration
        all_files = _indexable_paths(repo_root)
        applied, applied_docs = _apply_project_schema(engine, repo_id, repo_root)
        left_out: dict[str, str] = {} if skipped is None else skipped
        engine.update_skipped_files(repo_id, replace=True)  # every file is judged again below
        indexed = index_paths(
            engine, repo_id, repo_root, all_files, docs_path=docs_path, mentions_enabled=mentions_enabled,
            sync_provider=False,  # applied just above
            relink_outside=False,  # every file is in the batch
            skipped=left_out,
            resolver=resolver,
        )
        if left_out:
            counts = Counter(left_out.values())
            logger.info(
                "%s: skipped %d file(s) (%s); see `devgraph rescan %s`",
                repo_id, len(left_out), ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items())), repo_id,
            )
        if applied:
            _sync_docs_edges(engine, repo_id, repo_root, applied_docs)
        engine.write_resolver_config(repo_id, resolver)
        _check_full_scan_claim(repo_id)  # taken over meanwhile: that scan stamps it
        engine.set_index_format(repo_id, INDEX_FORMAT)
        return indexed
    finally:
        _release_full_scan_lock(repo_id, engine)


def _index_containerfile(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> ExtractionResult:
    content = read_source(path)
    result = ContainerExtractor(repo_id).extract_from_containerfile(content, rel_path)
    _upsert_container_result(engine, repo_id, rel_path, result)
    return result


def _index_compose_file(engine: GraphEngine, repo_id: str, path: Path, rel_path: str) -> ExtractionResult:
    content = read_source(path)
    result = ContainerExtractor(repo_id).extract_from_compose_file(content, rel_path)
    _upsert_container_result(engine, repo_id, rel_path, result)
    return result


def _relationship_dict(rel, repo_id: str, origin: str) -> dict:
    """Canonical upsert_relationships dict from any extractor's
    source_label/source_name/relationship_type/target_label/target_name
    Relationship dataclass shape (docs/apis/containers/datastores all share
    it, distinct from the python extractor's from_/to_/rel_type naming).

    from_file/to_file use getattr with a None default since only
    containers/extractor.py's Relationship carries them (Service is the one
    label from this family that's file-scoped) -- every other module's
    Relationship dataclass predates that field and stays bare-name matched.

    `origin` is the repo-relative path of the file that wrote the edge, added
    to the edge's `origins` (see GraphRelationship).
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
        "to_owner": getattr(rel, "to_owner", None),
        "origin": origin,
    }


def _upsert_container_result(engine: GraphEngine, repo_id: str, rel_path: str, result) -> None:
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
        # A build context the service no longer has is cleared (a None removes it).
        {"label": "Service", "repo_id": repo_id, "name": s.name, "properties": {"build_context": None, **s.properties}}
        for s in result.services
    ]
    # A replace, so a service or stage the file no longer declares is
    # retracted; a Container it still runs is re-claimed in place.
    rels = [_relationship_dict(rel, repo_id, rel_path) for rel in result.relationships]
    engine.replace_file_nodes(repo_id, rel_path, nodes, rels)


def _datastore_nodes(repo_id: str, rel_path: str, content: str, language: str = "py") -> list[dict]:
    """The datastore nodes a source file's clients claim: a Python file's
    (DatastoreExtractor), another language's (`client_datastores`, by its
    `_routes` name)."""
    if language == "py":
        datastores = DatastoreExtractor(repo_id).extract_from_source(content, rel_path).datastores
    else:
        datastores = client_datastores(content, language, repo_id, rel_path)
    return [{"label": ds.datastore_type, "repo_id": repo_id, "name": ds.name, "properties": ds.properties} for ds in datastores]


def _is_compose(rel_path: str) -> bool:
    """Whether a repo-relative path is a compose file (`_routes`)."""
    return posixpath.basename(rel_path).lower() in _COMPOSE_NAMES


def _relink_owning_services(engine: GraphEngine, repo_id: str, skip: set[str]) -> None:
    """Re-derive the owning-service edges of the files outside `skip` after a
    compose change: each claimed Endpoint's `CALLS` and datastore's `USES`
    (`_endpoint_service_relationships`, `_datastore_service_relationships`)
    are a function of its claiming file's path and the repository's Services'
    build contexts, so a file the change gives another owning Service (or
    none) is unclaimed from its old edges and writes its new ones, as a
    fresh scan would have them. Read from the graph: no file is read. The
    files in `skip` (the batch's) wrote theirs already."""
    services = _load_services_with_build_context(engine, repo_id)
    claims, edges = engine.owning_service_state(repo_id)
    expected = set()
    for label, name, sources in claims:
        for source in sources:
            owner = _match_owning_service(services, source)
            if source not in skip and source.endswith(SERVICE_API_SUFFIXES) and owner is not None:
                expected.add((label, name, owner, services[owner][1], source))
    written = {
        (edge["label"], edge["name"], edge["service"], edge["service_file"], origin)
        for edge in edges for origin in edge["origins"] or [] if origin not in skip
    }
    engine.unclaim_owning_service_edges(repo_id, [
        dict(zip(("label", "name", "service", "service_file", "origin"), row)) for row in sorted(written - expected)
    ])
    rels = []
    for label, name, service, service_file, origin in sorted(expected - written):
        if label == "Endpoint":
            rels.append({
                "from_label": "Endpoint", "from_name": name, "rel_type": "CALLS", "to_label": "Service",
                "to_name": service, "to_file": service_file, "repo_id": repo_id, "properties": {}, "origin": origin,
            })
        else:
            rels.append({
                "from_label": "Service", "from_name": service, "from_file": service_file, "rel_type": "USES",
                "to_label": label, "to_name": name, "repo_id": repo_id, "properties": {}, "origin": origin,
            })
    if rels:
        engine.upsert_relationships(rels)


def _load_services_with_build_context(engine: GraphEngine, repo_id: str) -> dict[str, tuple[str, str]]:
    """{Service name -> (build_context, file)} for every repo Service that has one.

    `file` (the compose file the Service was declared in) is carried
    alongside build_context so _owning_service_relationships can set
    from_file/to_file on the edges it emits -- Service is now file-scoped
    (see schema.py's FILE_SCOPED_LABELS), so a MATCH by bare name alone
    would hit every same-named Service across every compose file again.

    Loaded once per index_paths batch (not once per file) since Services are
    only written earlier in the same batch by the Containerfile/compose
    branches, so the set can't change again mid-batch.
    """
    results = engine.run_cypher(
        "MATCH (s:Service) USING INDEX SEEK s:Service(repo_id, name) "
        "WHERE s.repo_id = $repo_id AND s.name IS NOT NULL AND s.build_context IS NOT NULL "
        "RETURN s.name as name, s.build_context as build_context, s.file as file",
        {"repo_id": repo_id},
    )
    # Sorted so the result doesn't depend on the order Neo4j returns rows:
    # _match_owning_service keeps the first of several Services sharing a
    # build_context, and a name declared in two compose files keeps the last.
    results = sorted(results, key=lambda row: (row["name"], row["file"] or ""))
    return {row["name"]: (row["build_context"], row["file"]) for row in results}


def _match_owning_service(services: dict[str, tuple[str, str]], rel_path: str) -> str | None:
    """Find the Service whose build_context is the longest prefix of rel_path's
    directory; a context of "" (the repository root) holds every file."""
    file_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""

    best_match: str | None = None
    best_match_len = -1
    for name, (context, _file) in services.items():
        if context == "" or file_dir == context or file_dir.startswith(context + "/"):
            if len(context) > best_match_len:
                best_match = name
                best_match_len = len(context)

    return best_match


def _owning_service_relationships(
    repo_id: str, rel_path: str, content: str, services: dict[str, tuple[str, str]], endpoints: list[str],
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
    unrelated top-level Service with no build_context. Each edge's `origin`
    is the Python file, so a compose file's re-index leaves it alone.
    `endpoints` are the file's Endpoint names as the batch wrote them (a
    router's named by the mounts reaching it, apis/routes.py).
    """
    owning_service = _match_owning_service(services, rel_path)
    if owning_service is None:
        return []
    _owning_context, owning_service_file = services[owning_service]

    datastore_result = DatastoreExtractor(repo_id).extract_from_source(content, rel_path)
    datastores = [(ds.datastore_type, ds.name) for ds in datastore_result.datastores]
    return _datastore_service_relationships(repo_id, rel_path, datastores, services) + _endpoint_service_relationships(
        repo_id, rel_path, endpoints, services
    )


def _datastore_service_relationships(
    repo_id: str, rel_path: str, datastores: list[tuple[str, str]], services: dict[str, tuple[str, str]]
) -> list[dict]:
    """USES from the compose Service that owns the file to each of its
    datastores ((label, name)), as `_owning_service_relationships` writes them."""
    owning_service = _match_owning_service(services, rel_path)
    if owning_service is None:
        return []
    return [
        {
            "from_label": "Service",
            "from_name": owning_service,
            "from_file": services[owning_service][1],
            "rel_type": "USES",
            "to_label": label,
            "to_name": name,
            "repo_id": repo_id,
            "properties": {},
            "origin": rel_path,
        }
        for label, name in datastores
    ]


def _endpoint_service_relationships(
    repo_id: str, rel_path: str, endpoints: list[str], services: dict[str, tuple[str, str]]
) -> list[dict]:
    """CALLS from each of the file's Endpoints (by name) to the compose
    Service that owns the file (`_owning_service_relationships`)."""
    owning_service = _match_owning_service(services, rel_path)
    if owning_service is None:
        return []
    return [
        {
            "from_label": "Endpoint",
            "from_name": endpoint_id,
            "rel_type": "CALLS",
            "to_label": "Service",
            "to_name": owning_service,
            "to_file": services[owning_service][1],
            "repo_id": repo_id,
            "properties": {},
            "origin": rel_path,
        }
        for endpoint_id in endpoints
    ]


def _api_nodes_and_rels(
    repo_id: str, rel_path: str, content: str, roots: tuple[str, ...] = (),
) -> tuple[list[dict], list[dict], dict[str, list[str] | None]]:
    """A Python file's Endpoint and handler stub nodes, its IMPLEMENTS edges,
    and its Module's `route_mounts` and `route_exports` (apis/routes.py)."""
    result = APIExtractor(repo_id).extract_from_source(content, rel_path, roots)
    nodes = [
        {"label": "Endpoint", "repo_id": repo_id, "name": e.name, "properties": e.properties}
        for e in result.endpoints
    ] + [
        {"label": "Function", "repo_id": repo_id, "name": f.name, "properties": f.properties}
        for f in result.functions
    ]
    rels = [_relationship_dict(rel, repo_id, rel_path) for rel in result.relationships]
    return nodes, rels, {"route_mounts": result.route_mounts, "route_exports": result.route_exports}
