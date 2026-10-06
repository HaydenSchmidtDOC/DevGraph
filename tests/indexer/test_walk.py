"""The shared file-walk helpers: which files a scan (and the providers) may read."""

import os
from pathlib import Path

import pytest

from devgraph.indexer import dispatch, walk
from devgraph.indexer.walk import (
    IGNORED_DIR_NAMES,
    indexable_paths,
    is_ignored_dir_name,
    is_ignored_path,
    is_indexable_file,
    links_outside,
)


def test_ignored_dir_names_cover_venvs_build_output_and_worktrees():
    assert {".git", ".venv", "node_modules", "__pycache__", ".worktrees", "worktrees"} <= IGNORED_DIR_NAMES


def test_is_ignored_dir_name_also_ignores_egg_info():
    assert is_ignored_dir_name(".venv")
    assert is_ignored_dir_name("devgraph.egg-info")
    assert not is_ignored_dir_name("src")


def test_is_ignored_path_checks_every_part():
    assert is_ignored_path(Path("a/node_modules/b.js"))
    assert not is_ignored_path(Path("a/b/c.py"))


def test_is_indexable_file_is_a_file_check_that_never_raises(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "d").mkdir()
    assert is_indexable_file(tmp_path / "a.md")
    assert not is_indexable_file(tmp_path / "d")
    assert not is_indexable_file(tmp_path / "missing.md")


def test_is_indexable_file_skips_a_file_the_os_cannot_stat(tmp_path, monkeypatch):
    def boom(self):
        raise OSError("locked")

    monkeypatch.setattr(Path, "is_file", boom)
    assert not is_indexable_file(tmp_path / "a.md")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_indexable_paths_skips_ignored_dirs_and_outside_symlinks(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text("a")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "b.md").write_text("b")
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (repo / "docs" / "out.md").symlink_to(outside)
    (repo / "docs" / "in.md").symlink_to(repo / "docs" / "a.md")

    found = indexable_paths(repo)

    assert found == {repo / "docs" / "a.md", repo / "docs" / "in.md"}
    assert links_outside(repo / "docs" / "out.md", repo)
    assert not links_outside(repo / "docs" / "in.md", repo)
    assert not links_outside(repo / "docs" / "a.md", repo)


def test_dispatch_re_exports_the_helpers_under_their_current_names():
    assert dispatch.IGNORED_DIR_NAMES is walk.IGNORED_DIR_NAMES
    assert dispatch.is_ignored_dir_name is walk.is_ignored_dir_name
    assert dispatch.is_ignored_path is walk.is_ignored_path
    assert dispatch._is_indexable_file is walk.is_indexable_file
    assert dispatch._indexable_paths is walk.indexable_paths
    assert dispatch._links_outside is walk.links_outside


def _old_keyed(repo):
    """The walk and keying as they were before the root was resolved once: the oracle."""
    def old_repo_relative(path):
        try:
            return Path(path).resolve().relative_to(repo.resolve()).as_posix()
        except (OSError, ValueError):
            return None

    paths = {
        p for p in repo.rglob("*")
        if is_indexable_file(p) and not is_ignored_path(p) and not links_outside(p, repo)
    }
    keyed = {
        (p, rel) for p in paths
        if (rel := old_repo_relative(p)) is not None and not is_ignored_path(Path(rel))
    }
    return paths, keyed


@pytest.fixture
def linked_tree(tmp_path):
    repo = tmp_path / "repo"
    for rel in ("a.md", "docs/b.md", "docs/deep/c.md", "src/x.py", ".venv/lib/v.py", "node_modules/m.js", "build"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(rel)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "o.md").write_text("secret")
    return repo


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_keyed_indexable_paths_matches_the_old_walk_over_every_kind_of_link(linked_tree):
    repo = linked_tree
    (repo / "docs" / "link.md").symlink_to(repo / "docs" / "deep" / "c.md")
    (repo / "docs" / "rel-link.md").symlink_to(Path("deep") / "c.md")
    (repo / "linked-dir").symlink_to(repo / "docs")
    (repo / "docs" / "out.md").symlink_to(repo.parent / "outside" / "o.md")
    (repo / "out-dir").symlink_to(repo.parent / "outside")
    (repo / "venv-link.py").symlink_to(repo / ".venv" / "lib" / "v.py")
    (repo / "dangling.md").symlink_to(repo / "missing.md")
    (repo / "loop-a").symlink_to(repo / "loop-b")
    (repo / "loop-b").symlink_to(repo / "loop-a")
    (repo / "self").symlink_to(repo / "self")

    old_paths, old_keyed = _old_keyed(repo)

    assert indexable_paths(repo) == old_paths
    assert set(walk.keyed_indexable_paths(repo)) == old_keyed
    assert (repo / "docs" / "link.md", "docs/deep/c.md") in old_keyed


def test_keyed_indexable_paths_matches_the_old_walk_without_links(linked_tree):
    old_paths, old_keyed = _old_keyed(linked_tree)

    assert indexable_paths(linked_tree) == old_paths
    assert set(walk.keyed_indexable_paths(linked_tree)) == old_keyed
    assert ("docs/deep/c.md") in {rel for _, rel in old_keyed}


def test_keyed_indexable_paths_is_empty_under_an_ignored_root(tmp_path):
    repo = tmp_path / "build" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.md").write_text("a")

    assert indexable_paths(repo) == _old_keyed(repo)[0] == set()
    assert walk.keyed_indexable_paths(repo) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_keyed_indexable_paths_resolves_the_root_once_and_only_links(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    for n in range(50):
        (repo / "docs" / f"{n}.md").write_text("x")
    (repo / "link.md").symlink_to(repo / "docs" / "0.md")
    resolved = []
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(self) or real(self, *a, **k))

    keyed = walk.keyed_indexable_paths(repo)

    assert len(keyed) == 51
    assert all(p in (repo, repo / "link.md") for p in resolved), resolved
