"""A missing, unreadable or apparently unmounted repository root never wipes
the repository's graph: every scan entry point refuses and changes nothing.

Live tests against Neo4j: they scan a small repository, take its root away,
and assert the node count is unchanged.
"""

import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from devgraph.agent.sync import RepoSync
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import catch_up, full_scan, prune_stale_files, remove_paths
from devgraph.indexer.git_history.extractor import sync_git_history
from devgraph.indexer.walk import RepoRootEmpty, RepoRootUnavailable, check_repo_root, repo_root_problem
from devgraph.registry.store import RepoRecord

REPO = f"zz-root-guard-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("class Alpha:\n    pass\n")
    (root / "pkg" / "b.py").write_text("from pkg.a import Alpha\n\nclass Beta(Alpha):\n    pass\n")
    (root / "README.md").write_text("# Widget\n")
    return root


def count(engine) -> int:
    return engine.run_cypher("MATCH (n {repo_id: $r}) RETURN count(n) AS c", {"r": REPO})[0]["c"]


@pytest.fixture
def scanned(engine, repo):
    provision_repository_schema(engine, repo)
    engine.upsert_repository(REPO, REPO, str(repo))
    full_scan(engine, REPO, repo)
    before = count(engine)
    assert before > 3
    return before


def _missing(root):
    shutil.rmtree(root)


def _empty(root):
    """What a mount point with nothing mounted looks like: an empty folder."""
    shutil.rmtree(root)
    root.mkdir()


def _only_ignored(root):
    shutil.rmtree(root)
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")


@pytest.fixture
def unreadable(repo):
    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("permission bits don't stop this user reading the folder")
    yield lambda root: root.chmod(0)
    repo.chmod(0o755)


ENTRY_POINTS = {
    "full_scan": lambda engine, root, **kw: full_scan(engine, REPO, root, **kw),
    "catch_up": lambda engine, root, **kw: catch_up(
        engine, REPO, root, datetime.now(timezone.utc) - timedelta(minutes=5), **kw
    ),
    "prune_stale_files": lambda engine, root, **kw: prune_stale_files(engine, REPO, root, **kw),
}


def test_repo_root_problem_names_what_is_wrong(tmp_path):
    assert repo_root_problem(tmp_path) is None
    assert repo_root_problem(tmp_path / "gone") == "not found"
    (tmp_path / "file.txt").write_text("x")
    assert repo_root_problem(tmp_path / "file.txt") == "not a folder"
    with pytest.raises(RepoRootUnavailable, match=r"repository folder not found: .*gone; nothing was changed"):
        check_repo_root(tmp_path / "gone")


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_a_missing_root_changes_nothing(engine, repo, scanned, entry):
    _missing(repo)
    with pytest.raises(RepoRootUnavailable, match="not found"):
        ENTRY_POINTS[entry](engine, repo)
    assert count(engine) == scanned


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_an_unreadable_root_changes_nothing(engine, repo, scanned, unreadable, entry):
    unreadable(repo)
    with pytest.raises(RepoRootUnavailable, match="not readable"):
        ENTRY_POINTS[entry](engine, repo)
    assert count(engine) == scanned


@pytest.mark.parametrize("emptied", [_empty, _only_ignored], ids=["empty", "only-ignored"])
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_an_empty_root_changes_nothing_without_force(engine, repo, scanned, entry, emptied):
    emptied(repo)
    with pytest.raises(RepoRootEmpty, match="--force"):
        ENTRY_POINTS[entry](engine, repo)
    assert count(engine) == scanned


@pytest.mark.parametrize("entry", ["full_scan", "prune_stale_files"])
def test_force_prunes_an_empty_root(engine, repo, scanned, entry):
    _empty(repo)
    ENTRY_POINTS[entry](engine, repo, force=True)
    assert count(engine) < scanned


def test_an_outdated_index_upgrade_refuses_too(engine, repo, scanned):
    """catch_up of an index older than INDEX_FORMAT runs a full_scan instead."""
    engine.set_index_format(REPO, dispatch.INDEX_FORMAT - 1)
    _missing(repo)
    with pytest.raises(RepoRootUnavailable):
        ENTRY_POINTS["catch_up"](engine, repo)
    assert count(engine) == scanned


def test_watcher_deletes_under_a_missing_root_change_nothing(engine, repo, scanned):
    """The watcher's deleted-paths batch (`remove_paths`), as an unmount can
    report every file gone."""
    paths = {repo / "pkg", repo / "pkg" / "a.py", repo / "README.md"}
    _missing(repo)
    with pytest.raises(RepoRootUnavailable):
        remove_paths(engine, REPO, repo, paths)
    assert count(engine) == scanned


def test_git_history_sync_refuses_a_missing_root(tmp_path):
    registry = MagicMock()
    registry.get.return_value = RepoRecord(REPO, tmp_path / "gone", True, True, None)
    with pytest.raises(RepoRootUnavailable, match="not found"):
        sync_git_history(MagicMock(), registry, REPO)
    registry.set_last_indexed_commit.assert_not_called()


class _Agent:
    """The agent's RepoSync wired to the live engine, a mock registry and recorders."""

    def __init__(self, engine, root):
        self.registry = MagicMock()
        self.registry.get.return_value = RepoRecord(REPO, root, True, True, None)
        self.events: list[dict] = []
        self.requests: list[tuple] = []
        self.sync = RepoSync(engine, self.registry, self.events.append, lambda *a: self.requests.append(a))


@pytest.mark.parametrize("emptied", [_missing, _empty], ids=["missing", "empty"])
def test_the_agent_warns_once_and_skips_the_repo(engine, repo, scanned, caplog, emptied):
    agent = _Agent(engine, repo)
    emptied(repo)
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    with caplog.at_level(logging.WARNING, logger="devgraph.agent.sync"):
        assert agent.sync.on_catch_up(REPO, since) is False
        assert agent.sync.on_catch_up(REPO, since) is False
        if emptied is _missing:  # the live batch an unmount can report
            agent.sync.on_changes(REPO, set(), {repo / "pkg" / "a.py"})
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert str(repo) in warnings[0].getMessage() and warnings[0].exc_info is None
    assert agent.requests == []  # skipped, not retried every 30 s
    agent.registry.mark_indexed.assert_not_called()
    assert count(engine) == scanned


@pytest.fixture
def unreadable_subfolder(repo):
    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("permission bits don't stop this user reading the folder")
    yield repo / "pkg"
    (repo / "pkg").chmod(0o755)


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_files_under_an_unreadable_subfolder_are_kept(engine, repo, scanned, unreadable_subfolder, entry, caplog):
    unreadable_subfolder.chmod(0)
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
        ENTRY_POINTS[entry](engine, repo)
    assert count(engine) == scanned
    assert any("pkg" in r.getMessage() and "not readable" in r.getMessage() for r in caplog.records)


def test_walk_reports_unreadable_folders(repo, unreadable_subfolder):
    from devgraph.indexer.walk import keyed_indexable_paths

    unreadable_subfolder.chmod(0)
    unreadable: list[str] = []
    keyed = keyed_indexable_paths(repo, unreadable=unreadable)
    assert [rel for _, rel in keyed] == ["README.md"]
    assert unreadable == ["pkg/"]
