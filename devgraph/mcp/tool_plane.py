"""Serving a repository's `devgraph.tools.yaml` tools over MCP.

The session's repository is resolved once at startup (see
`resolve_session_repo`) and never changes, so a session pinned to one
repository can't reach another's tools. Each valid tool becomes an MCP tool
with a typed input schema; a call injects the session's `repo_id` and runs
read-only, bounded by the tool's timeout and row cap
(`GraphEngine.run_read_cypher`).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devgraph.config.project_tools import (
    INJECTED_PARAMETER,
    TOOLS_FILENAME,
    CypherTool,
    ProjectToolsError,
    load_project_tools,
    tools_file_path,
)
from devgraph.mcp.catalog import builtin_tool_names

from mcp.server.mcpserver.exceptions import ToolError

SESSION_REPO_ENV = "DEVGRAPH_MCP_REPO"

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


@dataclass
class ToolPlaneStatus:
    repo_id: str | None
    source: str
    tools_file: str | None = None
    served: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

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


def make_tool_function(tool: CypherTool, engine: Any, repo_id: str, notices: list[str]) -> Callable[..., dict[str, Any]]:
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
        envelope: dict[str, Any] = {"count": len(results), "results": results, "truncated": truncated}
        if notices:
            envelope["notices"] = list(notices)
        return envelope

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
) -> ToolPlaneStatus:
    """Register the session repository's tools on `server`; report what happened."""
    if repo is None:
        status = ToolPlaneStatus(repo_id=None, source=source)
        if source == "env":
            status.notices.append(
                f"{SESSION_REPO_ENV}={pinned or ''!r} matches no registered repository; "
                f"no project tools are served"
            )
        return status

    status = ToolPlaneStatus(repo_id=repo.repo_id, source=source)
    try:
        declared = load_project_tools(repo.path)
    except ProjectToolsError as exc:
        status.notices.append(f"invalid {TOOLS_FILENAME}; no project tools are served: {str(exc).splitlines()[0]}")
        return status
    if declared is None:
        return status

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
            fn = instrument(make_tool_function(tool, engine, repo.repo_id, status.notices))
            server.add_tool(fn, name=tool.name, description=tool.description, annotations=annotations)
        except Exception as exc:
            status.notices.append(f"project tool {tool.name!r} could not be served: {type(exc).__name__}")
            continue
        status.served.append(tool.name)
    return status
