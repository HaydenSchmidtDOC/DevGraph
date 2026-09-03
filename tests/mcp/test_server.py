"""Integration tests for the MCP server: tool registration and a real call
through the mcp SDK's MCPServer.call_tool()/list_tools(), against live Neo4j.

These exist because devgraph/mcp/server.py was previously written against
the wrong SDK shape (Server.add_request_handler(name, tool_def) instead of
the actual (method, params_type, handler) signature) and would have raised
TypeError the moment anything tried to run it — nothing exercised it end to
end. These tests call through the real MCPServer API so a similar mismatch
fails the test suite instead of only surfacing when a real client connects.
"""

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.server import build_server


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def seeded_graph(engine):
    engine.upsert_repository("_smoketest_mcp_server", "Smoke Test", "/tmp/smoketest")
    engine.upsert_node("Service", "_smoketest_mcp_server", "TestService", {})
    engine.upsert_node("Module", "_smoketest_mcp_server", "app.py", {})
    engine.upsert_relationship(
        "Module", "app.py", "CONTAINS", "Service", "TestService", "_smoketest_mcp_server"
    )
    yield engine
    engine.delete_repository("_smoketest_mcp_server")


class TestServerBuild:
    def test_all_18_tools_registered(self, engine):
        server = build_server(engine)
        import asyncio

        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}

        assert len(tools) == 19
        # spot check across all three phases plus Implementation Plan #3/6's new tools
        assert "search_component" in names
        assert "explain_decision" in names
        assert "blame_component" in names
        assert "impact_analysis_for_diff" in names
        assert "get_source" in names
        assert "find_mentions" in names

    def test_run_cypher_not_registered_by_default(self, engine):
        server = build_server(engine)
        import asyncio

        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert "run_cypher" not in names

    def test_all_tools_carry_read_only_annotation(self, engine):
        """Every DevGraph tool queries the graph or reads disk; none write —
        clients (and the MCP host UI) rely on readOnlyHint to treat these
        calls as safe to run without confirmation."""
        server = build_server(engine)
        import asyncio

        tools = asyncio.run(server.list_tools())
        for tool in tools:
            assert tool.annotations is not None, f"{tool.name} has no annotations"
            assert tool.annotations.read_only_hint is True, f"{tool.name} should be read_only_hint=True"


class TestServerResources:
    def test_client_guide_resource_registered_and_readable(self, engine):
        server = build_server(engine)
        import asyncio

        resources = asyncio.run(server.list_resources())
        uris = {str(r.uri) for r in resources}
        assert "devgraph://client-guide" in uris

        content = asyncio.run(server.read_resource("devgraph://client-guide"))
        text = content[0].content
        assert "DevGraph" in text
        assert "search_component" in text

    def test_tool_catalog_resource_lists_every_registered_tool(self, engine):
        import asyncio
        import json

        server = build_server(engine)
        tools = asyncio.run(server.list_tools())
        tool_names = {t.name for t in tools}

        content = asyncio.run(server.read_resource("devgraph://tool-catalog"))
        catalog = json.loads(content[0].content)
        catalog_names = {entry["name"] for entry in catalog}

        assert catalog_names == tool_names


class TestServerToolCall:
    def test_summarise_repository_via_server(self, seeded_graph):
        server = build_server(seeded_graph)
        import asyncio

        result = asyncio.run(
            server.call_tool("summarise_repository", {"repo_id": "_smoketest_mcp_server"})
        )
        assert result.is_error is False
        assert result.structured_content["service_count"] == 1
        assert result.structured_content["module_count"] == 1

    def test_search_component_via_server(self, seeded_graph):
        server = build_server(seeded_graph)
        import asyncio

        result = asyncio.run(
            server.call_tool(
                "search_component", {"repo_id": "_smoketest_mcp_server", "query": "TestService"}
            )
        )
        assert result.is_error is False
        # Envelope format: structured_content is now {count, results, truncated, ...}
        payload = result.structured_content
        assert "results" in payload
        assert "count" in payload
        assert "truncated" in payload
        assert any(item["name"] == "TestService" for item in payload["results"])

    def test_cross_repo_scoping_default_false(self, seeded_graph):
        seeded_graph.upsert_repository("_smoketest_mcp_server_b", "Other", "/tmp/other")
        seeded_graph.upsert_node("Service", "_smoketest_mcp_server_b", "OtherService", {})

        server = build_server(seeded_graph)
        import asyncio

        try:
            result = asyncio.run(
                server.call_tool(
                    "search_component", {"repo_id": "_smoketest_mcp_server", "query": "Service"}
                )
            )
            payload = result.structured_content["results"]
            assert all(item["repo_id"] == "_smoketest_mcp_server" for item in payload)
        finally:
            seeded_graph.delete_repository("_smoketest_mcp_server_b")
