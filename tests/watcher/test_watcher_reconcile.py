"""WatcherManager tests against the real OS watcher, without Neo4j (spec W1-W4).

The debounce timer is a FakeTimer, so batches are drained with `flush()`.
Reconcile timers (interval 0) are real threads. Every wait polls against a
deadline; no test counts batches.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager

DEADLINE_S = 10.0


class FakeTimer:
    def __init__(self, interval: float, function) -> None:
        self.interval = interval
        self.function = function
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.function()


def wait_for(predicate, what, timeout: float = DEADLINE_S) -> None:
    """`what` is a message, or a callable building one once the wait has
    timed out (so it reads the state then, not before the wait)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")


class Rig:
    """A registered repo, a WatcherManager over it, and the batches it delivers."""

    def __init__(self, tmp_path: Path, *, real_reconcile_timers: bool = True) -> None:
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "pkg/sub").mkdir(parents=True)
        (self.root / "pkg/a.py").write_text("a = 1\n")
        (self.root / "pkg/sub/b.py").write_text("b = 1\n")
        self.root = self.root.resolve()
        self.registry = RepoRegistry(tmp_path / "registry.db")
        self.repo_id = self.registry.add_repo(self.root).repo_id
        self.batches: list[tuple[set[str], set[str]]] = []
        self.debounce_timers: list[FakeTimer] = []
        self.reconcile_timers: list[FakeTimer] = []
        self._batches_lock = threading.Lock()

        def factory(interval: float, function):
            if interval == 0:
                if real_reconcile_timers:
                    return threading.Timer(0, function)
                timer = FakeTimer(interval, function)
                self.reconcile_timers.append(timer)
                return timer
            timer = FakeTimer(interval, function)
            self.debounce_timers.append(timer)
            return timer

        def on_changes(repo_id: str, changed: set[Path], deleted: set[Path]) -> None:
            with self._batches_lock:
                self.batches.append((self.rel(changed), self.rel(deleted)))

        self.manager = WatcherManager(
            self.registry, on_changes, timer_factory=factory, reconcile_delay_s=0
        )

    def rel(self, paths) -> set[str]:
        return {Path(p).relative_to(self.root).as_posix() for p in paths}

    def flush(self) -> None:
        handler = self.manager._handlers.get(self.repo_id)
        if handler is not None:
            handler.flush()

    def union(self, start: int = 0) -> tuple[set[str], set[str]]:
        self.flush()
        changed: set[str] = set()
        deleted: set[str] = set()
        with self._batches_lock:
            for c, d in self.batches[start:]:
                changed |= c
                deleted |= d
        return changed, deleted

    def wait_exact(self, changed=(), deleted=(), start: int = 0, maybe_deleted=()) -> None:
        """Wait until the batches since `start`, with reconciles idle, add up
        to exactly these changed and deleted sets. `maybe_deleted` may also be
        deleted: events the kernel reports or not, depending on timing."""
        changed, deleted, maybe = set(changed), set(deleted), set(maybe_deleted)

        def ok() -> bool:
            if not self.reconcile_idle():
                return False
            c, d = self.union(start)
            return c == changed and deleted <= d <= deleted | maybe

        wait_for(ok, lambda: f"exactly ({changed}, {deleted} + some of {maybe}); got {self.union(start)}")

    def watched(self) -> dict[str, bool]:
        """Top-level directory watches: name -> emitter alive."""
        with self.manager._lock:  # the reconcile changes _watches under it
            observer = self.manager._observers[self.repo_id]
            watches = list(self.manager._watches[self.repo_id].items())
        out = {}
        for path, (watch, _identity) in watches:
            emitter = observer._emitter_for_watch.get(watch)
            out[path.relative_to(self.root).as_posix()] = bool(emitter and emitter.is_alive())
        return out

    def reconcile_idle(self) -> bool:
        with self.manager._reconcile_lock:
            return (
                self.repo_id not in self.manager._reconcile_pending
                and self.repo_id not in self.manager._reconcile_running
            )

    def settle_watches(self, expected: set[str]) -> None:
        wait_for(
            lambda: self.reconcile_idle()
            and set(self.watched()) == expected
            and all(self.watched().values()),
            lambda: f"watches == {expected}, all alive; got {self.watched()}",
        )

    def close(self) -> None:
        self.manager.stop()
        self.registry.close()


@pytest.fixture
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_batch_lock_survives_recreation(rig):
    rig.manager.start()
    lock = rig.manager._handlers[rig.repo_id]._batch_lock
    assert rig.manager.run_exclusive(rig.repo_id, lock.locked) is True
    rig.manager.stop()
    rig.manager.start()
    assert rig.manager._handlers[rig.repo_id]._batch_lock is lock
    assert rig.manager.run_exclusive(rig.repo_id, lock.locked) is True
    assert not lock.locked()


def test_file_rename_and_nested_dir_move(rig):
    rig.manager.start()
    (rig.root / "pkg/a.py").rename(rig.root / "pkg/a2.py")
    rig.wait_exact(changed={"pkg/a2.py"}, deleted={"pkg/a.py"})
    start = len(rig.batches)
    (rig.root / "pkg/sub").rename(rig.root / "pkg/sub2")
    rig.wait_exact(changed={"pkg/sub2/b.py"}, deleted={"pkg/sub"}, start=start)


