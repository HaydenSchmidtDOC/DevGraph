"""Smoke tests for Phase 3 MCP tools: blame_component, find_related_prs,
issue_history_for.
"""

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import blame_component, find_related_prs, issue_history_for


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
    engine.upsert_repository("test_repo_a", "Test Repo A", "/path/to/repo_a")
    engine.upsert_node("Module", "test_repo_a", "auth.py")
    engine.upsert_node(
        "Commit",
        "test_repo_a",
        "sha1",
        {"message": "Add auth", "author": "Alice", "authored_date": "2026-01-01T00:00:00"},
    )
    engine.upsert_node(
        "Commit",
        "test_repo_a",
        "sha2",
        {"message": "Fix auth bug", "author": "Bob", "authored_date": "2026-01-02T00:00:00"},
    )
    engine.upsert_node("Issue", "test_repo_a", "42", {"title": "Auth bug", "state": "closed", "url": "http://x/42"})
    engine.upsert_node(
        "PullRequest", "test_repo_a", "7", {"title": "Fix auth bug", "state": "closed", "url": "http://x/pr7"}
    )

    engine.upsert_relationship("Commit", "sha1", "MODIFIES", "Module", "auth.py", "test_repo_a")
    engine.upsert_relationship("Commit", "sha2", "MODIFIES", "Module", "auth.py", "test_repo_a")
    engine.upsert_relationship("Commit", "sha2", "REFERENCES", "Issue", "42", "test_repo_a")
    engine.upsert_relationship("PullRequest", "7", "RESOLVES", "Issue", "42", "test_repo_a")

    # Repo B: isolation check
    engine.upsert_repository("test_repo_b", "Test Repo B", "/path/to/repo_b")
    engine.upsert_node("Module", "test_repo_b", "auth.py")
    engine.upsert_node("Commit", "test_repo_b", "sha_b", {"message": "Repo B commit", "author": "Carl", "authored_date": "2026-01-03T00:00:00"})
    engine.upsert_relationship("Commit", "sha_b", "MODIFIES", "Module", "auth.py", "test_repo_b")

    yield engine

    engine.delete_repository("test_repo_a")
    engine.delete_repository("test_repo_b")


class TestBlameComponent:
    def test_blame_returns_commits_most_recent_first(self, seeded_graph):
        result = blame_component(seeded_graph, "test_repo_a", "auth.py")
        shas = [r["sha"] for r in result]
        assert shas == ["sha2", "sha1"]

    def test_blame_no_cross_repo_leakage(self, seeded_graph):
        result = blame_component(seeded_graph, "test_repo_a", "auth.py")
        assert all(r["sha"] != "sha_b" for r in result)

    def test_blame_cross_repo_opt_in(self, seeded_graph):
        result = blame_component(seeded_graph, "test_repo_a", "auth.py", cross_repo=True)
        shas = {r["sha"] for r in result}
        assert "sha_b" in shas


class TestFindRelatedPrs:
    def test_find_related_prs_basic(self, seeded_graph):
        result = find_related_prs(seeded_graph, "test_repo_a", "auth.py")
        numbers = {r["number"] for r in result}
        assert "7" in numbers


class TestIssueHistoryFor:
    def test_issue_history_basic(self, seeded_graph):
        result = issue_history_for(seeded_graph, "test_repo_a", "auth.py")
        numbers = {r["number"] for r in result}
        assert "42" in numbers

    def test_issue_history_no_leakage(self, seeded_graph):
        result = issue_history_for(seeded_graph, "test_repo_b", "auth.py")
        assert result == []
