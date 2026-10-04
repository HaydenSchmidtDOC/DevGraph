"""Serving a repository's `devgraph.tools.yaml` tools, and the user's global tools, over MCP.

The session's repository is resolved once at startup (see
`resolve_session_repo`) and never changes, so a session pinned to one
repository can't reach another's tools. Each valid tool becomes an MCP tool
with a typed input schema; a call injects the session's `repo_id` and runs
read-only, bounded by the tool's timeout and row cap
(`GraphEngine.run_read_cypher`). Global tools (`global-tools.json`) are served
only in a scoped session; a project tool replaces a global one of the same name,
and a global one stands in when that project tool can't be served.
"""

from __future__ import annotations

import datetime
import inspect
import logging
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from devgraph.config.global_tools import GLOBAL_TOOLS_FILENAME, global_tools_fingerprint, global_tools_path
from devgraph.config.project_switch import project_config_enabled
from devgraph.config.project_tools import (
    INJECTED_PARAMETER,
    TOOLS_FILENAME,
    CypherTool,
    ProjectTools,
    YAML_LOAD_ERRORS,
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
    global_tools_file: str | None = None
    origins: dict[str, str] = field(default_factory=dict)  # served tool -> "global" | "project" | "project (overrides global)"
    functions: dict[str, Callable[..., Any]] = field(default_factory=dict, repr=False)  # served tool -> registered function
    # built-in tool -> the response notices saying which declared tools of its name were ignored
    shadowed: dict[str, list[str]] = field(default_factory=dict)
    # why the project file served none of its tools because it is invalid (None otherwise), and the names it declares as far as readable
    project_invalid: str | None = None
    project_invalid_names: set[str] | None = None
    # project tool -> why registering it failed (the global tool of its name, if any, is served instead)
    fallback_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": {"repo_id": self.repo_id, "source": self.source},
            "tools_file": self.tools_file,
            "global_tools_file": self.global_tools_file,
            "served": list(self.served),
            "origins": dict(self.origins),
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


def _failure(tool: CypherTool, exc: Exception, layer: str = "project") -> ToolError:
    """A client-safe error: the Neo4j status code only, never the raw message. `layer` is "project" or "global"."""
    from neo4j.exceptions import Neo4jError

    if isinstance(exc, Neo4jError):
        code = str(getattr(exc, "code", None) or "unknown")
        if "TransactionTimedOut" in code:
            return ToolError(f"{layer} tool {tool.name!r} timed out after {tool.timeout_s}s")
        if "AccessMode" in code:
            return ToolError(f"{layer} tool {tool.name!r} tried to write; {layer} tools are read-only")
        return ToolError(f"{layer} tool {tool.name!r} failed: {code}")
    return ToolError(f"{layer} tool {tool.name!r} failed")


def make_tool_function(
    tool: CypherTool, engine: Any, repo_id: str, notices: list[str] | None = None, layer: str = "project"
) -> Callable[..., dict[str, Any]]:
    """A function the SDK can register: its signature is the tool's parameters.

    `notices` (how this tool was resolved) go in every response's envelope when non-empty;
    `layer` ("project" or "global") names where the tool came from in its errors.
    """
    notices = list(notices or [])

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
            raise _failure(tool, exc, layer) from exc
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


class _NullServer:
    """A server that registers nothing: dry resolution only needs the outcome, not a live MCP server."""

    def add_tool(self, *args: Any, **kwargs: Any) -> None:
        return None


def _identity(fn: Callable[..., Any]) -> Callable[..., Any]:
    return fn


def resolve_tools(
    repo: Any | None,
    source: str = "dashboard",
    *,
    server: Any | None = None,
    engine: Any | None = None,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]] = _identity,
    annotations: Any = None,
    pinned: str | None = None,
    registry: Any | None = None,
    fingerprint: bytes | str | None = None,
    global_fingerprint: bytes | str | None = None,
) -> ToolPlaneStatus:
    """What a session scoped to `repo` serves (None: an unscoped session): the one resolution, for MCP and the dashboard.

    With a `server` the resolved tools are registered on it; without one (the
    dashboard) nothing is registered and no engine is touched, but `served`, `origins`,
    `notices`, `shadowed`, `definitions` and `project_invalid` are exactly what a
    session would report. `pinned`/`registry` only refine the unscoped notice.

    One thing the dry path cannot predict: a tool the live server refuses at
    registration (`server.add_tool` raising, e.g. a parameter schema the SDK
    can't build). Live, that project tool is not served -- the global tool of its
    name stands in if there is one -- and the reason lands in `fallback_reasons`
    and `notices`; dry, the null server accepts everything, so `fallback_reasons`
    stays empty and the tool is reported as served.
    """
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
        if global_fingerprint is None:
            global_fingerprint = global_tools_fingerprint()
        if global_fingerprint != "absent":
            status.notices.append(
                "global tools are served only in sessions scoped to a repository (they need one for $repo_id); "
                "this session has none"
            )
        return status

    status = ToolPlaneStatus(repo_id=repo.repo_id, source=source)
    _serve_repository(server if server is not None else _NullServer(), engine, repo, status,
                      instrument=instrument, annotations=annotations,
                      fingerprint=fingerprint, global_fingerprint=global_fingerprint)
    return status


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
    global_fingerprint: bytes | str | None = None,
) -> ToolPlaneStatus:
    """Register the session repository's project and global tools on `server`; report what happened."""
    return resolve_tools(repo, source, server=server, engine=engine, instrument=instrument,
                         annotations=annotations, pinned=pinned, registry=registry,
                         fingerprint=fingerprint, global_fingerprint=global_fingerprint)


