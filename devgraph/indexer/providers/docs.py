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
strings are capped at 4 KiB, integers at int64, and edge lists and the list
items a condition examines at 100. With the schema's own caps (docs types,
conditions and `*`s per `like`), that bounds the condition work per file, and
each (type, file) verdict is worked out once per read (see `Selected`).

A type keyed `[path]` names its nodes like filesystem nodes (`name = path =
<repo-relative path>`). A type keyed on a front-matter field names each node
by that field's text (`name = "ADR-012"`, with `path` still recording the
file). When several files claim one key, the file first in `claimant_order`
owns it (`keyed_owners`), and only the owner yields a node and edges. Every
node is tagged `extractor = "docs"`. This module is pure: it reads files and
returns plain data, and never touches the graph or imports the dispatcher.
"""

from __future__ import annotations

import fnmatch
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from devgraph.config.project_schema import INT64_MAX, INT64_MIN, Condition, DocsSource, EffectiveSchema
from devgraph.config.project_tools import YAML_LOAD_ERRORS
from devgraph.config.yaml_bound import YAML_MAX_NODES, bounded_safe_load
from devgraph.indexer.docs.extractor import _FRONTMATTER_RE
from devgraph.indexer.walk import is_ignored_path, repo_relative
from devgraph.paths import MAX_CONFIG_BYTES, FileTooLarge, NotRegularFile, read_bounded

EXTRACTOR = "docs"
MARKDOWN_SUFFIXES = (".md", ".markdown")

#: Longest string (in UTF-8 bytes) compared, written or used as an edge value.
MAX_VALUE_BYTES = 4096
#: Most items of a list-valued relationship field that become edges, and of a
#: list-valued field a condition examines.
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
    #: The field (one of `fields`) the type is keyed on, or None for `[path]`.
    key: DocsField | None = None


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
        key = next((field for field in fields if (field.name,) == node_type.key), None)
        types.append(DocsType(node_type.label, source.paths, source.where, fields, key))
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
    """Whether the condition holds for the front matter.

    A list value holds when any item holds, of its first `MAX_EDGE_VALUES`
    items only: a longer list would multiply the cost of every condition.
    """
    value = values.get(condition.field)
    items = value[:MAX_EDGE_VALUES] if type(value) is list else [value]
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


def _hidden(c: str) -> bool:
    """A control, format, line or paragraph separator, or noncharacter code point."""
    point = ord(c)
    return (
        unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
        or 0xFDD0 <= point <= 0xFDEF
        or point & 0xFFFE == 0xFFFE
    )


def _key_text(raw: Any) -> tuple[str | None, str | None]:
    """(key text, None), or (None, why the value can't name an entry, after the field's name).

    A str is itself and an int (never a bool, within int64) is its decimal
    text. Nothing is trimmed, case-folded or normalised: text a person could
    not tell apart from another key, or that a link could never name, is
    refused instead.
    """
    if type(raw) is int:
        if not _in_int64(raw):
            return None, "is too large; quote it to keep it as text"
        return str(raw), None
    if type(raw) is not str:
        return None, "is not text or a whole number"
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate, from a YAML "\ud800" escape
        return None, "isn't valid text; retype it"
    if size > MAX_VALUE_BYTES:
        return None, f"is longer than {MAX_VALUE_BYTES // 1024} KiB"
    if not raw:
        return None, "is empty"
    if raw != raw.strip():
        return None, "starts or ends with whitespace"
    if any(_hidden(c) for c in raw):
        return None, "holds an invisible or control character; retype it without the hidden character"
    if raw.startswith("./"):
        return None, "starts with ./, which a link can never name"
    return raw, None


def _node(docs_type: DocsType, repo_id: str, rel: str, values: Mapping[Any, Any]) -> tuple[dict | None, list[str]]:
    """The node for one selected file that meets every condition, or None, plus reasons.

    A field-keyed type's key is checked first and always required.
    """
    properties: dict[str, Any] = {"path": rel, "extractor": EXTRACTOR}
    name = rel
    key = docs_type.key
    if key is not None:
        raw = values.get(key.key)
        if raw is None:
            return None, [f"missing {key.key!r}, which names the entry; add an `{key.key}:` line"]
        name, why = _key_text(raw)
        if why is not None:
            return None, [f"{key.key!r} {why}"]
    reasons: list[str] = []
    for field in docs_type.fields:
        if field == key:
            properties[field.name] = name
            continue
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
    return {"label": docs_type.label, "repo_id": repo_id, "name": name, "properties": properties}, reasons


class Selected(dict[str, tuple[dict[Any, Any] | None, str | None]]):
    """What `read_selected` gives per file: (front matter, None) or (None, why it is skipped).

    It also remembers whether each type's conditions hold for each file, so
    the node, edge and report passes over one read evaluate them once.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.verdicts: dict[tuple[str, str], bool] = {}


