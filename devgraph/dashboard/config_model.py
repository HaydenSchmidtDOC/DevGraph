"""The Config page's read model: global and per-project node types, relationship types and tools.

Pure assembly over files, the registry and the tool plane's own dry resolution
(`resolve_tools`), so the badges say what an MCP session for the repo would
actually do. Nothing here writes, and nothing touches Neo4j: the caller supplies
the schema state (`schema_info`) computed from the applied schema.
"""

from __future__ import annotations

import inspect
import math
from pathlib import Path
from typing import Any, Callable

from devgraph.config import edits
from devgraph.config.list_edit import ListEditError, dump_entry, entries
from devgraph.config.project_schema import SCHEMA_FILENAME
from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError, parse_project_tools
from devgraph.config.project_trust import trust_command, untrusted_reason
from devgraph.config.settings import get_settings
from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES
from devgraph.mcp.catalog import TOOL_CATALOG, builtin_tool_names, scoped_tool_id

GLOBAL_SCOPE = "__global__"
GLOBAL_TOOLS_FILENAME = "global-tools.json"
_MAX_SAFE_INT = 2**53 - 1  # the largest integer a JavaScript number carries exactly


def _plain(value: Any, ancestors: frozenset[int] = frozenset()) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        return abs(value) <= _MAX_SAFE_INT
    if isinstance(value, float):
        return math.isfinite(value) and not value.is_integer()  # JSON cannot tell 3.0 from 3
    if isinstance(value, (list, dict)):
        if id(value) in ancestors:  # a self-referencing YAML anchor
            return False
        inside = ancestors | {id(value)}
        if isinstance(value, list):
            return all(_plain(v, inside) for v in value)
        return all(isinstance(k, str) and _plain(v, inside) for k, v in value.items())
    return False


def form_entry(entry: dict) -> dict | None:
    """The entry mapping when JSON carries it into the browser unchanged, else `None` (YAML-only)."""
    return entry if _plain(entry) else None


def badge(level: str, kind: str, text: str, detail: str = "") -> dict[str, str]:
    return {"level": level, "kind": kind, "text": text, "detail": detail}


def _display_path(path: Path) -> str:
    """The repo path with the home directory abbreviated, for display only."""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except (ValueError, RuntimeError, OSError):
        return str(path)


def scrub(message: str, path: Path, root: Path | None = None) -> str:
    """The message with absolute paths replaced by the file name (no host paths in the browser)."""
    for prefix, label in ((str(path), path.name), (str(root) if root else None, "")):
        if prefix:
            message = message.replace(prefix + "/", "").replace(prefix, label)
    return message


def _tool_entries(text: str) -> list[dict]:
    """The readable tool mappings with a string name; an unreadable document lists nothing."""
    try:
        found = entries(text, key="tools")
    except ListEditError:
        return []
    return [e for e in found if isinstance(e, dict) and isinstance(e.get("name"), str)]


def _read(path: Path, root: Path | None = None) -> tuple[str, str | None]:
    """The file's text ("" when absent) and a read error, if any; with `root`, see `edits.read_text`."""
    try:
        return edits.read_text(path, root), None
    except edits.ConfigEditError as exc:
        return "", scrub(exc.message, path)


def _tools_file_state(path: Path, text: str, read_error: str | None) -> tuple[str, str | None]:
    if read_error is not None:
        return "invalid", read_error
    if edits.file_fingerprint(path) == "absent":
        return "absent", None
    try:
        parse_project_tools(text, path)
    except ProjectToolsError as exc:
        return "invalid", scrub(str(exc), path)
    return "valid", None


def _builtin_tools() -> list[dict[str, Any]]:
    from devgraph.mcp import tools as devgraph_tools

    show_cypher = get_settings().enable_run_cypher
    return [
        {
            "name": tool["name"],
            "tool_id": scoped_tool_id(tool["name"], "builtin"),
            "locked": True,
            "description": (inspect.getdoc(getattr(devgraph_tools, tool["name"], None)) or "").split("\n")[0],
        }
        for tool in TOOL_CATALOG
        if show_cypher or tool["name"] != "run_cypher"
    ]


def _file_badges(state: str, error: str | None, filename: str) -> list[dict[str, str]]:
    if state != "invalid":
        return []
    return [badge(
        "error", "file-invalid", f"{filename} is invalid",
        f"{error} MCP sessions keep their last good tools if they had any.",
    )]


def _resolutions(records: list[Any]) -> dict[str, Any]:
    from devgraph.mcp.tool_plane import resolve_tools

    return {record.repo_id: resolve_tools(record) for record in records}


def _global_entry(entry: dict, overridden_in: list[str]) -> dict[str, Any]:
    name = entry["name"]
    badges: list[dict[str, str]] = []
    if name in builtin_tool_names():
        badges.append(badge("warn", "locked-shadow", "Ignored: shadows a locked tool",
                            "The fixed built-in implementation is used instead."))
    if overridden_in:
        badges.append(badge("info", "overridden", f"Overridden in {', '.join(overridden_in)}",
                            "These repositories' own tools of this name win over the global one."))
    return {"name": name, "tool_id": scoped_tool_id(name, "global"), "yaml": dump_entry(entry),
            "entry": form_entry(entry), "badges": badges}


