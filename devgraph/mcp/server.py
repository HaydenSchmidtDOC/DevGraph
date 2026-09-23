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

Every tool call is recorded, metadata only (timestamp, tool name, duration,
success), to a local JSONL store in the DevGraph state directory —
see `record_tool_call` below. It exists because this process is short-lived
and separate from the dashboard's, and the dashboard reads it back over
`GET /api/mcp-telemetry`. Nothing about it leaves the machine.

Run directly: `.venv/Scripts/python -m devgraph.mcp.server`
"""

from __future__ import annotations

import functools
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from devgraph.agent import lifecycle
from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools as devgraph_tools
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
# the call itself. Nothing derived from a tool's arguments belongs here — a
# repo_id in particular is caller-supplied data, not metadata. Written by
# record_tool_call and re-applied as an allow-list by read_tool_telemetry, so
# the guarantee holds at both ends of the store.
_TELEMETRY_FIELDS = ("ts", "tool", "duration_ms", "ok")
# Kept in step with QueryLog's own ring-buffer size, so the two telemetry
# sources the dashboard reads hold a comparable amount of history.
_TELEMETRY_MAX_ENTRIES = 500
# Trimming rewrites the whole file, so it's amortised: append freely until
# the store is comfortably past the cap's worth of ~120-byte records, then
# cut back to the newest _TELEMETRY_MAX_ENTRIES.
_TELEMETRY_TRIM_AT_BYTES = 256 * 1024

# Every DevGraph tool queries the graph or reads a file off disk; none of them
# ever write to Neo4j (writes only happen through the indexer/watcher/CLI, not
# through mcp/tools.py) — so every tool gets the same read-only annotation.
# run_cypher is the one exception: it's an arbitrary-query escape hatch, so it
# can't be vouched for as read-only in the general case.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)  # type: ignore[call-arg]
_ESCAPE_HATCH = ToolAnnotations(readOnlyHint=False, openWorldHint=True)  # type: ignore[call-arg]

# Machine-readable catalog backing the devgraph://tool-catalog resource, kept
# next to the @server.tool() registrations below so it can't silently drift
# out of sync with the actual tool surface — a client can read this in one
# call instead of relying on per-tool docstrings alone.
_TOOL_CATALOG: list[dict[str, Any]] = [
    {"name": "search_component", "identifier_kind": "name/description substring", "envelope": True, "phase": 1},
    {"name": "list_recent_changes", "identifier_kind": "commit-count window (within_commits), optional entity_type label", "envelope": True, "phase": 3},
    {"name": "trace_request_flow", "identifier_kind": "endpoint name", "envelope": False, "phase": 1},
    {"name": "get_service_dependencies", "identifier_kind": "service name", "envelope": False, "phase": 1},
    {"name": "find_callers", "identifier_kind": "function/class/service/endpoint name (not a file path)", "envelope": True, "phase": 1},
    {"name": "find_related_files", "identifier_kind": "function/class name (not a file path)", "envelope": True, "phase": 1},
    {"name": "summarise_repository", "identifier_kind": None, "envelope": False, "phase": 1},
    {"name": "compare_branches", "identifier_kind": "branch names", "envelope": False, "phase": 1, "note": "stub until git metadata is fully wired"},
    {"name": "impact_analysis", "identifier_kind": "function/class name (not a file path)", "envelope": True, "phase": 1},
    {"name": "impact_analysis_for_diff", "identifier_kind": "two git refs (base_ref, head_ref), both must exist locally", "envelope": True, "phase": 3},
    {"name": "explain_architecture", "identifier_kind": None, "envelope": False, "phase": 1},
    {"name": "list_services", "identifier_kind": None, "envelope": True, "phase": 1},
    {"name": "explain_decision", "identifier_kind": "DesignDecision name/id", "envelope": False, "phase": 2},
    {"name": "find_requirements_for", "identifier_kind": "component name", "envelope": False, "phase": 2},
    {"name": "trace_design_rationale", "identifier_kind": "component name", "envelope": False, "phase": 2},
    {"name": "find_mentions", "identifier_kind": "entity name (mentioned_by) or Document repo-relative path (mentions)", "envelope": True, "phase": 2},
    {"name": "blame_component", "identifier_kind": "file path (not a function name)", "envelope": False, "phase": 3},
    {"name": "find_related_prs", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in"},
    {"name": "god_nodes", "identifier_kind": None, "envelope": True, "phase": 3},
    {"name": "find_dependency_cycles", "identifier_kind": "dependency relationship type (CALLS/DEPENDS_ON/EXTENDS/IMPORTS/USES), not a component name", "envelope": True, "phase": 3},
    {"name": "issue_history_for", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in"},
    {"name": "get_source", "identifier_kind": "function/class name (not a file path)", "envelope": False, "phase": 2},
    {"name": "run_cypher", "identifier_kind": "raw Cypher", "envelope": False, "phase": None, "note": "only registered when enable_run_cypher=true; prefer the purpose-built tools above"},
]


def telemetry_path() -> Path:
    """Local JSONL store of MCP tool calls.

    Lives in the existing DevGraph state directory next to registry.sqlite3
    and devgraph.log (RepoRegistry already creates that directory). Local
    file only — nothing here is ever sent anywhere, and this is unrelated to
    `Settings.telemetry_enabled`, which governs outbound telemetry.
    """
    return get_settings().registry_db_path.parent / _TELEMETRY_FILENAME


def record_tool_call(*, tool: str, duration_ms: float, ok: bool) -> None:
    """Append one metadata-only record of a tool call.

    Records *that* a tool ran, never *what* was asked or answered: no
    arguments — not even the repo_id every tool takes — no Cypher and no
    results, only the four `_TELEMETRY_FIELDS` written below.

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
            {"ts": time.time(), "tool": tool, "duration_ms": duration_ms, "ok": ok},
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
    anything beyond the four allowed fields to the API.
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
            entries.append({field: entry[field] for field in _TELEMETRY_FIELDS if field in entry})
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
    took and whether it succeeded.
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
                duration_ms=(time.monotonic() - start) * 1000,
                ok=ok,
            )

    return wrapper