@dataclass
class _Resolved:
    """What one layer's file contributes: its tools (None for none).

    For a project file that served none because it is invalid: why (`invalid`), and the
    tool names it declares as far as they can be read (`invalid_names`, None if unknown).
    """

    declared: ProjectTools | None = None
    invalid: str | None = None
    invalid_names: set[str] | None = None


def _declared_names(fingerprint: bytes | str) -> set[str] | None:
    """Best effort: the tool names an invalid tools file declares; None if it isn't YAML at all."""
    if not isinstance(fingerprint, bytes):
        return None
    try:
        data = yaml.safe_load(fingerprint.decode("utf-8"))
    except (UnicodeDecodeError, *YAML_LOAD_ERRORS):
        return None
    tools = data.get("tools") if isinstance(data, dict) else None
    entries = tools if isinstance(tools, list) else []
    return {e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)}


def _project_layer(repo: Any, fingerprint: bytes | str, last_good: ProjectTools | None,
                   status: ToolPlaneStatus) -> _Resolved:
    """The project file's tools; an invalid file keeps `last_good` when there is one."""
    if fingerprint == "root-missing":
        status.notices.append(f"the root of repository {repo.repo_id!r} ({repo.path}) does not exist; no project tools are served")
        return _Resolved()
    if fingerprint == "disabled":
        status.notices.append(
            f"project config is disabled for repository {repo.repo_id!r}; "
            f"enable it with 'devgraph config enable {repo.repo_id}'"
        )
        return _Resolved()
    if fingerprint == "absent":
        return _Resolved()
    try:
        declared = _parse_fingerprint(repo, fingerprint)
    except ProjectToolsError as exc:
        reason = str(exc).splitlines()[0]
        if last_good is None:
            status.notices.append(f"invalid {TOOLS_FILENAME}; no project tools are served: {reason}")
            short = reason.replace(str(tools_file_path(repo.path)), TOOLS_FILENAME)
            status.project_invalid, status.project_invalid_names = short, _declared_names(fingerprint)
            return _Resolved(invalid=short, invalid_names=status.project_invalid_names)
        status.notices.append(f"invalid {TOOLS_FILENAME}; keeping the last good tools: {reason}")
        declared = last_good
    status.tools_file = str(tools_file_path(repo.path))
    return _Resolved(declared)


def _global_layer(fingerprint: bytes | str, last_good: ProjectTools | None, status: ToolPlaneStatus) -> _Resolved:
    """The global store's tools; an invalid store keeps `last_good` when there is one."""
    if fingerprint == "absent":
        return _Resolved()
    path = global_tools_path()
    try:
        declared = _parse_bytes(path, fingerprint)
    except ProjectToolsError as exc:
        reason = str(exc).splitlines()[0]
        if last_good is None:
            status.notices.append(f"invalid {GLOBAL_TOOLS_FILENAME}; no global tools are served: {reason}")
            return _Resolved()
        status.notices.append(f"invalid {GLOBAL_TOOLS_FILENAME}; keeping the last good global tools: {reason}")
        declared = last_good
    status.global_tools_file = str(path)
    return _Resolved(declared)


