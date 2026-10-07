"""The shared file-walk helpers: which files a scan (and the providers) may read."""

import os
import subprocess
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
from devgraph.paths import is_within


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


def _junction(link, target):
    # mklink /J needs no admin rights or Developer Mode, unlike a symlink.
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@pytest.fixture
def junction_tree(tmp_path):
    repo = tmp_path / "repo"
    for rel in ("docs/a.md", "docs/deep/b.md", "vendor/v.md", "src/x.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(rel)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    links = [repo / "docs-j", repo / "vendor-j", repo / "out-j"]
    _junction(links[0], repo / "docs")
    _junction(links[1], repo / "vendor")
    _junction(links[2], outside)
    yield repo
    for link in links:
        os.rmdir(link)  # removes the junction only, never its target's contents


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junctions_are_walked_as_rglob_walks_them_and_keyed_by_target(junction_tree):
    repo = junction_tree
    assert (repo / "docs-j").is_junction() and not (repo / "docs-j").is_symlink()

    old_paths, old_keyed = _old_keyed(repo)
    keyed = walk.keyed_indexable_paths(repo)

    # The old walk also followed the outside and ignored-target junctions; this one does not.
    assert indexable_paths(repo) == {
        p for p in old_paths if not p.is_relative_to(repo / "out-j") and not p.is_relative_to(repo / "vendor-j")
    }
    assert set(keyed) == old_keyed
    # Inside the repository: walked like rglob, keyed by the target, not the junction.
    assert (repo / "docs-j" / "deep" / "b.md", "docs/deep/b.md") in keyed
    assert "docs-j/deep/b.md" not in {rel for _, rel in keyed}
    # Into an ignored directory: keyed out, as a symlinked file into one is.
    assert not any(rel.startswith("vendor/") for _, rel in keyed)
    # Outside the repository: never keyed, and every key is its resolved path.
    root = repo.resolve()
    for path, rel in keyed:
        assert path.resolve().relative_to(root).as_posix() == rel
    assert not any(path.is_relative_to(repo / "out-j") for path, _ in keyed)


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_a_junction_out_of_the_repository_is_not_followed(junction_tree):
    repo = junction_tree
    paths = indexable_paths(repo)
    assert all(is_within(p.resolve(), repo) for p in paths), sorted(map(str, paths))
    assert not any(p.is_relative_to(repo / "out-j") for p in paths)
    assert not any(p.is_relative_to(repo / "vendor-j") for p in paths)
    assert repo / "docs-j" / "a.md" in paths  # an inside junction is still walked


class _FakeEngine:
    def __init__(self, files):
        self.files = files

    def list_indexed_files(self, repo_id):
        return set(self.files)

    def read_applied_schema(self, repo_id):
        return None

    def list_claim_sources(self, repo_id):
        return set()


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_prune_stale_files_tolerates_a_junction_out_of_the_repository(junction_tree, monkeypatch):
    repo = junction_tree
    removed = []
    monkeypatch.setattr(dispatch, "remove_paths", lambda engine, repo_id, root, paths: removed.append(paths) or len(paths))
    engine = _FakeEngine({"docs/a.md", "docs/deep/b.md", "src/x.py", "gone.md"})

    assert dispatch.prune_stale_files(engine, "r", repo) == 1
    assert removed == [{repo / "gone.md"}]
