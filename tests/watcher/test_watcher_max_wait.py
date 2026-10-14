"""The debounce has a maximum wait (round-3 audit, item 11): a batch flushes
at least every `watch_max_wait_ms` while events keep coming, instead of
waiting for a pause that a file written every few hundred milliseconds
never gives.

The handlers run on simulated time: a FakeTimer records when it would fire,
and `run` advances the clock, queueing an event every `every_s` and firing
whichever timer is due.
"""

from __future__ import annotations

import subprocess
import threading
import time

from pathlib import Path

from watchdog.events import FileModifiedEvent

from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager, _GitStateEventHandler, _RepoEventHandler


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeTimer:
    def __init__(self, clock: Clock, interval: float, function) -> None:
        self.due = clock.t + interval
        self.function = function
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True


def run(clock: Clock, timers: list[FakeTimer], event, *, every_s: float, for_s: float) -> None:
    """Call `event` every `every_s` for `for_s`, then let the last timer fire."""
    step = 0.01
    next_event = clock.t
    end = clock.t + for_s
    while clock.t < end + 2.0:
        if clock.t < end and clock.t >= next_event:
            event()
            next_event += every_s
        for timer in list(timers):
            if not timer.cancelled and clock.t >= timer.due:
                timer.cancelled = True
                timer.function()
        clock.t += step


def test_continuous_writes_flush_a_batch_at_least_every_max_wait(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n")
    clock, timers, fired = Clock(), [], []
    handler = _RepoEventHandler(
        "repo", tmp_path, 500,
        lambda _r, changed, _d: fired.append(clock.t),
        timer_factory=lambda interval, fn: timers.append(FakeTimer(clock, interval, fn)) or timers[-1],
        clock=clock,
        max_wait_ms=5000,
    )
    start = clock.t
    run(clock, timers, lambda: handler.dispatch(FileModifiedEvent(str(tmp_path / "a.py"))), every_s=0.3, for_s=12)
    # Batches at about 5 s and 10 s while writes continue, then one after them.
    gaps = [b - a for a, b in zip([start, *fired], fired)]
    assert len(fired) == 3, fired
    assert all(gap <= 5.0 + 0.3 + 0.02 for gap in gaps), gaps  # from the first event after the last batch


def test_a_lone_write_still_waits_only_the_debounce(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n")
    clock, timers, fired = Clock(), [], []
    handler = _RepoEventHandler(
        "repo", tmp_path, 500,
        lambda _r, changed, _d: fired.append(clock.t),
        timer_factory=lambda interval, fn: timers.append(FakeTimer(clock, interval, fn)) or timers[-1],
        clock=clock,
        max_wait_ms=5000,
    )
    start = clock.t
    run(clock, timers, lambda: handler.dispatch(FileModifiedEvent(str(tmp_path / "a.py"))), every_s=10, for_s=0.1)
    assert len(fired) == 1 and abs(fired[0] - start - 0.5) < 0.02, fired


def test_a_git_burst_waits_for_its_events_to_stop(tmp_path):
    """No maximum wait for git state: a rebase moves refs for as long as it
    runs, and a catch-up in the middle of it would read a half-done tree."""
    clock, timers, fired = Clock(), [], []
    handler = _GitStateEventHandler(
        "repo", 500, lambda _r, _start: fired.append(clock.t),
        timer_factory=lambda interval, fn: timers.append(FakeTimer(clock, interval, fn)) or timers[-1],
        now=clock,
    )
    start = clock.t
    run(clock, timers, lambda: handler.on_modified(FileModifiedEvent(str(tmp_path / ".git/HEAD"))), every_s=0.3, for_s=12)
    assert len(fired) == 1 and fired[0] > start + 12, fired


def test_a_file_written_every_200_ms_is_indexed_while_it_is_being_written(tmp_path, monkeypatch):
    """The real watcher, with a 1 s maximum wait: batches arrive during 3 s of
    writes, each within about the maximum wait of the last."""
    from devgraph import config

    monkeypatch.setenv("DEVGRAPH_WATCH_MAX_WAIT_MS", "1000")
    config.get_settings.cache_clear()
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "log.py").write_text("n = 0\n")
    registry = RepoRegistry(tmp_path / "registry.db")
    registry.add_repo(root)
    batches: list[float] = []
    manager = WatcherManager(registry, lambda _r, changed, _d: changed and batches.append(time.monotonic()))
    manager.start()
    try:
        time.sleep(0.3)
        start = time.monotonic()
        while time.monotonic() - start < 3.0:
            (root / "log.py").write_text(f"n = {time.monotonic()}\n")
            time.sleep(0.2)
        during = [t for t in batches if t < start + 3.0]
        assert len(during) >= 2, batches
        gaps = [b - a for a, b in zip([start, *during], during)]
        assert all(gap < 1.6 for gap in gaps), gaps  # 1 s, plus scheduling
    finally:
        manager.stop()
        registry.close()
        config.get_settings.cache_clear()


def test_events_while_a_batch_waits_for_the_batch_lock_start_no_more_timers(tmp_path):
    """A batch held up by a long job (a catch-up holding the batch lock) is in
    flight: events meanwhile join it rather than each firing a 0 s timer
    whose thread blocks on the lock too."""
    (tmp_path / "a.py").write_text("a = 1\n")
    batch_lock = threading.Lock()
    batches: list[set[Path]] = []
    handler = _RepoEventHandler(
        "repo", tmp_path, 20, lambda _r, changed, _d: batches.append(changed),
        batch_lock=batch_lock, max_wait_ms=100,
    )
    event = FileModifiedEvent(str(tmp_path / "a.py"))
    threads = threading.active_count()
    with batch_lock:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            handler.dispatch(event)
            time.sleep(0.005)
        assert threading.active_count() <= threads + 2, threading.active_count() - threads
    deadline = time.monotonic() + 5
    while not batches and time.monotonic() < deadline:
        time.sleep(0.01)
    assert batches == [{tmp_path / "a.py"}]
    handler.dispatch(event)  # the next event starts a new batch
    deadline = time.monotonic() + 5
    while len(batches) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(batches) == 2
