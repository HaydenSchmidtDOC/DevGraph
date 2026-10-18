"""SchemaRescanScheduler debounce decisions, with stubs and a fake clock."""

import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path

from devgraph.agent import schema_rescan
from devgraph.agent.schema_rescan import SchemaRescanScheduler


@dataclass
class Repo:
    repo_id: str
    path: Path
    docs_path: str | None = None
    mentions_enabled: bool = False
    watch_enabled: bool = True
    last_indexed: str | None = "2026-10-01T00:00:00+00:00"


class Registry:
    def __init__(self, repos):
        self.repos = repos
        self.marked = []
        self.stamps = []

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)

    def mark_indexed(self, repo_id, at=None):
        self.marked.append(repo_id)
        self.stamps.append(at)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def setup(monkeypatch, tmp_path, *, valid=True):
    state = {"pending": True, "hash": "h1", "scans": [], "outdated": set()}
    monkeypatch.setattr(schema_rescan, "index_outdated", lambda e, r: r in state["outdated"])
    monkeypatch.setattr(schema_rescan, "full_scan_elsewhere", lambda e, r: state.get("elsewhere"))
    monkeypatch.setattr(schema_rescan, "schema_pending", lambda e, r, p: state["pending"])
    monkeypatch.setattr(schema_rescan, "schema_file_hash", lambda p: state["hash"])

    def resolve(path):
        if not valid:
            raise schema_rescan.ProjectSchemaError("bad")

    monkeypatch.setattr(schema_rescan, "resolve_effective_schema", resolve)

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        state["pending"] = False
        return 7

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    return state


