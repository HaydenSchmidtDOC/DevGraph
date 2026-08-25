"""Tests for the Phase 3 registry columns: pr_source_enabled, issue_source_enabled,
last_indexed_commit.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


def test_pr_and_issue_source_default_off(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    assert record.pr_source_enabled is False
    assert record.issue_source_enabled is False


def test_enable_pr_source(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_pr_source_enabled(record.repo_id, True)

    fetched = registry.get(record.repo_id)
    assert fetched.pr_source_enabled is True
    assert fetched.issue_source_enabled is False


def test_enable_issue_source(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_issue_source_enabled(record.repo_id, True)

    fetched = registry.get(record.repo_id)
    assert fetched.issue_source_enabled is True
    assert fetched.pr_source_enabled is False


def test_disable_after_enable(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_pr_source_enabled(record.repo_id, True)
    registry.set_pr_source_enabled(record.repo_id, False)

    fetched = registry.get(record.repo_id)
    assert fetched.pr_source_enabled is False


def test_last_indexed_commit_defaults_to_none(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    assert record.last_indexed_commit is None


def test_set_last_indexed_commit(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_last_indexed_commit(record.repo_id, "abc123")

    fetched = registry.get(record.repo_id)
    assert fetched.last_indexed_commit == "abc123"


def test_set_last_indexed_commit_unknown_repo_raises(registry):
    with pytest.raises(ValueError):
        registry.set_last_indexed_commit("nonexistent", "abc123")


def test_docs_path_and_phase3_columns_coexist(registry, temp_git_repo):
    """Regression check: the multi-column migration loop applies each ALTER
    independently rather than short-circuiting after the first column exists.
    """
    record = registry.add_repo(temp_git_repo)
    registry.set_docs_path(record.repo_id, "docs")
    registry.set_pr_source_enabled(record.repo_id, True)
    registry.set_issue_source_enabled(record.repo_id, True)
    registry.set_last_indexed_commit(record.repo_id, "deadbeef")

    fetched = registry.get(record.repo_id)
    assert fetched.docs_path == "docs"
    assert fetched.pr_source_enabled is True
    assert fetched.issue_source_enabled is True
    assert fetched.last_indexed_commit == "deadbeef"
