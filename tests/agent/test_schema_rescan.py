"""SchemaRescanScheduler debounce decisions, with stubs and a fake clock."""

import threading
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


class Registry:
    def __init__(self, repos):
        self.repos = repos
        self.marked = []

    def list_repos(self, active_only=False):
        return list(self.repos)

    def mark_indexed(self, repo_id):
        self.marked.append(repo_id)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def setup(monkeypatch, tmp_path, *, valid=True):
    state = {"pending": True, "hash": "h1", "scans": []}
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
