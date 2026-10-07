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


def wait_for(predicate, what: str, timeout: float = DEADLINE_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


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

        wait_for(ok, f"exactly ({changed}, {deleted} + some of {maybe}); got {self.union(start)}")

    def watched(self) -> dict[str, bool]:
        """Top-level directory watches: name -> emitter alive."""
        observer = self.manager._observers[self.repo_id]
        out = {}
        for path, (watch, _identity) in self.manager._watches[self.repo_id].items():
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
            f"watches == {expected}, all alive; got {self.watched()}",
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
    # before the watch dies is queued on its own.
    rig.wait_exact(changed={"pkg/a.py"}, deleted={"pkg"}, maybe_deleted={"pkg/sub", "pkg/sub/b.py"})
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
