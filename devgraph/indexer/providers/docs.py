"""Docs provider: nodes and edges from Markdown front matter.

Fills the node types a project schema sources from `docs` (see `DocsSource`
in devgraph/config/project_schema.py): one node per Markdown file that
matches the type's globs and `where` conditions, with its declared metadata
read from the file's front matter, plus an edge for each value a docs
relationship's front-matter `field` names.

The schema ships inside the repository, so everything here treats it and
the files as hostile. Files are read with `read_bounded` and front matter
with `bounded_safe_load`; conditions are four plain text tests (`like` is
`fnmatch.fnmatchcase` with only `*` as a wildcard), and globs are matched
one folder at a time with `fnmatchcase` (see `glob_matches`). Nothing
compiles a user-supplied regex or runs repository code. Compared and written
strings are capped at 4 KiB, integers at int64 and edge lists at 100 items.

Nodes are keyed like filesystem nodes (`name = path = <repo-relative path>`)
and tagged `extractor = "docs"`. This module is pure: it reads files and
returns plain data, and never touches the graph or imports the dispatcher.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from devgraph.config.project_schema import INT64_MAX, INT64_MIN, Condition, DocsSource, EffectiveSchema
from devgraph.config.project_tools import YAML_LOAD_ERRORS
from devgraph.config.yaml_bound import YAML_MAX_NODES, bounded_safe_load
from devgraph.indexer.docs.extractor import _FRONTMATTER_RE
from devgraph.paths import MAX_CONFIG_BYTES, FileTooLarge, NotRegularFile, read_bounded

EXTRACTOR = "docs"
MARKDOWN_SUFFIXES = (".md", ".markdown")

#: Longest string (in UTF-8 bytes) compared, written or used as an edge value.
MAX_VALUE_BYTES = 4096
#: Most items of a list-valued relationship field that become edges.
MAX_EDGE_VALUES = 100
#: Most files `source_report` names before summarising the rest.
REPORT_FILE_LIMIT = 5


@dataclass(frozen=True)
class DocsField:
    """A declared metadata field and the front-matter key it is read from."""

    name: str
    key: str
    type: str
    required: bool


@dataclass(frozen=True)
class DocsType:
    label: str
    paths: tuple[str, ...]
    where: tuple[Condition, ...]
    fields: tuple[DocsField, ...]


@dataclass(frozen=True)
class DocsRelationship:
    type: str
    from_labels: tuple[str, ...]
    to_label: str
    field: str


@dataclass(frozen=True)
class DocsSpec:
    types: tuple[DocsType, ...]
    relationships: tuple[DocsRelationship, ...]


class Problem(NamedTuple):
    """Why a file was skipped for a type, or a value left blank."""

    label: str
    path: str
    reason: str


def docs_spec(effective: EffectiveSchema) -> DocsSpec | None:
    """What the schema asks this provider to build, or None if nothing."""
    types = []
    for node_type in effective.node_types:
        source = node_type.source
        if not isinstance(source, DocsSource):
            continue
        fields = tuple(
            DocsField(field.name, source.fields.get(field.name, field.name), field.type, field.required)
            for field in node_type.metadata
            if field.name != "path"
        )
        types.append(DocsType(node_type.label, source.paths, source.where, fields))
    if not types:
        return None
    relationships = tuple(
        DocsRelationship(r.type, r.from_labels, r.to, r.field)
        for r in effective.relationships
        if r.provider == EXTRACTOR and r.field is not None
    )
    return DocsSpec(tuple(types), relationships)


def glob_matches(glob: str, rel: str) -> bool:
    """Whether a repo-relative POSIX path matches a glob, case-sensitively.

    The glob and the path are split on "/". A `**` segment matches zero or
    more whole folders; every other segment is matched against one path
    segment with `fnmatch.fnmatchcase`. The segments before the first `**`
    are matched against the start of the path and those after the last
    `**` against its end, each in one pass. Only what lies between (nothing,
    once validation caps `**` at two) is walked as a set of reachable path
    positions, so no glob can backtrack the way `PurePosixPath.full_match`
    does on `*a*a*a...` or repeated `**/*/`.
    """
    parts = rel.split("/")
    segments = glob.split("/")
    if "**" not in segments:
        return len(segments) == len(parts) and _segments_match(segments, parts)
    first = segments.index("**")
    last = len(segments) - 1 - segments[::-1].index("**")
    head, middle, tail = segments[:first], segments[first:last + 1], segments[last + 1:]
    if len(head) + len(tail) > len(parts):
        return False
    end = len(parts) - len(tail)
    return (
        _segments_match(head, parts[:first])
        and _segments_match(tail, parts[end:])
        and _walk(middle, parts[first:end])
    )


def _segments_match(segments: list[str], parts: list[str]) -> bool:
    return all(fnmatch.fnmatchcase(part, segment) for part, segment in zip(parts, segments, strict=True))


def _walk(segments: list[str], parts: list[str]) -> bool:
    """Whether `segments` (which may hold `**`) match all of `parts`.

    Tracks the set of reachable path positions, dropping any from which
    fewer path segments remain than non-`**` segments are left to match.
    """
    still_needed = [0] * (len(segments) + 1)
    for index in range(len(segments) - 1, -1, -1):
        still_needed[index] = still_needed[index + 1] + (segments[index] != "**")
    reachable = {0}
    for index, segment in enumerate(segments):
        limit = len(parts) - still_needed[index + 1]
        if segment == "**":
            reachable = set(range(min(reachable), limit + 1))
        else:
            reachable = {
                i + 1 for i in reachable
                if i < len(parts) and fnmatch.fnmatchcase(parts[i], segment)
            }
        reachable = {i for i in reachable if i <= limit}
        if not reachable:
            return False
    return len(parts) in reachable


def selects(docs_type: DocsType, rel: str) -> bool:
    """Whether a repo-relative POSIX path is a Markdown file one of the type's globs matches."""
    return PurePosixPath(rel).suffix in MARKDOWN_SUFFIXES and any(glob_matches(g, rel) for g in docs_type.paths)