def test_waits_for_the_quiet_period_then_rescans_once(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock, registry, done = Clock(), Registry([Repo("r", tmp_path)]), []
    sched = SchemaRescanScheduler(None, registry, on_rescanned=lambda r, n: done.append((r, n)), clock=clock)
    assert sched.run_once() == []           # first sighting starts the quiet period
    clock.now += 299
    assert sched.run_once() == []           # still quiet
    clock.now += 2
    assert sched.run_once() == ["r"]
    assert state["scans"] == ["r"] and registry.marked == ["r"] and done == [("r", 7)]
    clock.now += 1000
    assert sched.run_once() == []           # applied: nothing pending


def test_a_new_edit_restarts_the_quiet_period(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 200
    state["hash"] = "h2"
    assert sched.run_once() == []           # restarted
    clock.now += 200
    assert sched.run_once() == []           # 200 s since h2
    clock.now += 101
    assert sched.run_once() == ["r"]


def test_an_invalid_schema_is_not_retried_until_it_changes(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path, valid=False)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == [] and state["scans"] == []
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == []


def test_one_failing_repo_does_not_stop_others(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    original = schema_rescan.full_scan

    def flaky(engine, repo_id, root, **kw):
        if repo_id == "bad":
            raise RuntimeError("neo4j down")
        return original(engine, repo_id, root, **kw)

    monkeypatch.setattr(schema_rescan, "full_scan", flaky)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("bad", tmp_path), Repo("good", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == ["good"]


def test_thread_survives_failures_and_stops(monkeypatch, tmp_path):
    passes = []
    ran = threading.Event()

    class Broken:
        def list_repos(self, active_only=False):
            passes.append(1)
            if len(passes) >= 2:
                ran.set()
            raise ConnectionError("registry unavailable")

    sched = SchemaRescanScheduler(None, Broken(), interval_s=0.01)
    sched.start()
    try:
        assert sched.running and ran.wait(2)
    finally:
        sched.stop()
    assert not sched.running


def test_a_scan_that_leaves_the_repo_pending_is_not_reported_or_retried(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        return 7  # schema could not be applied: stays pending

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    clock, registry, done = Clock(), Registry([Repo("r", tmp_path)]), []
    sched = SchemaRescanScheduler(None, registry, on_rescanned=lambda r, n: done.append(r), clock=clock)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == []
    assert state["scans"] == ["r"] and registry.marked == [] and done == []
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == ["r"]  # same hash: not retried
    state["hash"] = "h2"
    sched.run_once()                         # restarts the quiet period
    clock.now += 301
    sched.run_once()
    assert state["scans"] == ["r", "r"]


def test_a_watch_disabled_repo_is_never_scanned(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path, watch_enabled=False)]), clock=clock)
    sched.run_once()
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == []


def test_pause_suspends_scans_but_quiet_clock_carries_on(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock, paused = Clock(), {"v": False}
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock, is_paused=lambda: paused["v"])
    sched.run_once()                         # first sighting
    paused["v"] = True
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == []
    paused["v"] = False
    assert sched.run_once() == ["r"] and state["scans"] == ["r"]


def test_failure_streak_warns_once(monkeypatch, tmp_path, caplog):
    setup(monkeypatch, tmp_path)

    def boom(engine, repo_id, root, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(schema_rescan, "full_scan", boom)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 301
    with caplog.at_level("DEBUG", logger="devgraph.agent.schema_rescan"):
        sched.run_once()
        sched.run_once()
        sched.run_once()
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "check failed" in r.getMessage()]
    assert len(warnings) == 1


T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


def test_the_stamp_is_the_scans_start_and_the_scan_runs_exclusively(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return T1 if state["scans"] else T0

    monkeypatch.setattr(schema_rescan, "datetime", FakeDatetime)
    exclusive = []

    def run_exclusive(repo_id, fn):
        exclusive.append(repo_id)
        assert state["scans"] == []
        result = fn()
        assert state["scans"] == [repo_id]
        return result

    clock, registry = Clock(), Registry([Repo("r", tmp_path)])
    sched = SchemaRescanScheduler(None, registry, clock=clock, run_exclusive=run_exclusive)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == ["r"]
    assert exclusive == ["r"]
    assert registry.stamps == [T0]


def test_a_full_scan_another_process_is_running_is_left_to_it(monkeypatch, tmp_path):
    """`devgraph rescan` unstamps the index while it scans: an outdated index
    with a fresh claim elsewhere is not scanned again, nor a pending schema;
    once the claim is gone (finished, or stale after a crash) it is."""
    state = setup(monkeypatch, tmp_path)
    state["outdated"] = {"r"}
    state["elsewhere"] = "4321@host/abcd1234"
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    assert sched.run_once() == []
    state["outdated"] = set()
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == []
    state["outdated"] = {"r"}
    state["elsewhere"] = None
    assert sched.run_once() == ["r"] and state["scans"] == ["r"]


def test_outdated_index_rescans_without_quiet_period(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    state["pending"] = False
    state["outdated"] = {"old"}
    calls, done = [], []

    def exclusive(repo_id, fn):
        calls.append(repo_id)
        return fn()

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        state["outdated"].discard(repo_id)
        return 7

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    registry = Registry([Repo("old", tmp_path), Repo("current", tmp_path)])
    sched = SchemaRescanScheduler(
        None, registry, on_rescanned=lambda r, n: done.append((r, n)), clock=Clock(), run_exclusive=exclusive,
    )
    assert sched.run_once() == ["old"]      # the very first pass: no quiet period
    assert calls == ["old"] and state["scans"] == ["old"]
    assert done == [("old", 7)]
    assert registry.marked == ["old"]
    assert sched.run_once() == []           # now current: untouched
    assert state["scans"] == ["old"]


def test_a_failing_upgrade_backs_off_and_resets_on_success(monkeypatch, tmp_path, caplog):
    state = setup(monkeypatch, tmp_path)
    state["pending"] = False
    state["outdated"] = {"old"}
    attempts, broken = [], {"v": True}

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        attempts.append(clock.now)
        if broken["v"]:
            raise RuntimeError("neo4j down")
        state["outdated"].discard(repo_id)
        return 7

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("old", tmp_path)]), clock=clock, interval_s=30)
    with caplog.at_level("DEBUG", logger="devgraph.agent.schema_rescan"):
        for _ in range(400):                 # passes every 30 s for 200 minutes
            sched.run_once()
            clock.now += 30
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[:6] == [30, 60, 120, 240, 480, 960]
    assert set(gaps[6:]) == {1800}           # capped at 30 minutes
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "check failed" in r.getMessage()]
    assert len(warnings) == 1                # one warning per failure streak

    broken["v"] = False
    while sched.run_once() != ["old"]:
        clock.now += 30
    # Success resets the back-off: the next failure retries after one interval.
    state["outdated"].add("old")
    broken["v"] = True
    count = len(attempts)
    sched.run_once()
    clock.now += 30
    sched.run_once()
    assert len(attempts) == count + 2


