"""Serving a repository's `devgraph.tools.yaml` tools over MCP.

The session's repository is resolved once at startup (see
`resolve_session_repo`) and never changes, so a session pinned to one
repository can't reach another's tools. Each valid tool becomes an MCP tool
with a typed input schema; a call injects the session's `repo_id` and runs
read-only, bounded by the tool's timeout and row cap
(`GraphEngine.run_read_cypher`).
"""

from __future__ import annotations

import datetime
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devgraph.config.project_tools import (
    INJECTED_PARAMETER,
    TOOLS_FILENAME,
    CypherTool,
    ProjectToolsError,
    parse_project_tools,
    tools_file_path,
)
from devgraph.mcp.catalog import builtin_tool_names

from mcp.server.mcpserver.exceptions import ToolError
from neo4j import time as neo4j_time
from neo4j.spatial import Point

logger = logging.getLogger(__name__)

SESSION_REPO_ENV = "DEVGRAPH_MCP_REPO"
RELOAD_INTERVAL_S = 2.0

_PYTHON_TYPES: dict[str, type] = {"string": str, "integer": int, "float": float, "boolean": bool}


def _resolved(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path)


def _deepest_containing(repos: list[Any], target: Path) -> Any | None:
    target = _resolved(target)
    best, best_depth = None, -1
    for repo in repos:
        root = _resolved(repo.path)
        if target == root or target.is_relative_to(root):
            depth = len(root.parts)
            if depth > best_depth:
                best, best_depth = repo, depth
    return best


def resolve_session_repo(registry: Any, env: Mapping[str, str], cwd: Path) -> tuple[Any | None, str]:
    """The repository this MCP session serves project tools for, and how it was chosen.

    `DEVGRAPH_MCP_REPO` (a repo id, or an absolute path inside a registered
    repository; a relative value is only ever an id) wins; a value that matches nothing yields no scope rather than falling
    back. Otherwise the process's working directory, if it lies inside a
    registered repository (the deepest one). Otherwise none.
    """
    repos = registry.list_repos(active_only=True)
    pinned = (env.get(SESSION_REPO_ENV) or "").strip()
    if pinned:
        by_id = next((r for r in repos if r.repo_id == pinned), None)
        if by_id is None and Path(pinned).expanduser().is_absolute():
            by_id = _deepest_containing(repos, Path(pinned))
        return by_id, "env"
    match = _deepest_containing(repos, cwd)
    return (match, "cwd") if match is not None else (None, "none")


def _inactive_match(registry: Any | None, pinned: str) -> Any | None:
    """The inactive registered repository a pin names (by id or absolute path), if any."""
    if registry is None or not pinned:
        return None
    inactive = [r for r in registry.list_repos(active_only=False) if not r.active]
    found = next((r for r in inactive if r.repo_id == pinned), None)
    if found is None and Path(pinned).expanduser().is_absolute():
        found = _deepest_containing(inactive, Path(pinned))
    return found


@dataclass
class ToolPlaneStatus:
    repo_id: str | None
    source: str
    tools_file: str | None = None
    served: list[str] = field(default_factory=list)
    parameter_names: dict[str, list[str]] = field(default_factory=dict)  # served tool -> declared parameters
    notices: list[str] = field(default_factory=list)
    definitions: dict[str, CypherTool] = field(default_factory=dict, repr=False)  # served tool -> its declaration

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": {"repo_id": self.repo_id, "source": self.source},
            "tools_file": self.tools_file,
            "served": list(self.served),
            "notices": list(self.notices),
        }


def _sanitize_deep(value: Any) -> Any:
    from devgraph.mcp.tools import _sanitize_value

    if isinstance(value, dict):
        return {k: _sanitize_deep(v) for k, v in value.items()}
    # Duration and Point are tuple subclasses: match them before the generic sequence case.
    if isinstance(value, (neo4j_time.Date, neo4j_time.DateTime, neo4j_time.Time, neo4j_time.Duration)):
        return value.iso_format()
    if isinstance(value, (datetime.date, datetime.time)):  # datetime is a date
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, Point):
        point = {"srid": value.srid, "x": value.x, "y": value.y}
        if len(value) > 2:
            point["z"] = value.z
        return point
    if isinstance(value, (list, tuple)):
        return [_sanitize_deep(v) for v in value]
    return _sanitize_value(value)


def _failure(tool: CypherTool, exc: Exception) -> ToolError:
    """A client-safe error: the Neo4j status code only, never the raw message."""
    from neo4j.exceptions import Neo4jError

    if isinstance(exc, Neo4jError):
        code = str(getattr(exc, "code", None) or "unknown")
        if "TransactionTimedOut" in code:
            return ToolError(f"project tool {tool.name!r} timed out after {tool.timeout_s}s")
        if "AccessMode" in code:
            return ToolError(f"project tool {tool.name!r} tried to write; project tools are read-only")
        return ToolError(f"project tool {tool.name!r} failed: {code}")
    return ToolError(f"project tool {tool.name!r} failed")


def make_tool_function(tool: CypherTool, engine: Any, repo_id: str) -> Callable[..., dict[str, Any]]:
    """A function the SDK can register: its signature is the tool's parameters."""
    def call(**kwargs: Any) -> dict[str, Any]:
        params = {
            p.name: p.default if kwargs.get(p.name) is None else kwargs[p.name] for p in tool.parameters
        }
        params[INJECTED_PARAMETER] = repo_id  # always the session's repository
        try:
            rows, truncated = engine.run_read_cypher(
                tool.cypher, params, timeout_s=tool.timeout_s, max_rows=tool.max_rows
            )
        except Exception as exc:  # the driver raises its own hierarchy
            raise _failure(tool, exc) from exc
        results = [_sanitize_deep(row) for row in rows]
        return {"count": len(results), "results": results, "truncated": truncated}

    parameters = []
    for p in tool.parameters:
        python_type = _PYTHON_TYPES[p.type]
        if p.required:
            parameters.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, annotation=python_type))
        else:
            parameters.append(
                inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, default=p.default, annotation=python_type | None)
            )
    call.__name__ = tool.name
    call.__qualname__ = tool.name
    call.__doc__ = tool.description
    call.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    call.__annotations__ = {**{p.name: p.annotation for p in parameters}, "return": dict[str, Any]}
    return call