def read_front_matter(path: Path) -> tuple[dict[Any, Any] | None, str | None]:
    """(front matter, None), or (None, why the file can't be used).

    A file without front matter gives an empty mapping. Keys are kept as
    written (`on:` is the key "on"); values keep YAML 1.1 resolution.
    """
    try:
        text = read_bounded(path).decode("utf-8", errors="replace")
    except NotRegularFile:
        return None, "is not a regular file, not read"
    except FileTooLarge:
        return None, f"is larger than {MAX_CONFIG_BYTES // (1024 * 1024)} MiB, not read"
    except OSError:
        return None, "could not be read"
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, None
    try:
        values = bounded_safe_load(match.group(1), YAML_MAX_NODES, raw_keys=True)
    except YAML_LOAD_ERRORS:
        return None, "front matter is not valid YAML"
    if values is None:
        return {}, None
    if not isinstance(values, dict):
        return None, "front matter is not a set of key: value lines"
    return values, None


def _text_problem(text: str) -> str | None:
    """Why a string can't be compared or written, or None."""
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate, from a YAML "\ud800" escape
        return "is not valid text"
    return f"is longer than {MAX_VALUE_BYTES // 1024} KiB" if size > MAX_VALUE_BYTES else None


def _in_int64(value: int) -> bool:
    # Checked before str(): YAML 1.1 hex, binary and sexagesimal integers
    # have no size limit, but str() refuses one over 4300 digits.
    return INT64_MIN <= value <= INT64_MAX


def _as_text(value: Any) -> str | None:
    """A scalar's comparison text (bool as true/false, int64 in decimal), or None."""
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value) if _in_int64(value) else None
    if type(value) is str and _text_problem(value) is None:
        return value
    return None


def _star_only(pattern: str) -> str:
    """A `like` text as an fnmatch pattern in which only `*` is a wildcard."""
    return "".join({"?": "[?]", "[": "[[]"}.get(c, c) for c in pattern)


def _test(condition: Condition, text: str) -> bool:
    operand = condition.text
    match condition.operator:
        case "is":
            return text == operand
        case "starts_with":
            return text.startswith(operand)
        case "contains":
            return operand in text
        case _:
            return fnmatch.fnmatchcase(text, _star_only(operand))


def _holds(condition: Condition, values: Mapping[Any, Any]) -> bool:
    value = values.get(condition.field)
    items = value if type(value) is list else [value]
    return any(text is not None and _test(condition, text) for text in map(_as_text, items))


_TYPE_WORDS = {"integer": "a whole number", "float": "a number", "boolean": "true or false", "string": "text"}


def _coerce(value: Any, declared: str) -> tuple[Any, str | None]:
    """(property value, None), or (None, why the value can't be written)."""
    kind = type(value)
    if kind is int and declared != "boolean" and not _in_int64(value):
        return None, "does not fit in 64 bits"
    if declared == "string":
        if kind is bool:
            return ("true" if value else "false"), None
        if kind in (str, int, float):
            text = str(value)
            problem = _text_problem(text)
            return (None, problem) if problem else (text, None)
    elif declared == "integer":
        if kind is int:
            return value, None
    elif declared == "float":
        if kind is float:
            return value, None
        if kind is int:
            return float(value), None
    elif kind is bool:
        return value, None
    return None, f"is not {_TYPE_WORDS[declared]}"