def build_global(records: list[Any], resolutions: dict[str, Any] | None = None) -> dict[str, Any]:
    """The global block; `resolutions` (repo id -> resolved status) saves resolving each repo again."""
    resolutions = resolutions if resolutions is not None else _resolutions(records)
    path = edits.tools_path(None)
    text, read_error = _read(path)
    state, error = _tools_file_state(path, text, read_error)
    overridden: dict[str, list[str]] = {}
    for repo_id, status in resolutions.items():
        for name, origin in status.origins.items():
            if origin == "project (overrides global)":
                overridden.setdefault(name, []).append(repo_id)
    return {
        "node_types": [{"label": label, "locked": True} for label in NODE_LABELS],
        "relationship_types": [{"type": rel, "locked": True} for rel in RELATIONSHIP_TYPES],
        "tools": {
            "file": GLOBAL_TOOLS_FILENAME,
            "state": state,
            "error": error,
            "fingerprint": edits.file_fingerprint(path),
            "badges": _file_badges(state, error, GLOBAL_TOOLS_FILENAME),
            "effect_note": edits.tools_effect_note(None, None),
            "builtin": _builtin_tools(),
            "run_cypher_enabled": get_settings().enable_run_cypher,
            "entries": [_global_entry(e, overridden.get(e["name"], [])) for e in _tool_entries(text)],
        },
    }


def _project_tool_entry(record: Any, status: Any, entry: dict, tools_path: Path, root: Path) -> dict[str, Any]:
    name = entry["name"]
    origin = status.origins.get(name)
    badges: list[dict[str, str]] = []
    if not record.project_config_enabled:
        badges.append(badge("muted", "not-served", "Not served: project config disabled",
                            f"Enable it with `devgraph config enable {record.repo_id}`."))
    elif name in status.shadowed and status.project_invalid is None:  # a valid file: the project layer shadowed it
        badges.append(badge("warn", "locked-shadow", "Ignored: shadows a locked tool",
                            "The fixed built-in implementation is used instead."))
    elif origin == "project (overrides global)":
        badges.append(badge("info", "overrides-global", "Overrides global tool",
                            f"This repository's {name!r} wins over the global tool of the same name."))
    elif origin == "global":
        if status.project_invalid is not None:
            reason = (f"{TOOLS_FILENAME} is invalid, so the project tool of this name can't be served: "
                      f"{scrub(status.project_invalid, tools_path, root)}")
        else:
            reason = status.fallback_reasons.get(name, "")
        badges.append(badge("warn", "fallback-global", "Not served: using the global tool", reason))
    elif origin is None and status.project_trust not in (None, "trusted"):
        badges.append(badge("warn", "not-trusted", "Not served: not trusted",
                            status.fallback_reasons.get(name) or untrusted_reason(record.repo_id, status.project_trust)))
    elif origin is None and name in status.fallback_reasons:
        badges.append(badge("error", "not-served", "Not served", status.fallback_reasons[name]))
    return {
        "name": name,
        "tool_id": scoped_tool_id(name, "project", record.repo_id),
        "yaml": dump_entry(entry),
        "entry": form_entry(entry),
        "origin": origin,
        "badges": badges,
    }


_WHOLE_GRAPH = "An enabled project tool can read the whole graph: the $repo_id rule is a convention, not a sandbox."


def _trust_badges(repo_id: str, trust: str | None) -> list[dict[str, str]]:
    """The tools file's trust badge; none without a file to trust. Trusting is CLI-only."""
    if trust is None:
        return []
    if trust == "trusted":
        return [badge("info", "trusted", "Trusted",
                      f"Served: {TOOLS_FILENAME} matches the sha256 approved with `{trust_command(repo_id)}`. "
                      f"Any change to the file needs approving again. {_WHOLE_GRAPH}")]
    text = {"changed": "Changed since trusted", "error": "Trust state unreadable"}.get(trust, "Not trusted")
    return [badge("error" if trust == "error" else "warn", "not-trusted", text,
                  f"{untrusted_reason(repo_id, trust)}. Review the file, then run that in a terminal. {_WHOLE_GRAPH}")]


def _schema_badges(state: str, error: str | None) -> list[dict[str, str]]:
    return {
        "pending": [badge("warn", "schema-pending", "Schema change pending",
                          "The schema file changed since it was applied; rescan to apply it.")],
        "never": [badge("warn", "schema-never", "Schema never applied",
                        "The schema file has not been applied to the graph yet; rescan to apply it.")],
        "invalid": [badge("error", "schema-invalid", f"{SCHEMA_FILENAME} is invalid", error or "")],
        "disabled": [badge("muted", "not-served", "Project config disabled",
                           "The schema file is not applied while project config is disabled.")],
        "unknown": [badge("muted", "graph-unavailable", "Graph unavailable",
                          "Neo4j could not be reached, so whether the schema file is applied is unknown.")],
    }.get(state, [])


