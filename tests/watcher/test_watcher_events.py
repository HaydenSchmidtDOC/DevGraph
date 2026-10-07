"""Fake-event tests for _RepoEventHandler (spec W1, W2 watcher half, W3 signal, W4).

Events are built by hand and fed to the handler directly. Batches fire only
through `flush()` or a FakeTimer's `fire()`, never through a real debounce.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

import devgraph.watcher.manager as manager_module
from devgraph.indexer.walk import indexable_paths, indexable_paths_under
from devgraph.watcher.manager import _RepoEventHandler


class FakeTimer:
    def __init__(self, interval: float, function) -> None:
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.function()


class Harness:
    def __init__(self, root: Path, on_changes=None) -> None:
        self.root = root
        self.batches: list[tuple[set[Path], set[Path]]] = []
        self.timers: list[FakeTimer] = []
        self.reconciles: list[str] = []

        def record(repo_id: str, changed: set[Path], deleted: set[Path]) -> None:
            self.batches.append((set(changed), set(deleted)))

        def factory(interval: float, function) -> FakeTimer:
            timer = FakeTimer(interval, function)
            self.timers.append(timer)
            return timer

        self.handler = _RepoEventHandler(
            "repo",
            root,
            500,
            on_changes or record,
            timer_factory=factory,
            batch_lock=threading.Lock(),
            request_reconcile=self.reconciles.append,
        )

    def p(self, rel: str) -> str:
        return str(self.root / rel)

    def send(self, *events) -> None:
        for event in events:
            self.handler.dispatch(event)

    def rel(self, paths: set[Path]) -> set[str]:
        return {Path(p).relative_to(self.root).as_posix() for p in paths}

    def one_batch(self) -> tuple[set[str], set[str]]:
        self.handler.flush()
        assert len(self.batches) == 1, self.batches
        changed, deleted = self.batches[0]
        return self.rel(changed), self.rel(deleted)


def _write(path: Path, text: str = "x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    return repo


@pytest.fixture
def h(root: Path) -> Harness:
    return Harness(root)


# --- W1: files -------------------------------------------------------------


def test_file_rename_is_one_batch_of_delete_plus_change(h, root):
    _write(root / "pkg/a2.py")
    h.send(FileMovedEvent(h.p("pkg/a.py"), h.p("pkg/a2.py")))
    assert h.one_batch() == ({"pkg/a2.py"}, {"pkg/a.py"})


def test_atomic_save_keeps_real_path_out_of_deleted(h, root):
    _write(root / "pkg/real.py")
    h.send(
        FileDeletedEvent(h.p("pkg/real.py")),
        FileMovedEvent(h.p("pkg/real.py.tmp"), h.p("pkg/real.py")),
    )
    changed, deleted = h.one_batch()
    assert "pkg/real.py" in changed
    assert "pkg/real.py" not in deleted


def test_move_into_ignored_dir_is_a_delete(h, root):
    _write(root / "build/a.py")
    h.send(FileMovedEvent(h.p("pkg/a.py"), h.p("build/a.py")))
    assert h.one_batch() == (set(), {"pkg/a.py"})


def test_move_out_of_repo_is_a_delete(h, root, tmp_path):
    outside = _write(tmp_path / "outside/a.py")
    h.send(FileMovedEvent(h.p("pkg/a.py"), str(outside)))
    assert h.one_batch() == (set(), {"pkg/a.py"})


def test_move_from_ignored_source_is_a_change_only(h, root):
    _write(root / "pkg/a.py")
    h.send(FileMovedEvent(h.p("build/a.py"), h.p("pkg/a.py")))
    assert h.one_batch() == ({"pkg/a.py"}, set())


def test_empty_source_is_a_change_only(h, root):
    _write(root / "pkg/a.py")
    h.send(FileMovedEvent("", h.p("pkg/a.py")))
    assert h.one_batch() == ({"pkg/a.py"}, set())


def test_case_only_rename_delivers_both(h, root, monkeypatch):
    monkeypatch.setattr(os.path, "normcase", lambda s: os.fspath(s).lower())
    _write(root / "pkg/foo.py")
    h.send(FileMovedEvent(h.p("pkg/Foo.py"), h.p("pkg/foo.py")))
    assert h.one_batch() == ({"pkg/foo.py"}, {"pkg/Foo.py"})


# --- W2: directories ---------------------------------------------------------


def test_dir_delete_queues_the_directory(h, root):
    h.send(DirDeletedEvent(h.p("pkg/sub")))
    assert h.one_batch() == (set(), {"pkg/sub"})


def test_dir_delete_under_ignored_dir_is_nothing(h, root):
    h.send(DirDeletedEvent(h.p("node_modules/x")))
    h.handler.flush()
    assert h.batches == []


def test_dir_delete_of_root_is_nothing(h, root):
    h.send(DirDeletedEvent(str(root)))
    h.handler.flush()
    assert h.batches == []


def test_windows_cross_folder_dir_move(h, root):
    _write(root / "tools/sub/b.py")
    h.send(
        FileDeletedEvent(h.p("pkg/sub")),
        DirCreatedEvent(h.p("tools/sub")),
    )
    assert h.one_batch() == ({"tools/sub/b.py"}, {"pkg/sub"})


def test_dir_move_walks_destination_and_sub_moves_add_nothing(h, root):
    _write(root / "pkg/sub2/b.py")
    _write(root / "pkg/sub2/build/x.py")
    h.send(
        DirMovedEvent(h.p("pkg/sub"), h.p("pkg/sub2")),
        FileMovedEvent(h.p("pkg/sub/b.py"), h.p("pkg/sub2/b.py")),
        DirMovedEvent(h.p("pkg/sub/build"), h.p("pkg/sub2/build")),
        FileMovedEvent(h.p("pkg/sub/build/x.py"), h.p("pkg/sub2/build/x.py")),
    )
    assert h.one_batch() == ({"pkg/sub2/b.py"}, {"pkg/sub"})


def test_dir_created_walks_it(h, root):
    _write(root / "pkg/new/c.py")
    _write(root / "pkg/new/d.md", "# d\n")
    h.send(DirCreatedEvent(h.p("pkg/new")))
    assert h.one_batch() == ({"pkg/new/c.py", "pkg/new/d.md"}, set())


def test_empty_dir_created_gives_no_batch(h, root):
    (root / "pkg/empty").mkdir()
    h.send(DirCreatedEvent(h.p("pkg/empty")))
    h.handler.flush()
    assert h.batches == []


def test_delete_then_create_is_changed_only(h, root):
    _write(root / "pkg/a.py")
    h.send(FileDeletedEvent(h.p("pkg/a.py")), FileCreatedEvent(h.p("pkg/a.py")))
    assert h.one_batch() == ({"pkg/a.py"}, set())


def test_create_then_delete_is_deleted_only(h, root):
    path = _write(root / "pkg/a.py")
    h.send(FileCreatedEvent(h.p("pkg/a.py")))
    path.unlink()
    h.send(FileDeletedEvent(h.p("pkg/a.py")))
    assert h.one_batch() == (set(), {"pkg/a.py"})


# --- W3: top-level signal ----------------------------------------------------


@pytest.mark.parametrize(
    "make_event",
    [
        lambda r: DirCreatedEvent(str(r / "newtop")),
        lambda r: DirMovedEvent(str(r / "pkg"), str(r / "lib")),
        lambda r: DirDeletedEvent(str(r / "pkg")),
        lambda r: FileDeletedEvent(str(r / "pkg")),
        lambda r: FileCreatedEvent(str(r / "x.py")),
    ],
    ids=["dir-created", "dir-moved", "dir-deleted", "file-deleted-dir", "file-created"],
)
def test_top_level_events_request_one_reconcile(h, root, make_event):
    (root / "newtop").mkdir()
    (root / "lib").mkdir()
    _write(root / "x.py")
    h.send(make_event(root))
    assert h.reconciles == ["repo"]


def test_nested_dir_create_does_not_request_reconcile(h, root):
    (root / "pkg/x").mkdir()
    h.send(DirCreatedEvent(h.p("pkg/x")))
    assert h.reconciles == []


# --- W4: locks ---------------------------------------------------------------


def test_on_changes_runs_outside_handler_lock(root):
    seen = []
    harness: Harness

    def on_changes(repo_id, changed, deleted):
        seen.append(harness.handler._lock.locked())

    harness = Harness(root, on_changes)
    _write(root / "pkg/a.py")
    harness.send(FileModifiedEvent(harness.p("pkg/a.py")))
    harness.handler.flush()
    assert seen == [False]


def test_walks_run_outside_handler_lock(h, root, monkeypatch):
    seen = []
    real = manager_module.indexable_paths_under

    def spy(repo_root, directory):
        seen.append(h.handler._lock.locked())
        return real(repo_root, directory)

    monkeypatch.setattr(manager_module, "indexable_paths_under", spy)
    _write(root / "pkg/new/c.py")
    _write(root / "pkg/sub2/b.py")
    h.send(DirCreatedEvent(h.p("pkg/new")), DirMovedEvent(h.p("pkg/sub"), h.p("pkg/sub2")))
    assert seen == [False, False]


def test_events_are_accepted_while_a_batch_runs(root):
    entered = threading.Event()
    release = threading.Event()
    batches = []

    def on_changes(repo_id, changed, deleted):
        batches.append(set(changed))
        entered.set()
        assert release.wait(5)

    harness = Harness(root, on_changes)
    a = _write(root / "pkg/a.py")
    b = _write(root / "pkg/b.py")
    harness.send(FileModifiedEvent(str(a)))
    worker = threading.Thread(target=harness.handler.flush)
    worker.start()
    try:
        assert entered.wait(5)
        done = threading.Event()

        def deliver():
            harness.send(FileModifiedEvent(str(b)))
            done.set()

        threading.Thread(target=deliver).start()
        assert done.wait(2), "event delivery blocked behind a running batch"
    finally:
        release.set()
        worker.join(5)
    harness.handler.flush()
    assert batches == [{a}, {b}]


# --- flush() / cancel() ------------------------------------------------------


def test_flush_with_nothing_pending_makes_no_call(h):
    h.handler.flush()
    assert h.batches == []


def test_timer_fire_delivers_batch(h, root):
    _write(root / "pkg/a.py")
    h.send(FileModifiedEvent(h.p("pkg/a.py")))
    assert h.batches == []
    h.timers[-1].fire()
    assert h.rel(h.batches[0][0]) == {"pkg/a.py"}


def test_cancel_drops_timer_but_flush_still_fires(h, root):
    _write(root / "pkg/a.py")
    h.send(FileModifiedEvent(h.p("pkg/a.py")))
    timer = h.timers[-1]
    h.handler.cancel()
    assert timer.cancelled
    timer.fire()
    assert h.batches == []
    h.handler.flush()
    assert h.rel(h.batches[0][0]) == {"pkg/a.py"}


# --- walk.indexable_paths_under ---------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    repo = tmp_path / "tree"
    _write(repo / "top.py")
    _write(repo / "pkg/a.py")
    _write(repo / "pkg/sub/b.py")
    _write(repo / "pkg/build/x.py")
    _write(repo / "other/c.py")
    return repo


def test_indexable_paths_under_matches_filtered_full_walk(tree):
    expected = {p for p in indexable_paths(tree) if p.is_relative_to(tree / "pkg")}
    assert indexable_paths_under(tree, tree / "pkg") == expected
    assert {p.relative_to(tree).as_posix() for p in expected} == {"pkg/a.py", "pkg/sub/b.py"}


def test_indexable_paths_under_outside_or_ignored_is_empty(tree, tmp_path):
    _write(tmp_path / "elsewhere/e.py")
    assert indexable_paths_under(tree, tmp_path / "elsewhere") == set()
    assert indexable_paths_under(tree, tree / "pkg/build") == set()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_indexable_paths_under_does_not_follow_symlinked_dirs(tree):
    (tree / "pkg/link").symlink_to(tree / "other", target_is_directory=True)
    assert indexable_paths_under(tree, tree / "pkg/link") == set()
    found = {p.relative_to(tree).as_posix() for p in indexable_paths_under(tree, tree / "pkg")}
    assert found == {"pkg/a.py", "pkg/sub/b.py"}
