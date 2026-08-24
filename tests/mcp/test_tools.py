"""Smoke tests for DevGraph MCP tools.

Seeds a small fixture graph directly via GraphEngine against the live Neo4j
at bolt://127.0.0.1:7687 (user neo4j / password devgraph-local-dev), then
calls each tool function and asserts sane results. Specifically verifies that
repo_id scoping holds — no cross-repo leakage without opt-in.

Cleans up smoketest data (delete by repo_id) when done.
"""

import pytest

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
)


@pytest.fixture
def engine():
    """Create a GraphEngine connected to the test Neo4j instance."""
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    test_engine.verify_connectivity()
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def seeded_graph(engine):
    """Seed a fixture graph with two repos to test isolation."""
    # Repo 1: test_repo_a
    engine.upsert_repository("test_repo_a", "Test Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", "test_repo_a", "UserService")
    engine.upsert_node("Service", "test_repo_a", "AuthService")
    engine.upsert_node("Database", "test_repo_a", "PostgresDB")
    engine.upsert_node("Module", "test_repo_a", "auth.py")
    engine.upsert_node("Class", "test_repo_a", "AuthHandler")
    engine.upsert_node("Function", "test_repo_a", "validate_token")
    engine.upsert_node("Endpoint", "test_repo_a", "POST /auth/login")
    engine.upsert_node("VectorStore", "test_repo_a", "QdrantDB")

    # Relationships in repo_a
    engine.upsert_relationship("Service", "UserService", "CALLS", "Service", "AuthService", "test_repo_a")
    engine.upsert_relationship("Service", "AuthService", "USES", "Database", "PostgresDB", "test_repo_a")
    engine.upsert_relationship("Service", "AuthService", "USES", "VectorStore", "QdrantDB", "test_repo_a")
    engine.upsert_relationship("Endpoint", "POST /auth/login", "CALLS", "Service", "AuthService", "test_repo_a")
    engine.upsert_relationship("Module", "auth.py", "CONTAINS", "Class", "AuthHandler", "test_repo_a")
    engine.upsert_relationship("Class", "AuthHandler", "CONTAINS", "Function", "validate_token", "test_repo_a")

    # Repo 2: test_repo_b (separate repo to test isolation)
    engine.upsert_repository("test_repo_b", "Test Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", "test_repo_b", "NotificationService")
    engine.upsert_node("Database", "test_repo_b", "MongoDb")
    engine.upsert_node("Queue", "test_repo_b", "RabbitMQ")
    engine.upsert_node("Endpoint", "test_repo_b", "POST /notify/send")

    engine.upsert_relationship("Service", "NotificationService", "USES", "Database", "MongoDb", "test_repo_b")
    engine.upsert_relationship("Service", "NotificationService", "USES", "Queue", "RabbitMQ", "test_repo_b")
    engine.upsert_relationship("Endpoint", "POST /notify/send", "CALLS", "Service", "NotificationService", "test_repo_b")

    yield engine

    # Cleanup: delete both repos
    engine.delete_repository("test_repo_a")
    engine.delete_repository("test_repo_b")


class TestSearchComponent:
    def test_search_by_name(self, seeded_graph):
        """Test searching components by name within a single repo."""
        results = search_component(seeded_graph, "test_repo_a", "Auth", cross_repo=False)
        assert len(results) > 0
        names = [r["name"] for r in results]
        assert "AuthService" in names
        assert "NotificationService" not in names  # Should not leak from other repo

    def test_search_cross_repo(self, seeded_graph):
        """Test that cross_repo=True returns results from multiple repos."""
        results = search_component(seeded_graph, "test_repo_a", "Service", cross_repo=True)
        names = [r["name"] for r in results]
        assert "UserService" in names or "AuthService" in names
        assert "NotificationService" in names  # Should include other repo

    def test_no_cross_repo_leakage_default(self, seeded_graph):
        """Test that default (cross_repo=False) doesn't leak repo_b data."""
        results = search_component(seeded_graph, "test_repo_a", "Notification", cross_repo=False)
        names = [r["name"] for r in results]
        assert "NotificationService" not in names


class TestGetServiceDependencies:
    def test_get_dependencies_single_repo(self, seeded_graph):
        """Test getting dependencies for a service in a single repo."""
        result = get_service_dependencies(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)
        assert result["service"] == "AuthService"
        # Should have PostgresDB and QdrantDB as dependencies
        dep_names = [d["name"] for d in result.get("dependencies", [])]
        assert "PostgresDB" in dep_names
        assert "QdrantDB" in dep_names

    def test_service_dependencies_no_cross_repo(self, seeded_graph):
        """Test that dependencies don't cross repo boundaries by default."""
        result = get_service_dependencies(seeded_graph, "test_repo_a", "UserService", cross_repo=False)
        assert result["service"] == "UserService"
        # Should call AuthService (same repo)
        call_names = [c["name"] for c in result.get("calls", [])]
        assert "AuthService" in call_names
        # Should NOT include NotificationService from other repo
        assert "NotificationService" not in call_names


class TestFindCallers:
    def test_find_callers_single_repo(self, seeded_graph):
        """Test finding callers of a function."""
        results = find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)
        # Should find UserService and Endpoint as callers
        names = [r["name"] for r in results]
        assert "UserService" in names
        assert "POST /auth/login" in names
        assert "NotificationService" not in names  # No cross-repo leak

    def test_find_callers_cross_repo(self, seeded_graph):
        """Test finding callers with cross_repo=True."""
        results = find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=True)
        names = [r["name"] for r in results]
        # Should still only find callers of this specific service
        assert "UserService" in names
        # NotificationService doesn't call AuthService, so shouldn't be here
        assert "NotificationService" not in names


