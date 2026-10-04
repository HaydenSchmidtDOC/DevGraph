"""Comment-preserving edits to a tools document (`devgraph.tools.yaml`).

Thin wrappers over `devgraph.config.list_edit` fixing the `tools` list, its
`name` identity field and the tools file version. Functions here do not
validate tool semantics -- callers validate the result with `parse_project_tools`.
"""

from __future__ import annotations

from devgraph.config.list_edit import (
    ListEditError,
    add_entry_text,
    delete_entry_text,
    dump_entry,
    entries,
    replace_entry_text,
)
from devgraph.config.project_tools import TOOLS_VERSION

_KEY = "tools"
_IDENT = "name"
_NOUN = "tool"


#: A tools document cannot be edited as asked.
ToolsEditError = ListEditError


def dump_tool(tool: dict) -> str:
    """YAML for one tool mapping: key order kept, multi-line strings as `|` blocks."""
    return dump_entry(tool)


def tool_mappings(text: str) -> list[dict]:
    """The raw tool mappings of a tools document; `[]` for empty text or no tools."""
    return entries(text, key=_KEY)


def add_tool_text(text: str, tool: dict) -> str:
    """Append a tool to the document, creating `version`/`tools` as needed."""
    return add_entry_text(text, tool, key=_KEY, ident=_IDENT, version=TOOLS_VERSION, noun=_NOUN)


def replace_tool_text(text: str, name: str, tool: dict) -> str:
    """Swap one tool for a new mapping, in place."""
    return replace_entry_text(text, name, tool, key=_KEY, ident=_IDENT, noun=_NOUN)


def delete_tool_text(text: str, name: str) -> str:
    """Remove one tool's lines; comments outside the tool stay."""
    return delete_entry_text(text, name, key=_KEY, ident=_IDENT, noun=_NOUN)
