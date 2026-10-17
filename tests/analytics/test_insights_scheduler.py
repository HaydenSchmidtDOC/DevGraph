"""InsightsScheduler decisions, with stub engine and registry."""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from devgraph.analytics import insights
from devgraph.analytics.insights import InsightsScheduler


@dataclass
class Repo:
    repo_id: str
    last_indexed: str | None


class Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        assert active_only is True
        return list(self.repos)


class Engine:
    """Summaries per repo; records refreshes; can fail on demand."""

    def __init__(self, computed_at=None, fail_read=()):
        self.computed_at = dict(computed_at or {})
        self.fingerprint = {}
        self.fail_read = set(fail_read)

    def read_insights_summary(self, repo_id):
        if repo_id in self.fail_read:
            raise ConnectionError("neo4j down")
        at = self.computed_at.get(repo_id)
        return None if at is None else {"computed_at": at, "fingerprint": self.fingerprint.get(repo_id)}


def fake_refresh(calls, result=True):
    def refresh(engine, repo_id, *, blocking=True, unless_fingerprint=None):
        calls.append((repo_id, blocking))
        return {"computed_at": "x"} if result else None

    return refresh


def test_refreshes_never_computed_and_out_of_date_repos_only(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls))
    registry = Registry([
        Repo("never", "2026-10-01T10:00:00+00:00"),
        Repo("stale", "2026-10-01T10:00:00+00:00"),
        Repo("fresh", "2026-10-01T10:00:00+00:00"),
        Repo("unindexed", None),
    ])
    engine = Engine({"stale": "2026-10-01T09:00:00+00:00", "fresh": "2026-10-01T11:00:00+00:00"})
    refreshed = []
    scheduler = InsightsScheduler(engine, registry, on_refreshed=refreshed.append)
    assert scheduler.run_once() == ["never", "stale"]
    assert calls == [("never", False), ("stale", False)]
    assert refreshed == ["never", "stale"]


def test_a_repo_already_being_computed_is_skipped_not_reported(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls, result=False))
    scheduler = InsightsScheduler(Engine(), Registry([Repo("busy", "2026-10-01T10:00:00+00:00")]))
    assert scheduler.run_once() == []
    assert calls == [("busy", False)]


def test_one_failing_repo_does_not_stop_the_others(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls))
    registry = Registry([Repo("down", "2026-10-01T10:00:00+00:00"), Repo("ok", "2026-10-01T10:00:00+00:00")])
    scheduler = InsightsScheduler(Engine(fail_read={"down"}), registry)
    assert scheduler.run_once() == ["ok"]


def test_a_failing_callback_does_not_stop_the_pass(monkeypatch):
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh([]))
    registry = Registry([Repo("a", "2026-10-01T10:00:00+00:00"), Repo("b", "2026-10-01T10:00:00+00:00")])

    def explode(repo_id):
        raise RuntimeError("subscriber gone")

    assert InsightsScheduler(Engine(), registry, on_refreshed=explode).run_once() == ["a", "b"]


def test_thread_keeps_running_through_failed_passes_and_stops():
    passes = []
    ran_twice = threading.Event()

    class FlakyRegistry:
        def list_repos(self, active_only=False):
            passes.append(1)
            if len(passes) >= 2:
                ran_twice.set()
            raise ConnectionError("registry unavailable")

    scheduler = InsightsScheduler(Engine(), FlakyRegistry(), interval_s=0.01)
    scheduler.start()
    try:
        assert scheduler.running
        assert ran_twice.wait(2), "scheduler died after a failed pass"
    finally:
        scheduler.stop()
    assert not scheduler.running


class Clock:
    """A wall clock and a monotonic clock that move together."""

    def __init__(self, start="2026-10-01T10:00:00+00:00"):
        self.now = datetime.fromisoformat(start)
        self.mono = 1000.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def iso(self, offset=0.0):
        return (self.now + timedelta(seconds=offset)).isoformat()


class GraphRefresh:
    """A refresh_insights stand-in over a 'graph' the test edits: it stores
    the engine's computed_at and fingerprint, and answers unchanged when the
    graph's fingerprint is the one it was given. `takes` seconds pass during
    a run."""

    def __init__(self, engine, clock, takes=0.0):
        self.engine, self.clock, self.takes = engine, clock, takes
        self.graph = "g1"
        self.computes = []
        self.checks = []

    def __call__(self, engine, repo_id, *, blocking=True, unless_fingerprint=None):
        started = self.clock.iso()
        self.clock.advance(self.takes)
        if unless_fingerprint == self.graph:
            self.checks.append(repo_id)
            return {"computed_at": started, "fingerprint": self.graph, "unchanged": True}
        self.computes.append(repo_id)
        self.engine.computed_at[repo_id] = started
        self.engine.fingerprint[repo_id] = self.graph
        return {"computed_at": started, "fingerprint": self.graph}


