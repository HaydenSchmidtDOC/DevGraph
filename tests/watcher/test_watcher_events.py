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
        self.gone: list[str] = []

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
            request_reconcile=self.reconcile,
        )

    def reconcile(self, repo_id: str, gone: str | None = None) -> None:
        self.reconciles.append(repo_id)
        if gone is not None:
            self.gone.append(gone)

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


def test_a_gitignored_file_is_not_queued_but_the_gitignore_is(h, root):
    _write(root / ".gitignore", "*.log\nout/\n")
    _write(root / "pkg/debug.log")
    _write(root / "out/bundle.js")
    _write(root / "pkg/a.py")
    h.send(
        FileModifiedEvent(h.p("pkg/debug.log")),
        FileCreatedEvent(h.p("out/bundle.js")),
        FileModifiedEvent(h.p("pkg/a.py")),
        FileModifiedEvent(h.p(".gitignore")),
    )
    assert h.one_batch() == ({"pkg/a.py", ".gitignore"}, set())


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


def test_synthetic_sub_moves_are_not_walked(h, root, monkeypatch):
    walked = []
    real = manager_module.indexable_paths_under
    monkeypatch.setattr(
        manager_module, "indexable_paths_under", lambda r, d: walked.append(d) or real(r, d)
    )
    _write(root / "pkg/sub2/inner/b.py")
    h.send(
        DirMovedEvent(h.p("pkg/sub"), h.p("pkg/sub2")),
        DirMovedEvent(h.p("pkg/sub/inner"), h.p("pkg/sub2/inner"), is_synthetic=True),
        FileMovedEvent(h.p("pkg/sub/inner/b.py"), h.p("pkg/sub2/inner/b.py"), is_synthetic=True),
        DirCreatedEvent(h.p("pkg/sub2/inner"), is_synthetic=True),
    )
    assert walked == [root / "pkg/sub2"]
    assert h.one_batch() == ({"pkg/sub2/inner/b.py"}, {"pkg/sub"})


def test_top_level_dirs_are_left_to_the_reconcile_walk(h, root, monkeypatch):
    walked = []
    monkeypatch.setattr(manager_module, "indexable_paths_under", lambda r, d: walked.append(d) or set())
    (root / "newtop").mkdir()
    (root / "lib").mkdir()
    h.send(DirCreatedEvent(h.p("newtop")), DirMovedEvent(h.p("pkg"), h.p("lib")))
    assert walked == []
    assert h.reconciles == ["repo", "repo"]
    assert h.one_batch() == (set(), {"pkg"})


def test_a_top_level_folder_deleted_or_moved_away_is_named_to_the_reconcile(h, root):
    """Its watch must be replaced even if a new folder of the same name gets
    the same inode, which ext4 hands out again at once."""
    (root / "lib").mkdir()
    h.send(
        DirDeletedEvent(h.p("pkg")), FileDeletedEvent(h.p("old")), DirMovedEvent(h.p("tools"), h.p("lib")),
        DirCreatedEvent(h.p("newtop")), DirDeletedEvent(h.p("pkg/sub")),
    )
    assert h.reconciles == ["repo"] * 4
    assert h.gone == ["pkg", "old", "tools"]


def test_closed_handler_neither_queues_nor_fires(h, root):
    _write(root / "pkg/a.py")
    h.send(FileModifiedEvent(h.p("pkg/a.py")))
    h.handler.close()
    assert h.timers[-1].cancelled
    h.send(FileModifiedEvent(h.p("pkg/a.py")))
    assert len(h.timers) == 1
    h.handler.flush()
    assert h.batches == []


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


# --- review fixes: a raising handler, and self-ignoring .gitignore files -----


def test_events_that_raise_are_counted_warned_every_few_minutes_and_caught_up(root, monkeypatch, caplog):
    now = [1000.0]
    requests = []
    harness = Harness(root)
    handler = _RepoEventHandler(
        "repo", root, 500, lambda *a: None, timer_factory=lambda *a: FakeTimer(*a), batch_lock=threading.Lock(),
        request_catch_up=lambda *a: requests.append(a), clock=lambda: now[0],
    )
    real = handler._queue_rel
    monkeypatch.setattr(handler, "_queue_rel", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("DEBUG", logger="devgraph.watcher.manager"):
        for name in ("a", "b", "c"):
            handler.dispatch(FileDeletedEvent(harness.p(f"pkg/{name}.py")))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and warnings[0].exc_info
        now[0] += manager_module.DISPATCH_WARNING_INTERVAL_S
        handler.dispatch(FileDeletedEvent(harness.p("pkg/d.py")))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2 and "3 more" in warnings[1].getMessage() and "boom" in warnings[1].getMessage()
    assert len(requests) == 4 and all(r[0] == "repo" and r[3] == "retry" for r in requests)

    monkeypatch.setattr(handler, "_queue_rel", real)
    batches = []
    handler._on_changes = lambda repo_id, changed, deleted: batches.append(deleted)
    handler.dispatch(FileDeletedEvent(harness.p("pkg/e.py")))
    handler.flush()
    assert batches == [{root / "pkg/e.py"}]


def test_a_raising_handler_leaves_a_real_observer_alive(root, monkeypatch):
    import time

    from watchdog.observers import Observer

    calls = []

    def boom(path):
        calls.append(path)
        raise RuntimeError("boom")

    harness = Harness(root)
    monkeypatch.setattr(harness.handler, "_is_tracked_path", boom)
    observer = Observer()
    observer.schedule(harness.handler, str(root), recursive=True)
    observer.start()
    try:
        _write(root / "pkg/x.py")
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls
        time.sleep(0.2)
        assert observer.is_alive()
    finally:
        observer.stop()
        observer.join(timeout=5)


@pytest.mark.parametrize(
    ("ignores", "rel"),
    [({"pkg/.gitignore": "*\n"}, "pkg/.gitignore"), ({".gitignore": ".*\n"}, ".gitignore")],
)
def test_a_gitignore_that_ignores_itself_is_still_queued(h, root, ignores, rel):
    for path, text in ignores.items():
        _write(root / path, text)
    h.send(FileModifiedEvent(h.p(rel)))
    assert h.one_batch() == ({rel}, set())
    h.batches.clear()
    (root / rel).unlink()
    h.send(FileDeletedEvent(h.p(rel)))
    assert h.one_batch() == (set(), {rel})


def test_a_gitignore_inside_an_ignored_folder_is_not_queued(h, root):
    _write(root / ".gitignore", ".mypy_cache/\n")
    _write(root / ".mypy_cache/.gitignore", "*\n")
    h.send(FileModifiedEvent(h.p(".mypy_cache/.gitignore")))
    h.handler.flush()
    assert h.batches == []