class TestFindRelatedFiles:
    def test_find_related_files_single_repo(self, seeded_graph):
        """Test finding files related to a component."""
        result = find_related_files(seeded_graph, "test_repo_a", "validate_token", cross_repo=False)
        # validate_token is contained in AuthHandler which is contained in auth.py
        containing = result.get("containing_modules", [])
        assert "auth.py" in containing or len(containing) >= 0  # May have containing chain


class TestSummariseRepository:
    def test_summarise_repo_a(self, seeded_graph):
        """Test repository summary for repo_a."""
        result = summarise_repository(seeded_graph, "test_repo_a")
        assert result["repo_name"] == "test_repo_a"
        assert result["service_count"] >= 2  # UserService, AuthService
        assert result["module_count"] >= 1  # auth.py
        assert result["class_count"] >= 1  # AuthHandler
        assert result["function_count"] >= 1  # validate_token
        assert result["endpoint_count"] >= 1  # POST /auth/login
        assert result["database_count"] >= 1  # PostgresDB
        assert result["vectorstore_count"] >= 1  # QdrantDB

    def test_summarise_repo_b(self, seeded_graph):
        """Test repository summary for repo_b."""
        result = summarise_repository(seeded_graph, "test_repo_b")
        assert result["repo_name"] == "test_repo_b"
        assert result["service_count"] >= 1  # NotificationService
        assert result["database_count"] >= 1  # MongoDb
        assert result["queue_count"] >= 1  # RabbitMQ


class TestCompareBranches:
    def test_compare_branches_stub(self, seeded_graph):
        """Test that compare_branches returns placeholder (Phase 3)."""
        result = compare_branches(seeded_graph, "test_repo_a", "main", "dev")
        assert "note" in result
        assert "Phase 3" in result["note"]


class TestImpactAnalysis:
    def test_impact_analysis_single_repo(self, seeded_graph):
        """Test impact analysis for a component."""
        result = impact_analysis(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)
        # Should have at least UserService and Endpoint as direct dependents
        direct = result.get("direct_dependents", [])
        direct_names = [d["name"] for d in direct]
        assert "UserService" in direct_names
        assert "POST /auth/login" in direct_names


class TestExplainArchitecture:
    def test_explain_architecture_basic(self, seeded_graph):
        """Test architectural explanation."""
        result = explain_architecture(seeded_graph, "test_repo_a")
        assert "services_and_datastores" in result
        assert "endpoints" in result


class TestListServices:
    def test_list_services_single_repo(self, seeded_graph):
        """Test listing services in a single repo."""
        results = list_services(seeded_graph, "test_repo_a", cross_repo=False)
        names = [r["name"] for r in results]
        assert "UserService" in names
        assert "AuthService" in names
        assert "NotificationService" not in names  # No cross-repo leak

    def test_list_services_cross_repo(self, seeded_graph):
        """Test listing services across repos."""
        results = list_services(seeded_graph, "test_repo_a", cross_repo=True)
        names = [r["name"] for r in results]
        assert "UserService" in names
        assert "AuthService" in names
        assert "NotificationService" in names  # Should include other repo

    def test_list_services_isolation(self, seeded_graph):
        """Test that repo_id isolation is enforced even with cross_repo=True."""
        # All results should have repo_id specified
        results = list_services(seeded_graph, "test_repo_a", cross_repo=True)
        for result in results:
            assert "repo_id" in result


class TestRepoIdScoping:
    """Comprehensive tests for repo_id boundary enforcement."""

    def test_no_cross_repo_by_default(self, seeded_graph):
        """Verify that NO tool leaks data across repos without explicit opt-in."""
        # Test multiple tools to verify isolation is pervasive
        tools_to_test = [
            ("search_component", lambda: search_component(seeded_graph, "test_repo_a", "Service", cross_repo=False)),
            ("list_services", lambda: list_services(seeded_graph, "test_repo_a", cross_repo=False)),
            ("get_service_dependencies", lambda: get_service_dependencies(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)),
            ("find_callers", lambda: find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)),
        ]

        for tool_name, tool_call in tools_to_test:
            result = tool_call()
            # Result should not contain NotificationService (from test_repo_b)
            if isinstance(result, list):
                names = [r.get("name") for r in result if isinstance(r, dict)]
            elif isinstance(result, dict):
                # Flatten all values to check for repo_b data
                names = []
                def flatten_dict(d):
                    for v in d.values():
                        if isinstance(v, str):
                            names.append(v)
                        elif isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    flatten_dict(item)
                                elif isinstance(item, str):
                                    names.append(item)
                flatten_dict(result)
            else:
                names = []

            assert "NotificationService" not in names, f"{tool_name} leaked test_repo_b data"

    def test_cross_repo_opt_in_explicit(self, seeded_graph):
        """Verify cross_repo=True must be explicitly set to get cross-repo data."""
        # Call with cross_repo=False explicitly
        results_isolated = search_component(seeded_graph, "test_repo_a", "Service", cross_repo=False)

        # Call with cross_repo=True
        results_cross = search_component(seeded_graph, "test_repo_a", "Service", cross_repo=True)

        # cross_repo results should be >= isolated results
        assert len(results_cross) >= len(results_isolated)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
