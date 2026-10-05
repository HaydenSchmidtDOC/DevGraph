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
