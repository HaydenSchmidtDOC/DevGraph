"""Code symbols (functions and classes) of one file's text, extracted in memory, and their diff.

Every code route `index_paths` uses runs here on a string: nothing is read from
disk and nothing is written. See
docs/superpowers/specs/2026-10-08-compare-branches-design.md (C3, C6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from devgraph.indexer.common import ExtractionResult
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.dispatch import _CODE_ROUTES
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file
from devgraph.indexer import source_text

# One entry per `_CODE_ROUTES` value; a test fails if a route has none.
EXTRACTORS: dict[str, Callable[[str, str], ExtractionResult]] = {
    "py": lambda source, path: extract_python_file(source, path, ""),
    "js": lambda source, path: extract_js_file(source, path, ""),
    "cs": lambda source, path: extract_csharp_file(source, path, ""),
    "cpp": lambda source, path: extract_cpp_file(source, path, ""),
    "java": lambda source, path: extract_java_file(source, path, ""),
    "rs": lambda source, path: extract_rust_file(source, path, ""),
    "kt": lambda source, path: extract_kotlin_file(source, path, ""),
    # The resolver configuration only shapes IMPORTS and CALLS targets, which are dropped here.
    "go": lambda source, path: extract_go_file(source, path, ""),
}

_SYMBOL_LABELS = ("Function", "Class")


@dataclass
class Symbol:
    kind: str  # "Function" or "Class"
    name: str
    container: str | None  # the innermost enclosing class's name
    ordinal: int  # position among symbols with the same (kind, container, name)
    start_line: int
    end_line: int
    body: str


def language_for(path: str) -> str | None:
    """The code route of a repo-relative path, by its suffix, or `None`."""
    return _CODE_ROUTES.get(PurePosixPath(path).suffix)


def decode_source(data: bytes, path: str = "") -> str:
    """Blob bytes as the indexer would see the file at `path` (`source_text`:
    a Python coding line, else UTF-8, cp1252 or Latin-1), CRLF as LF."""
    return source_text.decode_source(data, python=source_text.is_python_path(path)).replace("\r\n", "\n")


class TooManySymbols(Exception):
    """The file has more functions and classes than `extract_symbols` was allowed to detail."""


def _containers(nodes: list) -> list[str | None]:
    """Each node's innermost enclosing class name, or `None`, in O(n log n).

    Enclosing means the class's lines include the node's (`<=` both ends) and the
    class is not the node itself. Innermost is the latest start, then the earliest
    end. Equal line ranges cannot show nesting, so among them the earlier node (the
    extractors list a parent before its children) is the outer one: a class is never
    the container of its own container."""
    order = sorted(
        range(len(nodes)),
        key=lambda i: (nodes[i].properties["start_line"], -nodes[i].properties["end_line"], i),
    )
    containers: list[str | None] = [None] * len(nodes)
    stack: list[tuple[int, str]] = []  # (end_line, name) of classes opened so far, outermost first
    for i in order:
        start, end = nodes[i].properties["start_line"], nodes[i].properties["end_line"]
        while stack and stack[-1][0] < start:
            stack.pop()  # closed before this node, and so before every later one
        # Classes are pushed in (start, -end) order, so the first from the top that
        # reaches `end` is the innermost. On properly nested ranges that is the top.
        for class_end, name in reversed(stack):
            if class_end >= end:
                containers[i] = name
                break
        if nodes[i].label == "Class":
            stack.append((end, nodes[i].name))
    return containers


def extract_symbols(path: str, text: str, max_symbols: int | None = None) -> list[Symbol]:
    """The functions and classes of `text` (already normalised), in source order.

    Raises `TooManySymbols` when there are more than `max_symbols`, before any
    per-symbol work, and whatever the language's extractor raises; `path` must have
    a code route."""
    result = EXTRACTORS[language_for(path)](text, path)
    nodes = [
        n
        for n in result.nodes
        if n.label in _SYMBOL_LABELS
        and n.properties.get("start_line") is not None
        and n.properties.get("end_line") is not None
    ]
    if max_symbols is not None and len(nodes) > max_symbols:
        raise TooManySymbols(len(nodes))
    containers = _containers(nodes)
    lines = text.split("\n")  # tree-sitter's rows; splitlines() would also break on \f, \x85, ...
    seen: dict[tuple[str, str | None, str], int] = {}
    symbols = []
    source_order = sorted(
        range(len(nodes)),
        key=lambda i: (nodes[i].properties["start_line"], nodes[i].properties["end_line"], i),
    )
    for i in source_order:
        node, container = nodes[i], containers[i]
        start, end = node.properties["start_line"], node.properties["end_line"]
        group = (node.label, container, node.name)
        ordinal = seen.get(group, 0)
        seen[group] = ordinal + 1
        symbols.append(Symbol(node.label, node.name, container, ordinal, start, end, "\n".join(lines[start - 1 : end])))
    return symbols


def _entry(symbol: Symbol) -> dict:
    return {
        "kind": symbol.kind,
        "name": symbol.name,
        "container": symbol.container,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
    }


def _order(entry: dict) -> tuple:
    return (entry["start_line"], entry["kind"], entry["name"])


def _group(symbols: list[Symbol]) -> dict[tuple[str, str | None, str], list[Symbol]]:
    groups: dict[tuple[str, str | None, str], list[Symbol]] = {}
    for symbol in symbols:
        groups.setdefault((symbol.kind, symbol.container, symbol.name), []).append(symbol)
    return groups


def diff_symbols(old: list[Symbol], new: list[Symbol]) -> tuple[list[dict], list[dict], list[dict]]:
    """(added, removed, changed) entries, paired by (kind, container, name).

    Within a group of same-keyed symbols (overloads, Go methods on two types), the
    ones with identical bodies pair first, so reordering them reports nothing; the
    rest pair in source order (by ordinal), and the leftovers are added or removed.
    `added` and `changed` carry head-side lines, `removed` base-side ones; a `changed`
    entry adds `old_start_line`/`old_end_line`. A symbol with the same body on both
    sides is not reported, wherever it moved. Each list is sorted by (line, kind, name)."""
    before, after = _group(old), _group(new)
    added, removed, changed = [], [], []
    for key in before.keys() | after.keys():
        olds, news = before.get(key, []), after.get(key, [])
        by_body: dict[str, list[Symbol]] = {}
        for symbol in reversed(olds):
            by_body.setdefault(symbol.body, []).append(symbol)  # popped from the end: source order
        matched: set[int] = set()
        unmatched_new = []
        for symbol in news:
            same = by_body.get(symbol.body)
            if same:
                matched.add(id(same.pop()))
            else:
                unmatched_new.append(symbol)
        unmatched_old = [s for s in olds if id(s) not in matched]
        for was, now in zip(unmatched_old, unmatched_new):
            changed.append({**_entry(now), "old_start_line": was.start_line, "old_end_line": was.end_line})
        added += [_entry(s) for s in unmatched_new[len(unmatched_old) :]]
        removed += [_entry(s) for s in unmatched_old[len(unmatched_new) :]]
    return sorted(added, key=_order), sorted(removed, key=_order), sorted(changed, key=_order)