def _conflict_badges(label: str, repo_id: str, conflicts: list[dict]) -> list[dict[str, str]]:
    for finding in conflicts:
        if finding["label"] != label.casefold() or repo_id not in finding["repo_ids"]:
            continue
        mine = next((d for d in finding["declarations"] if d["repo_id"] == repo_id), None)
        if mine is None:
            continue
        others = sorted({
            (d["repo_id"], d["disabled"]) for d in finding["declarations"]
            if d["repo_id"] != repo_id and (d["label"], d["key"]) != (mine["label"], mine["key"])
        })
        # a disabled peer still holds its constraint until its next rescan, so it is named, and marked
        names = ", ".join(f"{other}{' (disabled)' if disabled else ''}" for other, disabled in others)
        return [badge("error", "schema-conflict", f"Key conflict with {names}", finding["detail"])]
    return []


def _schema_block(record: Any, root: Path, info: dict[str, Any], conflicts: list[dict]) -> dict[str, Any]:
    from devgraph.config.project_schema import ProjectSchemaError, parse_project_schema

    path = root / SCHEMA_FILENAME
    text, read_error = _read(path)
    state = info["state"]
    error = info.get("error") or read_error
    if read_error and state != "disabled":
        state = "invalid"
    if error:
        error = scrub(error, path, root)

    node_entries: list[dict] = []
    rel_entries: list[dict] = []
    extends = "default"
    try:
        node_entries = [e for e in entries(text, key="node_types") if isinstance(e, dict) and isinstance(e.get("label"), str)]
        rel_entries = [e for e in entries(text, key="relationships") if isinstance(e, dict) and isinstance(e.get("type"), str)]
    except ListEditError:
        pass
    try:
        declaration = parse_project_schema(text, path)
        if declaration is not None:
            extends = declaration.extends
    except ProjectSchemaError:
        pass

    counts: dict[str, int] = {}
    for entry in rel_entries:
        counts[entry["type"]] = counts.get(entry["type"], 0) + 1
    relationships = []
    for entry in rel_entries:
        repeated = counts[entry["type"]] > 1
        relationships.append({
            "type": entry["type"],
            "yaml": dump_entry(entry),
            "editable": not repeated,
            "badges": [badge("warn", "ambiguous", "Declared more than once",
                             "Edit the file by hand.")] if repeated else [],
        })
    return {
        "file": SCHEMA_FILENAME,
        "state": state,
        "error": error,
        "fingerprint": edits.file_fingerprint(path),
        "extends": extends,
        "badges": _schema_badges(state, error),
        "node_types": [
            {"label": e["label"], "yaml": dump_entry(e), "entry": form_entry(e), "editable": True,
             "badges": _conflict_badges(e["label"], record.repo_id, conflicts)}
            for e in node_entries
        ],
        "relationships": relationships,
    }


def build_project(
    record: Any, schema_info: Callable[[Any], dict[str, Any]], status: Any | None = None,
    conflicts: list[dict] | None = None,
) -> dict[str, Any]:
    from devgraph.mcp.tool_plane import resolve_tools

    root = Path(record.path)
    status = status if status is not None else resolve_tools(record)
    path = edits.tools_path(root)
    text, read_error = _read(path, root)
    if not record.project_config_enabled:
        state, error = "disabled", None
    elif status.project_invalid is not None:
        state, error = "invalid", scrub(status.project_invalid, path, root)
    else:
        state, error = _tools_file_state(path, text, read_error)
    badges = _file_badges(state, error, TOOLS_FILENAME)
    if state == "disabled":
        badges.append(badge("muted", "not-served", "Project config disabled",
                            "Project tools are not served until it is enabled."))
    elif not (state == "invalid" and status.project_trust == "trusted"):  # an invalid file serves nothing new
        badges += _trust_badges(record.repo_id, status.project_trust)
    return {
        "repo_id": record.repo_id,
        "display_path": _display_path(root),
        "project_config_enabled": record.project_config_enabled,
        "effect_notes": {
            "tools": edits.tools_effect_note(root, record),
            "schema": edits.schema_effect_note(root, record),
        },
        "schema": _schema_block(record, root, schema_info(record), conflicts or []),
        "tools": {
            "file": TOOLS_FILENAME,
            "state": state,
            "error": error,
            "fingerprint": edits.file_fingerprint(path),
            "badges": badges,
            "trust": {
                "state": status.project_trust,
                "command": trust_command(record.repo_id),
                "revocable": getattr(record, "project_tools_sha256", None) is not None,
            },
            "entries": [_project_tool_entry(record, status, e, path, root) for e in _tool_entries(text)],
        },
    }


def build_config(
    records: list[Any], schema_info: Callable[[Any], dict[str, Any]], conflicts: list[dict] | None = None
) -> dict[str, Any]:
    resolutions = _resolutions(records)  # each repo once, shared by the global and project blocks
    return {
        "global": build_global(records, resolutions),
        "projects": [build_project(r, schema_info, status=resolutions[r.repo_id], conflicts=conflicts) for r in records],
    }
