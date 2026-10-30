"""Commit dates are stored in UTC, so "most recent" holds across authors'
timezones (audit 10 item 1), and history stored with authors' UTC offsets
is rewritten once on upgrade."""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.git_history.blame import compute_function_recency
from devgraph.indexer.git_history.extractor import GitHistoryExtractor, sync_git_history
from devgraph.indexer.git_history import open_repo
from devgraph.mcp import tools
from devgraph.registry.store import RepoRegistry

_TOKEN = uuid.uuid4().hex[:8]
NZ = "2026-10-27T09:00:00+13:00"  # 2026-10-26T20:00Z
US = "2026-10-26T18:00:00-07:00"  # 2026-10-27T01:00Z, five hours later


def _git(root: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Zed", "GIT_AUTHOR_EMAIL": "z@example.com",
           "GIT_COMMITTER_NAME": "Zed", "GIT_COMMITTER_EMAIL": "z@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=root, check=True, capture_output=True, env=env)


def _two_timezones(root: Path) -> Path:
    """`init`, then an NZ commit to alpha, then a US commit to beta five hours later."""
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", date="2026-10-20T12:00:00+00:00")
    (root / "a.py").write_text("def alpha():\n    return 2\n")
    _git(root, "commit", "-qam", "nz edits alpha", date=NZ)
    (root / "b.py").write_text("def beta():\n    return 2\n")
    _git(root, "commit", "-qam", "us edits beta (newest)", date=US)
    return root


def test_commit_dates_are_stored_in_utc(tmp_path):
    root = _two_timezones(tmp_path / "repo")
    dates = [c.properties["authored_date"] for c in GitHistoryExtractor("r", root).extract_new_commits().commits]
    assert dates == ["2026-10-20T12:00:00+00:00", "2026-10-26T20:00:00+00:00", "2026-10-27T01:00:00+00:00"]
    assert dates == sorted(dates)


def test_blame_recency_is_in_utc(tmp_path):
    root = _two_timezones(tmp_path / "repo")
    with open_repo(root) as repo:
        hunks = compute_function_recency(repo, "a.py")
    assert {h.last_modified_at for h in hunks} == {"2026-10-20T12:00:00+00:00", "2026-10-26T20:00:00+00:00"}


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
def synced(engine, tmp_path):
    """The two-timezone repository scanned and its history synced."""
    root = _two_timezones(tmp_path / "repo")
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    repo_id = registry.add_repo(root, repo_id=f"zz-utc-{_TOKEN}").repo_id
    engine.delete_repository(repo_id)
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    sync_git_history(engine, registry, repo_id)
    try:
        yield engine, registry, repo_id
    finally:
        engine.delete_repository(repo_id)
        registry.close()


def test_the_most_recent_change_is_the_latest_instant(synced):
    engine, _, repo_id = synced
    recent = tools.list_recent_changes(engine, repo_id, 1, entity_type="Function")["results"]
    assert [r["name"] for r in recent] == ["beta"]
    blamed = tools.blame_component(engine, repo_id, "b.py")["results"]
    assert [r["message"] for r in blamed] == ["us edits beta (newest)", "init"]
    assert tools.search_component(engine, repo_id, "alpha", modified_within_commits=1)["count"] == 0
    assert tools.search_component(engine, repo_id, "beta", modified_within_commits=1)["count"] == 1


def test_module_recency_spans_the_earliest_and_latest_instants(synced):
    engine, _, repo_id = synced
    [row] = engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'a.py'}) RETURN m.created_at AS c, m.last_modified_at AS l",
        {"r": repo_id},
    )
    assert (row["c"], row["l"]) == ("2026-10-20T12:00:00+00:00", "2026-10-26T20:00:00+00:00")


def test_history_stored_with_offsets_is_rewritten_in_utc(synced):
    """A graph synced before dates were UTC (no history stamp, offset dates)
    is migrated by the next sync, though HEAD has not moved."""
    engine, registry, repo_id = synced
    expected = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n:Commit OR n.last_modified_at IS NOT NULL "
        "RETURN labels(n) AS l, n.name AS n, n.authored_date AS d, n.created_at AS c, n.last_modified_at AS m "
        "ORDER BY l, n", {"r": repo_id},
    )
    # As an older DevGraph left it: the authors' offsets, no stamp.
    engine.run_cypher(
        "MATCH (c:Commit {repo_id: $r}) WHERE c.authored_date STARTS WITH '2026-10-26T20' SET c.authored_date = $nz",
        {"r": repo_id, "nz": NZ},
    )
    engine.run_cypher(
        "MATCH (c:Commit {repo_id: $r}) WHERE c.authored_date STARTS WITH '2026-10-27T01' SET c.authored_date = $us",
        {"r": repo_id, "us": US},
    )
    engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.last_modified_at STARTS WITH '2026-10-26T20' SET n.last_modified_at = $nz",
        {"r": repo_id, "nz": NZ},
    )
    engine.run_cypher("MATCH (r:Repository {repo_id: $r}) REMOVE r.history_format", {"r": repo_id})

    assert sync_git_history(engine, registry, repo_id)["mode"] == "upgraded"
    actual = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n:Commit OR n.last_modified_at IS NOT NULL "
        "RETURN labels(n) AS l, n.name AS n, n.authored_date AS d, n.created_at AS c, n.last_modified_at AS m "
        "ORDER BY l, n", {"r": repo_id},
    )
    assert actual == expected
    assert sync_git_history(engine, registry, repo_id)["mode"] == "noop"
