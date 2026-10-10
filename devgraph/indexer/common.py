"""Shared graph-write dataclasses used by every per-language source extractor.

Split out of `devgraph/indexer/python/extractor.py` so the JS/TS, C#, C++,
Java, Rust, and Go extractors (Implementation Plan #8) share one definition
instead of each redeclaring the same three classes.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field

from devgraph.graph.schema import (
    FILE_SCOPED_LABELS,
    NAME_REF_FILELESS,
    NAME_REF_PIN_SEP,
    NAME_REF_SEP,
    PIN_CONFIDENCE,
    PIN_KINDS,
    pin_kind,
    split_pin,
)

__all__ = [
    "NAME_REF_FILELESS",
    "NAME_REF_PIN_SEP",
    "NAME_REF_SEP",
    "PIN_BY_KIND",
    "ExtractionResult",
    "GraphNode",
    "GraphRelationship",
    "best_pin",
    "module_pin_name",
    "name_ref_properties",
    "own_edges",
    "parse_name_ref",
    "pin_confidence",
    "pin_excludes",
    "pin_matches",
]


@dataclass
class GraphNode:
    """A node to be upserted into the graph."""

    label: str
    repo_id: str
    name: str
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "repo_id": self.repo_id,
            "name": self.name,
            "properties": self.properties,
        }


@dataclass
class GraphRelationship:
    """A relationship to be upserted into the graph.

    from_file/to_file are an opt-in exactness constraint: when an endpoint's
    file is known for certain at extraction time (e.g. CONTAINS, where both
    ends are always the file currently being parsed), setting it makes the
    MATCH in engine.py require that node's `file` property too, instead of
    matching by name alone. Leave it None for endpoints whose file genuinely
    isn't knowable from syntax alone (e.g. a CALLS target, which could be
    defined anywhere in the repo) -- that keeps today's bare-name matching,
    ambiguity and all, since guessing wrong there would silently drop edges
    rather than just being imprecise.

    The source end is pinned for every edge out of one of the parsed file's
    own file-scoped nodes (see `own_edges`), so a `main` in one file never
    writes another file's calls. A source that isn't one of those (a Module,
    whose name is already its path, or a type defined in another file, such
    as a Rust `impl Trait for Foo`) stays bare.

    `origin` is the file that wrote the edge. The engine keeps every writer
    in the edge's sorted `origins` list, and a re-index of that file removes
    it from the edges it no longer writes, deleting an edge whose last writer
    is gone.

    A `to_file` may be a path pin instead of a file (see `pin_matches`): the
    edge goes to every node of the name in a file the pin matches, less the
    files any pin in `exclude` matches. A Module target's pin is matched on
    the Module's path, its `to_name` being the directory or basename the
    pin seeks (`module_pin_name`).

    `no_self` keeps the edge off its own source: a call `x.m()` on a receiver
    nothing types, inside `m` itself, is not a recursive call.
    """

    from_label: str
    from_name: str
    rel_type: str
    to_label: str
    to_name: str
    repo_id: str
    properties: dict | None = None
    from_file: str | None = None
    to_file: str | None = None
    origin: str | None = None
    exclude: list[str] | None = None
    no_self: bool = False

    def to_dict(self) -> dict:
        return {
            "from_label": self.from_label,
            "from_name": self.from_name,
            "rel_type": self.rel_type,
            "to_label": self.to_label,
            "to_name": self.to_name,
            "repo_id": self.repo_id,
            "properties": self.properties,
            "from_file": self.from_file,
            "to_file": self.to_file,
            "origin": self.origin,
            "exclude": self.exclude,
            "no_self": self.no_self,
        }


@dataclass
class ExtractionResult:
    """Result of parsing a source file: nodes and relationships."""

    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }


def own_edges(result: ExtractionResult, file_path: str) -> ExtractionResult:
    """Mark `result` as `file_path`'s own edges, in place, and return it.

    Pins the source end (`from_file`) of every relationship whose source
    `(label, name)` is one of the result's nodes with `file == file_path`,
    and stamps `origin = file_path` on every relationship. An already-set
    `from_file` is left as it is.
    """
    owned = {
        (node.label, node.name) for node in result.nodes if node.properties.get("file") == file_path
    }
    for rel in result.relationships:
        rel.origin = file_path
        if rel.from_file is None and (rel.from_label, rel.from_name) in owned:
            rel.from_file = file_path
    return result




def pin_matches(pin: str | None, path: str) -> bool:
    """Whether the path pin `pin` (a relationship's `to_file`, see
    graph/schema.py `pin_kind`) matches the repository-relative `path`: None
    matches every path, "" only the file-less node (path ""), a file only
    itself. The engine's Cypher mirrors this."""
    if pin is None:
        return True
    base, not_suffix = split_pin(pin)
    if not_suffix and path.endswith(not_suffix):
        return False
    kind = pin_kind(pin)
    if kind in ("fileless", "file"):
        return path == base
    if kind == "dir":
        folder = base[:-1]
        return path.startswith(folder) and "/" not in path[len(folder):]
    if kind == "suffix":
        return ("/" + path).endswith(base)
    if kind == "suffix_dir":
        return ("/" + path[: len(path) - len(path.rsplit("/", 1)[-1])]).endswith(base[:-1])
    return path.startswith(base)