def register_project_tools(
    server: Any,
    engine: Any,
    repo: Any | None,
    source: str,
    *,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]],
    annotations: Any,
    pinned: str | None = None,
    registry: Any | None = None,
    fingerprint: bytes | str | None = None,
) -> ToolPlaneStatus:
    """Register the session repository's tools on `server`; report what happened."""
    if repo is None:
        status = ToolPlaneStatus(repo_id=None, source=source)
        if source == "env":
            inactive = _inactive_match(registry, pinned or "")
            if inactive is not None:
                status.notices.append(
                    f"{SESSION_REPO_ENV}={pinned!r} names repository {inactive.repo_id!r}, which is "
                    f"registered but inactive; no project tools are served"
                )
            else:
                status.notices.append(
                    f"{SESSION_REPO_ENV}={pinned or ''!r} matches no registered repository; "
                    f"no project tools are served"
                )
        return status

    status = ToolPlaneStatus(repo_id=repo.repo_id, source=source)
    _serve_repository(server, engine, repo, status, instrument=instrument, annotations=annotations,
                      fingerprint=fingerprint)
    return status


def _serve_repository(
    server: Any,
    engine: Any,
    repo: Any,
    status: ToolPlaneStatus,
    *,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]],
    annotations: Any,
    fingerprint: bytes | str | None = None,
) -> None:
    """Parse the repository's tools file and register its tools, recording the outcome on `status`.

    The file is read once, as `fingerprint` (its bytes); the tools served are exactly
    the ones those bytes declare. Without a `fingerprint` it is read now.
    """
    if fingerprint is None:
        fingerprint = tools_fingerprint(repo.path)
    if fingerprint == "root-missing":
        status.notices.append(f"the root of repository {repo.repo_id!r} ({repo.path}) does not exist; no project tools are served")
        return
    if fingerprint == "absent":
        return
    path = tools_file_path(repo.path)
    try:
        if not isinstance(fingerprint, bytes):
            raise ProjectToolsError(f"{path}: cannot be read: {fingerprint.partition(':')[2]}")
        try:
            text = fingerprint.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectToolsError(f"{path}: is not valid UTF-8: {exc}") from exc
        declared = parse_project_tools(text, path)
    except ProjectToolsError as exc:
        status.notices.append(f"invalid {TOOLS_FILENAME}; no project tools are served: {str(exc).splitlines()[0]}")
        return

    status.tools_file = str(tools_file_path(repo.path))
    builtin = builtin_tool_names()
    for tool in declared.tools:
        if tool.name in builtin:
            status.notices.append(
                f"ignored: project tool {tool.name!r} has the name of a built-in tool; using the built-in"
            )
    for tool in declared.tools:
        if tool.name in builtin:
            continue
        try:
            fn = instrument(make_tool_function(tool, engine, repo.repo_id))
            server.add_tool(fn, name=tool.name, description=tool.description, annotations=annotations)
        except Exception as exc:
            status.notices.append(f"project tool {tool.name!r} could not be served: "
                f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"
            )
            continue
        status.served.append(tool.name)
        status.parameter_names[tool.name] = [p.name for p in tool.parameters]
        status.definitions[tool.name] = tool


def tools_fingerprint(repo_path: Path | str) -> bytes | str:
    """What the tools file looks like now: its bytes, 'root-missing', 'absent', or 'unreadable:<error>'."""
    if not Path(repo_path).is_dir():
        return "root-missing"
    try:
        return tools_file_path(Path(repo_path)).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


class ProjectToolPlane:
    """The session's served project tools, reloadable when the tools file changes.

    The scope (`repo`) never changes; only the tools file is re-read, under the
    same rules as at startup.
    """

    def __init__(self, server: Any, engine: Any, repo: Any | None, status: ToolPlaneStatus, *,
                 instrument: Callable[[Callable[..., Any]], Callable[..., Any]], annotations: Any,
                 fingerprint: bytes | str | None = None) -> None:
        """`fingerprint` is the tools file as the initial load read it (see `register_project_tools`)."""
        self.server, self.engine, self.repo, self.status = server, engine, repo, status
        self._instrument, self._annotations = instrument, annotations
        self._fingerprint = fingerprint

    def reload_if_changed(self) -> bool:
        """Re-serve the tools file if its bytes changed. True when the served tools changed."""
        if self.repo is None:
            return False
        fingerprint = tools_fingerprint(self.repo.path)
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint = None  # until the reload succeeds, so a failure is retried
        before = dict(self.status.definitions)
        for name in self.status.served:
            self.server.remove_tool(name)
        self.status.tools_file = None
        self.status.served.clear()
        self.status.parameter_names.clear()
        self.status.definitions.clear()
        self.status.notices.clear()
        _serve_repository(self.server, self.engine, self.repo, self.status,
                          instrument=self._instrument, annotations=self._annotations, fingerprint=fingerprint)
        self._fingerprint = fingerprint  # the reload parsed exactly these bytes
        if self.status.notices:
            logger.warning("reloaded %s with problems: %s", tools_file_path(self.repo.path), self.status.notices[0])
        return self.status.definitions != before
