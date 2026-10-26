"""A failing settings rescan (docs path or mentions changed) backs off like a
failing upgrade instead of running a full scan every pass."""

from devgraph.agent import schema_rescan
from devgraph.agent.schema_rescan import SchemaRescanScheduler
from tests.agent.test_schema_rescan import Clock, Registry, Repo, setup


def test_a_failing_settings_rescan_backs_off(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    state["pending"] = False
    monkeypatch.setattr(schema_rescan, "scan_options_pending", lambda e, r, d, m: True)

    def fail(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        raise RuntimeError("neo4j hiccup")

    monkeypatch.setattr(schema_rescan, "full_scan", fail)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock, interval_s=30)
    sched.run_once()
    assert state["scans"] == ["r"]
    clock.now += 29
    sched.run_once()
    assert state["scans"] == ["r"]           # waits one interval
    clock.now += 2
    sched.run_once()
    assert state["scans"] == ["r", "r"]
    clock.now += 59
    sched.run_once()
    assert state["scans"] == ["r", "r"]      # then twice that
    clock.now += 2
    sched.run_once()
    assert state["scans"] == ["r", "r", "r"]

    monkeypatch.setattr(schema_rescan, "full_scan", lambda *a, **k: 7)
    clock.now += 121
    assert sched.run_once() == ["r"]
    assert "r" not in sched._upgrade_backoff  # reset on success