def _rank(pin: str) -> int:
    """A pin's precedence, 0 the highest; the file-less node ranks as a file."""
    kind = pin_kind(pin)
    return PIN_KINDS.index(kind) if kind in PIN_KINDS else 0


def pin_excludes(pins: Iterable[str], file_path: str) -> dict[str, list[str]]:
    """Each of one call's pins, highest precedence first (graph/schema.py
    `PIN_KINDS`), with the pins it leaves out: every pin of higher
    precedence, so a file two pins match gets one edge, from the stronger
    one; a recursive prefix also leaves out the writer's own file."""
    ranks = {pin: _rank(pin) for pin in pins}
    ordered = sorted(ranks, key=lambda pin: (ranks[pin], pin))
    excludes = {}
    for pin in ordered:
        higher = {other for other in ordered if ranks[other] < ranks[pin]}
        if pin.endswith("/") and pin_kind(pin) == "prefix":
            higher.add(file_path)
        excludes[pin] = sorted(higher)
    return excludes


def best_pin(pins: Iterable[str], path: str, file_path: str) -> str | None:
    """The pin of `pins` (one call's, written by `file_path`) whose row links
    the node at `path`, or None when none does: the highest-precedence pin
    that matches it (see `pin_excludes`)."""
    for pin, excluded in pin_excludes(pins, file_path).items():
        if pin_matches(pin, path) and not any(pin_matches(other, path) for other in excluded):
            return pin
    return None


def module_pin_name(pin: str) -> str:
    """What a pinned Module target seeks: a directory pin its directory (as
    a Module's `dir`), a suffix pin its basename (as a Module's
    `basename`). A Module's own path is its name, so an exact file needs no
    pin."""
    base, _suffix = split_pin(pin)
    kind = pin_kind(pin)
    if kind == "dir":
        return base[:-2] if base.endswith("/.") else ""
    if kind == "suffix":
        return posixpath.basename(base)
    raise ValueError(f"a Module target takes only a directory or suffix pin: {pin!r}")


