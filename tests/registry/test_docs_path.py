"""Tests for the Phase 2 docs_path column on RepoRegistry."""

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


def test_docs_path_defaults_to_none(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    assert record.docs_path is None
    fetched = registry.get(record.repo_id)
    assert fetched.docs_path is None


def test_set_docs_path(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_docs_path(record.repo_id, "devgraph/docs")

    fetched = registry.get(record.repo_id)
    assert fetched.docs_path == "devgraph/docs"


def test_set_docs_path_reflected_in_list(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_docs_path(record.repo_id, "docs")

    repos = registry.list_repos()
    matched = next(r for r in repos if r.repo_id == record.repo_id)
    assert matched.docs_path == "docs"


def test_set_docs_path_unknown_repo_raises(registry):
    with pytest.raises(ValueError):
        registry.set_docs_path("nonexistent", "docs")


def test_clear_docs_path(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry.set_docs_path(record.repo_id, "docs")
    registry.set_docs_path(record.repo_id, None)

    fetched = registry.get(record.repo_id)
    assert fetched.docs_path is None