def _node(docs_type: DocsType, repo_id: str, rel: str, values: Mapping[Any, Any]) -> tuple[dict | None, list[str]]:
    """The node for one selected file that meets every condition, or None, plus reasons."""
    properties: dict[str, Any] = {"path": rel, "extractor": EXTRACTOR}
    reasons: list[str] = []
    for field in docs_type.fields:
        raw = values.get(field.key)
        if raw is None:
            if field.required:
                return None, [f"missing required {field.key!r}"]
            properties[field.name] = None
            continue
        value, why = _coerce(raw, field.type)
        if why is not None:
            if field.required:
                return None, [f"{field.key!r} {why}; it is required, so the file is skipped"]
            reasons.append(f"{field.key!r} {why}, left blank")
        properties[field.name] = value
    return {"label": docs_type.label, "repo_id": repo_id, "name": rel, "properties": properties}, reasons


#: What `read_selected` gives per file: (front matter, None) or (None, why it is skipped).
Selected = Mapping[str, tuple[dict[Any, Any] | None, str | None]]


def read_selected(spec: DocsSpec, files: Mapping[str, Path]) -> dict[str, tuple[dict[Any, Any] | None, str | None]]:
    """Front matter of the files some docs type selects, each read once.

    `files` maps a repo-relative POSIX path to the file on disk. Files no
    type's globs select are never read. Pass the result to `build_nodes`
    and `build_edges`.
    """
    return {
        rel: read_front_matter(files[rel])
        for rel in sorted(files)
        if any(selects(docs_type, rel) for docs_type in spec.types)
    }


def _evaluate(spec: DocsSpec, repo_id: str, selected: Selected):
    """Yield (node or None, problems, front matter) per selected (type, file)."""
    for rel in sorted(selected):
        values, problem = selected[rel]
        for docs_type in spec.types:
            if not selects(docs_type, rel):
                continue
            if values is None:
                yield None, [Problem(docs_type.label, rel, problem)], None
                continue
            if not all(_holds(condition, values) for condition in docs_type.where):
                continue
            node, reasons = _node(docs_type, repo_id, rel, values)
            yield node, [Problem(docs_type.label, rel, reason) for reason in reasons], values


def build_nodes(spec: DocsSpec, repo_id: str, selected: Selected) -> tuple[list[dict], list[Problem]]:
    """Nodes for the files `read_selected` read, and the problems met.

    A problem either skipped the file for that type or left a value blank.
    """
    nodes: list[dict] = []
    problems: list[Problem] = []
    for node, found, _values in _evaluate(spec, repo_id, selected):
        problems += found
        if node is not None:
            nodes.append(node)
    return nodes, problems


def _edge_values(value: Any) -> list[str]:
    """Target names: str or int (never bool) scalars or list items, leading `./`s removed, deduplicated."""
    items = value[:MAX_EDGE_VALUES] if type(value) is list else [value]
    out: list[str] = []
    for item in items:
        if type(item) not in (str, int):
            continue
        text = _as_text(item)
        if text is None:
            continue
        while text.startswith("./"):
            text = text[2:]
        if text and text not in out:
            out.append(text)
    return out


