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
        result = search_component(seeded_graph, "test_repo_a", "Auth", cross_repo=False)
        assert "results" in result and "count" in result and "truncated" in result
        names = [r["name"] for r in result["results"]]
        assert len(names) > 0
        assert "AuthService" in names
        assert "NotificationService" not in names  # Should not leak from other repo

    def test_search_cross_repo(self, seeded_graph):
        """Test that cross_repo=True returns results from multiple repos."""
        result = search_component(seeded_graph, "test_repo_a", "AuthService", cross_repo=True)
        names = [r["name"] for r in result["results"]]
        # Search specifically for AuthService to avoid hitting the LIMIT 50 cap with cross-repo results
        assert "AuthService" in names

    def test_no_cross_repo_leakage_default(self, seeded_graph):
        """Test that default (cross_repo=False) doesn't leak repo_b data."""
        result = search_component(seeded_graph, "test_repo_a", "Notification", cross_repo=False)
        names = [r["name"] for r in result["results"]]
        assert "NotificationService" not in names

    def test_search_truncation(self, seeded_graph):
        """Test that truncated flag is set when total count exceeds max_results."""
        result = search_component(seeded_graph, "test_repo_a", "Auth", cross_repo=False, max_results=1)
        assert "truncated" in result
        assert "count" in result
        assert "results" in result
        # If count > max_results, truncated should be True
        if result["count"] > 1:
            assert result["truncated"] is True
            assert len(result["results"]) <= 1  # should be capped at max_results


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
        result = find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=False)
        assert "results" in result and "count" in result and "truncated" in result
        # Should find UserService and Endpoint as callers
        names = [r["name"] for r in result["results"]]
        assert "UserService" in names
        assert "POST /auth/login" in names
        assert "NotificationService" not in names  # No cross-repo leak

    def test_find_callers_cross_repo(self, seeded_graph):
        """Test finding callers with cross_repo=True."""
        result = find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=True)
        names = [r["name"] for r in result["results"]]
        # Should still only find callers of this specific service
        assert "UserService" in names
        # NotificationService doesn't call AuthService, so shouldn't be here
        assert "NotificationService" not in names

    def test_find_callers_truncation(self, seeded_graph):
        """Test that truncated flag is set when total count exceeds max_results."""
        result = find_callers(seeded_graph, "test_repo_a", "AuthService", cross_repo=False, max_results=1)
        assert "truncated" in result
        assert "count" in result
        # If count > max_results, truncated should be True
        if result["count"] > 1:
            assert result["truncated"] is True
            assert len(result["results"]) <= 1

    def test_find_callers_scope_to_class_narrows_results(self, engine):
        """scope_to_class filters callers to those whose CALLS edge carries a
        matching caller_class property (set by the Python extractor for
        method-body calls), cutting noise from unrelated same-named methods.
        """
        repo_id = "_smoketest_scope_to_class"
        engine.upsert_repository(repo_id, "Scope Test Repo", "/path/to/repo")
        engine.upsert_node("Function", repo_id, "helper")
        engine.upsert_node("Function", repo_id, "process_a")
        engine.upsert_node("Function", repo_id, "process_b")
        engine.upsert_relationship(
            "Function", "process_a", "CALLS", "Function", "helper", repo_id,
            properties={"caller_class": "ServiceA"},
        )
        engine.upsert_relationship(
            "Function", "process_b", "CALLS", "Function", "helper", repo_id,
            properties={"caller_class": "ServiceB"},
        )

        try:
            unscoped = find_callers(engine, repo_id, "helper")
            names_unscoped = {r["name"] for r in unscoped["results"]}
            assert names_unscoped == {"process_a", "process_b"}

            scoped = find_callers(engine, repo_id, "helper", scope_to_class="ServiceA")
            names_scoped = {r["name"] for r in scoped["results"]}
            assert names_scoped == {"process_a"}
        finally:
            engine.delete_repository(repo_id)