def build_server(engine: GraphEngine, registry: RepoRegistry | None = None) -> MCPServer:
    """Construct an MCPServer with every DevGraph tool registered against `engine`.

    `registry` is required for `get_source` (it resolves a repo_id to its
    registered root path to read source off disk); when omitted, a registry
    is opened from settings so existing single-argument callers keep working.
    """
    settings = get_settings()
    if registry is None:
        registry = RepoRegistry(settings.registry_db_path)
    server = MCPServer(
        name="devgraph",
        version="0.1.0",
        instructions=(
            "DevGraph: a local architecture knowledge graph for explicitly-registered "
            "repositories. Prefer these tools over reading source files directly when "
            "answering structural/dependency/history questions — they query a "
            "pre-built graph instead of re-scanning the repo. Every tool takes a "
            "repo_id (the id shown by `devgraph list`) and defaults to that repo only; "
            "pass cross_repo=true only when the user explicitly wants results across "
            "multiple registered repositories."
        ),
    )

    # Single chokepoint for tool-call telemetry: every `@server.tool(...)`
    # registration below goes through this rebound decorator, including any
    # tool added later, so instrumentation can't be forgotten on a new tool
    # the way a per-tool wrapper would be.
    _register_tool = server.tool

    def _instrumented_tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = _register_tool(*args, **kwargs)
        return lambda fn: register(_instrument(fn))

    server.tool = _instrumented_tool  # type: ignore[method-assign]

    @server.tool(annotations=_READ_ONLY)
    def search_component(
        repo_id: str,
        query: str,
        cross_repo: bool = False,
        max_results: int = 15,
        modified_within_commits: int | None = None,
    ) -> dict[str, Any]:
        """Search for components by name/description; returns {count, results, truncated}.
        Pass modified_within_commits to restrict to components touched within the last
        N commits repo-wide (requires git-history recency staging; entities never staged
        are excluded, not silently included)."""
        return devgraph_tools.search_component(
            engine, repo_id, query, cross_repo, max_results, modified_within_commits
        )

    @server.tool(annotations=_READ_ONLY)
    def god_nodes(
        repo_id: str,
        cross_repo: bool = False,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Return the most-connected nodes in the graph — the core abstractions
        a new agent should look at first to orient itself in an unfamiliar repo.
        Returns {count, results, truncated} with degree (number of direct relationships)."""
        return devgraph_tools.god_nodes(engine, repo_id, cross_repo, max_results)

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
        restricts to one node label (validated against NODE_LABELS)."""
        return devgraph_tools.list_recent_changes(
            engine, repo_id, within_commits, entity_type, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def trace_request_flow(repo_id: str, start_endpoint: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the request flow from an endpoint through services, datastores, and queues."""
        return devgraph_tools.trace_request_flow(engine, repo_id, start_endpoint, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def get_service_dependencies(repo_id: str, service_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Get all dependencies (services, datastores, queues) for a given service."""
        return devgraph_tools.get_service_dependencies(engine, repo_id, service_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_callers(
        repo_id: str,
        target_name: str,
        cross_repo: bool = False,
        max_results: int = 15,
        scope_to_class: str | None = None,
        modified_within_commits: int | None = None,
    ) -> dict[str, Any]:
        """Find all callers of a target; returns {count, results, truncated}. CALLS is
        name-based, not type-resolved — pass scope_to_class to narrow to callers made
        from within a specific class's own methods and cut noise from unrelated
        same-named methods elsewhere in the repo. Pass modified_within_commits to
        restrict to targets touched within the last N commits repo-wide (requires
        git-history recency staging; entities never staged are excluded, not silently
        included)."""
        return devgraph_tools.find_callers(
            engine, repo_id, target_name, cross_repo, max_results, scope_to_class, modified_within_commits
        )

    @server.tool(annotations=_READ_ONLY)
    def find_related_files(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find related files; each list returns {count, results, truncated} envelope."""
        return devgraph_tools.find_related_files(engine, repo_id, component_name, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def summarise_repository(repo_id: str) -> dict[str, Any]:
        """Get a high-level summary of a repository's architecture (node counts by type)."""
        return devgraph_tools.summarise_repository(engine, repo_id)

    @server.tool(annotations=_READ_ONLY)
    def compare_branches(repo_id: str, branch_a: str, branch_b: str) -> dict[str, Any]:
        """Compare architecture between two branches. Stub until git metadata is fully wired (Phase 3)."""
        return devgraph_tools.compare_branches(engine, repo_id, branch_a, branch_b)

    @server.tool(annotations=_READ_ONLY)
    def impact_analysis(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Analyze component impact; dependents wrapped in {count, results, truncated} envelopes."""
        return devgraph_tools.impact_analysis(engine, repo_id, component_name, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def impact_analysis_for_diff(
        repo_id: str,
        base_ref: str,
        head_ref: str,
        cross_repo: bool = False,
        max_results: int = 15,
    ) -> dict[str, Any]:
        """Analyze the combined impact of every component changed between two git refs
        (e.g. a PR's base/head branches). Composes a local git diff with the same
        dependent-tracing impact_analysis uses, across every changed component at once.
        Both refs must already exist locally — never fetches from a remote. Dependents
        wrapped in {count, results, truncated} envelopes."""
        return devgraph_tools.impact_analysis_for_diff(
            engine, registry, repo_id, base_ref, head_ref, cross_repo, max_results
        )

    @server.tool(annotations=_READ_ONLY)
    def explain_architecture(repo_id: str) -> dict[str, Any]:
        """Generate a high-level architectural explanation of the repository."""
        return devgraph_tools.explain_architecture(engine, repo_id)

    @server.tool(annotations=_READ_ONLY)
    def list_services(repo_id: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """List all services; returns {count, results, truncated}."""
        return devgraph_tools.list_services(engine, repo_id, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def explain_decision(repo_id: str, decision_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Explain a design decision: its rationale, what it documents, and what it supersedes."""
        return devgraph_tools.explain_decision(engine, repo_id, decision_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_requirements_for(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find requirements a component (module/service/etc.) satisfies."""
        return devgraph_tools.find_requirements_for(engine, repo_id, component_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def trace_design_rationale(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the design rationale (requirements, decisions, notes) behind a component."""
        return devgraph_tools.trace_design_rationale(engine, repo_id, component_name, cross_repo)

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
        direction="mentions": find what the Document at repo-relative path name mentions."""
        return devgraph_tools.find_mentions(engine, repo_id, name, label, direction, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def blame_component(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find commits that modified a component's file, most recent first
        (pass a file path, not a function name)."""
        return devgraph_tools.blame_component(engine, repo_id, component_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_related_prs(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find related PRs; returns {count, results, truncated}. Falls back to gh CLI
        when PR ingestion is not configured."""
        return devgraph_tools.find_related_prs(engine, repo_id, component_name, cross_repo, max_results, registry)

    @server.tool(annotations=_READ_ONLY)
    def issue_history_for(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find issue history; returns {count, results, truncated}. Falls back to gh CLI
        when issue ingestion is not configured."""
        return devgraph_tools.issue_history_for(engine, repo_id, component_name, cross_repo, max_results, registry)

    @server.tool(annotations=_READ_ONLY)
    def get_source(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Fetch a Function or Class's actual source text and full docstring (when present),
        using the graph's last-indexed line range. Reads live from disk — rescan first if
        the file may have changed since the last index."""
        return devgraph_tools.get_source(engine, registry, repo_id, component_name, cross_repo)

    if settings.enable_run_cypher:

        @server.tool(annotations=_ESCAPE_HATCH)
        def run_cypher(query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            """Advanced escape hatch: run raw Cypher directly. Disabled by default; only
            registered because enable_run_cypher=true is set for this instance. Prefer
            the purpose-built tools above whenever one of them fits."""
            return devgraph_tools.run_cypher(engine, query, parameters)

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
            "read once than to infer from trial and error across 20 tools."
        ),
        mime_type="application/json",
    )
    def tool_catalog() -> str:
        catalog = _TOOL_CATALOG if settings.enable_run_cypher else [
            t for t in _TOOL_CATALOG if t["name"] != "run_cypher"
        ]
        return json.dumps(catalog, indent=2)

    return server


def main() -> None:
    """Entry point: connect to Neo4j, initialize schema, run the stdio MCP server.

    Also starts the tray app (watcher + incremental indexer) as a detached
    background process if one isn't already running, so a registered repo's
    saved changes get reindexed without anyone manually running
    `devgraph tray start` or `python -m devgraph.agent.tray` first. This is a
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

    Run with: `.venv/Scripts/python -m devgraph.mcp.server`
    """
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    engine.verify_connectivity()
    engine.init_schema()
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

    server = build_server(engine, registry)
    try:
        server.run("stdio")
    finally:
        engine.close()
        registry.close()
        try:
            lifecycle.stop_tray_if_last_holder()
        except Exception:
            logger.warning("failed to stop the DevGraph tray app on shutdown", exc_info=True)


if __name__ == "__main__":
    main()