def name_ref_properties(rels: list[dict]) -> dict:
    """The Module properties that record a file's by-name edges, so a batch
    that later adds one of their endpoints can relink them from the graph.

    An edge is recorded when another file can add its target: the target
    has no `to_file`, or one that is neither "" nor the writing file (a
    Python call resolved to an imported file, or to every file under a
    package directory). It is also recorded when its source is unpinned and
    not a Module (a Rust `impl Trait for Foo`, which another file's `Foo`
    can add), unless that source is not a code symbol (a route's Endpoint,
    which the same file writes).

    `name_refs` holds one entry per edge source and target name, sorted, as
    `rel_type, from_label, from_name, from_file, to_label, to_name,
    caller_class, pins, confidence` joined by NAME_REF_SEP (an unset field is
    empty). `pins` is empty for an unpinned target, else the target's
    `to_file`s joined by NAME_REF_PIN_SEP, with NAME_REF_FILELESS for "";
    a pinned and an unpinned edge of the same source and name are separate
    entries. An unpinned entry's `confidence` is its edges' `confidence`
    property. A pinned entry's is "pin" when its edges carry one, each pin's
    being its kind's (graph/schema.py `PIN_CONFIDENCE`), and empty when they
    carry none. `name_ref_targets` is their sorted
    distinct `to_name`s and `name_ref_sources` the sorted distinct
    `from_name`s of their unpinned non-Module sources. All three are lists,
    empty when there is nothing.
    """
    entries: dict[tuple, tuple[set, set]] = {}
    targets, sources = set(), set()
    for rel in rels:
        unpinned_source = not rel.get("from_file") and rel["from_label"] != "Module"
        to_file = rel.get("to_file")
        if (
            to_file is not None
            and to_file in ("", rel.get("origin"))
            and not (unpinned_source and rel["from_label"] in FILE_SCOPED_LABELS)
        ):
            continue
        properties = rel.get("properties") or {}
        key = (
            rel["rel_type"], rel["from_label"], rel["from_name"], rel.get("from_file") or "",
            rel["to_label"], rel["to_name"], properties.get("caller_class") or "",
            # Unpinned entries are kept apart by confidence; a pinned entry's
            # pins each carry their kind's.
            None if to_file is not None else properties.get("confidence") or "",
        )
        pins, confidences = entries.setdefault(key, (set(), set()))
        if to_file is not None:
            pins.add(to_file)
            if properties.get("confidence"):
                confidences.add(PIN_BY_KIND)
        targets.add(rel["to_name"])
        if unpinned_source:
            sources.add(rel["from_name"])
    refs = set()
    for key, (pins, confidences) in entries.items():
        *fields, unpinned_confidence = key
        encoded = NAME_REF_PIN_SEP.join(sorted(NAME_REF_FILELESS if pin == "" else pin for pin in pins))
        confidence = unpinned_confidence if unpinned_confidence is not None else min(confidences, default="")
        refs.add(NAME_REF_SEP.join((*fields, encoded, confidence)))
    return {"name_refs": sorted(refs), "name_ref_targets": sorted(targets), "name_ref_sources": sorted(sources)}


#: A pinned `name_refs` entry's confidence when its edges carry one: each
#: pin's is its kind's.
PIN_BY_KIND = "pin"


def pin_confidence(pin: str, entry_confidence: str) -> str:
    """The confidence an edge relinked through `pin` of a pinned `name_refs`
    entry gets: its kind's when the entry's edges carry one (or the pin is
    not a plain file, which only a resolver writes), else none. An entry
    written before INDEX_FORMAT 6 stores a confidence word instead of
    PIN_BY_KIND, read the same way."""
    kind = pin_kind(pin)
    if entry_confidence or kind not in ("file", "fileless"):
        return PIN_CONFIDENCE.get(kind, "")
    return ""


def parse_name_ref(entry: str) -> tuple[list[str], list[str] | None, str]:
    """An entry of `name_refs` as (its first seven fields, its target pins
    (None when unpinned, "" for the file-less node), its confidence). An
    entry written before pins (seven fields) is unpinned, with no
    confidence."""
    fields = entry.split(NAME_REF_SEP)
    if len(fields) == 7:
        return fields, None, ""
    *head, encoded, confidence = fields
    pins = [
        "" if pin == NAME_REF_FILELESS else pin for pin in encoded.split(NAME_REF_PIN_SEP)
    ] if encoded else None
    return head, pins, confidence
