"""Smoke tests for Phase 2 MCP tools: explain_decision, find_requirements_for,
trace_design_rationale.

Same pattern as tests/mcp/test_tools.py: seeds a fixture graph against the
live Neo4j instance, asserts repo_id scoping holds, cleans up after.
"""

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import (
    explain_decision,
    find_requirements_for,
    trace_design_rationale,
)


@pytest.fixture
def engine():
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def seeded_graph(engine):
    """Seed a fixture graph with docs nodes across two repos."""
    engine.upsert_repository("test_repo_a", "Test Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", "test_repo_a", "AuthService")
    engine.upsert_node(
        "Requirement",
        "test_repo_a",
        "req-auth-001",
        {"title": "Must authenticate", "body": "..."},
    )
    engine.upsert_node(
        "ArchitectureNote",
        "test_repo_a",
        "note-perf",
        {"title": "Perf analysis", "body": "..."},
    )
    engine.upsert_node(
        "DesignDecision",
        "test_repo_a",
        "dd-001",
        {"title": "Old sync approach", "body": "..."},
    )
    engine.upsert_node(
        "DesignDecision",
        "test_repo_a",
        "dd-002",
        {"title": "Switch to async", "body": "..."},
    )

    engine.upsert_relationship(
        "Service", "AuthService", "SATISFIES", "Requirement", "req-auth-001", "test_repo_a"
    )
    engine.upsert_relationship(
        "Service", "AuthService", "DOCUMENTED_BY", "DesignDecision", "dd-002", "test_repo_a"
    )
    engine.upsert_relationship(
        "DesignDecision", "dd-002", "SUPERSEDES", "DesignDecision", "dd-001", "test_repo_a"
    )
    engine.upsert_relationship(
        "DesignDecision", "dd-002", "DECIDED_BY", "ArchitectureNote", "note-perf", "test_repo_a"
    )

    # Repo B: isolation check
    engine.upsert_repository("test_repo_b", "Test Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", "test_repo_b", "AuthService")
    engine.upsert_node(
        "Requirement", "test_repo_b", "req-auth-001", {"title": "Repo B's own requirement"}
    )
    engine.upsert_relationship(
        "Service", "AuthService", "SATISFIES", "Requirement", "req-auth-001", "test_repo_b"
    )

    yield engine

    engine.delete_repository("test_repo_a")
    engine.delete_repository("test_repo_b")


class TestExplainDecision:
    def test_explain_decision_basic(self, seeded_graph):
        result = explain_decision(seeded_graph, "test_repo_a", "dd-002")
        assert result["name"] == "dd-002"
        assert result["title"] == "Switch to async"
        assert "AuthService" in result["documents"]
        assert "dd-001" in result["supersedes"]
        assert "note-perf" in result["backed_by"]

    def test_explain_decision_not_found(self, seeded_graph):
        result = explain_decision(seeded_graph, "test_repo_a", "nonexistent")
        assert result["documents"] == []


class TestFindRequirementsFor:
    def test_find_requirements_for_component(self, seeded_graph):
        result = find_requirements_for(seeded_graph, "test_repo_a", "AuthService")
        names = {r["name"] for r in result}
        assert "req-auth-001" in names

    def test_no_cross_repo_leakage_default(self, seeded_graph):
        result = find_requirements_for(seeded_graph, "test_repo_a", "AuthService")
        assert len(result) == 1  # not both repo_a's and repo_b's req-auth-001

    def test_cross_repo_opt_in(self, seeded_graph):
        result = find_requirements_for(
            seeded_graph, "test_repo_a", "AuthService", cross_repo=True
        )
        assert len(result) == 2


class TestTraceDesignRationale:
    def test_trace_rationale_basic(self, seeded_graph):
        result = trace_design_rationale(seeded_graph, "test_repo_a", "AuthService")
        assert result["component"] == "AuthService"
        req_names = {r["name"] for r in result["requirements"]}
        note_names = {n["name"] for n in result["notes"]}
        assert "req-auth-001" in req_names
        assert "dd-002" in note_names

    def test_trace_rationale_no_leakage(self, seeded_graph):
        result = trace_design_rationale(seeded_graph, "test_repo_b", "AuthService")
        req_names = {r["name"] for r in result["requirements"]}
        assert req_names == {"req-auth-001"}
        assert result["notes"] == [] or all(n["name"] is None for n in result["notes"])