def read_selected(
    spec: DocsSpec,
    files: Mapping[str, Path],
    *,
    read: Callable[[str, Path], tuple[dict[Any, Any] | None, str | None]] | None = None,
) -> Selected:
    """Front matter of the files some docs type selects, each read once.

    `files` maps a repo-relative POSIX path to the file on disk. Files no
    type's globs select are never read. Pass the result to `build_nodes`
    and `build_edges`.

    `read(rel, path)` reads one file and must return what
    `read_front_matter(path)` returns (dispatch passes the read cache).
    Without it, `read_front_matter` is looked up when called.
    """
    if read is None:
        def read(rel: str, path: Path) -> tuple[dict[Any, Any] | None, str | None]:
            return read_front_matter(path)

    return Selected(
        (rel, read(rel, files[rel]))
        for rel in sorted(files)
        if any(selects(docs_type, rel) for docs_type in spec.types)
    )


def _conditions_hold(docs_type: DocsType, rel: str, selected: Selected) -> bool:
    """Whether every condition of the type holds for a file read without problems, worked out once."""
    key = (docs_type.label, rel)
    verdict = selected.verdicts.get(key)
    if verdict is None:
        values = selected[rel][0]
        verdict = selected.verdicts[key] = all(_holds(condition, values) for condition in docs_type.where)
    return verdict


def claimant_order(rel: str) -> tuple[tuple[str, ...], str]:
    """Sort key among files claiming one key: folders and stem, then the full path.

    Comparing the stem without its suffix puts `adr-012.md` before the copies
    editors make of it (`adr-012 copy.md`, `adr-012 (1).md`, `adr-012-v2.md`),
    which plain path order would put first.
    """
    path = PurePosixPath(rel)
    return (*path.parent.parts, path.stem), rel


def keyed_claims(spec: DocsSpec, selected: Selected) -> dict[tuple[str, str], list[str]]:
    """{(label, key): the paths claiming it, in claimant order}, for the field-keyed types.

    A claim is a (type, file) pair the type selects, whose conditions hold,
    with no required-field skip and a valid key. Each label has its own keys.
    """
    claims: dict[tuple[str, str], list[str]] = {}
    for docs_type in spec.types:
        if docs_type.key is None:
            continue
        for rel, (values, _problem) in selected.items():
            if values is None or not selects(docs_type, rel) or not _conditions_hold(docs_type, rel, selected):
                continue
            node, _reasons = _node(docs_type, "", rel, values)
            if node is not None:
                claims.setdefault((docs_type.label, node["name"]), []).append(rel)
    for paths in claims.values():
        paths.sort(key=claimant_order)
    return claims


def keyed_owners(claims: Mapping[tuple[str, str], list[str]]) -> dict[tuple[str, str], str]:
    """{(label, key): the owning path}: the first claimant of each key."""
    return {key: paths[0] for key, paths in claims.items()}


@dataclass(frozen=True)
class KeyedView:
    """The repository's field-keyed docs files as one batch sees them.

    `files` maps every indexable repo-relative path to its file, `selected`
    is the front matter of the files a field-keyed type selects, and
    `claims` and `owners` are computed from all of it.
    """

    files: Mapping[str, Path]
    selected: Selected
    claims: Mapping[tuple[str, str], list[str]]
    owners: Mapping[tuple[str, str], str]


def expand_to_owners(view: KeyedView, batch_selected: Selected, keys: Iterable[tuple[str, str]]) -> Selected:
    """The batch's front matter plus that of the owner of each (label, key) in `keys`.

    Only owners are added, never other claimants, so the result holds at
    most |batch| + |keys| files. `batch_selected` is left unchanged.
    """
    expanded = Selected(batch_selected)
    expanded.verdicts.update(batch_selected.verdicts)
    for key in keys:
        owner = view.owners.get(key)
        if owner is not None and owner not in expanded:
            expanded[owner] = view.selected[owner]
    return expanded


def _evaluate(
    spec: DocsSpec, repo_id: str, selected: Selected, owners: Mapping[tuple[str, str], str] | None
):
    """Yield (node or None, problems, front matter) per selected (type, file).

    A field-keyed (type, file) pair whose key `owners` gives to another file
    yields nothing: losers are a doctor concern (`source_report`).
    """
    if owners is None and any(docs_type.key is not None for docs_type in spec.types):
        raise ValueError("owners are required for a docs type keyed on a front-matter field")
    for rel in sorted(selected):
        values, problem = selected[rel]
        for docs_type in spec.types:
            if not selects(docs_type, rel):
                continue
            if values is None:
                yield None, [Problem(docs_type.label, rel, problem)], None
                continue
            if not _conditions_hold(docs_type, rel, selected):
                continue
            node, reasons = _node(docs_type, repo_id, rel, values)
            if node is not None and docs_type.key is not None and owners.get((docs_type.label, node["name"])) != rel:
                continue
            yield node, [Problem(docs_type.label, rel, reason) for reason in reasons], values


def build_nodes(
    spec: DocsSpec,
    repo_id: str,
    selected: Selected,
    owners: Mapping[tuple[str, str], str] | None = None,
) -> tuple[list[dict], list[Problem]]:
    """Nodes for the files `read_selected` read, and the problems met.

    A problem either skipped the file for that type or left a value blank.
    `owners` (from `keyed_owners`, over every file of the repository) is
    required when a type is keyed on a front-matter field.
    """
    nodes: list[dict] = []
    problems: list[Problem] = []
    for node, found, _values in _evaluate(spec, repo_id, selected, owners):
        problems += found
        if node is not None:
            nodes.append(node)
    return nodes, problems