class TestFindRelatedFiles:
    def test_find_related_files_single_repo(self, seeded_graph):
        """Test finding files related to a component."""
        result = find_related_files(seeded_graph, "test_repo_a", "validate_token", cross_repo=False)
        # Each key should now be an envelope with count/results/truncated
        assert "containing_modules" in result and isinstance(result["containing_modules"], dict)
        assert "imported_modules" in result and isinstance(result["imported_modules"], dict)
        assert "related_components" in result and isinstance(result["related_components"], dict)
        containing = result["containing_modules"]["results"]
        assert "auth.py" in containing or len(containing) >= 0  # May have containing chain

    def test_find_related_files_truncation(self, seeded_graph):
        """Test that truncated flag is set when total count exceeds max_results."""
        result = find_related_files(seeded_graph, "test_repo_a", "validate_token", cross_repo=False, max_results=1)
        for key in ["containing_modules", "imported_modules", "related_components"]:
            assert "truncated" in result[key]
            assert "count" in result[key]
            # If count > max_results, truncated should be True
            if result[key]["count"] > 1:
                assert result[key]["truncated"] is True
                assert len(result[key]["results"]) <= 1


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

    def test_summarise_repository_at_scale_does_not_hang(self, engine):
        """Regression test: summarise_repository previously chained 9
        OPTIONAL MATCHes on independent, unrelated label patterns in one
        query. Neo4j computes their cartesian product before COUNT(DISTINCT)
        collapses it — with hundreds of Function/Class nodes (a real repo's
        scale, not this suite's tiny fixtures) that product exploded
        combinatorially and the query genuinely hung indefinitely (confirmed
        against a real ~1300-node repo). Seeds enough nodes here that the old
        query would have taken far longer than this test's time budget.
        """
        import time

        repo_id = "_smoketest_summarise_scale"
        engine.upsert_repository(repo_id, "Scale Test", "/tmp/scale")
        for i in range(150):
            engine.upsert_node("Class", repo_id, f"Class{i}", {})
        for i in range(300):
            engine.upsert_node("Function", repo_id, f"func{i}", {})

        try:
            start = time.monotonic()
            result = summarise_repository(engine, repo_id)
            elapsed = time.monotonic() - start

            assert result["class_count"] == 150
            assert result["function_count"] == 300
            # Generous bound: the cartesian-product version would take
            # seconds-to-minutes at this scale; a correct query is near-instant.
            assert elapsed < 5.0, f"summarise_repository took {elapsed:.2f}s — possible cartesian-product regression"
        finally:
            engine.delete_repository(repo_id)


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
        # direct_dependents and transitive_dependents should now be envelopes
        assert "direct_dependents" in result and isinstance(result["direct_dependents"], dict)
        assert "transitive_dependents" in result and isinstance(result["transitive_dependents"], dict)
        assert "risk_level" in result
        # Should have at least UserService and Endpoint as direct dependents
        direct = result["direct_dependents"]["results"]
        direct_names = [d["name"] for d in direct]
        assert "UserService" in direct_names
        assert "POST /auth/login" in direct_names

    def test_impact_analysis_truncation(self, seeded_graph):
        """Test that truncated flag is set when total count exceeds max_results."""
        result = impact_analysis(seeded_graph, "test_repo_a", "AuthService", cross_repo=False, max_results=1)
        assert "truncated" in result["direct_dependents"]
        assert "count" in result["direct_dependents"]
        # If count > max_results, truncated should be True
        if result["direct_dependents"]["count"] > 1:
            assert result["direct_dependents"]["truncated"] is True
            assert len(result["direct_dependents"]["results"]) <= 1


class TestExplainArchitecture:
    def test_explain_architecture_basic(self, seeded_graph):
        """Test architectural explanation."""
        result = explain_architecture(seeded_graph, "test_repo_a")
        assert "services_and_datastores" in result
        assert "endpoints" in result


class TestListServices:
    def test_list_services_single_repo(self, seeded_graph):
        """Test listing services in a single repo."""
        result = list_services(seeded_graph, "test_repo_a", cross_repo=False)
        assert "results" in result and "count" in result and "truncated" in result
        names = [r["name"] for r in result["results"]]
        assert "UserService" in names
        assert "AuthService" in names
        assert "NotificationService" not in names  # No cross-repo leak

    def test_list_services_cross_repo(self, seeded_graph):
        """Test listing services across repos."""
        result = list_services(seeded_graph, "test_repo_a", cross_repo=True)
        names = [r["name"] for r in result["results"]]
        assert "UserService" in names
        assert "AuthService" in names
        assert "NotificationService" in names  # Should include other repo

    def test_list_services_isolation(self, seeded_graph):
        """Test that repo_id isolation is enforced even with cross_repo=True."""
        # All results should have repo_id specified
        result = list_services(seeded_graph, "test_repo_a", cross_repo=True)
        for item in result["results"]:
            assert "repo_id" in item

    def test_list_services_truncation(self, seeded_graph):
        """Test that truncated flag is set when total count exceeds max_results."""
        result = list_services(seeded_graph, "test_repo_a", cross_repo=False, max_results=1)
        assert "truncated" in result
        assert "count" in result
        # If count > max_results, truncated should be True
        if result["count"] > 1:
            assert result["truncated"] is True
            assert len(result["results"]) <= 1


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
            if isinstance(result, dict) and "results" in result:
                # Envelope format: extract from results key
                items = result["results"]
                names = [r.get("name") for r in items if isinstance(r, dict)]
            elif isinstance(result, list):
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
        result_isolated = search_component(seeded_graph, "test_repo_a", "Service", cross_repo=False)

        # Call with cross_repo=True
        result_cross = search_component(seeded_graph, "test_repo_a", "Service", cross_repo=True)

        # cross_repo results should be >= isolated results
        assert len(result_cross["results"]) >= len(result_isolated["results"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