def _serve_repository(
    server: Any,
    engine: Any,
    repo: Any,
    status: ToolPlaneStatus,
    *,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]],
    annotations: Any,
    fingerprint: bytes | str | None = None,
    global_fingerprint: bytes | str | None = None,
    last_good: ProjectTools | None = None,
    global_last_good: ProjectTools | None = None,
) -> tuple[ProjectTools | None, ProjectTools | None]:
    """Resolve the project and global tools files and register the result, recording the outcome on `status`.

    Each file is read once, as its fingerprint (its bytes); the tools served are exactly
    the ones those bytes declare (or the layer's `last_good`, when the bytes are invalid).
    Without a fingerprint the file is read now. Built-in names are refused in both
    layers; a project tool replaces a global one of the same name, and a global tool
    stands in for a project tool that can't be served. Returns each layer's tools in use.
    """
    if fingerprint is None:
        fingerprint = tools_fingerprint(repo.path)
    if global_fingerprint is None:
        global_fingerprint = global_tools_fingerprint()
    project = _project_layer(repo, fingerprint, last_good, status)
    global_ = _global_layer(global_fingerprint, global_last_good, status)
    _register_layers(server, engine, repo, status, project, global_, instrument=instrument, annotations=annotations)
    return project.declared, global_.declared


def _register_layers(
    server: Any,
    engine: Any,
    repo: Any,
    status: ToolPlaneStatus,
    project: _Resolved,
    global_: _Resolved,
    *,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]],
    annotations: Any,
) -> None:
    """Register the resolved layers' tools on `server`, recording each on `status`."""
    builtin = builtin_tool_names()
    globals_by_name: dict[str, CypherTool] = {}
    for tool in global_.declared.tools if global_.declared else ():
        if tool.name in builtin:
            _shadows(status, tool.name, "global")
        else:
            globals_by_name[tool.name] = tool
    project_tools = project.declared.tools if project.declared else ()
    for tool in project_tools:
        if tool.name in builtin:
            _shadows(status, tool.name, "project")

    def register(tool: CypherTool, origin: str, notices: list[str]) -> str | None:
        """Serve `tool`; on failure record a status notice and return it."""
        layer = origin.split()[0]  # "project" or "global"
        try:
            fn = instrument(make_tool_function(tool, engine, repo.repo_id, notices, layer))
            server.add_tool(fn, name=tool.name, description=tool.description, annotations=annotations)
        except Exception as exc:
            failure = (f"{layer} tool {tool.name!r} could not be served: "
                       f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}")
            status.notices.append(failure)
            return failure
        status.served.append(tool.name)
        status.parameter_names[tool.name] = [p.name for p in tool.parameters]
        status.definitions[tool.name] = tool
        status.origins[tool.name] = origin
        status.functions[tool.name] = fn
        return None

    fallback_reasons = status.fallback_reasons  # project tool -> why it isn't served
    for tool in project_tools:
        if tool.name in builtin:
            continue
        overrides = tool.name in globals_by_name
        if overrides:
            failure = register(tool, "project (overrides global)",
                               [f"resolved: project override of global tool {tool.name!r}"])
        else:
            failure = register(tool, "project", [])
        if failure is not None:
            fallback_reasons[tool.name] = failure
        elif overrides:
            del globals_by_name[tool.name]
    for name, tool in globals_by_name.items():
        if name in fallback_reasons:
            reason = fallback_reasons[name]
        elif project.invalid and project.invalid_names is None:
            reason = f"{TOOLS_FILENAME} is invalid, so a project tool of this name (if any) can't be served: {project.invalid}"
        elif project.invalid and name in project.invalid_names:
            reason = f"{TOOLS_FILENAME} is invalid, so the project tool of this name can't be served: {project.invalid}"
        else:
            reason = None
        register(tool, "global", [f"used global tool {name!r}: {reason}"] if reason else [])


def _shadows(status: ToolPlaneStatus, name: str, layer: str) -> None:
    """Record that a `layer` tool named `name` was ignored for the built-in: in the status and the built-in's responses."""
    notice = f"ignored: {layer} tool {name!r} shadows a locked tool; using the fixed implementation"
    status.notices.append(notice)
    status.shadowed.setdefault(name, []).append(notice)


def _parse_fingerprint(repo: Any, fingerprint: bytes | str) -> ProjectTools:
    """The tools declared by `fingerprint` (file bytes or 'unreadable:<error>'); raises ProjectToolsError."""
    return _parse_bytes(tools_file_path(repo.path), fingerprint)


def _parse_bytes(path: Path, fingerprint: bytes | str) -> ProjectTools:
    """The tools a tools file at `path` declares, given its fingerprint; raises ProjectToolsError."""
    if not isinstance(fingerprint, bytes):
        raise ProjectToolsError(f"{path}: cannot be read: {fingerprint.partition(':')[2]}")
    try:
        text = fingerprint.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectToolsError(f"{path}: is not valid UTF-8: {exc}") from exc
    return parse_project_tools(text, path)


def tools_fingerprint(repo_path: Path | str) -> bytes | str:
    """What the tools file looks like now: its bytes, 'root-missing', 'disabled', 'absent', or 'unreadable:<error>'."""
    if not Path(repo_path).is_dir():
        return "root-missing"
    if not project_config_enabled(repo_path):
        return "disabled"
    path = tools_file_path(Path(repo_path))
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "unreadable:not_regular"  # never open a FIFO or device: the read would block
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


