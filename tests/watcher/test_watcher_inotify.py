"""How many inotify instances and watches the watcher uses on Linux, and that
a watch that can't start leaks nothing and is reported (round-3 audit, item 9).

Linux only: instances and watches are read from /proc/self/fd and
/proc/self/fdinfo. Every count is a difference from just before the watcher
started, so other tests' leftovers don't matter.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")

DEADLINE_S = 10.0


def inotify_watches() -> dict[int, int]:
    """This process's inotify instances: fd -> number of kernel watches."""
    out = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{name}") != "anon_inode:inotify":
                continue
            info = Path(f"/proc/self/fdinfo/{name}").read_text()
        except OSError:
            continue  # closed meanwhile
        out[int(name)] = info.count("inotify wd:")
    return out


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def wait_for(predicate, what) -> None:
    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")


def make_repo(parent: Path, name: str, top_folders: int) -> Path:
    root = parent / name
    (root / ".git/refs/heads").mkdir(parents=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    for i in range(top_folders):
        (root / f"pkg{i}").mkdir()
        (root / f"pkg{i}/m.py").write_text("x = 1\n")
    return root.resolve()


class Watching:
    """A registry with one repository and a WatcherManager over it, git
    watches included; batches are delivered to `batches`."""

    def __init__(self, tmp_path: Path, root: Path, *, git: bool = True) -> None:
        self.root = root
        self.registry = RepoRegistry(tmp_path / f"{root.name}.db")
        self.repo_id = self.registry.add_repo(root).repo_id
        self.batches: list[set[str]] = []
        self.manager = WatcherManager(
            self.registry, self._on_changes, on_git_state_changed=(lambda _r: None) if git else None
        )

    def _on_changes(self, _repo_id: str, changed: set[Path], deleted: set[Path]) -> None:
        self.batches.append({Path(p).relative_to(self.root).as_posix() for p in changed})

    def seen(self) -> set[str]:
        return set().union(*self.batches) if self.batches else set()

    def close(self) -> None:
        self.manager.stop()
        self.registry.close()


def test_inotify_instances_do_not_grow_with_top_level_folders(tmp_path):
    """Linux allows 128 inotify instances per user by default; one per
    top-level folder let a repository with ~100 folders use them all up."""
    used = {}
    for n in (3, 100):
        w = Watching(tmp_path, make_repo(tmp_path, f"repo{n}", n))
        before = set(inotify_watches())
        w.manager.start()
        try:
            assert w.manager.get_repo_issues() == {}
            used[n] = len(set(inotify_watches()) - before)
        finally:
            w.close()
    # One instance per repository: its folders and its git state (.git,
    # .git/refs, .git/refs/heads) share it.
    assert used == {3: 1, 100: 1}, used


def test_ignored_and_gitignored_folders_get_no_kernel_watch(tmp_path):
    """The repository is watched from its root, but folders the indexer skips
    (by name or by a .gitignore) at any depth are never put under a watch."""
    root = make_repo(tmp_path, "repo", 2)
    (root / ".gitignore").write_text("generated/\n")
    for folder in ("node_modules/a/b", "pkg0/.venv/lib/site", "pkg1/build/out", "generated/x/y", "pkg1/sub/generated/z"):
        (root / folder).mkdir(parents=True)
    w = Watching(tmp_path, root, git=False)
    before = set(inotify_watches())
    w.manager.start()
    try:
        watches = {fd: n for fd, n in inotify_watches().items() if fd not in before}
        # The root, pkg0, pkg1 and pkg1/sub.
        assert sorted(watches.values()) == [4], watches
    finally:
        w.close()


def test_a_folder_moved_in_from_outside_is_watched(tmp_path):
    root = make_repo(tmp_path, "repo", 1)
    outside = tmp_path / "outside/incoming/deep"
    outside.mkdir(parents=True)
    (outside / "c.py").write_text("c = 1\n")
    w = Watching(tmp_path, root, git=False)
    w.manager.start()
    try:
        time.sleep(0.3)
        shutil.move(str(tmp_path / "outside/incoming"), str(root / "pkg0/incoming"))
        wait_for(lambda: "pkg0/incoming/deep/c.py" in w.seen(), lambda: f"the moved-in file; got {w.seen()}")
        w.batches.clear()
        time.sleep(0.6)  # past the inotify buffer's move-pairing delay
        (root / "pkg0/incoming/deep/c.py").write_text("c = 2\n")
        wait_for(lambda: "pkg0/incoming/deep/c.py" in w.seen(), lambda: f"the edit; got {w.seen()}")
    finally:
        w.close()


def test_a_folder_no_longer_gitignored_is_watched(tmp_path):
    root = make_repo(tmp_path, "repo", 1)
    (root / ".gitignore").write_text("gen/\n")
    (root / "gen/deep").mkdir(parents=True)
    (root / "gen/deep/g.py").write_text("g = 1\n")
    w = Watching(tmp_path, root, git=False)
    w.manager.start()
    try:
        time.sleep(0.3)
        (root / ".gitignore").write_text("")
        wait_for(lambda: ".gitignore" in w.seen(), lambda: f"the .gitignore edit; got {w.seen()}")
        w.batches.clear()

        def edit_seen() -> bool:
            (root / "gen/deep/g.py").write_text(f"g = {time.monotonic()}\n")
            time.sleep(0.8)
            return "gen/deep/g.py" in w.seen()

        wait_for(edit_seen, lambda: f"the edit under gen/; got {w.seen()}")
    finally:
        w.close()


def _fail_after(monkeypatch, name: str, calls: int, err: int) -> None:
    """Make watchdog's `name` (a libc inotify call) fail with `err` from its
    `calls`+1-th call on."""
    from watchdog.observers import inotify_c

    real = getattr(inotify_c, name)
    count = [0]

    def failing(*args):
        count[0] += 1
        if count[0] > calls:
            ctypes.set_errno(err)
            return -1
        return real(*args)

    monkeypatch.setattr(inotify_c, name, failing)


@pytest.mark.parametrize(
    ("call", "after", "err", "limit"),
    [
        # The instance can't be made: the user's instance limit.
        ("inotify_init", 0, errno.EMFILE, "max_user_instances"),
        # A watch can't be added mid-way through the first instance's tree.
        ("inotify_add_watch", 2, errno.ENOSPC, "max_user_watches"),
    ],
)
def test_a_watch_that_cannot_start_leaks_nothing_and_is_reported(tmp_path, monkeypatch, call, after, err, limit):
    w = Watching(tmp_path, make_repo(tmp_path, "repo", 5))
    threads, fds = threading.active_count(), open_fds()
    _fail_after(monkeypatch, call, after, err)
    w.manager.start()
    try:
        issue = w.manager.get_repo_issues().get(w.repo_id, "")
        assert issue.startswith("watching unavailable: inotify limit reached"), issue
        assert limit in issue
        wait_for(
            lambda: threading.active_count() == threads and open_fds() == fds,
            lambda: f"threads {threading.active_count()} (was {threads}), fds {open_fds()} (was {fds})",
        )
    finally:
        w.close()


def test_git_state_changes_reach_the_git_handler_and_nothing_else_does(tmp_path):
    """The root watch carries .git, .git/refs and .git/refs/heads (a branch
    folder named like an ignored one included) to the git handler only; a
    source file named HEAD or a folder named refs is a plain file event."""
    root = make_repo(tmp_path, "repo", 1)
    (root / ".git/refs/heads/build").mkdir()
    (root / "pkg0/refs").mkdir()
    bursts: list[str] = []
    registry = RepoRegistry(tmp_path / "reg.db")
    repo_id = registry.add_repo(root).repo_id
    changed: list[str] = []
    manager = WatcherManager(
        registry,
        lambda _r, c, _d: changed.extend(Path(p).relative_to(root).as_posix() for p in c),
        on_git_state_changed=bursts.append,
    )
    manager.start()
    try:
        time.sleep(0.3)
        (root / "pkg0/HEAD").write_text("not git\n")
        (root / "pkg0/refs/x.py").write_text("x = 1\n")
        wait_for(lambda: {"pkg0/HEAD", "pkg0/refs/x.py"} <= set(changed), lambda: f"file events; got {changed}")
        time.sleep(1.0)
        assert bursts == []
        (root / ".git/refs/heads/build/feature").write_text("0" * 40 + "\n")
        wait_for(lambda: bursts == [repo_id], lambda: f"one git burst; got {bursts}")
        assert not any(p.startswith(".git") for p in changed)
    finally:
        manager.stop()
        registry.close()
