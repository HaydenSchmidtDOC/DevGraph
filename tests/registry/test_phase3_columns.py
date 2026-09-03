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


def test_mentions_enabled_defaults_to_false(registry, temp_git_repo):
    """Fresh add_repo defaults mentions_enabled to False."""
    record = registry.add_repo(temp_git_repo)
    assert record.mentions_enabled is False


def test_enable_mentions(registry, temp_git_repo):
    """set_mentions_enabled can enable mentions indexing."""
    record = registry.add_repo(temp_git_repo)
    registry.set_mentions_enabled(record.repo_id, True)

    fetched = registry.get(record.repo_id)
    assert fetched.mentions_enabled is True


def test_disable_mentions_after_enable(registry, temp_git_repo):
    """set_mentions_enabled can disable mentions indexing after enabling."""
    record = registry.add_repo(temp_git_repo)
    registry.set_mentions_enabled(record.repo_id, True)
    registry.set_mentions_enabled(record.repo_id, False)

    fetched = registry.get(record.repo_id)
    assert fetched.mentions_enabled is False


def test_mentions_enabled_roundtrips(registry, temp_git_repo):
    """mentions_enabled flag survives get() roundtrip."""
    record = registry.add_repo(temp_git_repo)
    registry.set_mentions_enabled(record.repo_id, True)

    fetched = registry.get(record.repo_id)
    assert fetched.mentions_enabled is True

    # Verify it persists across multiple get() calls
    fetched_again = registry.get(record.repo_id)
    assert fetched_again.mentions_enabled is True


def test_mentions_coexists_with_other_flags(registry, temp_git_repo):
    """mentions_enabled flag can coexist with other Phase 3 flags."""
    record = registry.add_repo(temp_git_repo)
    registry.set_mentions_enabled(record.repo_id, True)
    registry.set_pr_source_enabled(record.repo_id, True)
    registry.set_issue_source_enabled(record.repo_id, True)

    fetched = registry.get(record.repo_id)
    assert fetched.mentions_enabled is True
    assert fetched.pr_source_enabled is True
    assert fetched.issue_source_enabled is True
