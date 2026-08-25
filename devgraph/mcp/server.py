"""MCP server implementation for DevGraph.

Exposes DevGraph's high-level tools (devgraph/mcp/tools.py) over the MCP
stdio transport via the `mcp` SDK's MCPServer, so any MCP-capable client
(Claude Code, etc.) can register this as a server and call tools without
ever writing Cypher. `run_cypher` is only registered when
Settings.enable_run_cypher is true (default off, per Design Brief
Principle 2/4) — it never appears in the tool listing otherwise.

Run directly: `.venv/Scripts/python -m devgraph.mcp.server`
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools as devgraph_tools
from devgraph.registry.store import RepoRegistry


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

    @server.tool()
    def search_component(repo_id: str, query: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Search for components (modules, services, classes, functions) by name or description."""
        return devgraph_tools.search_component(engine, repo_id, query, cross_repo)

    @server.tool()
    def trace_request_flow(repo_id: str, start_endpoint: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the request flow from an endpoint through services, datastores, and queues."""
        return devgraph_tools.trace_request_flow(engine, repo_id, start_endpoint, cross_repo)

    @server.tool()
    def get_service_dependencies(repo_id: str, service_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Get all dependencies (services, datastores, queues) for a given service."""
        return devgraph_tools.get_service_dependencies(engine, repo_id, service_name, cross_repo)

    @server.tool()
    def find_callers(repo_id: str, target_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find all functions, services, or endpoints that call a given target
        (pass a function/class name, not a file path)."""
        return devgraph_tools.find_callers(engine, repo_id, target_name, cross_repo)

    @server.tool()
    def find_related_files(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Find all files related to a component via CONTAINS, IMPORTS, CALLS relationships
        (pass a function/class name, not a file path)."""
        return devgraph_tools.find_related_files(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def summarise_repository(repo_id: str) -> dict[str, Any]:
        """Get a high-level summary of a repository's architecture (node counts by type)."""
        return devgraph_tools.summarise_repository(engine, repo_id)

    @server.tool()
    def compare_branches(repo_id: str, branch_a: str, branch_b: str) -> dict[str, Any]:
        """Compare architecture between two branches. Stub until git metadata is fully wired (Phase 3)."""
        return devgraph_tools.compare_branches(engine, repo_id, branch_a, branch_b)

    @server.tool()
    def impact_analysis(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Analyze the impact of changing a component: direct/transitive dependents and risk level
        (pass a function/class name, not a file path)."""
        return devgraph_tools.impact_analysis(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def explain_architecture(repo_id: str) -> dict[str, Any]:
        """Generate a high-level architectural explanation of the repository."""
        return devgraph_tools.explain_architecture(engine, repo_id)

    @server.tool()
    def list_services(repo_id: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """List all services in a repository."""
        return devgraph_tools.list_services(engine, repo_id, cross_repo)

    @server.tool()
    def explain_decision(repo_id: str, decision_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Explain a design decision: its rationale, what it documents, and what it supersedes."""
        return devgraph_tools.explain_decision(engine, repo_id, decision_name, cross_repo)

    @server.tool()
    def find_requirements_for(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find requirements a component (module/service/etc.) satisfies."""
        return devgraph_tools.find_requirements_for(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def trace_design_rationale(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Trace the design rationale (requirements, decisions, notes) behind a component."""
        return devgraph_tools.trace_design_rationale(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def blame_component(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find commits that modified a component's file, most recent first
        (pass a file path, not a function name)."""
        return devgraph_tools.blame_component(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def find_related_prs(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find pull requests related to a component via commits/issues (requires PR/issue ingestion having been run)."""
        return devgraph_tools.find_related_prs(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def issue_history_for(repo_id: str, component_name: str, cross_repo: bool = False) -> list[dict[str, Any]]:
        """Find issues referenced by commits that touched a component (requires PR/issue ingestion having been run)."""
        return devgraph_tools.issue_history_for(engine, repo_id, component_name, cross_repo)

    @server.tool()
    def get_source(repo_id: str, component_name: str, cross_repo: bool = False) -> dict[str, Any]:
        """Fetch a Function or Class's actual source text and full docstring (when present),
        using the graph's last-indexed line range. Reads live from disk — rescan first if
        the file may have changed since the last index."""
        return devgraph_tools.get_source(engine, registry, repo_id, component_name, cross_repo)

    if settings.enable_run_cypher:

        @server.tool()
        def run_cypher(query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            """Advanced escape hatch: run raw Cypher directly. Disabled by default; only
            registered because enable_run_cypher=true is set for this instance. Prefer
            the purpose-built tools above whenever one of them fits."""
            return devgraph_tools.run_cypher(engine, query, parameters)

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
