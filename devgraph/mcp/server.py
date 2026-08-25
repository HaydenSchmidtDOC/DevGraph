"""MCP server implementation for DevGraph.

Exposes high-level tools for architecture queries without requiring direct Cypher.
Registers tools with the mcp.server.Server and handles request routing.
"""

import json
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import (
    search_component,
    trace_request_flow,
    get_service_dependencies,
    find_callers,
    find_related_files,
    summarise_repository,
    compare_branches,
    impact_analysis,
    explain_architecture,
    list_services,
    explain_decision,
    find_requirements_for,
    trace_design_rationale,
    run_cypher,
)


# Tool definitions for MCP registration
TOOL_DEFINITIONS = [
    Tool(
        name="search_component",
        description="Search for components (modules, services, classes, functions) by name or description",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID to search within",
                },
                "query": {
                    "type": "string",
                    "description": "Search term (name substring or keyword)",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, search across all repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "query"],
        },
    ),
    Tool(
        name="trace_request_flow",
        description="Trace the request flow from an endpoint through services, datastores, and queues",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "start_endpoint": {
                    "type": "string",
                    "description": "Name of the endpoint to start tracing from",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, cross repository boundaries (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "start_endpoint"],
        },
    ),
    Tool(
        name="get_service_dependencies",
        description="Get all dependencies (services, datastores, queues) for a given service",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "service_name": {
                    "type": "string",
                    "description": "Name of the service to analyze",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, include cross-repo dependencies (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "service_name"],
        },
    ),
    Tool(
        name="find_callers",
        description="Find all functions, services, or endpoints that call a given target",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "target_name": {
                    "type": "string",
                    "description": "Name of the function/service/endpoint being called",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, find callers across repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "target_name"],
        },
    ),
    Tool(
        name="find_related_files",
        description="Find all files related to a component",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "component_name": {
                    "type": "string",
                    "description": "Name of the component",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, include cross-repo relationships (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "component_name"],
        },
    ),
    Tool(
        name="summarise_repository",
        description="Get a high-level summary of a repository's architecture",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID to summarize",
                },
            },
            "required": ["repo_id"],
        },
    ),
    Tool(
        name="compare_branches",
        description="Compare architecture between two branches (Phase 3: git metadata)",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "branch_a": {
                    "type": "string",
                    "description": "First branch name",
                },
                "branch_b": {
                    "type": "string",
                    "description": "Second branch name",
                },
            },
            "required": ["repo_id", "branch_a", "branch_b"],
        },
    ),
    Tool(
        name="impact_analysis",
        description="Analyze the impact of changing a component on the rest of the system",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "component_name": {
                    "type": "string",
                    "description": "Name of the component to analyze",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, include cross-repo impacts (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "component_name"],
        },
    ),
    Tool(
        name="explain_architecture",
        description="Generate a high-level architectural explanation of the repository",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
            },
            "required": ["repo_id"],
        },
    ),
    Tool(
        name="list_services",
        description="List all services in a repository",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, list services from all repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id"],
        },
    ),
    Tool(
        name="explain_decision",
        description="Explain a design decision: its rationale, what it documents, and history (Phase 2)",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "decision_name": {
                    "type": "string",
                    "description": "Name (id) of the DesignDecision note",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, search across repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "decision_name"],
        },
    ),
    Tool(
        name="find_requirements_for",
        description="Find requirements a component satisfies (Phase 2)",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "component_name": {
                    "type": "string",
                    "description": "Name of the component",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, search across repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "component_name"],
        },
    ),
    Tool(
        name="trace_design_rationale",
        description="Trace the design rationale (decisions, notes, requirements) behind a component (Phase 2)",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID",
                },
                "component_name": {
                    "type": "string",
                    "description": "Name of the component",
                },
                "cross_repo": {
                    "type": "boolean",
                    "description": "If true, search across repos (default: false)",
                    "default": False,
                },
            },
            "required": ["repo_id", "component_name"],
        },
    ),
]

# run_cypher is only registered if enable_run_cypher is True
CYPHER_TOOL_DEFINITION = Tool(
    name="run_cypher",
    description="Advanced escape hatch for direct Cypher queries (gated by enable_run_cypher setting)",
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Cypher query string",
            },
            "parameters": {
                "type": "object",
                "description": "Optional query parameters",
                "additionalProperties": True,
            },
        },
        "required": ["query"],
    },
)


