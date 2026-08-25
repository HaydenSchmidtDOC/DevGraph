"""Unit tests for the Phase 3 git history extractor."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from devgraph.indexer.git_history.extractor import GitHistoryExtractor


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, check=True)


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        (repo_path / "module_a.py").write_text("x = 1\n")
        _run_git(repo_path, "add", "module_a.py")
        _run_git(repo_path, "commit", "-m", "Add module_a")

        (repo_path / "module_b.py").write_text("y = 2\n")
        _run_git(repo_path, "add", "module_b.py")
        _run_git(repo_path, "commit", "-m", "Add module_b")

        (repo_path / "module_a.py").write_text("x = 2\n")
        _run_git(repo_path, "add", "module_a.py")
        _run_git(repo_path, "commit", "-m", "Update module_a")

        yield repo_path


class TestGitHistoryExtractor:
    def test_extract_full_history(self, temp_git_repo):
        extractor = GitHistoryExtractor("test-repo", temp_git_repo)
        result = extractor.extract_new_commits()

        assert len(result.commits) == 3
        messages = [c.properties["message"] for c in result.commits]
        assert messages == ["Add module_a", "Add module_b", "Update module_a"]

        for commit in result.commits:
            assert commit.repo_id == "test-repo"
            assert commit.properties["author"]  # non-empty, whatever local git config says

    def test_extract_modifies_relationships(self, temp_git_repo):
        extractor = GitHistoryExtractor("test-repo", temp_git_repo)
        result = extractor.extract_new_commits()

        modifies = [
            (r.source_name, r.target_name) for r in result.relationships if r.relationship_type == "MODIFIES"
        ]
        assert any(t == "module_a.py" for _, t in modifies)
        assert any(t == "module_b.py" for _, t in modifies)
        # module_a.py touched by 2 commits (add + update)
        assert sum(1 for _, t in modifies if t == "module_a.py") == 2

    def test_incremental_since_sha(self, temp_git_repo):
        extractor = GitHistoryExtractor("test-repo", temp_git_repo)
        full = extractor.extract_new_commits()
        first_sha = full.commits[0].sha

        incremental = extractor.extract_new_commits(since_sha=first_sha)
        assert len(incremental.commits) == 2
        assert incremental.commits[0].properties["message"] == "Add module_b"

    def test_since_latest_sha_returns_nothing(self, temp_git_repo):
        extractor = GitHistoryExtractor("test-repo", temp_git_repo)
        full = extractor.extract_new_commits()
        latest_sha = full.commits[-1].sha

        incremental = extractor.extract_new_commits(since_sha=latest_sha)
        assert incremental.commits == []

    def test_max_count_limits_walk(self, temp_git_repo):
        extractor = GitHistoryExtractor("test-repo", temp_git_repo)
        result = extractor.extract_new_commits(max_count=1)
        assert len(result.commits) == 1
        # max_count=1 with newest-first iter_commits then reversed -> the latest commit
        assert result.commits[0].properties["message"] == "Update module_a"
