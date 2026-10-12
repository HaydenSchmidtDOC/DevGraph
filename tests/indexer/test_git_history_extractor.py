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


def test_an_initial_sync_reports_how_many_commits_it_will_read(tmp_path):
    """The agent logs the count before the (possibly long) first read."""
    import os
    import subprocess
    from unittest.mock import MagicMock

    from devgraph.indexer.git_history.extractor import sync_git_history
    from devgraph.registry.store import RepoRegistry

    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@example.com",
           "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    git = lambda *a: subprocess.run(["git", "-c", "commit.gpgsign=false", *a], cwd=root, check=True,  # noqa: E731
                                    capture_output=True, env=env)
    git("init", "-q", "-b", "main")
    for i in range(3):
        (root / "a.py").write_text(f"a = {i}\n")
        git("add", "-A")
        git("commit", "-q", "-m", f"c{i}")
    registry = RepoRegistry(tmp_path / "registry.db")
    try:
        repo_id = registry.add_repo(root).repo_id
        counts: list[int] = []
        assert sync_git_history(MagicMock(), registry, repo_id, on_initial=counts.append)["mode"] == "initial"
        assert counts == [3]
        assert sync_git_history(MagicMock(), registry, repo_id, on_initial=counts.append)["mode"] == "noop"
        assert counts == [3]
    finally:
        registry.close()


@pytest.fixture(scope="module")
def five_commit_origin(tmp_path_factory):
    """pkg/a.py, b.py, c.py, d.py and e.py, one commit each, then an edit of a.py."""
    origin = tmp_path_factory.mktemp("origin")
    _run_git(origin, "init")
    _run_git(origin, "config", "user.email", "test@example.com")
    _run_git(origin, "config", "user.name", "Test Author")
    (origin / "pkg").mkdir()
    for name in ("a", "b", "c", "d", "e"):
        (origin / f"pkg/{name}.py").write_text(f"{name} = 1\n")
        _run_git(origin, "add", ".")
        _run_git(origin, "commit", "-m", f"Add {name}")
    (origin / "pkg/a.py").write_text("a = 2\n")
    _run_git(origin, "commit", "-am", "Edit a")
    return origin


def _shallow_clone(origin: Path, dest: Path, depth: int) -> Path:
    subprocess.run(
        ["git", "clone", "--quiet", f"--depth={depth}", f"file://{origin}", str(dest)], capture_output=True, check=True
    )
    assert (dest / ".git/shallow").exists()
    return dest


def _modifies(result) -> dict[str, set[str]]:
    by_message = {c.sha: c.properties["message"] for c in result.commits}
    out: dict[str, set[str]] = {}
    for r in result.relationships:
        out.setdefault(by_message[r.source_name], set()).add(r.target_name)
    return out


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        # The one commit is the boundary: its parent isn't in the clone, so it
        # is read like a root commit, every file it has counting as touched.
        (1, {"Edit a": {f"pkg/{n}.py" for n in "abcde"}}),
        (3, {"Add d": {f"pkg/{n}.py" for n in "abcd"}, "Add e": {"pkg/e.py"}, "Edit a": {"pkg/a.py"}}),
    ],
)
def test_a_shallow_clone_has_history(five_commit_origin, tmp_path, caplog, depth, expected):
    """A shallow clone's oldest commit names a parent the clone doesn't have:
    diffing against it failed, and its files got no MODIFIES edges."""
    clone = _shallow_clone(five_commit_origin, tmp_path / "clone", depth)
    with caplog.at_level("WARNING"):
        result = GitHistoryExtractor("shallow", clone).extract_new_commits()
    assert [c.properties["message"] for c in result.commits] == list(expected)
    assert _modifies(result) == expected
    assert "partial clone" not in caplog.text


def test_a_shallow_clone_deepened_later_reads_only_new_commits(five_commit_origin, tmp_path):
    """After `git fetch --deepen`, an incremental sync from the old boundary
    reads nothing new: the commits it gained are older than it."""
    clone = _shallow_clone(five_commit_origin, tmp_path / "clone", 1)
    head = GitHistoryExtractor("shallow", clone).extract_new_commits().commits[-1].sha
    _run_git(clone, "fetch", "--quiet", "--deepen=2")
    assert GitHistoryExtractor("shallow", clone).extract_new_commits(since_sha=head).commits == []
