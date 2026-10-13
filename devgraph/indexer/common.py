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
    NAME_REF_OWNED,
    NAME_REF_PIN_SEP,
    NAME_REF_SEP,
    OWNER_PIN_SEP,
    PIN_CONFIDENCE,
    PIN_KINDS,
    pin_kind,
    pin_root,
    split_pin,
    unscoped_pin,
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
    "member_owner",
    "module_pin_name",
    "name_ref_properties",
    "own_edges",
    "owned_pin",
    "owners_by_pin",
    "parse_name_ref",
    "pin_confidence",
    "pin_excludes",
    "pin_matches",
    "shadowed",
    "split_owned_pin",
    "tiered",
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
    the Module's path, its `to_name` being the directory, directory name or
    basename the pin seeks (`module_pin_name`).

    `no_self` keeps the edge off its own source: a call `x.m()` on a receiver
    nothing types, inside `m` itself, is not a recursive call.

    `from_owner`/`to_owner` tell same-named siblings of one file apart (two
    classes' `save`, see `member_owner`): on an end pinned to a file, a
    non-None owner must equal the node's `owner`, and None matches every
    node of the name there. `own_edges` fills them in where the file alone
    decides them.
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
    from_owner: str | None = None
    to_owner: str | None = None

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
            "from_owner": self.from_owner,
            "to_owner": self.to_owner,
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


def member_owner(parent_label: str, parent_name: str | None, parent_owner: str | None) -> str:
    """The `owner` of a Class or Function declared directly in a `parent_label`
    named `parent_name` whose own owner is `parent_owner`.

    The owner grammar: the names of the declarations enclosing it, classes
    and functions alike, outermost first, joined by "." (`Outer.Inner` for a
    method of a nested class, `A.save` for a function nested in method
    `A.save`, `save` for one nested in a top-level `save`); "" at a Module's
    (or a namespace's) top level. A namespace, package or module is never
    part of it. Two same-named declarations of one file with different
    owners are two nodes (graph/engine.py `_upsert_nodes_tx`)."""
    if parent_label in ("Class", "Function") and parent_name:
        return f"{parent_owner}.{parent_name}" if parent_owner else parent_name
    return ""


def owned_pin(pin: str, owner: str | None) -> str:
    """A resolver's pin narrowed to the members of `owner` (the declarations
    they are in, common.member_owner) named like the callee, in the files
    `pin` matches (`calls.call_rows` turns it back into `to_file` and
    `to_owner`); `pin` itself when the owner is unknown."""
    return f"{pin}{OWNER_PIN_SEP}{owner}" if owner is not None else pin


def split_owned_pin(pin: str | None) -> tuple[str | None, str | None]:
    """An `owned_pin` as (pin, owner); any other pin as (pin, None)."""
    if pin is None or OWNER_PIN_SEP not in pin:
        return pin, None
    base, owner = pin.split(OWNER_PIN_SEP, 1)
    return base, owner


def owners_by_pin(pins: Iterable[str]) -> dict[str, set[str | None]]:
    """Owned pins grouped by their pin: the owners each names (None for a
    pin with no owner: every member of the name in its files)."""
    found: dict[str, set[str | None]] = {}
    for pin in pins:
        base, owner = split_owned_pin(pin)
        found.setdefault(base, set()).add(owner)
    return found


def own_edges(result: ExtractionResult, file_path: str) -> ExtractionResult:
    """Mark `result` as `file_path`'s own edges, in place, and return it.

    Pins the source end (`from_file`) of every relationship whose source
    `(label, name)` is one of the result's nodes with `file == file_path`,
    and stamps `origin = file_path` on every relationship. An already-set
    `from_file` is left as it is. A pinned source whose `from_owner` is
    unset gets the owner of the file's one node of that label and name, if
    there is one (with several it stays unset, matching them all); a
    CONTAINS into the file whose `to_owner` is unset gets the one its parent
    implies (`member_owner`), once the parent's owner is known.
    """
    owners: dict[tuple[str, str], set[str]] = {}
    for node in result.nodes:
        if node.properties.get("file") == file_path:
            owners.setdefault((node.label, node.name), set()).add(node.properties.get("owner") or "")
    for rel in result.relationships:
        rel.origin = file_path
        source = (rel.from_label, rel.from_name)
        if rel.from_file is None and source in owners:
            rel.from_file = file_path
        if rel.from_file == file_path and rel.from_owner is None and len(owners.get(source, ())) == 1:
            (rel.from_owner,) = owners[source]
        if (
            rel.rel_type == "CONTAINS" and rel.to_file == file_path and rel.to_owner is None
            and (rel.from_owner is not None or rel.from_label not in FILE_SCOPED_LABELS)
        ):
            rel.to_owner = member_owner(rel.from_label, rel.from_name, rel.from_owner)
    return result


def pin_matches(pin: str | None, path: str, keys: Iterable[str] = ()) -> bool:
    """Whether the path pin `pin` (a relationship's `to_file`, see
    graph/schema.py `pin_kind` and `split_pin` for its flags) matches the
    node at the repository-relative `path` whose `mkeys` are `keys`: None
    matches every path, "" only the file-less node (path ""), a file only
    itself. The engine's Cypher mirrors this."""
    if pin is None:
        return True
    parts = split_pin(pin)
    base = parts.base
    if parts.scope and not set(parts.scope) & set(keys):
        return False
    if parts.not_suffix and path.endswith(parts.not_suffix):
        return False
    if parts.only and not path.endswith(parts.only):
        return False
    kind = pin_kind(pin)
    if kind in ("fileless", "file"):
        return path == base
    if kind in ("dir", "loose_dir"):
        folder = base[:-1]
        matched = path.startswith(folder) and "/" not in path[len(folder):]
    elif kind == "suffix":
        matched = ("/" + path).endswith(base)
    elif kind == "suffix_dir":
        matched = ("/" + path[: len(path) - len(path.rsplit("/", 1)[-1])]).endswith(base[:-1])
    else:
        matched = path.startswith(base)
    if not matched or not (parts.skip or parts.skip_roots):
        return matched
    root = pin_root(pin, path)
    return not any(segment in parts.skip for segment in root.split("/")) and not any(
        fragment in f"/{root}/" for fragment in parts.skip_roots
    )


def tiered(pin: str | None) -> bool:
    """Whether `pin`'s `@` keys come in tiers (graph/schema.py `tiered_pin`)."""
    return pin is not None and bool(split_pin(pin).ranks)


def shadowed(
    pin: str, keys: Iterable[str] | None, others: Iterable[tuple[str | None, Iterable[str] | None]],
) -> bool:
    """Whether the tiered pin `pin` leaves out a node it matches whose `mkeys`
    are `keys`: one of `others` (the (path, mkeys) of the nodes of its label
    and name) that the pin's path and flags match, its keys aside, holds a
    key of a nearer tier than the node's nearest. The engine's Cypher
    mirrors this: a node without `mkeys` (None) holds none, and a
    file-less one (path None or "") no path pin matches. A pin without
    tiers shadows nothing."""
    parts = split_pin(pin)
    if not parts.ranks:
        return False
    rank = dict(zip(parts.scope, parts.ranks))
    own = min((rank[key] for key in keys or () if key in rank), default=len(rank))
    path_only = pin.rsplit("@", 1)[0]
    return any(
        bool(path) and pin_matches(path_only, path) and any(rank.get(key, own) < own for key in other_keys or ())
        for path, other_keys in others
    )


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


def best_pin(pins: Iterable[str], path: str, file_path: str, keys: Iterable[str] = ()) -> str | None:
    """The pin of `pins` (one call's, written by `file_path`) whose row links
    the node at `path` whose `mkeys` are `keys`, or None when none does: the
    highest-precedence pin that matches it (see `pin_excludes`)."""
    keys = tuple(keys)
    for pin, excluded in pin_excludes(pins, file_path).items():
        if pin_matches(pin, path, keys) and not any(pin_matches(other, path, keys) for other in excluded):
            return pin
    return None


def module_pin_name(pin: str) -> str:
    """What a pinned Module target seeks: a directory pin its directory (as
    a Module's `dir`), a suffix-directory pin its directory's last name (as
    a Module's `dir_name`), a suffix pin its basename (as a Module's
    `basename`). A Module's own path is its name, so an exact file needs no
    pin."""
    base = split_pin(pin).base
    kind = pin_kind(pin)
    if kind == "dir":
        return base[:-2] if base.endswith("/.") else ""
    if kind == "suffix_dir":
        return posixpath.basename(base[:-2])
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
    which the same file writes). Every edge of a group (one source's to one
    `to_name`) with a tiered pin (graph/schema.py `tiered_pin`) is
    recorded, its own file's and the file-less node's too: the relink
    writes such a group again whole (dispatch `_relink_name_refs`).

    `name_refs` holds one entry per edge source and target name, sorted, as
    `rel_type, from_label, from_name, from_file, to_label, to_name,
    caller_class, from_owner, pins, confidence, keys, NAME_REF_OWNED` joined
    by NAME_REF_SEP (an unset field is empty). `from_owner` is "=" and the
    source's owner, or empty when unknown, so two same-named sources of one
    file are two entries. `pins` is empty for an unpinned target, else the
    target's `to_file`s joined by NAME_REF_PIN_SEP, with NAME_REF_FILELESS
    for "", each followed by OWNER_PIN_SEP and its `to_owner` when it names
    one (`owned_pin`); a pinned and an unpinned edge of the same source and
    name are separate entries. A pin's `@` flag keeps only its "@" there, and
    its match keys are the `keys` field, once, as the flag wrote them (in
    tiers, when it has them): the pins of one source and
    name with different keys (two call sites on receivers of two types) are
    separate entries, which relink takes together. The trailing
    NAME_REF_OWNED marks the layout; an older entry has none (see
    `parse_name_ref`). An unpinned entry's `confidence` is its edges' `confidence`
    property. A pinned entry's is "pin" when its edges carry one, each pin's
    being its kind's (graph/schema.py `PIN_CONFIDENCE`), and empty when they
    carry none. `name_ref_targets` is their sorted
    distinct `to_name`s and `name_ref_sources` the sorted distinct
    `from_name`s of their unpinned non-Module sources. All three are lists,
    empty when there is nothing.
    """
    entries: dict[tuple, tuple[set, set]] = {}
    targets, sources = set(), set()

    def group(rel: dict) -> tuple:
        return (
            rel["rel_type"], rel["from_label"], rel["from_name"], rel.get("from_file"), rel.get("from_owner"),
            rel["to_label"], rel["to_name"],
        )

    whole = {group(rel) for rel in rels if tiered(rel.get("to_file"))}
    for rel in rels:
        unpinned_source = not rel.get("from_file") and rel["from_label"] != "Module"
        to_file = rel.get("to_file")
        if (
            to_file is not None
            and to_file in ("", rel.get("origin"))
            and not (unpinned_source and rel["from_label"] in FILE_SCOPED_LABELS)
            and group(rel) not in whole
        ):
            continue
        properties = rel.get("properties") or {}
        to_file, scope = unscoped_pin(to_file) if to_file is not None else (None, "")
        if to_file is not None:
            to_file = owned_pin(to_file, rel.get("to_owner"))
        from_owner = rel.get("from_owner")
        key = (
            rel["rel_type"], rel["from_label"], rel["from_name"], rel.get("from_file") or "",
            rel["to_label"], rel["to_name"], properties.get("caller_class") or "",
            "" if from_owner is None else "=" + from_owner,
            # Unpinned entries are kept apart by confidence; a pinned entry's
            # pins each carry their kind's.
            None if to_file is not None else properties.get("confidence") or "", scope,
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
        *fields, unpinned_confidence, scope = key
        encoded = NAME_REF_PIN_SEP.join(sorted(
            NAME_REF_FILELESS + pin if split_owned_pin(pin)[0] == "" else pin for pin in pins
        ))
        confidence = unpinned_confidence if unpinned_confidence is not None else min(confidences, default="")
        refs.add(NAME_REF_SEP.join((*fields, encoded, confidence, scope, NAME_REF_OWNED)))
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


def parse_name_ref(entry: str) -> tuple[list, list[str] | None, str]:
    """An entry of `name_refs` as (its first eight fields, the eighth its
    source's owner, None when unknown; its target pins, None when unpinned,
    "" for the file-less node, each an `owned_pin` when it names its
    target's owner; its confidence), each `@` pin with the entry's match keys
    back on it.

    An entry written before owners (no NAME_REF_OWNED last) is read by its
    length: seven fields (before pins) is unpinned, with no confidence; nine
    is pinned; ten is pinned with match keys. Either has an unknown source
    owner and owner-less pins. Any other entry raises ValueError, which a
    reader skips the entry for."""
    fields = entry.split(NAME_REF_SEP)
    if fields[-1] == NAME_REF_OWNED and len(fields) == 12:
        *head, from_owner, encoded, confidence, scope, _marker = fields
        if from_owner and not from_owner.startswith("="):
            raise ValueError(f"not a name_refs owner: {from_owner!r}")
        owner = from_owner[1:] if from_owner else None
    elif len(fields) == 7:
        return [*fields, None], None, ""
    elif len(fields) in (9, 10):
        head, encoded, confidence, scope = fields[:7], fields[7], fields[8], fields[9] if len(fields) == 10 else ""
        owner = None
    else:
        raise ValueError(f"not a name_refs entry: {len(fields)} fields")

    def pin_of(text: str) -> str:
        base, pin_owner = split_owned_pin(text)
        if base.startswith(NAME_REF_FILELESS):
            base = ""
        elif base.endswith("@") and split_pin(base + scope).scope:
            base += scope
        return owned_pin(base, pin_owner)

    pins = [pin_of(pin) for pin in encoded.split(NAME_REF_PIN_SEP)] if encoded else None
    return [*head, owner], pins, confidence
