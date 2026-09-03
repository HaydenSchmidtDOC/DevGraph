"""Smoke tests for git-history recency filtering (Implementation Plan Item 6):
modified_within_commits on find_callers/search_component, and the new
list_recent_changes tool.

Same pattern as tests/mcp/test_tools_phase3.py: seeds Commit nodes with
authored_date strings and entities with a last_modified_at property directly
via GraphEngine (the actual staging is done elsewhere, by git-history
indexing — these tools only ever read the property, never write it).
"""

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import find_callers, search_component, list_recent_changes


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

    # Three commits, oldest to newest.
    engine.upsert_node("Commit", "test_repo_a", "sha1", {"authored_date": "2026-01-01T00:00:00"})
    engine.upsert_node("Commit", "test_repo_a", "sha2", {"authored_date": "2026-01-02T00:00:00"})
    engine.upsert_node("Commit", "test_repo_a", "sha3", {"authored_date": "2026-01-03T00:00:00"})

    # target_recent was touched by the most recent commit; target_old predates
    # the window; target_untouched has never been staged (no property at all).
    engine.upsert_node("Function", "test_repo_a", "target_recent", {"last_modified_at": "2026-01-03T00:00:00"})
    engine.upsert_node("Function", "test_repo_a", "target_old", {"last_modified_at": "2026-01-01T00:00:00"})
    engine.upsert_node("Function", "test_repo_a", "target_untouched")
    engine.upsert_node("Function", "test_repo_a", "caller")

    engine.upsert_relationship("Function", "caller", "CALLS", "Function", "target_recent", "test_repo_a")
    engine.upsert_relationship("Function", "caller", "CALLS", "Function", "target_old", "test_repo_a")
    engine.upsert_relationship("Function", "caller", "CALLS", "Function", "target_untouched", "test_repo_a")

    engine.upsert_node("Service", "test_repo_a", "RecentService", {"last_modified_at": "2026-01-03T00:00:00"})
    engine.upsert_node("Service", "test_repo_a", "OldService", {"last_modified_at": "2026-01-01T00:00:00"})

    yield engine

    engine.delete_repository("test_repo_a")


class TestFindCallersRecency:
    def test_modified_within_commits_excludes_stale_and_untouched_targets(self, seeded_graph):
        """With 1 commit as the window, only target_recent (touched by the
        newest commit) qualifies — target_old is too stale, target_untouched
        has never been staged at all."""
        result = find_callers(seeded_graph, "test_repo_a", "target_recent", modified_within_commits=1)
        names = {r["name"] for r in result["results"]}
        assert "caller" in names

        result_old = find_callers(seeded_graph, "test_repo_a", "target_old", modified_within_commits=1)
        assert result_old["results"] == []

        result_untouched = find_callers(seeded_graph, "test_repo_a", "target_untouched", modified_within_commits=1)
        assert result_untouched["results"] == []

    def test_modified_within_commits_no_cutoff_does_not_filter(self, seeded_graph):
        """Requesting more commits than exist repo-wide means no cutoff can be
        resolved, so behavior falls back to unfiltered (matches pre-existing
        behavior when the param is omitted)."""
        unfiltered = find_callers(seeded_graph, "test_repo_a", "target_old")
        with_window = find_callers(seeded_graph, "test_repo_a", "target_old", modified_within_commits=100)
        assert with_window["results"] == unfiltered["results"]

    def test_modified_within_commits_omitted_is_unchanged(self, seeded_graph):
        """Omitting the param entirely (default None) leaves find_callers exactly
        as it behaved before this feature existed."""
        result = find_callers(seeded_graph, "test_repo_a", "target_old")
        names = {r["name"] for r in result["results"]}
        assert "caller" in names


class TestSearchComponentRecency:
    def test_modified_within_commits_filters_by_recency(self, seeded_graph):
        result = search_component(
            seeded_graph, "test_repo_a", "Service", modified_within_commits=1
        )
        names = {r["name"] for r in result["results"]}
        assert "RecentService" in names
        assert "OldService" not in names

    def test_modified_within_commits_no_cutoff_does_not_filter(self, seeded_graph):
        unfiltered = search_component(seeded_graph, "test_repo_a", "Service")
        with_window = search_component(seeded_graph, "test_repo_a", "Service", modified_within_commits=100)
        assert {r["name"] for r in with_window["results"]} == {r["name"] for r in unfiltered["results"]}


class TestListRecentChanges:
    def test_lists_entities_within_window_ordered_most_recent_first(self, seeded_graph):
        result = list_recent_changes(seeded_graph, "test_repo_a", within_commits=1)
        names = [r["name"] for r in result["results"]]
        assert "target_recent" in names
        assert "RecentService" in names
        assert "target_old" not in names
        assert "OldService" not in names
        assert "target_untouched" not in names

    def test_entity_type_filter(self, seeded_graph):
        result = list_recent_changes(
            seeded_graph, "test_repo_a", within_commits=1, entity_type="Service"
        )
        names = {r["name"] for r in result["results"]}
        assert names == {"RecentService"}

    def test_invalid_entity_type_returns_empty(self, seeded_graph):
        result = list_recent_changes(
            seeded_graph, "test_repo_a", within_commits=1, entity_type="NotALabel"
        )
        assert result["count"] == 0
        assert result["results"] == []
        assert result["truncated"] is False

    def test_no_cutoff_returns_every_staged_entity(self, seeded_graph):
        """Requesting a window larger than the repo's whole commit history
        means every ever-staged entity qualifies (nothing to exclude), rather
        than returning nothing."""
        result = list_recent_changes(seeded_graph, "test_repo_a", within_commits=100)
        names = {r["name"] for r in result["results"]}
        assert {"target_recent", "target_old", "RecentService", "OldService"} <= names
        assert "target_untouched" not in names

    def test_no_cross_repo_leakage_default(self, seeded_graph):
        result = list_recent_changes(seeded_graph, "test_repo_a", within_commits=1)
        for item in result["results"]:
            assert item["repo_id"] == "test_repo_a"

    def test_envelope_structure(self, seeded_graph):
        result = list_recent_changes(seeded_graph, "test_repo_a", within_commits=1)
        assert "count" in result
        assert "results" in result
        assert "truncated" in result
