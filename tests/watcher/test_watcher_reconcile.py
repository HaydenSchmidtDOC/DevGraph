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

    def wait_union(self, changed=(), deleted=(), start: int = 0) -> None:
        def ok() -> bool:
            c, d = self.union(start)
            return set(changed) <= c and set(deleted) <= d

        wait_for(ok, f"changed>={set(changed)} deleted>={set(deleted)}; got {self.union(start)}")

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
    rig.wait_union(changed={"pkg/a2.py"}, deleted={"pkg/a.py"})
    (rig.root / "pkg/sub").rename(rig.root / "pkg/sub2")
    rig.wait_union(changed={"pkg/sub2/b.py"}, deleted={"pkg/sub"})


def test_folder_moved_out_of_repo_is_deleted(rig, tmp_path):
    rig.manager.start()
    outside = tmp_path / "trash"
    outside.mkdir()
    shutil.move(str(rig.root / "pkg/sub"), str(outside / "sub"))
    rig.wait_union(deleted={"pkg/sub"})


def test_top_level_delete_recreate_then_edit(rig):
    """C2: the dead emitter for a deleted top-level dir is replaced."""
    rig.manager.start()
    shutil.rmtree(rig.root / "pkg")
    (rig.root / "pkg").mkdir()
    (rig.root / "pkg/a.py").write_text("a = 2\n")
    rig.settle_watches({"pkg"})
    rig.wait_union(changed={"pkg/a.py"}, deleted={"pkg"})
    start = len(rig.batches)
    (rig.root / "pkg/a.py").write_text("a = 3\n")
    (rig.root / "pkg/after.py").write_text("after = 1\n")
    rig.wait_union(changed={"pkg/a.py", "pkg/after.py"}, start=start)


def test_top_level_rename_then_edit(rig):
    rig.manager.start()
    (rig.root / "pkg").rename(rig.root / "lib")
    rig.wait_union(changed={"lib/a.py", "lib/sub/b.py"}, deleted={"pkg"})
    rig.settle_watches({"lib"})
    start = len(rig.batches)
    (rig.root / "lib/a.py").write_text("a = 2\n")
    (rig.root / "lib/after.py").write_text("after = 1\n")
    rig.wait_union(changed={"lib/a.py", "lib/after.py"}, start=start)
    changed, deleted = rig.union(start)
    assert not any(p.startswith("pkg/") for p in changed | deleted), (changed, deleted)


def test_new_top_level_dir_is_watched_and_walked(rig):
    rig.manager.start()
    (rig.root / "newtop").mkdir()
    (rig.root / "newtop/c.py").write_text("c = 1\n")
    rig.wait_union(changed={"newtop/c.py"})
    rig.settle_watches({"pkg", "newtop"})
    start = len(rig.batches)
    (rig.root / "newtop/c.py").write_text("c = 2\n")
    rig.wait_union(changed={"newtop/c.py"}, start=start)


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
    begun = time.monotonic()
    stopper.start()
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
