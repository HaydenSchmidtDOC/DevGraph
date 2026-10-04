"""The global tools store: `global-tools.json` in the user's DevGraph directory.

Global tools are the user's own Cypher tools, available in every scoped MCP
session. They live next to the registry database, never in the install
location. The file holds `{"version": 1, "tools": [...]}` and is validated by
the same loader as `devgraph.tools.yaml` (JSON is YAML). Writes are atomic
(temporary file plus rename) so a reader never sees a half-written file.

Import this module directly, like `devgraph.config.project_tools`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from devgraph.config.project_tools import (
    TOOLS_VERSION,
    ProjectTools,
    ProjectToolsError,
    parse_project_tools,
    validate_project_tools,
)
from devgraph.config.settings import get_settings

GLOBAL_TOOLS_FILENAME = "global-tools.json"


def _default_path() -> Path:
    return get_settings().registry_db_path.parent / GLOBAL_TOOLS_FILENAME


def global_tools_path() -> Path:
    """Where the global tools store lives."""
    return _default_path()


def load_global_tools(path: Path | None = None) -> ProjectTools | None:
    """Load and validate the global store; None if and only if it is absent."""
    path = path or global_tools_path()
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise ProjectToolsError(f"{path}: global tools store is not a regular file")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectToolsError(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectToolsError(f"{path}: is not valid UTF-8: {exc}") from exc
    return parse_project_tools(text, path)


def global_tools_fingerprint(path: Path | None = None) -> bytes | str:
    """The store's bytes, or "absent" / "unreadable:<Exc>": a cheap change detector."""
    path = path or global_tools_path()
    try:
        if not path.exists():
            return "absent"
        return path.read_bytes()
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def global_tools_text(tool_mappings: list[dict], path: Path | None = None) -> str:
    """The store's file text for these tools, validated; raises ProjectToolsError. Writes nothing."""
    path = path or global_tools_path()
    document = {"version": TOOLS_VERSION, "tools": tool_mappings}
    # Validate the mappings first: a YAML-sourced value (a date, say) is refused
    # by the schema here rather than failing in json.dumps.
    validate_project_tools(document, path)
    try:
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:  # a value the schema coerces but JSON can't hold (bytes)
        raise ProjectToolsError(f"{path}: a tool holds a value JSON cannot store: {exc}") from exc
    parse_project_tools(text, path)
    return text


def save_global_tools(tool_mappings: list[dict], path: Path | None = None) -> None:
    """Validate and atomically write the store; the existing file is untouched on any failure."""
    path = path or global_tools_path()
    text = global_tools_text(tool_mappings, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