def test_folder_moved_out_of_repo_is_deleted(rig, tmp_path):
    rig.manager.start()
    outside = tmp_path / "trash"
    outside.mkdir()
    shutil.move(str(rig.root / "pkg/sub"), str(outside / "sub"))
    rig.wait_exact(deleted={"pkg/sub"})


def test_top_level_delete_recreate_then_edit(rig):
    """C2: the dead emitter for a deleted top-level dir is replaced."""
    rig.manager.start()
    shutil.rmtree(rig.root / "pkg")
    (rig.root / "pkg").mkdir()
    (rig.root / "pkg/a.py").write_text("a = 2\n")
    rig.settle_watches({"pkg"})
    # Children are deleted before their folder, so each that inotify reports
    # before the watch dies is queued on its own; the old pkg/a.py's delete
    # can land in a batch before its recreation.
    rig.wait_exact(
        changed={"pkg/a.py"}, deleted={"pkg"}, maybe_deleted={"pkg/sub", "pkg/sub/b.py", "pkg/a.py"}
    )
    start = len(rig.batches)
    (rig.root / "pkg/a.py").write_text("a = 3\n")
    (rig.root / "pkg/after.py").write_text("after = 1\n")
    rig.wait_exact(changed={"pkg/a.py", "pkg/after.py"}, start=start)


def test_top_level_rename_then_edit(rig):
    rig.manager.start()
    (rig.root / "pkg").rename(rig.root / "lib")
    rig.wait_exact(changed={"lib/a.py", "lib/sub/b.py"}, deleted={"pkg"})
    rig.settle_watches({"lib"})
    start = len(rig.batches)
    (rig.root / "lib/a.py").write_text("a = 2\n")
    (rig.root / "lib/after.py").write_text("after = 1\n")
    rig.wait_exact(changed={"lib/a.py", "lib/after.py"}, start=start)


def test_new_top_level_dir_is_watched_and_walked(rig):
    rig.manager.start()
    (rig.root / "newtop").mkdir()
    (rig.root / "newtop/c.py").write_text("c = 1\n")
    rig.wait_exact(changed={"newtop/c.py"})
    rig.settle_watches({"pkg", "newtop"})
    start = len(rig.batches)
    (rig.root / "newtop/c.py").write_text("c = 2\n")
    rig.wait_exact(changed={"newtop/c.py"}, start=start)