def _edge_values(value: Any) -> list[str]:
    """Target names: str or int (never bool) scalars or list items, leading `./`s removed, deduplicated."""
    items = value[:MAX_EDGE_VALUES] if type(value) is list else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if type(item) not in (str, int):
            continue
        text = _as_text(item)
        if text is None:
            continue
        while text.startswith("./"):
            text = text[2:]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def build_edges(
    spec: DocsSpec,
    repo_id: str,
    selected: Selected,
    targets: set[tuple[str, str]] | None = None,
    owners: Mapping[tuple[str, str], str] | None = None,
) -> list[dict]:
    """Edges from the docs nodes of the files `read_selected` read to the nodes their front matter names.

    With `targets`, only edges to those (label, name) pairs are returned.
    Whether a target exists is the graph's business: an edge to a missing
    node is skipped when it is written. `owners` is as for `build_nodes`.
    """
    if not spec.relationships:
        return []
    rels: list[dict] = []
    for node, _problems, values in _evaluate(spec, repo_id, selected, owners):
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
                    # which declaration and file made the edge, for `unmatched_report`; not written
                    "field": relationship.field,
                    "from_path": node["properties"]["path"],
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
    """`files` keyed by their repo-relative POSIX path, as `read_selected` takes them.

    Keyed like the indexer (`walk.repo_relative`), so a symlink is keyed by its
    target; a path outside the repository or, through a symlink, under an
    ignored directory is left out.
    """
    return {
        rel: path for path in files
        if (rel := repo_relative(repo_root, path)) is not None and not is_ignored_path(Path(rel))
    }


def source_report(
    repo_root: Path, effective: EffectiveSchema, files: Iterable[Path], *, selected: Selected | None = None,
    claims: Mapping[tuple[str, str], list[str]] | None = None,
) -> list[dict[str, str]]:
    """Doctor lines for each docs-sourced type: {"status": "ok" | "warning", "detail": ...}.

    `files` are the repository's indexable paths. The report reads only
    those files (none, when `selected` is the `read_selected` result for
    them already, and `claims` its `keyed_claims`) and never the graph, so
    edge values that match no target are left to the caller.
    """
    spec = docs_spec(effective)
    if spec is None:
        return []
    by_rel = files_by_rel(repo_root, files)
    if selected is None:
        selected = read_selected(spec, by_rel)
    if claims is None:
        claims = keyed_claims(spec, selected)
    nodes, problems = build_nodes(spec, "", selected, keyed_owners(claims))
    for (label, key), (owner, *losers) in sorted(claims.items()):
        field = next(docs_type.key.key for docs_type in spec.types if docs_type.label == label)
        for rel in losers:
            problems.append(Problem(label, rel, (
                f"{field!r} '{key}' is also used by {owner}, whose path sorts first and keeps it; "
                f"change the {field} in one of them"
            )))
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
                if selected[rel][0] is not None and not _conditions_hold(docs_type, rel, selected)
            )
            counts = [counts[0] + " the paths", f"{left_out} left out by conditions"]
        words = [_plural(entries, f"{label} entry", f"{label} entries")]
        duplicates = sum(1 for (claimed, _key), paths in claims.items() if claimed == label and len(paths) > 1)
        if duplicates:
            words.append(_plural(duplicates, "duplicate id", "duplicate ids"))
        lines.append({
            "status": "ok" if entries else "warning",
            "detail": f"{label}: " + ("; " if docs_type.where else ", ").join(counts + words),
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
    key_names = {docs_type.label: docs_type.key.key for docs_type in spec.types if docs_type.key is not None}
    buckets: dict[tuple[str, str, str, str], list[dict]] = {}
    for edge in edges:
        buckets.setdefault((edge["rel_type"], edge["field"], edge["from_label"], edge["to_label"]), []).append(edge)
    for relationship in spec.relationships:
        known = present.get(relationship.to_label, set())
        missing = sorted(
            {
                (edge["from_path"], edge["to_name"], edge["from_label"])
                for from_label in relationship.from_labels
                for edge in buckets.get((relationship.type, relationship.field, from_label, relationship.to_label), ())
                if edge["to_name"] not in known
            }
        )
        to_label = relationship.to_label
        hint = (
            f" ({to_label} entries are named by '{_printable(key_names[to_label])}', not by file path)"
            if to_label in key_names else ""
        )
        for path, value, label in missing[:REPORT_FILE_LIMIT]:
            lines.append({
                "status": "warning",
                "detail": f"{label}: {_printable(relationship.field)} '{_printable(value)}' in "
                          f"{_printable(path)} matches no {to_label}{hint}",
            })
        if len(missing) > REPORT_FILE_LIMIT:
            lines.append({
                "status": "warning",
                "detail": f"{', '.join(relationship.from_labels)}: and {len(missing) - REPORT_FILE_LIMIT} more "
                          f"{_printable(relationship.field)} values that match no {relationship.to_label}",
            })
    return lines
