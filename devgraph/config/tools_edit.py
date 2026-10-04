"""Comment-preserving edits to a tools document (`devgraph.tools.yaml`).

The tools file is committed and hand-edited, so its comments matter. PyYAML
cannot round-trip them, so edits are text splices: `yaml.compose` gives each
tool's line range and only those lines change; every other byte is kept.
Functions here do not validate tool semantics -- callers validate the result
with `parse_project_tools`.
"""

from __future__ import annotations

import yaml

from devgraph.config.project_tools import TOOLS_VERSION


class ToolsEditError(Exception):
    """A tools document cannot be edited as asked."""


class _ToolDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ToolDumper.add_representer(str, _represent_str)


def dump_tool(tool: dict) -> str:
    """YAML for one tool mapping: key order kept, multi-line strings as `|` blocks."""
    return yaml.dump(
        tool, Dumper=_ToolDumper, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000
    )


def _load(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolsEditError(f"malformed YAML: {exc}") from exc


def tool_mappings(text: str) -> list[dict]:
    """The raw tool mappings of a tools document; `[]` for empty text or no tools."""
    document = _load(text)
    if document is None:
        return []
    if not isinstance(document, dict):
        raise ToolsEditError("expected a YAML mapping at the document root")
    tools = document.get("tools")
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ToolsEditError("'tools' must be a list")
    return tools


class _Doc:
    """A tools document split into lines, with the `tools` node located."""

    def __init__(self, text: str) -> None:
        self.crlf = "\r\n" in text
        self.lines = text.replace("\r\n", "\n").split("\n")
        if self.lines[-1] == "":
            self.lines.pop()
        normalized = "\n".join(self.lines) + "\n" if self.lines else ""
        self.mappings = tool_mappings(normalized)  # also validates root and `tools` shape
        self.key = self.value = None
        self.items: list[yaml.Node] = []
        try:
            root = yaml.compose(normalized)
        except yaml.YAMLError as exc:
            raise ToolsEditError(f"malformed YAML: {exc}") from exc
        if root is None:
            return
        for key, value in root.value:
            if key.value == "tools":
                self.key, self.value = key, value
        if isinstance(self.value, yaml.SequenceNode):
            self.items = self.value.value
            if self.value.flow_style and self.items:
                raise ToolsEditError(
                    "'tools' is a flow-style list; edit it by hand or rewrite it as a block list"
                )

    def index(self, name: str) -> int:
        for position, mapping in enumerate(self.mappings):
            if isinstance(mapping, dict) and mapping.get("name") == name:
                return position
        raise ToolsEditError(f"no tool named {name!r}")

    def has(self, name: str) -> bool:
        return any(isinstance(m, dict) and m.get("name") == name for m in self.mappings)

    def dash_column(self, position: int) -> int:
        node = self.items[position]
        before = self.lines[node.start_mark.line][: node.start_mark.column].rstrip()
        if not before.endswith("-"):
            raise ToolsEditError("a tool entry does not start on its '- ' line; edit the file by hand")
        return len(before) - 1

    def span(self, position: int) -> tuple[int, int]:
        """Line range [start, end) of one tool, without trailing blank lines or comments at or left of its dash."""
        start = self.items[position].start_mark.line
        if position + 1 < len(self.items):
            end = self.items[position + 1].start_mark.line
        else:
            seq = self.value
            end = seq.end_mark.line + (1 if seq.end_mark.column > 0 else 0)
        end = min(end, len(self.lines))
        dash = self.dash_column(position)
        while end > start + 1:
            line = self.lines[end - 1]
            stripped = line.strip()
            if not stripped or (stripped.startswith("#") and len(line) - len(line.lstrip()) <= dash):
                end -= 1
            else:
                break
        return start, end

    def render(self, tool: dict, dash: int) -> list[str]:
        body = dump_tool(tool).rstrip("\n").split("\n")
        pad = " " * dash
        return [pad + "- " + body[0]] + [pad + "  " + line if line else line for line in body[1:]]

    def result(self) -> str:
        eol = "\r\n" if self.crlf else "\n"
        return eol.join(self.lines) + eol


def add_tool_text(text: str, tool: dict) -> str:
    """Append a tool to the document, creating `version`/`tools` as needed."""
    doc = _Doc(text)
    if doc.has(tool.get("name")):
        raise ToolsEditError(f"a tool named {tool.get('name')!r} already exists")
    if not doc.lines:
        doc.lines = [f"version: {TOOLS_VERSION}", "tools:"] + doc.render(tool, 0)
    elif doc.key is None:
        doc.lines += ["tools:"] + doc.render(tool, 0)
    elif doc.items:
        dash = doc.dash_column(len(doc.items) - 1)
        _, end = doc.span(len(doc.items) - 1)
        doc.lines[end:end] = doc.render(tool, dash)
    else:
        # `tools:` with no value, `tools: null` or `tools: []`: turn it into a block list.
        line_no = doc.key.start_mark.line
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
        doc.lines[line_no : line_no + 1] = [head] + doc.render(tool, doc.key.start_mark.column)
    return doc.result()


def replace_tool_text(text: str, name: str, tool: dict) -> str:
    """Swap one tool for a new mapping, in place."""
    doc = _Doc(text)
    position = doc.index(name)
    start, end = doc.span(position)
    doc.lines[start:end] = doc.render(tool, doc.dash_column(position))
    return doc.result()


def delete_tool_text(text: str, name: str) -> str:
    """Remove one tool's lines; comments outside the tool stay."""
    doc = _Doc(text)
    position = doc.index(name)
    start, end = doc.span(position)
    del doc.lines[start:end]
    if len(doc.items) == 1:
        line_no = doc.key.start_mark.line
        doc.lines[line_no] = doc.lines[line_no][: doc.key.end_mark.column + 1] + " []"
    return doc.result()