def test_an_upgrade_already_done_under_the_lock_is_not_repeated(monkeypatch, tmp_path):
    """The start catch-up upgrades under the repo's batch lock while the
    scheduler's pass, which saw the index outdated, waits on that lock: the
    scheduler must re-check under the lock, so exactly one full scan runs."""
    from devgraph.watcher.manager import WatcherManager

    state = setup(monkeypatch, tmp_path)
    state["pending"] = False
    state["outdated"] = {"old"}
    holding, release = threading.Event(), threading.Event()

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        state["outdated"].discard(repo_id)
        return 7

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    watcher = WatcherManager(None, lambda *a: None)

    def start_catch_up():
        holding.set()
        assert release.wait(5)
        scan(None, "old", tmp_path)          # catch_up's upgrade path

    catch_up = threading.Thread(target=watcher.run_exclusive, args=("old", start_catch_up))
    catch_up.start()
    assert holding.wait(5)
    done = []
    sched = SchemaRescanScheduler(
        None, Registry([Repo("old", tmp_path)]), on_rescanned=lambda r, n: done.append(r),
        clock=Clock(), run_exclusive=watcher.run_exclusive,
    )
    passing = threading.Thread(target=lambda: done.append(sched.run_once()))
    passing.start()                          # sees "old" outdated, then waits on the lock
    passing.join(0.2)
    assert passing.is_alive()
    release.set()
    catch_up.join(5)
    passing.join(5)
    assert state["scans"] == ["old"]         # exactly one full scan
    assert done == [[]]                      # the scheduler reports nothing


def test_a_never_indexed_repo_is_left_to_its_first_scan(monkeypatch, tmp_path):
    """`devgraph add` registers the repo (watched) before its first full scan
    stamps the format: the missing stamp must not start a parallel scan."""
    state = setup(monkeypatch, tmp_path)
    state["pending"] = False
    state["outdated"] = {"new"}
    registry = Registry([Repo("new", tmp_path, last_indexed=None)])
    sched = SchemaRescanScheduler(None, registry, clock=Clock())
    assert sched.run_once() == []
    assert state["scans"] == []


def test_a_rescan_waiting_on_the_batch_lock_at_stop_never_starts(monkeypatch, tmp_path):
    """A pass that passed its stop check but still waits on the batch lock
    (a watcher batch holds it) skips its scan once it gets the lock: the agent
    closes the graph engine right after stop."""
    state = setup(monkeypatch, tmp_path)
    lock, waiting = threading.Lock(), threading.Event()
    lock.acquire()

    def run_exclusive(repo_id, fn):
        waiting.set()
        with lock:
            return fn()

    sched = SchemaRescanScheduler(
        None, Registry([Repo("r", tmp_path)]), quiet_s=0, interval_s=0.01, clock=Clock(),
        run_exclusive=run_exclusive,
    )
    sched.start()
    assert waiting.wait(5)
    threading.Timer(0.3, lock.release).start()
    sched.stop()
    assert state["scans"] == []


def test_a_rescan_of_a_repo_removed_while_it_waited_on_the_batch_lock_never_starts(monkeypatch, tmp_path):
    """`devgraph remove` (another process) may unregister the repo while the
    pass waits on the batch lock: the scan then writes nothing, or its nodes
    would outlive the removal's delete."""
    state = setup(monkeypatch, tmp_path)
    registry = Registry([Repo("r", tmp_path)])

    def run_exclusive(repo_id, fn):
        registry.repos.clear()  # removed while waiting
        return fn()

    sched = SchemaRescanScheduler(None, registry, quiet_s=0, clock=Clock(), run_exclusive=run_exclusive)
    sched.run_once()
    assert sched.run_once() == []
    assert state["scans"] == [] and registry.marked == []