def scheduler_for(monkeypatch, clock, takes=0.0, settle_s=60, max_lag_s=600):
    engine = Engine()
    repo = Repo("r", None)
    refresh = GraphRefresh(engine, clock, takes)
    monkeypatch.setattr(insights, "refresh_insights", refresh)
    refreshed = []
    scheduler = InsightsScheduler(
        engine, Registry([repo]), on_refreshed=refreshed.append, settle_s=settle_s, max_lag_s=max_lag_s,
        clock=lambda: clock.now, monotonic=lambda: clock.mono,
    )
    return scheduler, repo, refresh, refreshed


def test_saves_in_quick_succession_cause_one_refresh_after_they_settle(monkeypatch):
    clock = Clock()
    scheduler, repo, refresh, refreshed = scheduler_for(monkeypatch, clock)
    repo.last_indexed = clock.iso()
    scheduler.run_once()  # never computed: backfilled at once
    assert refresh.computes == ["r"]
    for _ in range(5):  # a save every 10 s
        clock.advance(10)
        repo.last_indexed = clock.iso()
        refresh.graph = f"g{clock.mono}"
        assert scheduler.run_once() == []
    clock.advance(59)
    assert scheduler.run_once() == []
    clock.advance(1)
    assert scheduler.run_once() == ["r"]
    assert refresh.computes == ["r", "r"]
    assert refreshed == ["r", "r"]
    assert scheduler.run_once() == []


def test_continuous_edits_still_refresh_once_the_lag_reaches_the_maximum(monkeypatch):
    clock = Clock()
    scheduler, repo, refresh, _ = scheduler_for(monkeypatch, clock, max_lag_s=600)
    repo.last_indexed = clock.iso()
    scheduler.run_once()
    runs = 0
    for _ in range(130):  # a save every 10 s for 1,300 s, never settling
        clock.advance(10)
        repo.last_indexed = clock.iso()
        refresh.graph = f"g{clock.mono}"
        runs += len(scheduler.run_once())
    assert runs == 2


def test_a_save_that_changed_nothing_insights_read_is_checked_not_recomputed(monkeypatch):
    clock = Clock()
    scheduler, repo, refresh, refreshed = scheduler_for(monkeypatch, clock)
    repo.last_indexed = clock.iso()
    scheduler.run_once()
    clock.advance(10)
    repo.last_indexed = clock.iso()  # a comment edit: the graph is still g1
    clock.advance(60)
    assert scheduler.run_once() == []
    assert refresh.computes == ["r"] and refresh.checks == ["r"]
    assert refreshed == ["r"]  # no insights_refreshed for the check
    # The check covers the save: nothing more until the next one.
    clock.advance(600)
    assert scheduler.run_once() == []
    assert refresh.checks == ["r"]


def test_a_slow_refresh_holds_off_the_next_for_a_multiple_of_its_duration(monkeypatch):
    clock = Clock()
    scheduler, repo, refresh, _ = scheduler_for(monkeypatch, clock, takes=30)
    repo.last_indexed = clock.iso()
    scheduler.run_once()
    repo.last_indexed = clock.iso()
    refresh.graph = "g2"
    clock.advance(100)  # settled, but within 5 x 30 s of the last run's end
    assert scheduler.run_once() == []
    clock.advance(50)
    assert scheduler.run_once() == ["r"]


def test_the_stored_fingerprint_survives_a_restart(monkeypatch):
    """A new scheduler (the agent restarted) checks against the fingerprint
    the last refresh stored, so an unchanged graph is still not recomputed."""
    clock = Clock()
    scheduler, repo, refresh, _ = scheduler_for(monkeypatch, clock)
    repo.last_indexed = clock.iso()
    scheduler.run_once()
    clock.advance(10)
    repo.last_indexed = clock.iso()
    clock.advance(60)
    restarted = InsightsScheduler(
        refresh.engine, Registry([repo]), clock=lambda: clock.now, monotonic=lambda: clock.mono,
    )
    assert restarted.run_once() == []
    assert refresh.computes == ["r"] and refresh.checks == ["r"]