def test_top_level_swap_then_edit(rig):
    """`mv pkg old; mv other pkg`: the pkg watch's emitter is alive and its
    path still exists, but it watches the folder now called `old`."""
    (rig.root / "other").mkdir()
    (rig.root / "other/c.py").write_text("c = 1\n")
    rig.manager.start()
    (rig.root / "pkg").rename(rig.root / "old")
    (rig.root / "other").rename(rig.root / "pkg")
    rig.settle_watches({"old", "pkg"})
    rig.wait_exact(
        changed={"old/a.py", "old/sub/b.py", "pkg/c.py"},
        deleted={"pkg", "other"},
    )
    start = len(rig.batches)
    (rig.root / "pkg/c.py").write_text("c = 2\n")
    rig.wait_exact(changed={"pkg/c.py"}, start=start)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_top_level_dir_is_never_scheduled(rig, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (rig.root / "linked").symlink_to(outside, target_is_directory=True)
    rig.manager.start()
    assert set(rig.watched()) == {"pkg"}
    rig.manager._request_reconcile(rig.repo_id)
    rig.settle_watches({"pkg"})


def test_outside_junction_is_never_scheduled(rig, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = rig.root / "jout"
    inside_junction = rig.root / "jin"
    junction.mkdir()
    inside_junction.mkdir()
    real_is_junction = Path.is_junction
    real_resolve = Path.resolve

    def is_junction(self):
        return self in (junction, inside_junction) or real_is_junction(self)

    def resolve(self, strict=False):
        if self == junction:
            return outside
        if self == inside_junction:
            return rig.root / "pkg"
        return real_resolve(self, strict)

    monkeypatch.setattr(Path, "is_junction", is_junction)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert junction not in rig.manager._desired_dirs(rig.root)
    assert inside_junction in rig.manager._desired_dirs(rig.root)
    rig.manager.start()
    assert "jout" not in rig.watched()
    rig.manager._request_reconcile(rig.repo_id)
    wait_for(rig.reconcile_idle, "reconcile to finish")
    assert "jout" not in rig.watched()


def test_stop_with_reconcile_pending(tmp_path, monkeypatch):
    rig = Rig(tmp_path, real_reconcile_timers=False)
    try:
        calls = []
        real = rig.manager._desired_dirs
        monkeypatch.setattr(rig.manager, "_desired_dirs", lambda root: calls.append(root) or real(root))
        rig.manager.start()
        calls.clear()
        rig.manager._request_reconcile(rig.repo_id)
        assert len(rig.reconcile_timers) == 1
        timer = rig.reconcile_timers[0]
        rig.manager.stop()
        assert timer.cancelled
        timer.cancelled = False  # a timer that slipped past cancel()
        timer.fire()
        assert calls == []
        rig.manager._request_reconcile(rig.repo_id)
        assert len(rig.reconcile_timers) == 1, "a stopped manager scheduled a reconcile"

        # A reconcile that finds the repo gone from _observers does nothing.
        rig.manager.start()
        rig.manager._request_reconcile(rig.repo_id)
        rig.registry.disable_watch(rig.repo_id)
        rig.manager.refresh()
        calls.clear()
        rig.reconcile_timers[-1].cancelled = False
        rig.reconcile_timers[-1].fire()
        assert calls == []
    finally:
        rig.close()


def test_stop_with_reconcile_running(rig, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    real = rig.manager._desired_dirs

    def blocking(root):
        calls.append(root)
        if len(calls) > 1:  # the first call is start()'s
            entered.set()
            assert release.wait(5)
        return real(root)

    monkeypatch.setattr(rig.manager, "_desired_dirs", blocking)
    rig.manager.start()
    rig.manager._request_reconcile(rig.repo_id)
    assert entered.wait(5)
    stopper = threading.Thread(target=rig.manager.stop)
    stopper.start()
    # stop() has set _stopping and is now waiting on the reconcile's _lock.
    wait_for(lambda: rig.manager._stopping, "stop() to begin")
    assert stopper.is_alive(), "stop() did not wait for the running reconcile"
    begun = time.monotonic()
    release.set()
    stopper.join(5)
    assert not stopper.is_alive(), "stop() hung behind a running reconcile"
    assert time.monotonic() - begun < 5
    count = len(calls)
    rig.manager._request_reconcile(rig.repo_id)
    wait_for(rig.reconcile_idle, "reconcile state to clear")
    assert len(calls) == count


def test_stop_cancels_pending_debounce(rig):
    rig.manager.start()
    (rig.root / "pkg/a.py").write_text("a = 2\n")
    handler = rig.manager._handlers[rig.repo_id]
    wait_for(lambda: handler._debounce_timer is not None, "a pending debounce")
    timer = handler._debounce_timer
    rig.manager.stop()
    assert timer.cancelled
    timer.fire()
    assert rig.batches == []


def test_reconcile_spanning_stop_start_queues_nothing_into_old_handler(rig, monkeypatch):
    import devgraph.watcher.manager as manager_module

    entered = threading.Event()
    release = threading.Event()
    real_walk = manager_module.indexable_paths_under

    def blocking_walk(root, directory):
        if isinstance(threading.current_thread(), threading.Timer):  # the reconcile
            entered.set()
            assert release.wait(5)
        return real_walk(root, directory)

    rig.manager.start()
    old = rig.manager._handlers[rig.repo_id]
    monkeypatch.setattr(manager_module, "indexable_paths_under", blocking_walk)
    (rig.root / "newtop").mkdir()
    (rig.root / "newtop/c.py").write_text("c = 1\n")
    assert entered.wait(5), "reconcile never walked the new folder"
    rig.manager.stop()
    rig.manager.start()
    release.set()
    new = rig.manager._handlers[rig.repo_id]
    assert new is not old
    wait_for(rig.reconcile_idle, "the spanning reconcile to finish")
    assert old._changed == {} and old._debounce_timer is None
    old.flush()
    assert rig.batches == []


def test_no_batch_starts_after_stop(rig):
    rig.manager.start()
    handler = rig.manager._handlers[rig.repo_id]
    (rig.root / "pkg/a.py").write_text("a = 2\n")
    wait_for(lambda: handler._changed, "a pending change")
    rig.manager.stop()
    handler.flush()  # a timer that fired just as stop() ran
    assert rig.batches == []


# --- fake observer: the reconcile's keep/replace rules and schedule retries --


class FakeEmitter:
    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class FakeObserver:
    def __init__(self) -> None:
        self._emitter_for_watch: dict = {}
        self.fail = False
        self.schedule_calls: list[str] = []
        self.unscheduled: list[str] = []

    def schedule(self, handler, path, *, recursive=False):
        from watchdog.observers.api import ObservedWatch

        self.schedule_calls.append(path)
        if self.fail:
            raise OSError(28, "inotify instance limit reached")
        watch = ObservedWatch(path, recursive=recursive)
        self._emitter_for_watch[watch] = FakeEmitter()
        return watch

    def unschedule(self, watch) -> None:
        del self._emitter_for_watch[watch]
        self.unscheduled.append(watch.path)


class FakeRig:
    """A WatcherManager whose one repo is wired to a FakeObserver; every timer
    is a FakeTimer, fired by hand."""

    def __init__(self, tmp_path: Path, dirs=("pkg",)) -> None:
        from devgraph.watcher.manager import _RepoEventHandler

        self.root = tmp_path / "repo"
        for name in dirs:
            (self.root / name).mkdir(parents=True)
        self.timers: list[FakeTimer] = []

        def factory(interval, function):
            timer = FakeTimer(interval, function)
            self.timers.append(timer)
            return timer

        self.manager = WatcherManager(None, lambda *a: None, timer_factory=factory, reconcile_delay_s=0)
        self.observer = FakeObserver()
        self.handler = _RepoEventHandler("r", self.root, 500, lambda *a: None, timer_factory=factory)
        self.manager._observers["r"] = self.observer
        self.manager._handlers["r"] = self.handler
        self.manager._watches["r"] = {}

    def watch(self, name: str):
        """Schedule `name` as start() would; returns its watch."""
        path = self.root / name
        self.manager._watches["r"][path] = self.manager._schedule_dir(self.observer, self.handler, path)
        return self.manager._watches["r"][path][0]

    def reconcile(self) -> None:
        self.manager._request_reconcile("r")
        self.fire_pending()

    def fire_pending(self) -> FakeTimer | None:
        pending = self.manager._reconcile_pending.get("r")
        if pending is not None:
            pending.fire()
        return pending

    def watched(self) -> dict[str, bool]:
        return {
            p.name: self.observer._emitter_for_watch[w].is_alive()
            for p, (w, _) in self.manager._watches["r"].items()
        }


def test_dead_emitter_with_same_identity_is_rescheduled(tmp_path):
    rig = FakeRig(tmp_path)
    watch = rig.watch("pkg")
    rig.observer._emitter_for_watch[watch].alive = False
    rig.reconcile()
    assert rig.observer.unscheduled == [str(rig.root / "pkg")]
    assert rig.watched() == {"pkg": True}


def test_live_emitter_on_a_replaced_folder_is_rescheduled(tmp_path):
    rig = FakeRig(tmp_path)
    rig.watch("pkg")
    path, (watch, identity) = next(iter(rig.manager._watches["r"].items()))
    rig.manager._watches["r"][path] = (watch, (identity[0], identity[1] + 1))
    rig.reconcile()
    assert rig.observer.unscheduled == [str(rig.root / "pkg")]
    assert rig.watched() == {"pkg": True}


def test_live_emitter_on_the_same_folder_is_kept(tmp_path):
    rig = FakeRig(tmp_path)
    rig.watch("pkg")
    rig.reconcile()
    assert rig.observer.unscheduled == []
    assert rig.watched() == {"pkg": True}


def test_live_emitter_on_a_folder_deleted_and_recreated_with_the_same_inode_is_rescheduled(tmp_path):
    """rmtree then mkdir can reuse the inode, so the identity matches while the
    old emitter has not yet read its IN_DELETE_SELF; the delete itself says
    the watch is stale."""
    rig = FakeRig(tmp_path, dirs=("pkg", "lib"))
    rig.watch("pkg")
    rig.watch("lib")
    rig.manager._request_reconcile("r", "pkg")
    rig.fire_pending()
    assert rig.observer.unscheduled == [str(rig.root / "pkg")]
    assert rig.watched() == {"pkg": True, "lib": True}
    rig.reconcile()  # the name is used once
    assert rig.observer.unscheduled == [str(rig.root / "pkg")]


class OpaqueObserver(FakeObserver):
    """An observer without watchdog's private `_emitter_for_watch` (a future
    watchdog may rename it): the reconcile falls back to identity checks."""

    def __init__(self) -> None:
        super().__init__()
        self.emitters = self.__dict__.pop("_emitter_for_watch")

    def schedule(self, handler, path, *, recursive=False):
        self._emitter_for_watch = self.emitters
        try:
            return super().schedule(handler, path, recursive=recursive)
        finally:
            del self._emitter_for_watch

    def unschedule(self, watch) -> None:
        del self.emitters[watch]
        self.unscheduled.append(watch.path)


def _opaque_rig(tmp_path, dirs=("pkg",)):
    rig = FakeRig(tmp_path, dirs=dirs)
    rig.observer = rig.manager._observers["r"] = OpaqueObserver()
    return rig


def test_without_the_emitter_map_a_watch_on_the_same_folder_is_kept(tmp_path):
    rig = _opaque_rig(tmp_path)
    rig.watch("pkg")
    rig.reconcile()
    assert rig.observer.unscheduled == []
    assert set(rig.manager._watches["r"]) == {rig.root / "pkg"}


def test_without_the_emitter_map_a_replaced_or_deleted_folder_is_rescheduled(tmp_path):
    rig = _opaque_rig(tmp_path, dirs=("pkg", "lib", "old"))
    rig.watch("pkg")
    rig.watch("lib")
    rig.watch("old")
    path = rig.root / "pkg"
    watch, identity = rig.manager._watches["r"][path]
    rig.manager._watches["r"][path] = (watch, (identity[0], identity[1] + 1))
    (rig.root / "old").rmdir()
    rig.manager._request_reconcile("r", "lib")
    rig.fire_pending()
    assert sorted(rig.observer.unscheduled) == sorted(str(rig.root / n) for n in ("pkg", "lib", "old"))
    assert set(rig.manager._watches["r"]) == {rig.root / "pkg", rig.root / "lib"}


def test_failed_schedule_retries_with_backoff_then_succeeds(tmp_path):
    rig = FakeRig(tmp_path)
    rig.observer.fail = True
    rig.reconcile()
    assert rig.watched() == {}
    retry = rig.manager._reconcile_pending["r"]
    assert retry.interval == 0.5
    rig.observer.fail = False
    rig.fire_pending()
    assert rig.watched() == {"pkg": True}
    assert "r" not in rig.manager._reconcile_pending


def test_failed_schedule_stops_at_the_cap(tmp_path):
    from devgraph.watcher import manager as manager_module

    names = [f"d{i}" for i in range(6)]
    rig = FakeRig(tmp_path, dirs=names)
    rig.observer.fail = True
    rig.reconcile()
    per_run = manager_module.MAX_SCHEDULE_FAILURES_PER_RECONCILE
    assert len(rig.observer.schedule_calls) == per_run
    intervals = []
    while (timer := rig.fire_pending()) is not None and len(intervals) < 20:
        intervals.append(timer.interval)
    assert intervals == list(manager_module.RECONCILE_RETRY_DELAYS_S)
    runs = 1 + len(intervals)
    assert len(rig.observer.schedule_calls) == per_run * runs
    assert "r" not in rig.manager._reconcile_pending, "kept retrying after giving up"
    # A later top-level event starts afresh, and a working schedule watches all.
    rig.observer.fail = False
    rig.reconcile()
    assert rig.watched() == {name: True for name in names}


def test_folder_gone_before_schedule_is_skipped(tmp_path, monkeypatch):
    rig = FakeRig(tmp_path, dirs=("pkg", "gone"))
    real = rig.manager._desired_dirs
    monkeypatch.setattr(
        rig.manager, "_desired_dirs",
        lambda root: real(root) | {root / "never-there"},
    )
    rig.reconcile()
    assert str(rig.root / "never-there") not in rig.observer.schedule_calls
    assert set(rig.watched()) == {"pkg", "gone"}
    assert "r" not in rig.manager._reconcile_pending


# --- catch-up scheduling (W5, W6, W7): every timer a FakeTimer --------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from watchdog.events import (  # noqa: E402
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from devgraph.watcher.manager import GIT_LOCK_WINDOW_S  # noqa: E402

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


class CatchUpRig:
    """A registered repo watched by a real observer, with every timer fake.
    `on_catch_up` and `on_changes` record their calls; either can be replaced
    by setting `catch_up_hook` / `changes_hook`."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        (self.root / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.root / "pkg").mkdir()
        (self.root / "pkg/a.py").write_text("a = 1\n")
        self.root = self.root.resolve()
        self.registry = RepoRegistry(tmp_path / "registry.db")
        self.repo_id = self.registry.add_repo(self.root).repo_id
        self.timers: list[FakeTimer] = []
        self.catch_ups: list[tuple] = []
        self.changes: list[tuple] = []
        self.catch_up_hook = None
        self.changes_hook = None
        self.clock = 0.0

        def factory(interval, function):
            timer = FakeTimer(interval, function)
            self.timers.append(timer)
            return timer

        def on_catch_up(repo_id, since, reason="start"):
            self.catch_ups.append((repo_id, since, reason))
            if self.catch_up_hook is not None:
                return self.catch_up_hook(repo_id, since)
            return None

        def on_changes(repo_id, changed, deleted):
            self.changes.append((repo_id, changed, deleted))
            if self.changes_hook is not None:
                self.changes_hook(repo_id)

        self.manager = WatcherManager(
            self.registry,
            on_changes,
            on_catch_up=on_catch_up,
            timer_factory=factory,
            reconcile_delay_s=0,
            git_catch_up_delay_s=2.0,
            now=lambda: self.clock,
        )

    def stamp(self, when: datetime) -> None:
        self.registry.mark_indexed(self.repo_id, at=when)

    def start_timer(self) -> FakeTimer:
        """The pending start catch-up."""
        return self.manager._start_catch_ups[self.repo_id]

    def pending_catch_up(self):
        entry = self.manager._catch_up_pending.get(self.repo_id)
        return None if entry is None else entry[2]

    def handler(self):
        return self.manager._handlers[self.repo_id]

    def git(self, kind: str, name: str, when: float, dest: str | None = None) -> None:
        """Deliver a `.git/<name>` event at clock time `when`."""
        self.clock = when
        handler = self.manager._git_handlers[self.repo_id]
        path = str(self.root / ".git" / name)
        if kind == "created":
            handler.on_created(FileCreatedEvent(path))
        elif kind == "deleted":
            handler.on_deleted(FileDeletedEvent(path))
        elif kind == "modified":
            handler.on_modified(FileModifiedEvent(path))
        else:
            handler.on_moved(FileMovedEvent(path, str(self.root / ".git" / dest)))

    def fire_git_burst(self) -> None:
        self.manager._git_handlers[self.repo_id]._debounce_timer.fire()

    def close(self) -> None:
        self.manager.stop()
        self.registry.close()


@pytest.fixture
def cu(tmp_path: Path):
    r = CatchUpRig(tmp_path)
    yield r
    r.close()


def run_in_thread(fn) -> threading.Thread:
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def test_a_never_indexed_repo_gets_no_catch_up(cu, caplog):
    cu.manager.start()
    with caplog.at_level("INFO", logger="devgraph.watcher.manager"):
        cu.start_timer().fire()
    assert cu.catch_ups == []
    assert f'DevGraph hasn\'t indexed {cu.repo_id} yet; run "devgraph rescan {cu.repo_id}"' in caplog.messages


def test_since_is_snapshotted_before_the_watch_starts(cu):
    cu.stamp(T0)
    cu.manager.start()
    cu.changes_hook = lambda repo_id: cu.stamp(T0 + timedelta(minutes=1))
    cu.handler().queue_changed({cu.root / "pkg/a.py"})
    cu.handler()._debounce_timer.fire()  # a live batch wins the lock first
    assert cu.registry.get(cu.repo_id).last_indexed == (T0 + timedelta(minutes=1)).isoformat()
    cu.start_timer().fire()
    assert cu.catch_ups == [(cu.repo_id, T0, "start")]


def test_pause_then_resume_with_a_debounce_pending(cu):
    cu.stamp(datetime.now(timezone.utc) - timedelta(minutes=1))
    cu.manager.start()
    cu.start_timer().fire()
    edited = datetime.now(timezone.utc)
    (cu.root / "pkg/a.py").write_text("a = 2\n")
    cu.handler().queue_changed({cu.root / "pkg/a.py"})
    old = cu.handler()
    cu.manager.stop()
    old.flush()
    assert cu.changes == []
    cu.manager.start()
    cu.start_timer().fire()
    assert len(cu.catch_ups) == 2 and cu.catch_ups[1][1] <= edited


def test_live_batches_wait_for_a_running_catch_up(cu):
    cu.stamp(T0)
    cu.manager.start()
    release, entered = threading.Event(), threading.Event()
    active, peak = [0], [0]
    count_lock = threading.Lock()

    def track(block: bool):
        with count_lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if block:
            entered.set()
            assert release.wait(5)
        with count_lock:
            active[0] -= 1

    cu.catch_up_hook = lambda repo_id, since: track(True)
    cu.changes_hook = lambda repo_id: track(False)
    catching = run_in_thread(cu.start_timer().fire)
    assert entered.wait(5)
    cu.handler().queue_changed({cu.root / "pkg/a.py"})
    batch = run_in_thread(cu.handler()._debounce_timer.fire)
    batch.join(0.2)
    assert batch.is_alive() and cu.changes == []
    release.set()
    catching.join(5)
    batch.join(5)
    assert len(cu.changes) == 1 and peak[0] == 1


def test_registration_under_the_lock_then_the_catch_up_sees_its_stamp(cu, caplog):
    cu.manager.start()  # snapshot: never indexed
    registering, release = threading.Event(), threading.Event()

    def register():
        registering.set()
        assert release.wait(5)
        cu.stamp(T0)

    reg = run_in_thread(lambda: cu.manager.run_exclusive(cu.repo_id, register))
    assert registering.wait(5)
    with caplog.at_level("INFO", logger="devgraph.watcher.manager"):
        catching = run_in_thread(cu.start_timer().fire)
        catching.join(0.2)
        assert catching.is_alive()
        release.set()
        reg.join(5)
        catching.join(5)
    assert cu.catch_ups == [(cu.repo_id, T0, "start")]
    assert not [m for m in caplog.messages if "hasn't indexed" in m]


def test_request_catch_up_never_blocks_on_the_batch_lock_or_the_manager_lock(cu):
    cu.stamp(T0)
    cu.manager.start()
    done = []
    t = run_in_thread(
        lambda: cu.manager.run_exclusive(
            cu.repo_id, lambda: (cu.manager.request_catch_up(cu.repo_id, T0, 30), done.append(1))
        )
    )
    t.join(1)
    assert done == [1]

    with cu.manager._lock:
        t = run_in_thread(lambda: (cu.manager.request_catch_up(cu.repo_id, T0, 30), done.append(2)))
        t.join(1)
        assert done == [1, 2]


def test_requests_coalesce_to_the_earliest_since_and_run_exclusively(cu):
    cu.stamp(T0)
    cu.manager.start()
    cu.manager.request_catch_up(cu.repo_id, T0 + timedelta(minutes=2), 30)
    timer = cu.pending_catch_up()
    cu.manager.request_catch_up(cu.repo_id, T0 + timedelta(minutes=1), 30)
    assert cu.pending_catch_up() is timer and timer.interval == 30
    holding = threading.Event()
    release = threading.Event()
    holder = run_in_thread(lambda: cu.manager.run_exclusive(cu.repo_id, lambda: (holding.set(), release.wait(5))))
    assert holding.wait(5)
    firing = run_in_thread(timer.fire)
    firing.join(0.2)
    assert firing.is_alive() and cu.catch_ups == []
    release.set()
    holder.join(5)
    firing.join(5)
    assert cu.catch_ups == [(cu.repo_id, T0 + timedelta(minutes=1), "retry")]
    assert cu.pending_catch_up() is None


def test_stop_cancels_a_pending_catch_up(cu):
    cu.stamp(T0)
    cu.manager.start()
    cu.manager.request_catch_up(cu.repo_id, T0, 30)
    timer = cu.pending_catch_up()
    cu.manager.stop()
    assert timer.cancelled
    timer.function()  # a timer that fired just as stop() ran
    assert cu.catch_ups == []
    cu.manager.request_catch_up(cu.repo_id, T0, 30)
    assert cu.pending_catch_up() is None


def test_a_git_burst_catches_up_from_the_lock_that_preceded_it(cu):
    assert GIT_LOCK_WINDOW_S == 5
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("created", "index.lock", 10)
    cu.git("modified", "HEAD", 11)
    cu.git("moved", "index.lock", 11.5, dest="index")
    cu.fire_git_burst()
    timer = cu.pending_catch_up()
    assert timer.interval == 2.0 and cu.catch_ups == []
    timer.fire()
    assert cu.catch_ups == [(cu.repo_id, at(10), "git")]


def test_two_bursts_within_the_delay_coalesce(cu):
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("modified", "refs/heads/main", 30)
    cu.fire_git_burst()
    cu.git("created", "index.lock", 10)
    cu.git("modified", "HEAD", 11)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(10), "git")]


def test_a_ref_only_burst_uses_last_indexed(cu):
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("modified", "refs/heads/main", 30)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(20), "git")]


def test_an_old_git_status_lock_is_ignored(cu):
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("created", "index.lock", 0)
    cu.git("deleted", "index.lock", 0.1)
    cu.git("modified", "HEAD", 60)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(20), "git")]


def test_a_long_checkout_counts_from_its_lock(cu):
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("created", "index.lock", 0)
    cu.git("deleted", "index.lock", 40)
    cu.git("modified", "HEAD", 41)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(0), "git")]


def test_a_lock_after_head_is_ignored(cu):
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("modified", "HEAD", 5)
    cu.git("created", "index.lock", 6)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(20), "git")]


def test_stop_returns_while_a_catch_up_runs_and_its_failure_is_quiet(cu, caplog, monkeypatch):
    from devgraph.agent import sync as sync_module
    from devgraph.agent.sync import RepoSync

    entered, release = threading.Event(), threading.Event()

    def blocking_catch_up(*a, **k):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("driver closed")

    monkeypatch.setattr(sync_module, "catch_up", blocking_catch_up)
    repo_sync = RepoSync(None, cu.registry, lambda event: None, lambda *a: None)
    cu.catch_up_hook = lambda repo_id, since: repo_sync.on_catch_up(repo_id, since)
    cu.stamp(T0)
    cu.manager.start()
    catching = run_in_thread(cu.start_timer().fire)
    assert entered.wait(5)
    began = time.monotonic()
    repo_sync.stopping = True
    cu.manager.stop()
    assert time.monotonic() - began < 5
    with caplog.at_level("DEBUG", logger="devgraph.agent.sync"):
        release.set()
        catching.join(5)
    assert not [r for r in caplog.records if r.levelno >= 30]


def test_a_shorter_request_replaces_a_pending_longer_one(cu):
    cu.stamp(T0)
    cu.manager.start()
    cu.manager.request_catch_up(cu.repo_id, T0 + timedelta(minutes=1), 30)
    slow = cu.pending_catch_up()
    cu.manager.request_catch_up(cu.repo_id, T0 + timedelta(minutes=2), 0)
    fast = cu.pending_catch_up()
    assert slow.cancelled and fast is not slow and fast.interval == 0
    cu.manager.request_catch_up(cu.repo_id, T0, 30)  # a longer one coalesces
    assert cu.pending_catch_up() is fast
    fast.fire()
    assert cu.catch_ups == [(cu.repo_id, T0, "retry")]


def test_a_start_catch_up_waiting_on_the_lock_does_not_run_after_pause(cu):
    cu.stamp(T0)
    cu.manager.start()
    holding, release = threading.Event(), threading.Event()
    holder = run_in_thread(lambda: cu.manager.run_exclusive(cu.repo_id, lambda: (holding.set(), release.wait(5))))
    assert holding.wait(5)
    catching = run_in_thread(cu.start_timer().fire)
    catching.join(0.2)
    assert catching.is_alive()
    cu.manager.stop()
    release.set()
    holder.join(5)
    catching.join(5)
    assert cu.catch_ups == []


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_stash_pop_triggers_a_post_git_catch_up(tmp_path):
    root = (tmp_path / "repo")
    (root / "pkg").mkdir(parents=True)
    root = root.resolve()
    (root / "pkg" / "a.py").write_text("a = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "pkg" / "a.py").write_text("a = 2\n")
    _git(root, "stash", "-q")
    registry = RepoRegistry(tmp_path / "registry.db")
    repo_id = registry.add_repo(root).repo_id
    registry.mark_indexed(repo_id, at=T0)
    timers: list[FakeTimer] = []

    def factory(interval, function):
        timer = FakeTimer(interval, function)
        timers.append(timer)
        return timer

    manager = WatcherManager(
        registry, lambda *a: None, on_catch_up=lambda *a: None, timer_factory=factory, reconcile_delay_s=0
    )
    manager.start()
    try:
        git_handler = manager._git_handlers[repo_id]
        _git(root, "stash", "pop", "-q")  # moves no HEAD and no branch: only refs/stash
        wait_for(lambda: git_handler._debounce_timer is not None, "a git burst from stash pop")
        git_handler._debounce_timer.fire()
        entry = manager._catch_up_pending.get(repo_id)
        assert entry is not None and entry[1] == "git"
    finally:
        manager.stop()
        registry.close()


def test_the_git_sync_runs_after_the_post_git_catch_up_under_the_lock(cu):
    order: list[str] = []
    cu.manager._on_git_state_changed = lambda repo_id: order.append(
        "sync, locked" if cu.manager._batch_locks[repo_id].locked() else "sync, unlocked"
    )
    cu.catch_up_hook = lambda repo_id, since: order.append("catch-up")
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("modified", "HEAD", 30)
    cu.fire_git_burst()
    assert order == []  # nothing until the delayed catch-up
    cu.pending_catch_up().fire()
    assert order == ["catch-up", "sync, locked"]


def test_a_retry_that_a_git_burst_joined_also_syncs(cu):
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.stamp(at(20))
    cu.manager.start()
    cu.manager.request_catch_up(cu.repo_id, at(5), 30)
    cu.git("modified", "HEAD", 30)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert cu.catch_ups == [(cu.repo_id, at(5), "git")] and synced == [cu.repo_id]


def test_a_never_indexed_repo_syncs_git_history_at_once(cu):
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.manager.start()
    cu.git("modified", "HEAD", 30)
    cu.fire_git_burst()
    assert synced == [cu.repo_id] and cu.pending_catch_up() is None


def _ordered_sync(cu) -> list[str]:
    order: list[str] = []
    cu.manager._on_git_state_changed = lambda repo_id: order.append(
        "sync, locked" if cu.manager._batch_locks[repo_id].locked() else "sync, unlocked"
    )
    cu.catch_up_hook = lambda repo_id, since: order.append("catch-up")
    return order


def test_the_start_catch_up_syncs_git_history_after_it_under_the_lock(cu):
    """A commit whose post-git job was cancelled (stop or pause within the
    delay), or made while the agent was off, is synced when watching starts."""
    order = _ordered_sync(cu)
    cu.stamp(T0)
    cu.manager.start()
    cu.start_timer().fire()
    assert order == ["catch-up", "sync, locked"]


def test_resume_syncs_git_history_again(cu):
    order = _ordered_sync(cu)
    cu.stamp(T0)
    cu.manager.start()
    cu.start_timer().fire()
    cu.manager.stop()
    cu.manager.start()
    cu.start_timer().fire()
    assert order == ["catch-up", "sync, locked"] * 2


def test_a_cancelled_post_git_job_is_synced_on_the_next_start(cu):
    order = _ordered_sync(cu)
    cu.stamp(at(20))
    cu.manager.start()
    cu.start_timer().fire()
    cu.git("modified", "HEAD", 30)
    cu.fire_git_burst()
    pending = cu.pending_catch_up()
    cu.manager.stop()
    pending.function()  # fired just as stop() ran
    assert order == ["catch-up", "sync, locked"]
    cu.manager.start()
    cu.start_timer().fire()
    assert order == ["catch-up", "sync, locked"] * 2


def test_a_retry_catch_up_syncs_git_history(cu):
    """A retry repairs a failed post-git catch-up, whose sync was skipped."""
    order = _ordered_sync(cu)
    cu.stamp(T0)
    cu.manager.start()
    cu.manager.request_catch_up(cu.repo_id, T0, 30)
    cu.pending_catch_up().fire()
    assert order == ["catch-up", "sync, locked"]


def test_a_failed_catch_up_skips_the_sync(cu):
    """Recency only annotates nodes that exist, and a fast-mode sync never
    looks at its commits again: it waits for the retry instead."""
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.catch_up_hook = lambda repo_id, since: False
    cu.stamp(at(20))
    cu.manager.start()
    cu.start_timer().fire()
    cu.git("modified", "HEAD", 30)
    cu.fire_git_burst()
    cu.pending_catch_up().fire()
    assert len(cu.catch_ups) == 2 and synced == []


def test_a_repo_without_a_git_folder_never_syncs(cu):
    shutil.rmtree(cu.root / ".git")
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.stamp(T0)
    cu.manager.start()
    cu.start_timer().fire()
    cu.manager.request_catch_up(cu.repo_id, T0, 30)
    cu.pending_catch_up().fire()
    assert len(cu.catch_ups) == 2 and synced == []


def test_a_never_indexed_repo_does_not_sync_on_start(cu):
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.manager.start()
    cu.start_timer().fire()
    assert cu.catch_ups == [] and synced == []


def test_stop_cancels_a_pending_git_burst(cu):
    """The next start's catch-up and sync cover it; firing after stop would
    read a registry the agent may have closed, and sync outside the lock."""
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.stamp(at(20))
    cu.manager.start()
    cu.git("modified", "HEAD", 30)
    handler = cu.manager._git_handlers[cu.repo_id]
    burst = handler._debounce_timer
    cu.manager.stop()
    assert burst.cancelled
    burst.function()  # a timer that fired just as stop() ran
    assert synced == [] and cu.pending_catch_up() is None


def test_a_burst_after_stop_neither_syncs_nor_catches_up(cu):
    synced: list[str] = []
    cu.manager._on_git_state_changed = synced.append
    cu.stamp(at(20))
    cu.manager.start()
    cu.manager.stop()
    cu.manager._on_git_burst(cu.repo_id, None)
    assert synced == [] and cu.pending_catch_up() is None
