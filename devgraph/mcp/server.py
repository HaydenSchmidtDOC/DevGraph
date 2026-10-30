"""MCP server implementation for DevGraph.

Exposes DevGraph's high-level tools (devgraph/mcp/tools.py) over the MCP
stdio transport via the `mcp` SDK's MCPServer, so any MCP-capable client
(Claude Code, etc.) can register this as a server and call tools without
ever writing Cypher. `run_cypher` is only registered when
Settings.enable_run_cypher is true (default off, per Design Brief
Principle 2/4) — it never appears in the tool listing otherwise.

Client handover — how a connecting client discovers what it needs, without
a human copy-pasting a doc into another repo's CLAUDE.md/AGENTS.md:
  - `instructions` (below) is sent once at session init; most MCP clients
    fold it into context automatically. Short, load-bearing rules only.
  - Every tool call below carries a docstring (tool-local usage notes) and
    a `ToolAnnotations` hint (`_READ_ONLY`/`_ESCAPE_HATCH` — every DevGraph
    tool queries the graph or reads disk, none write, so a client/host UI
    can treat these calls as safe without confirmation prompts).
  - Two MCP *resources* carry the rest: `devgraph://client-guide` (the full
    prose guide, DEVGRAPH-CLIENT.md's actual content — this is the thing
    that used to only exist as a file a human had to remember to paste
    elsewhere) and `devgraph://tool-catalog` (a machine-readable per-tool
    summary: identifier kind, envelope shape, build phase). A client can
    `list_resources()`/`read_resource()` either one on its own, live, and
    it can never drift out of sync with the actual tool surface since it's
    served from the same process that registers the tools.

Built-in tools take an optional `repo_id`. Omitted, it defaults to this
session's repository (`DEVGRAPH_MCP_REPO`, else the working directory; see
`resolve_session_repo`), which the `instructions` name; a defaulted dict
response carries `repo_id` and a notice. With no session repository the call
errors and lists the registered repositories rather than picking one.

Every tool call is recorded, metadata only (timestamp, tool name, scoped tool
id, origin, duration, success), to a local JSONL store in the DevGraph state directory —
see `record_tool_call` below. It exists because this process is short-lived
and separate from the dashboard's, and the dashboard reads it back over
`GET /api/mcp-telemetry`. Nothing about it leaves the machine.

Run directly: `.venv/Scripts/python -P -m devgraph.mcp.server`
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pydantic_core
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from devgraph.agent import lifecycle
from devgraph.config.global_tools import GLOBAL_TOOLS_FILENAME, global_tools_fingerprint
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools as devgraph_tools
from devgraph.mcp.catalog import TOOL_CATALOG as _TOOL_CATALOG
from devgraph.mcp.catalog import builtin_tool_names  # noqa: F401  (re-exported)
from devgraph.mcp.tool_plane import (
    SESSION_REPO_ENV,
    ProjectToolPlane,
    _inactive_match,
    register_project_tools,
    resolve_session_repo,
    tools_fingerprint,
)
from devgraph.mcp.tool_reload import run_stdio
from devgraph.registry.store import RepoRegistry

logger = logging.getLogger(__name__)

_CLIENT_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent / "DEVGRAPH-CLIENT.md"

# Tool-call telemetry. Deliberately a file rather than an in-process buffer
# like dashboard/query_log.py's: an MCP client spawns its own short-lived
# server process per connection (see this module's docstring), so a record
# that dies with the process would be unreadable by the dashboard running in
# a different one, and several connected clients record at the same time.
_TELEMETRY_FILENAME = "mcp_telemetry.jsonl"
# The whole of a record: metadata about the call, never anything drawn from
# the call itself. Nothing derived from a tool's arguments belongs here — the
# built-ins' repo_id argument in particular is caller-supplied data. The one
# repository name that appears is inside a project tool's tool_id, and it comes
# from the server's own session configuration, not from the call. Written by
# record_tool_call and re-applied as an allow-list by read_tool_telemetry, so
# the guarantee holds at both ends of the store.
_TELEMETRY_FIELDS = ("ts", "tool", "tool_id", "origin", "duration_ms", "ok")
_TELEMETRY_ORIGINS = ("builtin", "global", "project", "unscoped")
# Kept in step with QueryLog's own ring-buffer size, so the two telemetry
# sources the dashboard reads hold a comparable amount of history.
_TELEMETRY_MAX_ENTRIES = 500
# Trimming rewrites the whole file, so it's amortised: append freely until
# the store is comfortably past the cap's worth of ~150-byte records, then
# cut back to the newest _TELEMETRY_MAX_ENTRIES.
_TELEMETRY_TRIM_AT_BYTES = 256 * 1024

# Every DevGraph tool queries the graph or reads a file off disk; none of them
# ever write to Neo4j (writes only happen through the indexer/watcher/CLI, not
# through mcp/tools.py) — so every tool gets the same read-only annotation.
# run_cypher is the one exception: it's an arbitrary-query escape hatch, so it
# can't be vouched for as read-only in the general case.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)  # type: ignore[call-arg]
_ESCAPE_HATCH = ToolAnnotations(readOnlyHint=False, openWorldHint=True)  # type: ignore[call-arg]

def telemetry_path() -> Path:
    """Local JSONL store of MCP tool calls.

    Lives in the existing DevGraph state directory next to registry.sqlite3
    and devgraph.log (RepoRegistry already creates that directory). Local
    file only — nothing here is ever sent anywhere, and this is unrelated to
    `Settings.telemetry_enabled`, which governs outbound telemetry.
    """
    return get_settings().registry_db_path.parent / _TELEMETRY_FILENAME


def record_tool_call(
    *, tool: str, duration_ms: float, ok: bool, tool_id: str | None = None, origin: str = "builtin"
) -> None:
    """Append one metadata-only record of a tool call.

    Records *that* a tool ran, never *what* was asked or answered: no
    arguments — not even the repo_id the built-ins take — no Cypher and no
    results, only the six `_TELEMETRY_FIELDS` written below. `tool` is the wire
    name, `tool_id` the scoped id (a project tool's names its session's
    repository, taken from server configuration) and `origin` is
    builtin | global | project.

    Append-safe across the concurrently-connected clients' separate server
    processes: a single O_APPEND write of one line well under PIPE_BUF, which
    the OS will not interleave with another process's. Never raises and never
    blocks on a lock — telemetry must not be able to fail a tool call.

    Everything runs inside the guard, resolving the store's location and
    building the line included: this is called from the instrumentation
    wrapper's `finally`, so anything raising here would replace the tool's own
    result or exception. Settings/path resolution can fail (a missing or
    unreadable state directory config) as readily as the write itself, so the
    two are not split across the try.
    """
    try:
        path = telemetry_path()
        line = json.dumps(
            {
                "ts": time.time(),
                "tool": tool,
                "tool_id": tool if tool_id is None else tool_id,
                "origin": origin,
                "duration_ms": duration_ms,
                "ok": ok,
            },
            separators=(",", ":"),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        if path.stat().st_size > _TELEMETRY_TRIM_AT_BYTES:
            _trim_telemetry(path)
    except Exception:
        logger.debug("failed to record MCP tool telemetry", exc_info=True)


def _trim_telemetry(path: Path) -> None:
    """Cut the store back to its newest `_TELEMETRY_MAX_ENTRIES` records.

    Bounded by rewriting rather than by locking: the replacement is built in
    a private unique temp file and swapped in with an atomic os.replace, so a
    reader never observes a half-written store, and two server processes
    trimming at the same moment can at worst drop each other's newest few
    records instead of corrupting the file. NamedTemporaryFile creates the
    replacement with owner-only permissions, preserving the store's privacy
    after the inode swap.
    """
    lines = path.read_bytes().splitlines(keepends=True)[-_TELEMETRY_MAX_ENTRIES:]
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        tmp = Path(stream.name)
        stream.write(b"".join(lines))
    os.replace(tmp, path)


def read_tool_telemetry(limit: int) -> list[dict[str, Any]]:
    """Return up to `limit` recorded tool calls, newest first.

    Read-only and never raises: a missing store reads as no records, and an
    unparseable line is skipped rather than failing the whole read, so a
    corrupt file costs the dashboard some history instead of an error. The
    guard spans resolving the store's location too, since settings can fail
    for reasons a write never reaches (a missing or unreadable state
    directory config) and the dashboard endpoint behind this must lose
    history rather than return a 500.

    Each record is rebuilt from `_TELEMETRY_FIELDS` alone rather than passed
    through as parsed, so a line that is valid JSON but carries extra keys —
    a store corrupted or hand-edited outside this module — can never relay
    anything beyond the six allowed fields to the API. Records written before
    scoped ids are normalised here, not migrated: a missing or non-string
    `tool_id` becomes `tool`, and a missing or unknown `origin` becomes
    `builtin` for a built-in name and `unscoped` otherwise. A line whose `tool` is present but not a string is
    skipped like any other unreadable line.
    """
    try:
        raw = telemetry_path().read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.debug("failed to read MCP tool telemetry", exc_info=True)
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines()[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            record = {field: entry[field] for field in _TELEMETRY_FIELDS if field in entry}
            if "tool" in record:
                if not isinstance(record["tool"], str):
                    continue
                if not isinstance(record.get("tool_id"), str):
                    record["tool_id"] = record["tool"]
                if record.get("origin") not in _TELEMETRY_ORIGINS:
                    record["origin"] = "builtin" if record["tool"] in builtin_tool_names() else "unscoped"
            entries.append(record)
    entries.reverse()
    return entries


def _instrument(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap one tool function so that every call to it is recorded.

    `functools.wraps` carries over __name__/__doc__/__annotations__ and sets
    __wrapped__, so the SDK derives the same tool name, description and
    argument schema from the wrapper as it did from the function — the tool
    surface a client sees is unchanged.

    The result and any exception pass through untouched: the record is
    written from a `finally`, so a failing call is recorded as failed and
    then keeps propagating as the exact exception the tool raised.

    The call's arguments are never inspected: the wrapper passes *args and
    **kwargs straight through and records only the tool's name, how long it
    took and whether it succeeded. A declared tool's function carries its
    scoped id and origin (stamped by the tool plane); a built-in carries
    neither and records its bare name as origin `builtin`.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        ok = False
        try:
            result = fn(*args, **kwargs)
            ok = True
            return result
        finally:
            record_tool_call(
                tool=fn.__name__,
                tool_id=getattr(fn, "devgraph_tool_id", fn.__name__),
                origin=getattr(fn, "devgraph_tool_origin", "builtin"),
                duration_ms=(time.monotonic() - start) * 1000,
                ok=ok,
            )

    return wrapper


def compact_json(value: Any) -> str:
    """`value` as compact JSON: no indentation or spaces after separators, key order and
    non-ASCII text kept. Everything a client's model reads goes through this, so it pays
    no tokens for whitespace."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _compact_result(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so a dict result reaches the client as one compact JSON text block.

    Left to itself the SDK renders a returned dict with `indent=2`, about 40% of the
    characters being indentation. The wrapper builds the `CallToolResult` itself: the
    text block is `compact_json` of the result and `structuredContent` is the same
    JSON-ready data the SDK would send (non-JSON values become strings, as the SDK's
    `fallback=str` does), so the two still round-trip to each other and the declared
    `dict[str, Any]` output schema still validates. Anything but a dict, and every
    exception (a ToolError's message is plain text), passes through untouched.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        data = pydantic_core.to_jsonable_python(result, fallback=str)
        return CallToolResult(content=[TextContent(type="text", text=compact_json(data))], structured_content=data)

    return wrapper


def _served(fn: Callable[..., Any]) -> Callable[..., Any]:
    """What every served tool, built-in or declared, is wrapped in: telemetry, then compact JSON."""
    return _compact_result(_instrument(fn))


def _with_neo4j_errors(fn: Callable[..., Any], uri: str) -> Callable[..., Any]:
    """Wrap a built-in tool so Neo4j being down or refusing the login reaches the
    client as a ToolError saying what to do (see `tools.neo4j_unavailable`), not
    as the driver's bare error; every other exception passes through."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            error = devgraph_tools.neo4j_unavailable(exc, uri)
            if error is None:
                raise
            raise error from exc

    return wrapper


class _SchemaProvisioner:
    """Runs `init_schema` once, for a server that started with Neo4j down (its
    startup could not). Thread-safe; marked done only when it succeeded."""

    def __init__(self, engine: GraphEngine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self.ready = False

    def ensure(self) -> None:
        """Provision the schema unless done. Neo4j still unreachable raises the
        driver's error at once (one probe, not retried); any other failure is
        logged and retried on the next call."""
        if self.ready:
            return
        with self._lock:
            if self.ready:
                return
            self._engine.probe_connectivity()
            try:
                self._engine.init_schema(wait=False)
            except Exception:
                logger.warning("provisioning the graph schema failed; retrying on the next call", exc_info=True)
                return
            self.ready = True


def _with_schema(fn: Callable[..., Any], provisioner: _SchemaProvisioner | None) -> Callable[..., Any]:
    """Wrap a built-in tool so `provisioner` (if any) runs before the call."""
    if provisioner is None:
        return fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        provisioner.ensure()
        return fn(*args, **kwargs)

    return wrapper


def _with_shadow_notices(fn: Callable[..., Any], shadowed: Callable[[], dict[str, list[str]]]) -> Callable[..., Any]:
    """Wrap a built-in tool so its dict result carries the notices for declared tools its name shadows.

    `shadowed()` is read on every call, so a tools-file reload is reflected at once.
    Every dict-returning built-in is typed `dict[str, Any]`, so the extra `notices` key
    fits its schema. A list result can't carry a key and passes through unchanged; for
    those built-ins `devgraph://project-tools`, `config validate`, `config tools list`
    and `doctor` report the shadowing. Nothing shadowing the tool: the result is untouched.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        notices = shadowed().get(fn.__name__)
        if notices and isinstance(result, dict):
            existing = result.get("notices")
            result = {**result, "notices": [*(existing if isinstance(existing, list) else []), *notices]}
        return result

    return wrapper


_SESSION_SOURCES = {"env": SESSION_REPO_ENV, "cwd": "the server's working directory"}


def _active_listing(active_repos: list[Any]) -> str:
    if not active_repos:
        return "none registered (register one with `devgraph add <path>`)"
    # `name` in `id (name)` is the repository's folder name: RepoRecord has no name field.
    return ", ".join(f"{r.repo_id} ({Path(r.path).name})" for r in active_repos)


def _unscoped_error(reason: str, active_repos: list[Any]) -> ToolError:
    """Why a call without repo_id can't run: the reason, the active repositories, and how to fix it."""
    return ToolError(
        f"repo_id is required because this session has no repository: {reason}. "
        f"Active registered repositories: {_active_listing(active_repos)}. "
        "To fix: pass repo_id explicitly, or restart the MCP server after registering a repository"
    )


def _removed_session_error(repo_id: str, active_repos: list[Any]) -> ToolError:
    """A call without repo_id whose session repository was removed or deactivated after startup.

    No restart hint: re-registering under the same id is picked up by the next call.
    """
    return ToolError(
        f"this session's repository {repo_id!r} is no longer registered or active; "
        "re-register it, or pass repo_id explicitly. "
        f"Active registered repositories: {_active_listing(active_repos)}"
    )


def _with_repo_default(
    fn: Callable[..., Any],
    session_repo: Any | None,
    session_source: str,
    registry: Any,
    unscoped_error: Callable[[list[Any]], Exception],
) -> Callable[..., Any]:
    """Make a built-in's `repo_id` optional, defaulting to the session's repository.

    A function without a `repo_id` parameter (run_cypher) is returned unchanged. Otherwise
    the wrapper's signature is `fn`'s with every parameter keyword-only (MCP always calls by
    keyword) and `repo_id: str | None = None`; nothing else in the tool's schema changes.

    An explicit `repo_id` (anything but None) must be registered (else the ToolError
    "no such repo_id"); `fn` is then called untouched and its result returned as is.
    Omitted or None: one `list_repos(active_only=True)` read; if the session repository is in
    it, `fn` runs against it and a dict result gains `repo_id` and a notice. With no session
    repository `unscoped_error(active_repos)` is raised, and with one no longer active
    `_removed_session_error`; either way `fn` never runs.
    """
    signature = inspect.signature(fn, eval_str=True)
    if "repo_id" not in signature.parameters:
        return fn
    parameters = [
        p.replace(kind=inspect.Parameter.KEYWORD_ONLY, annotation=str | None, default=None)
        if p.name == "repo_id"
        else p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for p in signature.parameters.values()
    ]

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        if kwargs.get("repo_id") is not None:
            devgraph_tools._registered(registry, kwargs["repo_id"])
            return fn(**kwargs)
        active = registry.list_repos(active_only=True)
        if session_repo is None:
            raise unscoped_error(active)
        repo_id = session_repo.repo_id
        if all(r.repo_id != repo_id for r in active):
            raise _removed_session_error(repo_id, active)
        result = fn(**{**kwargs, "repo_id": repo_id})
        if isinstance(result, dict):
            notice = f"repo_id not given; used this session's repository {repo_id!r} (from {_SESSION_SOURCES[session_source]})"
            existing = result.get("notices")
            result = {**result, "repo_id": repo_id, "notices": [*(existing if isinstance(existing, list) else []), notice]}
        return result

    wrapper.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    return wrapper


CROSS_REPO_DISABLED = (
    "cross_repo is disabled: results from other repositories are off unless the user sets "
    "DEVGRAPH_ALLOW_CROSS_REPO=true for the DevGraph MCP server. Call again without cross_repo "
    "to answer for this repository."
)


def _with_cross_repo_gate(fn: Callable[..., Any], allow: bool, registry: Any) -> Callable[..., Any]:
    """Enforce `Settings.allow_cross_repo` on a built-in that takes `cross_repo`.

    `cross_repo=True` raises the ToolError `CROSS_REPO_DISABLED` while the setting
    is off; `fn` never runs. With it on, `fn` runs with `tools.cross_repo_ids` set to
    every active registered repository's id (the dashboard's All Repos set), so a cross-repo query never reaches a
    repository left in the graph after it was unregistered. Calls without
    `cross_repo` pass through untouched. MCP always calls by keyword.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not kwargs.get("cross_repo"):
            return fn(*args, **kwargs)
        if not allow:
            raise ToolError(CROSS_REPO_DISABLED)
        token = devgraph_tools.cross_repo_ids.set([r.repo_id for r in registry.list_repos(active_only=True)])
        try:
            return fn(*args, **kwargs)
        finally:
            devgraph_tools.cross_repo_ids.reset(token)

    return wrapper


def build_server(
    engine: GraphEngine,
    registry: RepoRegistry | None = None,
    *,
    session_repo: Any | None = None,
    session_source: str = "none",
    session_pinned: str | None = None,
    provision_schema: bool = False,
) -> MCPServer:
    """Construct an MCPServer with every DevGraph tool registered against `engine`.

    `registry` is required for `get_source` (it resolves a repo_id to its
    registered root path to read source off disk); when omitted, a registry
    is opened from settings so existing single-argument callers keep working.

    `session_repo` is the registered repository this session serves
    `devgraph.tools.yaml` tools for (None serves none); `session_source` says
    how it was chosen ("env", "cwd" or "none") and is reported by the
    `devgraph://project-tools` resource. `session_pinned` is the raw
    DEVGRAPH_MCP_REPO value, used only in the unmatched-value notice and the
    no-session error.

    `provision_schema` (the server started with Neo4j down) makes the first
    built-in call that finds Neo4j reachable run `init_schema` first; see
    `_SchemaProvisioner`.

    The same `session_repo` is the default `repo_id` for every built-in tool
    called without one (see `_with_repo_default`), and the `instructions` name
    it. With no session repository, such a call errors; it never picks one.
    """
    settings = get_settings()
    if registry is None:
        registry = RepoRegistry(settings.registry_db_path)
    if session_repo is not None:
        repo_scope = (
            f"This session's repository is {session_repo.repo_id!r} (from {_SESSION_SOURCES[session_source]}); "
            "built-in tools use it when repo_id is omitted, and a dict response then carries repo_id "
            "and a notice. Pass repo_id (the id shown by `devgraph list`) only for a different repository."
        )
    else:
        repo_scope = (
            "This session has no repository, so every built-in tool that takes a repo_id requires one "
            "(the id shown by `devgraph list`)."
        )
    server = MCPServer(
        name="devgraph",
        version="0.1.0",
        instructions=(
            "DevGraph: a local architecture knowledge graph for explicitly-registered "
            "repositories. Prefer these tools over reading source files directly when "
            "answering structural/dependency/history questions — they query a "
            f"pre-built graph instead of re-scanning the repo. {repo_scope} Built-in tools answer "
            "for a single repository; "
            "pass cross_repo=true only when the user explicitly wants results across "
            "multiple registered repositories. Project-specific tools declared in a "
            "repository's devgraph.tools.yaml (served only once the user trusts the file) are "
            "scoped to this session's repository and take no repo_id; see devgraph://project-tools."
        ),
    )

    neo4j_uri = getattr(engine, "uri", None) or settings.neo4j_uri
    provisioner = _SchemaProvisioner(engine) if provision_schema else None
    # Single chokepoint for tool-call telemetry: every `@server.tool(...)`
    # registration below goes through this rebound decorator, including any
    # tool added later, so instrumentation can't be forgotten on a new tool
    # the way a per-tool wrapper would be.
    _register_tool = server.tool

    # Why a call without repo_id has no repository to default to (R1.3); fixed at startup.
    if session_repo is not None:
        no_session_reason = ""
    elif session_source == "env":
        pinned = session_pinned or ""
        inactive = _inactive_match(registry, pinned)
        no_session_reason = (
            f"{SESSION_REPO_ENV}={pinned!r} names repository {inactive.repo_id!r}, which is registered but inactive"
            if inactive is not None
            else f"{SESSION_REPO_ENV}={pinned!r} matches no registered repository"
        )
    else:
        no_session_reason = (
            f"the server's working directory is not inside a registered repository and {SESSION_REPO_ENV} is unset"
        )

    def unscoped_error(active_repos: list[Any]) -> ToolError:
        return _unscoped_error(no_session_reason, active_repos)

    def _instrumented_tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = _register_tool(*args, **kwargs)
        # `status` (the tool plane's, assigned below) is looked up at call time.
        return lambda fn: register(
            _served(
                _with_neo4j_errors(
                    _with_schema(
                        _with_shadow_notices(
                            _with_cross_repo_gate(
                                _with_repo_default(fn, session_repo, session_source, registry, unscoped_error),
                                settings.allow_cross_repo,
                                registry,
                            ),
                            lambda: status.shadowed,
                        ),
                        provisioner,
                    ),
                    neo4j_uri,
                )
            )
        )

    server.tool = _instrumented_tool  # type: ignore[method-assign]

    def declared(repo_id: str, cross_repo: bool) -> tuple[str, ...]:
        """The declared labels a component lookup adds: the repository's, or every registered one's."""
        if cross_repo:
            return devgraph_tools.all_declared_node_labels(registry)
        return devgraph_tools.declared_node_labels(registry, repo_id)

    @server.tool(annotations=_READ_ONLY)
    def search_component(
        repo_id: str,
        query: str,
        cross_repo: bool = False,
        max_results: int = 15,
        modified_within_commits: int | None = None,
    ) -> dict[str, Any]:
        """Search for components (services, modules, classes, functions, endpoints,
        databases, vector stores, queues, caches and compose containers) by
        name/description; returns {count, results, truncated},
        each result with name, labels, repo_id, description and file. Ranked: the whole
        query as the exact name, then the same ignoring case, then names starting with
        or containing the whole query, then names equal to, starting with or containing
        one of its words (run_cypher before run), then description only. count is the true
        number of matches, unless count_is_lower_bound is true (exact and prefix matches
        alone filled max_results). max_results is capped at 200. A snake_case query such as
        user_service also matches as a whole word. An exact name in unusual case (gEt for
        get) is found by the substring scan, so it can be missed when exact and prefix
        matches alone fill max_results. A route's handler stub is left out when it folds
        into the one real function it stands for. Pass modified_within_commits to restrict to components touched within the last
        N commits repo-wide (requires git-history recency staging; entities never staged
        are excluded, not silently included).
        Also searches node types the repository's devgraph.schema.yaml declares (for
        example File/Folder from the filesystem provider); with cross_repo=True only the
        calling repository's declared labels are added."""
        return devgraph_tools.search_component(
            engine, repo_id, query, cross_repo, max_results, modified_within_commits,
            extra_labels=devgraph_tools.declared_node_labels(registry, repo_id),
        )

    @server.tool(annotations=_READ_ONLY)
    def god_nodes(
        repo_id: str,
        cross_repo: bool = False,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Return the most-connected nodes in the graph — the core abstractions
        a new agent should look at first to orient itself in an unfamiliar repo.
        Returns {count, results, truncated} with degree (number of direct relationships).
        Git history (commits, PRs, issues) and mentions documents are left out."""
        return devgraph_tools.god_nodes(
            engine, repo_id, cross_repo, max_results,
            declared_labels=(
                devgraph_tools.all_declared_node_labels(registry)
                if cross_repo
                else devgraph_tools.declared_node_labels(registry, repo_id)
            ),
        )

    @server.tool(annotations=_READ_ONLY)
    def find_dependency_cycles(
        repo_id: str,
        relationship: str = "IMPORTS",
        max_length: int = 5,
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """Find circular dependency chains over one already-indexed relationship type
        (CALLS, DEPENDS_ON, EXTENDS, IMPORTS or USES — anything else is rejected);
        returns {count, results, truncated} of {length, nodes} rows. Each cycle is
        reported once whatever node it was found from; opposite-direction cycles over
        the same nodes are distinct and both reported. max_length is in edges and is
        clamped to 2..8. count is a lower bound when truncated is true."""
        return devgraph_tools.find_dependency_cycles(
            engine, repo_id, relationship, max_length, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def find_communities(
        repo_id: str,
        max_results: int = 10,
        members_per_community: int = 5,
    ) -> dict[str, Any]:
        """Return the repository's subsystems: Louvain communities over dependency and
        containment edges, largest first, as {count, results, truncated} of
        {community, label, size, top_members}. top_members (highest PageRank first) is
        filled for the communities within max_results. Errors if graph insights have
        never been computed for the repository (the agent computes them after indexing)."""
        return devgraph_tools.find_communities(engine, registry, repo_id, max_results, members_per_community)

    @server.tool(annotations=_READ_ONLY)
    def key_nodes(
        repo_id: str,
        metric: str = "pagerank",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Rank the repository's entities by PageRank over dependency edges (the core
        abstractions everything leans on) or by betweenness (bridges between subsystems,
        where a change ripples furthest); returns {count, results, truncated} of
        {name, labels, file, score, community}. metric is pagerank or betweenness.
        CALLS edges are resolved by name, so widely used generic method names (such as get or
        close) can rank high.
        Errors if graph insights have never been computed for the repository."""
        return devgraph_tools.key_nodes(engine, registry, repo_id, metric, max_results)

    @server.tool(annotations=_READ_ONLY)
    def list_recent_changes(
        repo_id: str,
        within_commits: int,
        entity_type: str | None = None,
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """List entities touched within the last N commits repo-wide, most-recently-modified
        first; returns {count, results, truncated}. Requires git-history recency staging —
        entities never staged with last_modified_at are excluded. entity_type optionally
        restricts to one node label; an unknown label is an error naming the valid ones.
        within_commits must be at least 1."""
        return devgraph_tools.list_recent_changes(
            engine, repo_id, within_commits, entity_type, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def trace_request_flow(repo_id: str, start_endpoint: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the request flow from an endpoint through services, datastores, and queues
        (every node within five hops). start_endpoint is an endpoint name like
        "GET /users/<id>", or a bare path like "/users/<id>" to start from that path's
        endpoint for every method (a Django route with or without its path's leading
        "/"). A route's handler stub is listed as the one real
        function it stands for (the traced route's own when routes share a handler
        name; its calls are followed too), or as itself when it stands for none. Each
        component is {name, labels, repo_id, file, owner}; edges are {type, from, to}
        between listed components. An unknown endpoint is an error with similar names."""
        return devgraph_tools.trace_request_flow(engine, repo_id, start_endpoint, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def get_service_dependencies(repo_id: str, service_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Get all dependencies (services, datastores, queues) for a given service. An
        unknown service is an error with similar names."""
        return devgraph_tools.get_service_dependencies(engine, repo_id, service_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_callers(
        repo_id: str,
        target_name: str,
        cross_repo: bool = False,
        max_results: int = 15,
        scope_to_class: str | None = None,
        modified_within_commits: int | None = None,
        resolved_only: bool = False,
        file: str | None = None,
        owner: str | None = None,
        include_tests: bool = True,
    ) -> dict[str, Any]:
        """Find all callers of a target; returns {count, results, truncated, test_count}, each caller
        with its name, type, repo_id, file, owner and start_line (a module caller has
        neither) and confidence, most certain first. Calls
        in every language are resolved through the caller's scope, imports and types:
        confidence "resolved" (callee in a file the caller names), "package" (in a
        file the caller's imports reach less exactly, e.g. under an imported package
        or a class file found by suffix) or "name" (a method on an untyped receiver,
        matched by name only). Pass resolved_only to keep only "resolved"/"package"
        callers, or scope_to_class to narrow to callers made from within a specific
        class's own methods. Pass
        modified_within_commits to restrict to targets touched within the last N
        commits repo-wide (requires git-history recency staging; entities never staged
        are excluded, not silently included). An unknown target is an error with similar
        names; a known one nothing calls is count 0. When the name is defined in several
        files, pass file (the target's repo-relative path) to pick one; without it each
        caller adds target_files (the files of the targets it calls, at most 10) and
        ambiguous_targets lists the definitions. To pick one class's method, pass owner
        (the classes or functions it is declared in, "" at the top level) or name it
        "A.run" / "A::run". Callers in test code (under tests/, test/, __tests__/,
        src/test/, or a test file such as test_*.py, *_test.go, *.spec.ts, *Test.java)
        come after all others; test_count says how many there are, and
        include_tests=false leaves them out. An API route reaches its handler by
        IMPLEMENTS, not a call: routed_by lists the target's endpoints apart, a
        {count, results, truncated} envelope of rows as list_endpoints gives them."""
        return devgraph_tools.find_callers(
            engine, repo_id, target_name, cross_repo, max_results, scope_to_class, modified_within_commits,
            resolved_only, file, declared(repo_id, cross_repo), owner, include_tests,
        )

    @server.tool(annotations=_READ_ONLY)
    def find_related_files(
        repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15, file: str | None = None,
        owner: str | None = None, include_tests: bool = True,
    ) -> dict[str, Any]:
        """Find related files; each list returns {count, results, truncated, test_count} envelope. An
        unknown component is an error with similar names. When the name is defined in
        several files, pass file (repo-relative path) to pick one; without it each row is
        {name, target_files} and ambiguous_targets lists them. Pass owner, or name a
        method "A.run", to pick one class's method. Entries in test code come last in each
        list (test_count counts them); include_tests=false leaves them out."""
        return devgraph_tools.find_related_files(
            engine, repo_id, component_name, cross_repo, max_results, file, declared(repo_id, cross_repo), owner,
            include_tests,
        )

    @server.tool(annotations=_READ_ONLY)
    def summarise_repository(repo_id: str) -> dict[str, Any]:
        """Get a high-level summary of a repository's architecture (node counts by type,
        cache_count included)."""
        return devgraph_tools.summarise_repository(engine, repo_id)

    @server.tool(annotations=_READ_ONLY)
    def compare_branches(repo_id: str, branch_a: str, branch_b: str) -> dict[str, Any]:
        """What changed on branch_b (the head) since it diverged from branch_a (the base),
        compared from their merge base like `git diff branch_a...branch_b` (what a pull
        request from branch_b into branch_a shows). Both refs (branch, tag, SHA, HEAD~2...)
        must exist locally; never fetches. Lists the changed files (added, removed,
        modified, exact-content renamed) and, for Python, JS/TS, C#, C++, Java, Rust,
        Kotlin and Go files, the functions and classes added, removed and changed, with
        line numbers. Capped (200 files, 50 symbols per list, 1,000 symbols, 16 MiB
        parsed, 20 s); `truncated` and `truncated_reasons` say when a cap was hit.
        impacted_callers are the graph's callers of the changed and removed symbols,
        from the last index of the working tree, not from either ref: read them as a
        hint. The merge-base step has a timeout on POSIX only. files and
        impacted_callers are {count, results, truncated}."""
        return devgraph_tools.compare_branches(engine, registry, repo_id, branch_a, branch_b)

    @server.tool(annotations=_READ_ONLY)
    def impact_analysis(
        repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15, file: str | None = None,
        owner: str | None = None, include_tests: bool = True,
    ) -> dict[str, Any]:
        """Analyze component impact: direct dependents (one CALLS/USES/DEPENDS_ON hop) and
        transitive ones (two to four hops; further ones are not followed), each {name, type,
        file, owner} (owner left out and file null when it has none), wrapped in {count,
        results, truncated} envelopes. Pass owner, or name a method "A.run", to pick one
        class's method. An unknown component is an
        error with similar names. When the name is defined in several files, pass file
        (repo-relative path) to pick one; without it ambiguous_targets lists them, and for
        up to five files each is traced apart (every dependent adds target_files, the
        targets it depends on, and risk_level is the riskiest one's); past five, their
        dependents are listed together, with a notice. Another same-named definition
        can itself appear as a dependent. Dependents in test code come after all others in
        each list, which adds test_count; include_tests=false leaves them out.
        endpoints lists the API routes whose handler is the component or a dependent
        (rows as list_endpoints gives them, in an envelope); they are not dependents,
        so risk_level, from the direct dependents, leaves them out."""
        return devgraph_tools.impact_analysis(
            engine, repo_id, component_name, cross_repo, max_results, file, declared(repo_id, cross_repo), owner,
            include_tests,
        )

    @server.tool(annotations=_READ_ONLY)
    def impact_analysis_for_diff(
        repo_id: str,
        base_ref: str,
        head_ref: str,
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """What a change between two local git refs could break. Compares head_ref with
        its merge base with base_ref, like `git diff base_ref...head_ref` (what a pull
        request shows), as compare_branches does, and lists the functions and classes
        added, changed and removed (added_symbols, changed_symbols, removed_symbols).
        Dependents of the changed and removed ones are traced, direct (one
        CALLS/USES/DEPENDS_ON hop) and transitive (two to four hops); added ones have no
        dependents yet. A changed code file whose symbols weren't diffed (past the
        200-file cap, unreadable, or renamed) counts every indexed symbol in it, with a
        notice; a changed symbol the index doesn't have is named in a notice. Never fetches; same ref rules and caps as
        compare_branches. Dependents come from the last index of the working tree and are
        wrapped in {count, results, truncated} envelopes; endpoints lists the API routes
        whose handler is a changed symbol or a dependent, as impact_analysis does."""
        return devgraph_tools.impact_analysis_for_diff(
            engine, registry, repo_id, base_ref, head_ref, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def explain_architecture(repo_id: str, max_results: int = 15) -> dict[str, Any]:
        """Generate a high-level architectural explanation of the repository (a service
        uses databases, vector stores, queues and caches):
        services_and_datastores ({service, uses}) and endpoints ({endpoints, calls}, only
        services some endpoint calls), each a {count, results, truncated} envelope. Each
        endpoint's path has a leading "/" (a Django pattern's added, as list_endpoints
        does)."""
        return devgraph_tools.explain_architecture(engine, repo_id, max_results)

    @server.tool(annotations=_READ_ONLY)
    def list_services(repo_id: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """List all services; returns {count, results, truncated}."""
        return devgraph_tools.list_services(engine, repo_id, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def list_endpoints(
        repo_id: str,
        method: str | None = None,
        path_prefix: str | None = None,
        framework: str | None = None,
        service: str | None = None,
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """List API endpoints (routes); returns {count, results, truncated}, each result
        {name, method, path, mounted, framework, files, handlers ({label, name, file, owner}:
        a Function, or a Django class-based view's Class), services, repo_id}. Filters: method ("GET", "*" for an every-method route),
        path_prefix (whole segments: "/api" matches "/api/users", not "/apis"), framework
        (ASP.NET Core, Django, Express, FastAPI, Flask, Go, NestJS, Spring) and service (the compose
        service an endpoint CALLS). A route whose mount its file doesn't show has mounted
        false, its own path, and a name like "GET ?/:id (routes/users.js)"; no path_prefix
        matches it; a Django pattern's path gains its leading "/". method takes any HTTP
        method token (PROPFIND too). A method that is no token, an unknown framework,
        or an unknown service is an error."""
        return devgraph_tools.list_endpoints(
            engine, repo_id, method, path_prefix, framework, service, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def explain_decision(repo_id: str, decision_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Explain a design decision: its rationale, what it documents, and what it supersedes.
        An unknown decision name is an error with similar names."""
        return devgraph_tools.explain_decision(engine, repo_id, decision_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_requirements_for(
        repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15
    ) -> dict[str, Any]:
        """Find requirements a component (module/service/etc.) satisfies; returns
        {count, results, truncated}. An unknown component is an error with similar names.
        A `notice` says when a changed docs path is not indexed yet."""
        return devgraph_tools.find_requirements_for(
            engine, repo_id, component_name, cross_repo, max_results, declared(repo_id, cross_repo), registry
        )

    @server.tool(annotations=_READ_ONLY)
    def trace_design_rationale(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the design rationale (requirements, decisions, notes) behind a component.
        An unknown component is an error with similar names. A `notice` says when a changed
        docs path is not indexed yet."""
        return devgraph_tools.trace_design_rationale(
            engine, repo_id, component_name, cross_repo, declared(repo_id, cross_repo), registry
        )

    @server.tool(annotations=_READ_ONLY)
    def find_mentions(
        repo_id: str,
        name: str,
        label: str | None = None,
        direction: str = "mentioned_by",
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """Find Documents mentioning an entity or what a Document mentions; returns {count, results, truncated}.
        direction="mentioned_by" (default): find Documents mentioning the entity named name.
        direction="mentions": find what the Document at repo-relative path name mentions.
        With the repository's mentions indexing off, the envelope is empty and a `notice`
        says how to enable it; a `notice` also says when a change to it awaits its rescan."""
        return devgraph_tools.find_mentions(
            engine, repo_id, name, label, direction, cross_repo, max_results, registry
        )

    @server.tool(annotations=_READ_ONLY)
    def blame_component(
        repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15
    ) -> dict[str, Any]:
        """Find commits that modified a component's file, most recent first
        (pass a file path, not a function name); returns {count, results, truncated}."""
        return devgraph_tools.blame_component(engine, repo_id, component_name, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def find_related_prs(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find related PRs; returns {count, results, truncated}. With the repository's PR
        ingestion off, the envelope is empty and a `notice` says how to enable it (never
        contacts the network). An unknown file is an error with similar names."""
        return devgraph_tools.find_related_prs(engine, repo_id, component_name, cross_repo, max_results, registry)

    @server.tool(annotations=_READ_ONLY)
    def issue_history_for(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find issue history; returns {count, results, truncated}. With the repository's
        issue ingestion off, the envelope is empty and a `notice` says how to enable it
        (never contacts the network). An unknown file is an error with similar names."""
        return devgraph_tools.issue_history_for(engine, repo_id, component_name, cross_repo, max_results, registry)

    @server.tool(annotations=_READ_ONLY)
    def get_source(
        repo_id: str, component_name: str, cross_repo: bool = False, file: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a Function or Class's actual source text and full docstring (when present),
        using the graph's last-indexed line range. Reads live from disk — rescan first if
        the file may have changed since the last index. When several nodes share the name,
        returns status "ambiguous" with candidates ({label, name, file, owner}) instead of
        picking one: pass the candidate's file (repo-relative path) and owner (the classes
        or functions it is declared in, "" at the top level) to choose. A method may also
        be named with its class, "A.save" or "A::save". A route's handler stub never makes
        the one function it stands for ambiguous; a stub standing for none is a
        candidate marked unresolved_handler. A file that is not valid UTF-8 is
        decoded with replacement characters, and a notice says so. An unknown name (or
        none of it in file, of owner) is an error with similar names; file may be
        repo-relative or an absolute path inside the repository."""
        return devgraph_tools.get_source(engine, registry, repo_id, component_name, cross_repo, file, owner)

    @server.tool(annotations=_READ_ONLY)
    def describe_node(
        repo_id: str,
        name: str,
        label: str | None = None,
        file: str | None = None,
        direction: str = "both",
        relationship_types: list[str] | None = None,
        neighbor_labels: list[str] | None = None,
        max_per_type: int = 10,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Look at one node: its properties and its relationships grouped by type and
        direction, with each neighbour's identity. A file, folder or docs node is named by
        its repo-relative path or id; a method may be named with its class, "A.save" or
        "A::save". An ambiguous name returns candidates; call again with label, file and
        owner. A route handler answers as its real function, with its stub's
        relationships folded in when the stub provably stands for it; a stub routes
        share because their handlers share a name is left out when each route resolved
        to its own function; any other stub is marked unresolved_handler. A code symbol's ref carries owner (the classes or functions it is declared
        in, "" at the top level), which picks it from a same-named one in its file. Each
        ref in the result is the next call's
        arguments. One hop per call, at most 50 neighbours per type; direction,
        relationship_types, neighbor_labels and max_per_type narrow it."""
        return devgraph_tools.describe_node(
            engine, repo_id, name, label, file, direction, relationship_types, neighbor_labels, max_per_type,
            declared_labels=devgraph_tools.declared_node_labels(registry, repo_id), owner=owner,
        )

    if settings.enable_run_cypher:

        @server.tool(annotations=_ESCAPE_HATCH)
        def run_cypher(query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            """Advanced escape hatch: run raw Cypher directly. Disabled by default; only
            registered because enable_run_cypher=true is set for this instance. Prefer
            the purpose-built tools above whenever one of them fits."""
            return devgraph_tools.run_cypher(engine, query, parameters)

    # The load parses exactly these bytes.
    fingerprint = tools_fingerprint(session_repo.path) if session_repo is not None else None
    global_fingerprint = global_tools_fingerprint()
    status = register_project_tools(
        server, engine, session_repo, session_source, instrument=_served, annotations=_READ_ONLY, pinned=session_pinned,
        registry=registry, fingerprint=fingerprint, global_fingerprint=global_fingerprint,
    )
    # main() polls this for tools-file and global-store reloads.
    server.devgraph_tool_plane = ProjectToolPlane(  # type: ignore[attr-defined]
        server, engine, session_repo, status, instrument=_served, annotations=_READ_ONLY,
        fingerprint=fingerprint, global_fingerprint=global_fingerprint,
    )

    @server.resource(
        "devgraph://project-tools",
        name="devgraph-project-tools",
        title="DevGraph project tools",
        description=(
            f"Which repository this session serves {TOOLS_FILENAME} and global tools for, how it was "
            "chosen, which tools are served and where each comes from (global, project, or a "
            "project override of a global tool), whether the user trusts the project tools file, and "
            "notices about ignored, invalid or untrusted declarations."
        ),
        mime_type="application/json",
    )
    def project_tools() -> str:
        return compact_json(status.to_dict())

    @server.resource(
        "devgraph://client-guide",
        name="devgraph-client-guide",
        title="DevGraph client usage guide",
        description=(
            "Full usage guide for a client repo connecting to this DevGraph instance: "
            "registration steps, the tool surface, response-shape conventions "
            "(count/results/truncated envelopes), identifier-type gotchas (name vs. "
            "file path per tool), and known extraction gaps. Read this once at the "
            "start of a session before using DevGraph's tools, instead of asking a "
            "human to paste DEVGRAPH-CLIENT.md into this repo's own docs."
        ),
        mime_type="text/markdown",
    )
    def client_guide() -> str:
        try:
            return _CLIENT_GUIDE_PATH.read_text(encoding="utf-8")
        except OSError:
            return "DEVGRAPH-CLIENT.md not found at the expected path in this DevGraph checkout."

    @server.resource(
        "devgraph://tool-catalog",
        name="devgraph-tool-catalog",
        title="DevGraph tool catalog",
        description=(
            "Machine-readable summary of every registered tool: what kind of "
            "identifier it expects (a name vs. a file path vs. git refs — the most "
            "common usage mistake), whether its response uses the count/results/"
            "truncated envelope, and which build phase introduced it. Cheaper to "
            "read once than to infer from trial and error across every tool."
        ),
        mime_type="application/json",
    )
    def tool_catalog() -> str:
        catalog = _TOOL_CATALOG if settings.enable_run_cypher else [
            t for t in _TOOL_CATALOG if t["name"] != "run_cypher"
        ]
        catalog = [
            *catalog,
            *(
                {
                    "name": n,
                    "identifier_kind": "parameters: " + (", ".join(params) or "none"),
                    "envelope": True,
                    "phase": None,
                    "note": (
                        f"global tool from {GLOBAL_TOOLS_FILENAME}" if status.origins.get(n) == "global"
                        else f"from {TOOLS_FILENAME} in {status.repo_id}"
                    ),
                }
                for n, params in status.parameter_names.items()
            ),
        ]
        return compact_json(catalog)

    return server


#: How long the MCP server's engine may take to get a connection (see
#: `GraphEngine`, which it runs `fail_fast`: a read that failed once connected
#: is retried once, one that could not connect is not). With Neo4j down a call
#: fails in about this long on every platform, not after the driver's default
#: 30 s of retries.
MCP_CONNECTION_TIMEOUT_S = 2.0


def mcp_engine(settings: Any) -> GraphEngine:
    """The MCP server's engine: `settings`' Neo4j, with the short limits above."""
    return GraphEngine(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        fail_fast=True,
        connection_timeout_s=MCP_CONNECTION_TIMEOUT_S,
    )


def main() -> None:
    """Entry point: connect to Neo4j, initialize schema, run the stdio MCP server.

    Also starts the tray app (watcher + incremental indexer) as a detached
    background process if one isn't already running, so a registered repo's
    saved changes get reindexed without anyone manually running
    `devgraph tray start` or `python -P -m devgraph.agent.tray` first. This is a
    no-op when a tray process is already alive (per its PID file) — safe to
    call from every concurrently-connected MCP client's own server process,
    since an MCP client spawns one of these per connection (see this
    module's docstring).

    This process also registers itself as a "holder" of the shared tray
    process (devgraph/agent/lifecycle.py's holder tracking) and unregisters
    on shutdown, stopping the tray only if it was the last holder. Multiple
    MCP clients can be connected at once, each with its own server process —
    tying the tray's lifetime to any single one of those exiting would
    silently stop live indexing for every other still-connected client, so
    shutdown is refcounted across all of them instead. `devgraph tray stop`
    remains available to force a stop regardless of holders.

    Run with: `.venv/Scripts/python -P -m devgraph.mcp.server`
    """
    settings = get_settings()
    engine = mcp_engine(settings)
    provision_schema = False
    try:
        # One check, not retried, so a server with Neo4j down starts serving at once.
        engine.probe_connectivity()
        # Not waiting for indexes an upgrade is still building: that can outlast
        # the client's handshake timeout, and each query waits for its own.
        engine.init_schema(wait=False)
    except Exception as exc:
        # Neo4j down (or refusing the login) at startup: serve anyway, so the
        # client gets each tool's error saying how to fix it, and provision the
        # schema on the first call that finds Neo4j reachable.
        error = devgraph_tools.neo4j_unavailable(exc, settings.neo4j_uri)
        if error is None:
            raise
        logger.warning("%s; serving anyway, each tool answers with this until Neo4j is reachable", error)
        provision_schema = True
    registry = RepoRegistry(settings.registry_db_path)

    try:
        lifecycle.start_tray_if_not_running()
        lifecycle.register_tray_holder()
    except Exception:
        logger.warning(
            "failed to auto-start the DevGraph tray app; live reindexing will not run "
            "until 'devgraph tray start' is run manually",
            exc_info=True,
        )

    try:
        session_repo, source = resolve_session_repo(registry, os.environ, Path.cwd())
        server = build_server(
            engine,
            registry,
            session_repo=session_repo,
            session_source=source,
            session_pinned=os.environ.get(SESSION_REPO_ENV),
            provision_schema=provision_schema,
        )
        anyio.run(run_stdio, server)
    finally:
        engine.close()
        registry.close()
        try:
            lifecycle.stop_tray_if_last_holder()
        except Exception:
            logger.warning("failed to stop the DevGraph tray app on shutdown", exc_info=True)


if __name__ == "__main__":
    main()
