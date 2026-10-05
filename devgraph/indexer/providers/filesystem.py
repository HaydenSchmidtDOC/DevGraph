"""Filesystem provider: nodes for a repository's own files and folders.

Fills the node types a project schema sources from the filesystem (see
`NodeSource` in devgraph/config/project_schema.py): one file-kind node per
indexable file, one folder-kind node per ancestor directory ("." is the
repository root), and, if declared, an edge from each child to its parent
folder.

Every node is keyed by its repo-relative path, written to both `path` (the
declared key) and `name` (what the engine MERGEs and matches edges on), so
the engine's (repo_id, name) MERGE is exactly a merge on the declared key.
Nodes are tagged `extractor = "filesystem"` and carry none of the built-in
provenance properties, so this module alone decides when they go away.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from devgraph.config.project_schema import EffectiveSchema

EXTRACTOR = "filesystem"
ROOT_PATH = "."


@dataclass(frozen=True)
class FilesystemSpec:
    file_label: str | None
    folder_label: str | None
    relationship: str | None
    child_labels: frozenset[str]


def filesystem_spec(effective: EffectiveSchema) -> FilesystemSpec | None:
    """What the schema asks this provider to build, or None if nothing."""
    file_label = folder_label = None
    for node_type in effective.node_types:
        if node_type.source is None or node_type.source.provider != EXTRACTOR:
            continue
        if node_type.source.kind == "file":
            file_label = node_type.label
        else:
            folder_label = node_type.label
    if file_label is None and folder_label is None:
        return None
    relationship = next((r for r in effective.relationships if r.provider == EXTRACTOR), None)
    return FilesystemSpec(
        file_label=file_label,
        folder_label=folder_label,
        relationship=relationship.type if relationship else None,
        child_labels=frozenset(relationship.from_labels) if relationship else frozenset(),
    )


def ancestors(path: str) -> list[str]:
    """Folders containing `path`, nearest first, ending at the root "."."""
    out = []
    parent = PurePosixPath(path).parent
    while str(parent) != ROOT_PATH:
        out.append(str(parent))
        parent = parent.parent
    out.append(ROOT_PATH)
    return out


def _node(label: str, repo_id: str, path: str) -> dict[str, Any]:
    return {"label": label, "repo_id": repo_id, "name": path, "properties": {"path": path, "extractor": EXTRACTOR}}


def _edge(spec: FilesystemSpec, child_label: str, child: str, repo_id: str) -> dict[str, Any]:
    return {
        "from_label": child_label,
        "from_name": child,
        "rel_type": spec.relationship,
        "to_label": spec.folder_label,
        "to_name": ancestors(child)[0],
        "repo_id": repo_id,
        "properties": {},
    }


def build_graph(spec: FilesystemSpec, repo_id: str, files: set[str]) -> tuple[list[dict], list[dict]]:
    """Nodes and parent edges for these repo-relative files and their folders."""
    ordered = sorted(files)
    folders = sorted({folder for path in ordered for folder in ancestors(path)})
    nodes: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    if spec.file_label:
        nodes += [_node(spec.file_label, repo_id, path) for path in ordered]
    if spec.folder_label:
        nodes += [_node(spec.folder_label, repo_id, folder) for folder in folders]
    if spec.relationship and spec.folder_label:
        if spec.file_label in spec.child_labels:
            rels += [_edge(spec, spec.file_label, path, repo_id) for path in ordered]
        if spec.folder_label in spec.child_labels:
            rels += [_edge(spec, spec.folder_label, folder, repo_id) for folder in folders if folder != ROOT_PATH]
    return nodes, rels


def sync_present(engine: Any, repo_id: str, spec: FilesystemSpec, files: set[str]) -> None:
    """Upsert these existing files, their ancestor folders and the edges between them."""
    if not files:
        return
    nodes, rels = build_graph(spec, repo_id, files)
    engine.upsert_nodes(nodes)
    engine.upsert_relationships(rels)


def sync_absent(
    engine: Any,
    repo_id: str,
    repo_root: Path,
    spec: FilesystemSpec,
    paths: set[str],
    is_indexable: Callable[[Path], bool],
    is_ignored_dir: Callable[[str], bool],
) -> None:
    """Remove nodes at (or below) deleted paths, then folders now empty on disk.

    "Empty" means no indexable file remains anywhere below the folder,
    decided from the disk rather than the graph so a missed earlier event
    can't keep a dead folder alive.
    """
    if not paths:
        return
    engine.delete_extracted_nodes(repo_id, EXTRACTOR, sorted(paths))
    if not spec.folder_label:
        return
    candidates = {folder for path in paths for folder in ancestors(path) if folder != ROOT_PATH}
    dead = sorted(f for f in candidates if not _holds_indexable_file(repo_root / f, is_indexable, is_ignored_dir))
    if dead:
        engine.delete_extracted_nodes(repo_id, EXTRACTOR, dead)


def _holds_indexable_file(
    folder: Path, is_indexable: Callable[[Path], bool], is_ignored_dir: Callable[[str], bool]
) -> bool:
    """Whether any indexable file lives below `folder`, never descending into ignored directories."""
    if not folder.is_dir():
        return False
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not is_ignored_dir(d)]
        if any(is_indexable(Path(dirpath) / name) for name in filenames):
            return True
    return False


def reconcile(engine: Any, repo_id: str, spec: FilesystemSpec | None, files: set[str]) -> int:
    """Prune this provider's nodes that the current schema and disk don't produce.

    With no spec (the schema declares no filesystem types, or the file is
    gone) every filesystem node in the repository is pruned.
    """
    keep: list[str] = []
    if spec is not None:
        nodes, _ = build_graph(spec, repo_id, files)
        keep = [f"{n['label']}:{n['name']}" for n in nodes]
    return engine.prune_extracted_nodes(repo_id, EXTRACTOR, keep)
