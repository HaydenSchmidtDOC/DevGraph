"""Git status paths are the paths themselves (audit 10, item 8e).

`git status --porcelain` without `-z` C-quotes a path with spaces or
non-ASCII characters (`"caf\\303\\251.py"`) and prints a staged rename as
`old -> new`; the dashboard's git panel and `devgraph info` showed both
verbatim. No Neo4j needed.
"""

import subprocess
from pathlib import Path

import pytest

from devgraph.dashboard.git_info import get_git_status


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Example Dev")
    (tmp_path / "old name.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _entries(repo: Path) -> list[dict[str, str]]:
    return get_git_status(repo)["uncommitted"]


def test_untracked_paths_with_spaces_and_non_ascii_are_not_quoted(repo):
    (repo / "café.py").write_text("", encoding="utf-8")
    (repo / "with space.py").write_text("", encoding="utf-8")
    entries = {(e["path"], e["state"]) for e in _entries(repo)}
    assert ("café.py", "untracked") in entries
    assert ("with space.py", "untracked") in entries


def test_a_modified_path_with_a_space_is_not_quoted(repo):
    (repo / "old name.py").write_text("x = 2\n", encoding="utf-8")
    assert {(e["path"], e["state"]) for e in _entries(repo)} == {("old name.py", "modified")}


def test_a_staged_rename_is_the_new_path_with_its_old_one(repo):
    _git(repo, "mv", "old name.py", "new näme.py")
    entries = _entries(repo)
    assert entries == [{"path": "new näme.py", "state": "renamed", "old_path": "old name.py"}]
