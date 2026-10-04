"""Comment-preserving edits to a top-level list in a config document.

Config files such as `devgraph.tools.yaml` and `devgraph.schema.yaml` are
committed and hand-edited, so their comments matter. PyYAML cannot round-trip
them, so edits are text splices: `yaml.compose` gives each entry's line range
and only those lines change; every other byte is kept. Functions here do not
validate entry semantics -- callers validate the result with their own parser.
"""

from __future__ import annotations

import re

import yaml

from devgraph.config.project_tools import YAML_LOAD_ERRORS


def _a(noun: str) -> str:
    """`noun` with its indefinite article."""
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


class ListEditError(Exception):
    """A list document cannot be edited as asked."""


class _EntryDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_EntryDumper.add_representer(str, _represent_str)


def dump_entry(entry: dict) -> str:
    """YAML for one entry mapping: key order kept, multi-line strings as `|` blocks."""
    return yaml.dump(
        entry, Dumper=_EntryDumper, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000
    )


def _load(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except YAML_LOAD_ERRORS as exc:
        raise ListEditError(f"malformed YAML: {exc}") from exc


def entries(text: str, *, key: str) -> list[dict]:
    """The raw entry mappings under `key`; `[]` for empty text or no such list."""
    document = _load(text)
    if document is None:
        return []
    if not isinstance(document, dict):
        raise ListEditError("expected a YAML mapping at the document root")
    items = document.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ListEditError(f"{key!r} must be a list")
    return items


class _Doc:
    """A config document split into lines, with the `key` list node located."""

    def __init__(self, text: str, key: str, ident: str, noun: str) -> None:
        self.list_key, self.ident, self.noun = key, ident, noun
        self.crlf = "\r\n" in text
        self.lines = text.replace("\r\n", "\n").split("\n")
        if self.lines[-1] == "":
            self.lines.pop()
        normalized = "\n".join(self.lines) + "\n" if self.lines else ""
        self.mappings = entries(normalized, key=key)  # also validates root and list shape
        self.key_node = self.value = None
        self.items: list[yaml.Node] = []
        try:
            root = yaml.compose(normalized)
        except YAML_LOAD_ERRORS as exc:
            raise ListEditError(f"malformed YAML: {exc}") from exc
        if root is None:
            return
        for node_key, value in root.value:
            if node_key.value == key:
                self.key_node, self.value = node_key, value
        if isinstance(self.value, yaml.SequenceNode):
            self.items = self.value.value
            if self.value.flow_style and self.items:
                raise ListEditError(
                    f"{key!r} is a flow-style list; edit it by hand or rewrite it as a block list"
                )

    def _matches(self, name: object) -> list[int]:
        return [
            position
            for position, mapping in enumerate(self.mappings)
            if isinstance(mapping, dict) and mapping.get(self.ident) == name
        ]

    def index(self, name: str) -> int:
        matches = self._matches(name)
        if not matches:
            raise ListEditError(f"no {self.noun} named {name!r}")
        if len(matches) > 1:
            raise ListEditError(f"{self.noun} {name!r} is declared {len(matches)} times; edit the file by hand")
        return matches[0]

    def has(self, name: object) -> bool:
        return bool(self._matches(name))

    def dash_line(self, position: int) -> int:
        """Line of the item's `- `; an anchor/tag alone after the dash puts the mapping on the next line."""
        node = self.items[position]
        line = node.start_mark.line
        if not self.lines[line][: node.start_mark.column].strip() and line > 0:
            if re.fullmatch(r"\s*-\s+[&!]\S+\s*", self.lines[line - 1]):
                return line - 1
        return line

    def dash_column(self, position: int) -> int:
        node = self.items[position]
        line = self.dash_line(position)
        column = node.start_mark.column if line == node.start_mark.line else len(self.lines[line])
        before = self.lines[line][:column].rstrip()
        if not before.endswith("-") and line != node.start_mark.line:
            before = self.lines[line].split("-", 1)[0] + "-"
        if not before.endswith("-"):
            raise ListEditError(f"{_a(self.noun)} entry does not start on its '- ' line; edit the file by hand")
        return len(before) - 1

    def span(self, position: int) -> tuple[int, int]:
        """Line range [start, end) of one entry.

        Trailing comment lines that are not clearly the entry's own are left
        out: before the next entry, any comment run (it introduces that entry);
        after the last entry, comments at or left of its dash. Blank lines are
        never moved on their own (they can belong to a keep-chomped scalar);
        they are excluded only together with a comment run that follows them.
        """
        start = self.dash_line(position)
        last = position + 1 >= len(self.items)
        if last:
            seq = self.value
            end = seq.end_mark.line + (1 if seq.end_mark.column > 0 else 0)
        else:
            end = self.dash_line(position + 1)
        end = min(end, len(self.lines))
        dash = self.dash_column(position)
        first_comment = None
        probe = end
        while probe > start + 1:
            line = self.lines[probe - 1]
            stripped = line.strip()
            if not stripped:
                probe -= 1
            elif stripped.startswith("#") and (not last or len(line) - len(line.lstrip()) <= dash):
                first_comment = probe - 1
                probe -= 1
            else:
                break
        return start, first_comment if first_comment is not None else end

    def render(self, entry: dict, dash: int) -> list[str]:
        body = dump_entry(entry).rstrip("\n").split("\n")
        pad = " " * dash
        return [pad + "- " + body[0]] + [pad + "  " + line if line else line for line in body[1:]]

    def check_anchors(self, removed: list[str], name: str) -> None:
        """Refuse an edit that leaves an alias without its anchor."""
        try:
            yaml.compose("\n".join(self.lines) + "\n")
        except YAML_LOAD_ERRORS as exc:
            anchors = sorted(set(re.findall(r"undefined alias '([^']+)'", str(exc))))
            if anchors:
                raise ListEditError(
                    f"{self.noun} {name!r} defines an anchor used elsewhere ({', '.join(repr('&' + a) for a in anchors)}); "
                    "edit the file by hand"
                ) from exc
            raise ListEditError(f"the edit would produce malformed YAML: {exc}") from exc

    def result(self) -> str:
        eol = "\r\n" if self.crlf else "\n"
        return eol.join(self.lines) + eol


def add_entry_text(
    text: str, entry: dict, *, key: str, ident: str, version: int, noun: str = "entry", unique: bool = True
) -> str:
    """Append an entry to the document, creating `version`/`key` as needed.

    `unique=False` skips the same-identity refusal, for lists whose identity may repeat.
    """
    doc = _Doc(text, key, ident, noun)
    if unique and doc.has(entry.get(ident)):
        raise ListEditError(f"{_a(noun)} named {entry.get(ident)!r} already exists")
    if not doc.lines:
        doc.lines = [f"version: {version}", f"{key}:"] + doc.render(entry, 0)
    elif doc.key_node is None:
        doc.lines += [f"{key}:"] + doc.render(entry, 0)
    elif doc.items:
        dash = doc.dash_column(len(doc.items) - 1)
        _, end = doc.span(len(doc.items) - 1)
        doc.lines[end:end] = doc.render(entry, dash)
    else:
        # `key:` with no value, `key: null` or `key: []`: turn it into a block list.
        line_no = doc.key_node.start_mark.line
        line = doc.lines[line_no]
        value = doc.value
        if isinstance(value, yaml.SequenceNode):
            start, end = value.start_mark.column, value.end_mark.column
        elif value.start_mark.line == line_no and value.value:
            start, end = value.start_mark.column, value.end_mark.column
        else:
            start = end = len(line)
        rest = line[end:].strip()
        head = line[:start].rstrip() + ("  " + rest if rest else "")
        doc.lines[line_no : line_no + 1] = [head] + doc.render(entry, doc.key_node.start_mark.column)
    return doc.result()


def replace_entry_text(text: str, name: str, entry: dict, *, key: str, ident: str, noun: str = "entry") -> str:
    """Swap one entry for a new mapping, in place."""
    doc = _Doc(text, key, ident, noun)
    position = doc.index(name)
    start, end = doc.span(position)
    removed = doc.lines[start:end]
    doc.lines[start:end] = doc.render(entry, doc.dash_column(position))
    doc.check_anchors(removed, name)
    return doc.result()


def delete_entry_text(text: str, name: str, *, key: str, ident: str, noun: str = "entry") -> str:
    """Remove one entry's lines; comments outside the entry stay."""
    doc = _Doc(text, key, ident, noun)
    position = doc.index(name)
    start, end = doc.span(position)
    removed = doc.lines[start:end]
    del doc.lines[start:end]
    if len(doc.items) == 1:
        line_no = doc.key_node.start_mark.line
        line = doc.lines[line_no]
        colon = line.index(":", doc.key_node.end_mark.column)
        doc.lines[line_no] = line[: colon + 1] + " []" + line[colon + 1 :].rstrip()
    doc.check_anchors(removed, name)
    return doc.result()