class DevGraphMCPServer:
    """MCP server for DevGraph architecture queries."""

    def __init__(self, engine: GraphEngine) -> None:
        self.engine = engine
        self.settings = get_settings()
        self.server = Server("devgraph")
        self._setup_tools()

    def _setup_tools(self) -> None:
        """Register all available tools with the MCP server."""
        # Register standard tools
        for tool_def in TOOL_DEFINITIONS:
            self.server.add_request_handler(
                lambda name=tool_def.name: self._handle_list_tools_request(name),
                tool_def
            )

        # Register run_cypher only if enabled
        if self.settings.enable_run_cypher:
            self.server.add_request_handler(
                lambda: self._handle_list_tools_request(CYPHER_TOOL_DEFINITION.name),
                CYPHER_TOOL_DEFINITION
            )

    def _handle_list_tools_request(self, tool_name: str) -> list[TextContent]:
        """Return the tool definitions for list_tools requests."""
        return [TextContent(type="text", text=json.dumps(TOOL_DEFINITIONS, indent=2))]

    def _handle_tool_call(self, name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Route tool calls to their implementation functions."""
        try:
            if name == "search_component":
                result = search_component(
                    self.engine,
                    arguments["repo_id"],
                    arguments["query"],
                    arguments.get("cross_repo", False),
                )
            elif name == "trace_request_flow":
                result = trace_request_flow(
                    self.engine,
                    arguments["repo_id"],
                    arguments["start_endpoint"],
                    arguments.get("cross_repo", False),
                )
            elif name == "get_service_dependencies":
                result = get_service_dependencies(
                    self.engine,
                    arguments["repo_id"],
                    arguments["service_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "find_callers":
                result = find_callers(
                    self.engine,
                    arguments["repo_id"],
                    arguments["target_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "find_related_files":
                result = find_related_files(
                    self.engine,
                    arguments["repo_id"],
                    arguments["component_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "summarise_repository":
                result = summarise_repository(
                    self.engine,
                    arguments["repo_id"],
                )
            elif name == "compare_branches":
                result = compare_branches(
                    self.engine,
                    arguments["repo_id"],
                    arguments["branch_a"],
                    arguments["branch_b"],
                )
            elif name == "impact_analysis":
                result = impact_analysis(
                    self.engine,
                    arguments["repo_id"],
                    arguments["component_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "explain_architecture":
                result = explain_architecture(
                    self.engine,
                    arguments["repo_id"],
                )
            elif name == "list_services":
                result = list_services(
                    self.engine,
                    arguments["repo_id"],
                    arguments.get("cross_repo", False),
                )
            elif name == "explain_decision":
                result = explain_decision(
                    self.engine,
                    arguments["repo_id"],
                    arguments["decision_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "find_requirements_for":
                result = find_requirements_for(
                    self.engine,
                    arguments["repo_id"],
                    arguments["component_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "trace_design_rationale":
                result = trace_design_rationale(
                    self.engine,
                    arguments["repo_id"],
                    arguments["component_name"],
                    arguments.get("cross_repo", False),
                )
            elif name == "run_cypher":
                if not self.settings.enable_run_cypher:
                    return [
                        TextContent(
                            type="text",
                            text="run_cypher tool is disabled. Set enable_run_cypher=true in config to enable.",
                        )
                    ]
                result = run_cypher(
                    self.engine,
                    arguments["query"],
                    arguments.get("parameters"),
                )
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {str(e)}")]

    async def run(self) -> None:
        """Run the MCP server."""
        # This would be called by the MCP transport layer
        pass


def create_and_run_server() -> None:
    """Create engine, initialize schema, and return the server."""
    settings = get_settings()

    # Connect to Neo4j and initialize schema
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    engine.verify_connectivity()
    engine.init_schema()

    # Create and return server
    return DevGraphMCPServer(engine)


if __name__ == "__main__":
    server = create_and_run_server()
    print("DevGraph MCP server created successfully")
    print(f"Tools registered: {len(TOOL_DEFINITIONS)}")
