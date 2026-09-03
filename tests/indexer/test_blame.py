"""Unit tests for blame-based Function/Class recency."""

import subprocess
import tempfile
from pathlib import Path

import pytest
from git import Repo

from devgraph.indexer.git_history.blame import compute_function_recency


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, check=True)


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        (repo_path / "mod.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
        _run_git(repo_path, "add", "mod.py")
        _run_git(repo_path, "commit", "-m", "Add a and b")

        # Only touch b()'s body line, so blame should attribute lines 1-4 to
        # the first commit and lines 5-6 to the second.
        (repo_path / "mod.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 3\n")
        _run_git(repo_path, "add", "mod.py")
        _run_git(repo_path, "commit", "-m", "Update b")

        yield repo_path


def test_compute_function_recency_covers_whole_file_in_order(temp_git_repo):
    repo = Repo(str(temp_git_repo))
    try:
        hunks = compute_function_recency(repo, "mod.py")
    finally:
        repo.close()

    # Contiguous, 1-indexed, covering the whole 6-line file with no gaps/overlaps.
    assert hunks[0].start_line == 1
    assert hunks[-1].end_line == 6
    for prev, nxt in zip(hunks, hunks[1:]):
        assert nxt.start_line == prev.end_line + 1

    # The last hunk (covering b()'s changed line) should have the later date.
    assert hunks[-1].last_modified_at >= hunks[0].last_modified_at
    assert hunks[0].last_modified_by == "Test Author"


def test_compute_function_recency_missing_file_raises(temp_git_repo):
    repo = Repo(str(temp_git_repo))
    try:
        with pytest.raises(Exception):
            compute_function_recency(repo, "does_not_exist.py")
    finally:
        repo.close()
