"""InsightsScheduler decisions, with stub engine and registry."""

import threading
from dataclasses import dataclass

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
        self.fail_read = set(fail_read)

    def read_insights_summary(self, repo_id):
        if repo_id in self.fail_read:
            raise ConnectionError("neo4j down")
        at = self.computed_at.get(repo_id)
        return None if at is None else {"computed_at": at}


def fake_refresh(calls, result=True):
    def refresh(engine, repo_id, *, blocking=True):
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
