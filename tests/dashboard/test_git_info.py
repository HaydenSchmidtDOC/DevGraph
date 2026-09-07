"""Tests for devgraph.dashboard.git_info -- real git log/status, no Neo4j
needed (unlike most of tests/dashboard, which hits the local test instance)."""

import tempfile
from pathlib import Path

import pytest
from git import Actor, Repo

from devgraph.dashboard.git_info import get_git_log, get_git_status

_AUTHOR = Actor("Test Author", "test@example.com")


@pytest.fixture
def repo_with_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        with Repo.init(path) as repo:
            (path / "a.txt").write_text("one")
            repo.index.add(["a.txt"])
            repo.index.commit("First commit", author=_AUTHOR, committer=_AUTHOR)

            (path / "a.txt").write_text("two")
            repo.index.add(["a.txt"])
            repo.index.commit("Second commit\n\nWith a body.", author=_AUTHOR, committer=_AUTHOR)

            (path / "untracked.txt").write_text("new")
            (path / "a.txt").write_text("three")  # modified, not staged

        yield path


def test_get_git_log_returns_real_commits_newest_first(repo_with_history):
    log = get_git_log(repo_with_history, limit=10)
    assert len(log) == 2
    assert log[0]["title"] == "Second commit"
    assert log[0]["body"] == "With a body."
    assert log[0]["author"] == "Test Author"
    assert log[0]["merge"] is False
    assert log[1]["title"] == "First commit"
    # newest commit's single parent is the oldest commit
    assert log[0]["parents"] == [log[1]["hash"]]
    assert log[1]["parents"] == []


def test_get_git_log_respects_limit(repo_with_history):
    log = get_git_log(repo_with_history, limit=1)
    assert len(log) == 1
    assert log[0]["title"] == "Second commit"


def test_get_git_status_reports_real_working_tree_state(repo_with_history):
    status = get_git_status(repo_with_history)
    assert status["branch"] in {"master", "main"}
    paths_and_states = {(e["path"], e["state"]) for e in status["uncommitted"]}
    assert ("untracked.txt", "untracked") in paths_and_states
    assert ("a.txt", "modified") in paths_and_states


def test_get_git_log_on_repo_with_no_commits_returns_empty_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        with Repo.init(path):
            pass
        assert get_git_log(path, limit=10) == []


def test_get_git_status_clean_tree_reports_no_uncommitted():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        with Repo.init(path) as repo:
            (path / "a.txt").write_text("one")
            repo.index.add(["a.txt"])
            repo.index.commit("Only commit", author=_AUTHOR, committer=_AUTHOR)
        status = get_git_status(path)
        assert status["uncommitted"] == []