class ProjectToolPlane:
    """The session's served project and global tools, reloadable when either file changes.

    The scope (`repo`) never changes; only the project tools file and the global
    store are re-read, under the same rules as at startup.
    """

    def __init__(self, server: Any, engine: Any, repo: Any | None, status: ToolPlaneStatus, *,
                 instrument: Callable[[Callable[..., Any]], Callable[..., Any]], annotations: Any,
                 fingerprint: bytes | str | None = None, global_fingerprint: bytes | str | None = None) -> None:
        """The fingerprints are the files as the initial load read them (see `register_project_tools`)."""
        self.server, self.engine, self.repo, self.status = server, engine, repo, status
        self._instrument, self._annotations = instrument, annotations
        self._fingerprint, self._global_fingerprint = fingerprint, global_fingerprint
        # The tools the initial load served from each file (re-parsed from the same bytes),
        # kept if a later save is invalid.
        self._last_good = self._global_last_good = None
        if repo is not None and status.tools_file is not None and fingerprint is not None:
            self._last_good = _parse_fingerprint(repo, fingerprint)
        if status.global_tools_file is not None and global_fingerprint is not None:
            self._global_last_good = _parse_bytes(global_tools_path(), global_fingerprint)

    def _resolved(self) -> dict[str, tuple[str, CypherTool]]:
        return {name: (self.status.origins[name], self.status.definitions[name]) for name in self.status.served}

    def reload_if_changed(self) -> bool:
        """Re-resolve the tools if either file's bytes changed. True when the served tools changed.

        Both files are parsed before any served tool is removed. If anything fails
        unexpectedly, the tools served before stay served (with a notice and a
        warning) and the reload is retried at the next poll.
        """
        if self.repo is None:
            return False
        fingerprint, global_fingerprint = tools_fingerprint(self.repo.path), global_tools_fingerprint()
        if (fingerprint, global_fingerprint) == (self._fingerprint, self._global_fingerprint):
            return False
        # Until the reload succeeds, so a failure is retried.
        self._fingerprint = self._global_fingerprint = None
        before = self._resolved()
        new = ToolPlaneStatus(repo_id=self.status.repo_id, source=self.status.source)
        try:
            project = _project_layer(self.repo, fingerprint, self._last_good, new)
            global_ = _global_layer(global_fingerprint, self._global_last_good, new)
        except Exception as exc:
            self._keep_served(exc)
            return False
        removed: list[str] = []
        try:
            for name in self.status.served:
                self.server.remove_tool(name)
                removed.append(name)
            _register_layers(self.server, self.engine, self.repo, new, project, global_,
                             instrument=self._instrument, annotations=self._annotations)
        except Exception as exc:
            self._restore_served(new, removed)
            self._keep_served(exc)
            return False
        self._adopt(new)
        self._last_good, self._global_last_good = project.declared, global_.declared
        # The reload parsed exactly these bytes.
        self._fingerprint, self._global_fingerprint = fingerprint, global_fingerprint
        if self.status.notices:
            logger.warning("reloaded the tools for repository %r with problems: %s",
                           self.repo.repo_id, self.status.notices[0])
        return self._resolved() != before

    def _adopt(self, new: ToolPlaneStatus) -> None:
        """Make `new` the session's status, in place (the status resource holds this object)."""
        for name in ("tools_file", "global_tools_file", "served", "parameter_names", "notices",
                     "definitions", "origins", "functions", "shadowed",
                     "project_invalid", "project_invalid_names", "fallback_reasons"):
            setattr(self.status, name, getattr(new, name))

    def _restore_served(self, partial: ToolPlaneStatus, removed: list[str]) -> None:
        """Undo a half-done swap: drop what `partial` registered, re-register what was `removed`."""
        for name in partial.served:
            try:
                self.server.remove_tool(name)
            except Exception:
                pass
        for name in removed:
            try:
                self.server.add_tool(self.status.functions[name], name=name,
                                     description=self.status.definitions[name].description,
                                     annotations=self._annotations)
            except Exception:
                logger.exception("could not restore tool %r after a failed reload", name)

    def _keep_served(self, exc: Exception) -> None:
        notice = (f"reloading the tools failed ({type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}); "
                  "keeping the tools served before; retrying at the next change check")
        if notice in self.status.notices:  # the same failure on a retry: already reported
            logger.debug("repository %r: %s", self.repo.repo_id, notice)
            return
        self.status.notices.append(notice)
        logger.warning("repository %r: %s", self.repo.repo_id, notice, exc_info=True)