def build_edges(
    spec: DocsSpec,
    repo_id: str,
    selected: Selected,
    targets: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Edges from the docs nodes of the files `read_selected` read to the nodes their front matter names.

    With `targets`, only edges to those (label, name) pairs are returned.
    Whether a target exists is the graph's business: an edge to a missing
    node is skipped when it is written.
    """
    if not spec.relationships:
        return []
    rels: list[dict] = []
    for node, _problems, values in _evaluate(spec, repo_id, selected):
        if node is None:
            continue
        for relationship in spec.relationships:
            if node["label"] not in relationship.from_labels:
                continue
            for name in _edge_values(values.get(relationship.field)):
                if targets is not None and (relationship.to_label, name) not in targets:
                    continue
                rels.append({
                    "from_label": node["label"],
                    "from_name": node["name"],
                    "rel_type": relationship.type,
                    "to_label": relationship.to_label,
                    "to_name": name,
                    "repo_id": repo_id,
                    "properties": {},
                })
    return rels


def _printable(text: str) -> str:
    """`text` with every unprintable character (control, format, lone surrogate...) escaped.

    File names come from the repository, and a lone surrogate stands for an
    undecodable byte, so a report line could otherwise reach a terminal raw.
    """
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def files_by_rel(repo_root: Path, files: Iterable[Path]) -> dict[str, Path]:
    """`files` keyed by their repo-relative POSIX path, as `read_selected` takes them."""
    by_rel: dict[str, Path] = {}
    for path in files:
        try:
            by_rel[path.relative_to(repo_root).as_posix()] = path
        except ValueError:
            continue
    return by_rel


def source_report(
    repo_root: Path, effective: EffectiveSchema, files: Iterable[Path], *, selected: Selected | None = None
) -> list[dict[str, str]]:
    """Doctor lines for each docs-sourced type: {"status": "ok" | "warning", "detail": ...}.

    `files` are the repository's indexable paths. The report reads only
    those files (none, when `selected` is the `read_selected` result for
    them already) and never the graph, so edge values that match no target
    are left to the caller.
    """
    spec = docs_spec(effective)
    if spec is None:
        return []
    by_rel = files_by_rel(repo_root, files)
    if selected is None:
        selected = read_selected(spec, by_rel)
    nodes, problems = build_nodes(spec, "", selected)
    lines: list[dict[str, str]] = []
    for docs_type in spec.types:
        label = docs_type.label
        matched = [rel for rel in by_rel if selects(docs_type, rel)]
        if not matched:
            lines.append({
                "status": "warning",
                "detail": f"{label}: no file matches {_printable(', '.join(docs_type.paths))} "
                          f"(matching is case-sensitive; use **/*.md for every folder)",
            })
            continue
        entries = sum(1 for node in nodes if node["label"] == label)
        counts = [_plural(len(matched), "file matches", "files match")]
        if docs_type.where:
            left_out = sum(
                1 for rel in matched
                if selected[rel][0] is not None
                and not all(_holds(condition, selected[rel][0]) for condition in docs_type.where)
            )
            counts = [counts[0] + " the paths", f"{left_out} left out by conditions"]
        entry_words = _plural(entries, f"{label} entry", f"{label} entries")
        lines.append({
            "status": "ok" if entries else "warning",
            "detail": f"{label}: " + ("; ".join(counts + [entry_words]) if docs_type.where else f"{counts[0]}, {entry_words}"),
        })
        reasons: dict[str, list[str]] = {}
        for problem in problems:
            if problem.label == label:
                reasons.setdefault(problem.path, []).append(problem.reason)
        named = sorted(reasons)
        for rel in named[:REPORT_FILE_LIMIT]:
            detail = f"{label}: {_printable(rel)}: {_printable('; '.join(reasons[rel]))}"
            lines.append({"status": "warning", "detail": detail})
        if len(named) > REPORT_FILE_LIMIT:
            lines.append({
                "status": "warning",
                "detail": f"{label}: and {len(named) - REPORT_FILE_LIMIT} more files with problems",
            })
    return lines


def edge_targets(edges: Iterable[dict]) -> dict[str, list[str]]:
    """The target names `build_edges` produced, per target label, sorted."""
    targets: dict[str, set[str]] = {}
    for edge in edges:
        targets.setdefault(edge["to_label"], set()).add(edge["to_name"])
    return {label: sorted(names) for label, names in targets.items()}


def unmatched_report(spec: DocsSpec, edges: list[dict], present: Mapping[str, set[str]]) -> list[dict[str, str]]:
    """Doctor lines for front-matter values that name no node, per docs relationship.

    `edges` come from `build_edges`; `present` maps a target label to the
    names the graph holds (the caller asks the graph, this module never
    does). Names up to `REPORT_FILE_LIMIT` values per relationship, then
    how many more.
    """
    lines: list[dict[str, str]] = []
    for relationship in spec.relationships:
        missing = sorted(
            {
                (edge["from_name"], edge["to_name"], edge["from_label"])
                for edge in edges
                if edge["rel_type"] == relationship.type
                and edge["to_name"] not in present.get(relationship.to_label, set())
            }
        )
        for path, value, label in missing[:REPORT_FILE_LIMIT]:
            lines.append({
                "status": "warning",
                "detail": f"{label}: {_printable(relationship.field)} '{_printable(value)}' in "
                          f"{_printable(path)} matches no {relationship.to_label}",
            })
        if len(missing) > REPORT_FILE_LIMIT:
            lines.append({
                "status": "warning",
                "detail": f"{', '.join(relationship.from_labels)}: and {len(missing) - REPORT_FILE_LIMIT} more "
                          f"{_printable(relationship.field)} values that match no {relationship.to_label}",
            })
    return lines
