"""Smoke tests for Phase 2 MCP tools: explain_decision, find_requirements_for,
trace_design_rationale, find_mentions.

Same pattern as tests/mcp/test_tools.py: seeds a fixture graph against the
live Neo4j instance, asserts repo_id scoping holds, cleans up after.
"""

import uuid

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import (
    explain_decision,
    find_requirements_for,
    trace_design_rationale,
    find_mentions,
)

# Unique per run, so sessions sharing the Neo4j never seed or delete each
# other's repositories; tests/conftest.py sweeps any a teardown leaves.
_TOKEN = uuid.uuid4().hex[:8]
REPO_A, REPO_B = f"test_repo_a_{_TOKEN}", f"test_repo_b_{_TOKEN}"


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
    engine.upsert_repository(REPO_A, "Test Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", REPO_A, "AuthService")
    engine.upsert_node("Function", REPO_A, "authenticate")
    engine.upsert_node("Class", REPO_A, "User")
    engine.upsert_node(
        "Requirement",
        REPO_A,
        "req-auth-001",
        {"title": "Must authenticate", "body": "..."},
    )
    engine.upsert_node(
        "ArchitectureNote",
        REPO_A,
        "note-perf",
        {"title": "Perf analysis", "body": "..."},
    )
    engine.upsert_node(
        "DesignDecision",
        REPO_A,
        "dd-001",
        {"title": "Old sync approach", "body": "..."},
    )
    engine.upsert_node(
        "DesignDecision",
        REPO_A,
        "dd-002",
        {"title": "Switch to async", "body": "..."},
    )
    engine.upsert_node(
        "Document",
        REPO_A,
        "docs/api_guide.md",
        {"title": "API Guide", "source_file": "docs/api_guide.md"},
    )
    engine.upsert_node(
        "Document",
        REPO_A,
        "README.md",
        {"title": "ReadMe", "source_file": "README.md"},
    )

    engine.upsert_relationship(
        "Service", "AuthService", "SATISFIES", "Requirement", "req-auth-001", REPO_A
    )
    engine.upsert_relationship(
        "Service", "AuthService", "DOCUMENTED_BY", "DesignDecision", "dd-002", REPO_A
    )
    engine.upsert_relationship(
        "DesignDecision", "dd-002", "SUPERSEDES", "DesignDecision", "dd-001", REPO_A
    )
    engine.upsert_relationship(
        "DesignDecision", "dd-002", "DECIDED_BY", "ArchitectureNote", "note-perf", REPO_A
    )
    # Add MENTIONS relationships
    engine.upsert_relationship(
        "Document", "docs/api_guide.md", "MENTIONS", "Function", "authenticate", REPO_A
    )
    engine.upsert_relationship(
        "Document", "docs/api_guide.md", "MENTIONS", "Class", "User", REPO_A
    )
    engine.upsert_relationship(
        "Document", "README.md", "MENTIONS", "Service", "AuthService", REPO_A
    )

    # Repo B: isolation check
    engine.upsert_repository(REPO_B, "Test Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", REPO_B, "AuthService")
    engine.upsert_node("Function", REPO_B, "authenticate")
    engine.upsert_node(
        "Requirement", REPO_B, "req-auth-001", {"title": "Repo B's own requirement"}
    )
    engine.upsert_node(
        "Document",
        REPO_B,
        "GUIDE.md",
        {"title": "Guide", "source_file": "GUIDE.md"},
    )
    engine.upsert_relationship(
        "Service", "AuthService", "SATISFIES", "Requirement", "req-auth-001", REPO_B
    )
    engine.upsert_relationship(
        "Document", "GUIDE.md", "MENTIONS", "Function", "authenticate", REPO_B
    )

    yield engine

    engine.delete_repository(REPO_A)
    engine.delete_repository(REPO_B)


class TestExplainDecision:
    def test_explain_decision_basic(self, seeded_graph):
        result = explain_decision(seeded_graph, REPO_A, "dd-002")
        assert result["name"] == "dd-002"
        assert result["title"] == "Switch to async"
        assert "AuthService" in result["documents"]
        assert "dd-001" in result["supersedes"]
        assert "note-perf" in result["backed_by"]

    def test_explain_decision_not_found(self, seeded_graph):
        with pytest.raises(ToolError, match=f"no design decision named 'nonexistent' in repository '{REPO_A}'"):
            explain_decision(seeded_graph, REPO_A, "nonexistent")


class TestFindRequirementsFor:
    def test_find_requirements_for_component(self, seeded_graph):
        result = find_requirements_for(seeded_graph, REPO_A, "AuthService")
        names = {r["name"] for r in result["results"]}
        assert "req-auth-001" in names

    def test_no_cross_repo_leakage_default(self, seeded_graph):
        result = find_requirements_for(seeded_graph, REPO_A, "AuthService")
        assert result["count"] == 1  # not both repo_a's and repo_b's req-auth-001

    def test_cross_repo_opt_in(self, seeded_graph):
        result = find_requirements_for(
            seeded_graph, REPO_A, "AuthService", cross_repo=True
        )
        # By title, not count: another session's copy of these repos is reachable too.
        assert {r["title"] for r in result["results"]} == {"Must authenticate", "Repo B's own requirement"}


