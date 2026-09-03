"""Integration tests for index_repo_history with live Neo4j and a real RepoRegistry."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.git_history.extractor import index_repo_history, sync_git_history
from devgraph.registry.store import RepoRegistry


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, check=True)


@pytest.fixture
def graph_engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    yield engine
    engine.close()


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")
        (repo_path / "service.py").write_text("x = 1\n")
        _run_git(repo_path, "add", "service.py")
        _run_git(repo_path, "commit", "-m", "Initial commit")
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


def test_index_repo_history_end_to_end(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_git_history"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        count = index_repo_history(graph_engine, registry, record.repo_id)
        assert count == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN c.message as message",
            {"repo_id": record.repo_id},
        )
        assert len(result) == 1
        assert result[0]["message"] == "Initial commit"

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1

        # last_indexed_commit was persisted
        updated = registry.get(record.repo_id)
        assert updated.last_indexed_commit is not None

        # Re-running is a no-op (incremental, nothing new)
        count_again = index_repo_history(graph_engine, registry, record.repo_id)
        assert count_again == 0
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_index_repo_history_unknown_repo_raises(graph_engine, registry):
    with pytest.raises(ValueError):
        index_repo_history(graph_engine, registry, "nonexistent")


def test_modifies_edge_resolves_for_nested_file(graph_engine, registry):
    """MODIFIES targets must be the full repo-relative path (matching how
    Module nodes are keyed since the multi-level relative-import fix), not
    bare filename — otherwise a commit touching a nested file's MODIFIES
    edge silently never resolves (blame_component would come back empty for
    every file except ones at the repo root).
    """
    from devgraph.indexer.python.extractor import index_file

    repo_id = "_smoketest_git_history_nested"
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        (repo_path / "services" / "api").mkdir(parents=True)
        nested_file = repo_path / "services" / "api" / "main.py"
        nested_file.write_text("x = 1\n")
        _run_git(repo_path, "add", "services/api/main.py")
        _run_git(repo_path, "commit", "-m", "Add nested main.py")

        record = registry.add_repo(repo_path, repo_id=repo_id)

        try:
            index_file(graph_engine, record.repo_id, nested_file, repo_root=repo_path)
            index_repo_history(graph_engine, registry, record.repo_id)

            result = graph_engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->"
                "(m:Module {name: 'services/api/main.py'}) RETURN COUNT(*) as count",
                {"repo_id": record.repo_id},
            )
            assert result[0]["count"] == 1
        finally:
            graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_initial_walk_stages_module_recency(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_sync_initial"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "initial"
        assert outcome["commits_indexed"] == 1

        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id, name: 'service.py'}) "
            "RETURN m.created_at AS created_at, m.last_modified_at AS last_modified_at",
            {"repo_id": record.repo_id},
        )
        assert result[0]["created_at"] is not None
        assert result[0]["created_at"] == result[0]["last_modified_at"]

        # HEAD unchanged -> no-op
        outcome_again = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome_again["mode"] == "noop"
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_fast_path_advances_recency(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_sync_fast"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        sync_git_history(graph_engine, registry, record.repo_id)

        (temp_git_repo / "service.py").write_text("x = 2\n")
        _run_git(temp_git_repo, "add", "service.py")
        _run_git(temp_git_repo, "commit", "-m", "Update service")

        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "fast"
        assert outcome["commits_indexed"] == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 2
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_reconcile_deletes_orphans_and_resets_recency(
    graph_engine, temp_git_repo, registry
):
    repo_id = "_smoketest_sync_reconcile"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        sync_git_history(graph_engine, registry, record.repo_id)

        first_head = registry.get(record.repo_id).last_indexed_commit

        # Simulate a rebase/reset: amend the last commit so the previously
        # indexed SHA is no longer reachable from HEAD.
        (temp_git_repo / "service.py").write_text("x = 3\n")
        _run_git(temp_git_repo, "add", "service.py")
        _run_git(temp_git_repo, "commit", "--amend", "-m", "Rewritten initial commit")

        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "reconcile"
        assert outcome["commits_deleted"] == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id, name: $sha}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id, "sha": first_head},
        )
        assert result[0]["count"] == 0

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_unknown_repo_raises(graph_engine, registry):
    with pytest.raises(ValueError):
        sync_git_history(graph_engine, registry, "nonexistent")
