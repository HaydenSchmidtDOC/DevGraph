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

Run directly: `.venv/Scripts/python -m devgraph.mcp.server`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools as devgraph_tools
from devgraph.registry.store import RepoRegistry

_CLIENT_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent / "DEVGRAPH-CLIENT.md"

# Every DevGraph tool queries the graph or reads a file off disk; none of them
# ever write to Neo4j (writes only happen through the indexer/watcher/CLI, not
# through mcp/tools.py) — so every tool gets the same read-only annotation.
# run_cypher is the one exception: it's an arbitrary-query escape hatch, so it
# can't be vouched for as read-only in the general case.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_ESCAPE_HATCH = ToolAnnotations(readOnlyHint=False, openWorldHint=True)

# Machine-readable catalog backing the devgraph://tool-catalog resource, kept
# next to the @server.tool() registrations below so it can't silently drift
# out of sync with the actual tool surface — a client can read this in one
# call instead of relying on per-tool docstrings alone.
_TOOL_CATALOG: list[dict[str, Any]] = [
    {"name": "search_component", "identifier_kind": "name/description substring", "envelope": True, "phase": 1},
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
    {"name": "blame_component", "identifier_kind": "file path (not a function name)", "envelope": False, "phase": 3},
    {"name": "find_related_prs", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in"},
    {"name": "issue_history_for", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in"},
    {"name": "get_source", "identifier_kind": "function/class name (not a file path)", "envelope": False, "phase": 2},
    {"name": "run_cypher", "identifier_kind": "raw Cypher", "envelope": False, "phase": None, "note": "only registered when enable_run_cypher=true; prefer the purpose-built tools above"},
]


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

    @server.tool(annotations=_READ_ONLY)
    def search_component(repo_id: str, query: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Search for components by name/description; returns {count, results, truncated}."""
        return devgraph_tools.search_component(engine, repo_id, query, cross_repo, max_results)

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
    ) -> dict[str, Any]:
        """Find all callers of a target; returns {count, results, truncated}. CALLS is
        name-based, not type-resolved — pass scope_to_class to narrow to callers made
        from within a specific class's own methods and cut noise from unrelated
        same-named methods elsewhere in the repo."""
        return devgraph_tools.find_callers(engine, repo_id, target_name, cross_repo, max_results, scope_to_class)

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
    def blame_component(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find commits that modified a component's file, most recent first
        (pass a file path, not a function name)."""
        return devgraph_tools.blame_component(engine, repo_id, component_name, cross_repo)

    @server.tool(annotations=_READ_ONLY)
    def find_related_prs(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find related PRs; returns {count, results, truncated}."""
        return devgraph_tools.find_related_prs(engine, repo_id, component_name, cross_repo, max_results)

    @server.tool(annotations=_READ_ONLY)
    def issue_history_for(repo_id: str, component_name: str, cross_repo: bool = False, max_results: int = 15) -> dict[str, Any]:
        """Find issue history; returns {count, results, truncated}."""
        return devgraph_tools.issue_history_for(engine, repo_id, component_name, cross_repo, max_results)

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
            "read once than to infer from trial and error across 18 tools."
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

    Run with: `.venv/Scripts/python -m devgraph.mcp.server`
    """
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    engine.verify_connectivity()
    engine.init_schema()
    registry = RepoRegistry(settings.registry_db_path)

    server = build_server(engine, registry)
    try:
        server.run("stdio")
    finally:
        engine.close()
        registry.close()


if __name__ == "__main__":
    main()