class TestTraceDesignRationale:
    def test_trace_rationale_basic(self, seeded_graph):
        result = trace_design_rationale(seeded_graph, REPO_A, "AuthService")
        assert result["component"] == "AuthService"
        req_names = {r["name"] for r in result["requirements"]}
        note_names = {n["name"] for n in result["notes"]}
        assert "req-auth-001" in req_names
        assert "dd-002" in note_names

    def test_trace_rationale_no_leakage(self, seeded_graph):
        result = trace_design_rationale(seeded_graph, REPO_B, "AuthService")
        req_names = {r["name"] for r in result["requirements"]}
        assert req_names == {"req-auth-001"}
        assert result["notes"] == [] or all(n["name"] is None for n in result["notes"])


class TestFindMentions:
    def test_find_mentions_mentioned_by_default(self, seeded_graph):
        """direction='mentioned_by' (default) finds Documents that mention an entity."""
        result = find_mentions(seeded_graph, REPO_A, "authenticate")
        assert result["count"] == 1
        assert result["truncated"] is False
        names = {r["name"] for r in result["results"]}
        assert "docs/api_guide.md" in names

    def test_find_mentions_direction_mentioned_by(self, seeded_graph):
        """Explicit direction='mentioned_by' finds Documents mentioning an entity."""
        result = find_mentions(
            seeded_graph, REPO_A, "User", direction="mentioned_by"
        )
        assert result["count"] == 1
        names = {r["name"] for r in result["results"]}
        assert "docs/api_guide.md" in names

    def test_find_mentions_direction_mentions(self, seeded_graph):
        """direction='mentions' finds what a Document mentions."""
        result = find_mentions(
            seeded_graph, REPO_A, "docs/api_guide.md", direction="mentions"
        )
        assert result["count"] == 2
        assert result["truncated"] is False
        names = {r["name"] for r in result["results"]}
        assert "authenticate" in names
        assert "User" in names

    def test_find_mentions_with_label_filter(self, seeded_graph):
        """Label filter narrows results to specific node types."""
        result = find_mentions(
            seeded_graph, REPO_A, "docs/api_guide.md",
            direction="mentions", label="Function"
        )
        assert result["count"] == 1
        assert result["results"][0]["name"] == "authenticate"

    def test_find_mentions_envelope_structure(self, seeded_graph):
        """Result envelope includes count, results, and truncated."""
        result = find_mentions(seeded_graph, REPO_A, "authenticate")
        assert "count" in result
        assert "results" in result
        assert "truncated" in result
        assert isinstance(result["count"], int)
        assert isinstance(result["results"], list)
        assert isinstance(result["truncated"], bool)

    def test_find_mentions_result_fields(self, seeded_graph):
        """Each result has name, type (labels), and repo_id."""
        result = find_mentions(seeded_graph, REPO_A, "authenticate")
        assert result["count"] > 0
        for item in result["results"]:
            assert "name" in item
            assert "type" in item
            assert "repo_id" in item
            assert isinstance(item["type"], list)

    def test_find_mentions_no_cross_repo_leakage_default(self, seeded_graph):
        """Default behavior scopes to repo_id, preventing cross-repo leakage."""
        result = find_mentions(seeded_graph, REPO_A, "authenticate")
        # Should only find mentions from test_repo_a (2 matches in api_guide.md + 0 in README)
        for item in result["results"]:
            assert item["repo_id"] == REPO_A

    def test_find_mentions_cross_repo_opt_in(self, seeded_graph):
        """cross_repo=True allows finding mentions across repositories."""
        result = find_mentions(
            seeded_graph, REPO_A, "authenticate", cross_repo=True
        )
        # Now should find authenticate mentioned in both repos
        assert result["count"] >= 2
        repos = {item["repo_id"] for item in result["results"]}
        assert REPO_A in repos
        assert REPO_B in repos

    def test_find_mentions_not_found(self, seeded_graph):
        """Non-existent entity returns empty results with truncated=False."""
        result = find_mentions(seeded_graph, REPO_A, "nonexistent_function")
        assert result["count"] == 0
        assert result["results"] == []
        assert result["truncated"] is False

    def test_find_mentions_invalid_label_returns_empty(self, seeded_graph):
        """Invalid label filter returns empty results."""
        result = find_mentions(
            seeded_graph, REPO_A, "authenticate", label="InvalidLabel"
        )
        assert result["count"] == 0
        assert result["truncated"] is False

    def test_find_mentions_max_results_respected(self, seeded_graph):
        """max_results parameter limits the envelope output (not the count)."""
        # Create multiple mentions by querying on a Document that mentions multiple entities
        result = find_mentions(
            seeded_graph, REPO_A, "docs/api_guide.md",
            direction="mentions", max_results=1
        )
        assert result["count"] == 2  # Still 2 total matches
        assert len(result["results"]) == 1  # But only 1 in results
        assert result["truncated"] is True
