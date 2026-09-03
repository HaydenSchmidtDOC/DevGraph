"""Smoke tests for Phase 2 MCP tools: explain_decision, find_requirements_for,
trace_design_rationale, find_mentions.

Same pattern as tests/mcp/test_tools.py: seeds a fixture graph against the
live Neo4j instance, asserts repo_id scoping holds, cleans up after.
"""

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import (
    explain_decision,
    find_requirements_for,
    trace_design_rationale,
    find_mentions,
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
    """Seed a fixture graph with docs nodes and mentions across two repos."""
    engine.upsert_repository("test_repo_a", "Test Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", "test_repo_a", "AuthService")
    engine.upsert_node("Function", "test_repo_a", "authenticate")
    engine.upsert_node("Class", "test_repo_a", "User")
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
    engine.upsert_node(
        "Document",
        "test_repo_a",
        "docs/api_guide.md",
        {"title": "API Guide", "source_file": "docs/api_guide.md"},
    )
    engine.upsert_node(
        "Document",
        "test_repo_a",
        "README.md",
        {"title": "ReadMe", "source_file": "README.md"},
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
    # Add MENTIONS relationships
    engine.upsert_relationship(
        "Document", "docs/api_guide.md", "MENTIONS", "Function", "authenticate", "test_repo_a"
    )
    engine.upsert_relationship(
        "Document", "docs/api_guide.md", "MENTIONS", "Class", "User", "test_repo_a"
    )
    engine.upsert_relationship(
        "Document", "README.md", "MENTIONS", "Service", "AuthService", "test_repo_a"
    )

    # Repo B: isolation check
    engine.upsert_repository("test_repo_b", "Test Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", "test_repo_b", "AuthService")
    engine.upsert_node("Function", "test_repo_b", "authenticate")
    engine.upsert_node(
        "Requirement", "test_repo_b", "req-auth-001", {"title": "Repo B's own requirement"}
    )
    engine.upsert_node(
        "Document",
        "test_repo_b",
        "GUIDE.md",
        {"title": "Guide", "source_file": "GUIDE.md"},
    )
    engine.upsert_relationship(
        "Service", "AuthService", "SATISFIES", "Requirement", "req-auth-001", "test_repo_b"
    )
    engine.upsert_relationship(
        "Document", "GUIDE.md", "MENTIONS", "Function", "authenticate", "test_repo_b"
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


class TestFindMentions:
    def test_find_mentions_mentioned_by_default(self, seeded_graph):
        """direction='mentioned_by' (default) finds Documents that mention an entity."""
        result = find_mentions(seeded_graph, "test_repo_a", "authenticate")
        assert result["count"] == 1
        assert result["truncated"] is False
        names = {r["name"] for r in result["results"]}
        assert "docs/api_guide.md" in names

    def test_find_mentions_direction_mentioned_by(self, seeded_graph):
        """Explicit direction='mentioned_by' finds Documents mentioning an entity."""
        result = find_mentions(
            seeded_graph, "test_repo_a", "User", direction="mentioned_by"
        )
        assert result["count"] == 1
        names = {r["name"] for r in result["results"]}
        assert "docs/api_guide.md" in names

    def test_find_mentions_direction_mentions(self, seeded_graph):
        """direction='mentions' finds what a Document mentions."""
        result = find_mentions(
            seeded_graph, "test_repo_a", "docs/api_guide.md", direction="mentions"
        )
        assert result["count"] == 2
        assert result["truncated"] is False
        names = {r["name"] for r in result["results"]}
        assert "authenticate" in names
        assert "User" in names

    def test_find_mentions_with_label_filter(self, seeded_graph):
        """Label filter narrows results to specific node types."""
        result = find_mentions(
            seeded_graph, "test_repo_a", "docs/api_guide.md",
            direction="mentions", label="Function"
        )
        assert result["count"] == 1
        assert result["results"][0]["name"] == "authenticate"

    def test_find_mentions_envelope_structure(self, seeded_graph):
        """Result envelope includes count, results, and truncated."""
        result = find_mentions(seeded_graph, "test_repo_a", "authenticate")
        assert "count" in result
        assert "results" in result
        assert "truncated" in result
        assert isinstance(result["count"], int)
        assert isinstance(result["results"], list)
        assert isinstance(result["truncated"], bool)

    def test_find_mentions_result_fields(self, seeded_graph):
        """Each result has name, type (labels), and repo_id."""
        result = find_mentions(seeded_graph, "test_repo_a", "authenticate")
        assert result["count"] > 0
        for item in result["results"]:
            assert "name" in item
            assert "type" in item
            assert "repo_id" in item
            assert isinstance(item["type"], list)

    def test_find_mentions_no_cross_repo_leakage_default(self, seeded_graph):
        """Default behavior scopes to repo_id, preventing cross-repo leakage."""
        result = find_mentions(seeded_graph, "test_repo_a", "authenticate")
        # Should only find mentions from test_repo_a (2 matches in api_guide.md + 0 in README)
        for item in result["results"]:
            assert item["repo_id"] == "test_repo_a"

    def test_find_mentions_cross_repo_opt_in(self, seeded_graph):
        """cross_repo=True allows finding mentions across repositories."""
        result = find_mentions(
            seeded_graph, "test_repo_a", "authenticate", cross_repo=True
        )
        # Now should find authenticate mentioned in both repos
        assert result["count"] >= 2
        repos = {item["repo_id"] for item in result["results"]}
        assert "test_repo_a" in repos
        assert "test_repo_b" in repos

    def test_find_mentions_not_found(self, seeded_graph):
        """Non-existent entity returns empty results with truncated=False."""
        result = find_mentions(seeded_graph, "test_repo_a", "nonexistent_function")
        assert result["count"] == 0
        assert result["results"] == []
        assert result["truncated"] is False

    def test_find_mentions_invalid_label_returns_empty(self, seeded_graph):
        """Invalid label filter returns empty results."""
        result = find_mentions(
            seeded_graph, "test_repo_a", "authenticate", label="InvalidLabel"
        )
        assert result["count"] == 0
        assert result["truncated"] is False

    def test_find_mentions_max_results_respected(self, seeded_graph):
        """max_results parameter limits the envelope output (not the count)."""
        # Create multiple mentions by querying on a Document that mentions multiple entities
        result = find_mentions(
            seeded_graph, "test_repo_a", "docs/api_guide.md",
            direction="mentions", max_results=1
        )
        assert result["count"] == 2  # Still 2 total matches
        assert len(result["results"]) == 1  # But only 1 in results
        assert result["truncated"] is True
