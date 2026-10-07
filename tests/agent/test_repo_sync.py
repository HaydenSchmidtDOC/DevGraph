"""RepoSync: stamps, failure floors, catch-up events and logs (spec W7, W9).

Engine and registry are mocks; the clock is injected.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgraph.agent import sync
from devgraph.agent.sync import FAILURE_RETRY_DELAY_S, RepoSync
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import CatchUp
from devgraph.registry.store import RepoRecord

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
REPO = "r"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: int = 1) -> datetime:
        self.now += timedelta(minutes=minutes)
        return self.now


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = Clock()
        self.registry = MagicMock()
        self.registry.get.return_value = RepoRecord(REPO, root, True, True, None, docs_path=None)
        self.engine = MagicMock()
        self.events: list[dict] = []
        self.requests: list[tuple] = []
        self.sync = RepoSync(
            self.engine,
            self.registry,
            self.events.append,
            lambda *a: self.requests.append(a),
            now=self.clock,
        )

    def stamps(self) -> list[datetime]:
        return [c.kwargs["at"] for c in self.registry.mark_indexed.call_args_list]

    def kinds(self) -> list[tuple]:
        return [(e["type"], e.get("state")) for e in self.events]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def indexing():
    """sync's index_paths/remove_paths/catch_up, each a MagicMock."""
    with patch.object(sync, "index_paths", return_value=1) as index, \
         patch.object(sync, "remove_paths", return_value=1) as remove, \
         patch.object(sync, "catch_up", return_value=CatchUp(0, 0, 4)) as catch:
        yield {"index": index, "remove": remove, "catch_up": catch}


def test_a_batch_stamps_its_start(rig, indexing):
    def index(*a, **k):
        rig.clock.tick()  # the batch takes a minute
        return 1

    indexing["index"].side_effect = index
    rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    assert rig.stamps() == [T0]
    rig.registry.mark_indexed.assert_called_once_with(REPO, at=T0)


def test_a_failed_batch_sets_the_floor_and_asks_for_a_catch_up(rig, indexing):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    assert rig.stamps() == []
    assert rig.requests == [(REPO, T0, FAILURE_RETRY_DELAY_S)]
    assert FAILURE_RETRY_DELAY_S == 30

    indexing["index"].side_effect = None
    rig.clock.tick()
    rig.sync.on_changes(REPO, {rig.root / "b.py"}, set())
    assert rig.stamps() == [T0]  # min(started, floor)


def test_a_catch_up_from_after_the_floor_starts_at_the_floor_and_clears_it(rig, indexing):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    indexing["index"].side_effect = None

    t2 = rig.clock.tick(2)
    rig.sync.on_catch_up(REPO, T0 + timedelta(minutes=1))
    assert indexing["catch_up"].call_args.args[3] == T0
    assert rig.stamps() == [t2]

    t3 = rig.clock.tick()
    rig.sync.on_changes(REPO, {rig.root / "b.py"}, set())
    assert rig.stamps() == [t2, t3]  # floor cleared


def test_a_failing_catch_up_keeps_the_floor(rig, indexing):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    indexing["index"].side_effect = None
    indexing["catch_up"].side_effect = RuntimeError("still down")
    rig.clock.tick()
    rig.sync.on_catch_up(REPO, T0)
    assert rig.stamps() == []
    rig.clock.tick()
    rig.sync.on_changes(REPO, {rig.root / "b.py"}, set())
    assert rig.stamps() == [T0]


def test_retry_failed_asks_once_per_floored_repository(rig, indexing):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    rig.clock.tick()
    rig.sync.on_changes(REPO, {rig.root / "b.py"}, set())
    rig.registry.get.return_value = RepoRecord("other", rig.root, True, True, None, docs_path=None)
    rig.sync.on_changes("other", {rig.root / "c.py"}, set())
    rig.requests.clear()
    rig.sync.retry_failed()
    assert sorted(r[:2] for r in rig.requests) == [("other", T0 + timedelta(minutes=1)), (REPO, T0)]


def test_a_failure_while_stopping_logs_no_warning(rig, indexing, caplog):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    indexing["catch_up"].side_effect = RuntimeError("neo4j down")
    rig.sync.stopping = True
    with caplog.at_level(logging.DEBUG, logger="devgraph.agent.sync"):
        rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
        rig.sync.on_catch_up(REPO, T0)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_failure_warns_in_plain_words(rig, indexing, caplog):
    indexing["index"].side_effect = RuntimeError("neo4j down")
    with caplog.at_level(logging.INFO, logger="devgraph.agent.sync"):
        rig.sync.on_changes(REPO, {rig.root / "a.py"}, set())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f'Couldn\'t update {REPO}; DevGraph will retry, or run "devgraph rescan {REPO}"'
    ]
    assert warnings[0].exc_info is not None


def test_catch_up_events_running_then_done_and_reindexed(rig, indexing):
    seen_running = []
    indexing["catch_up"].side_effect = lambda *a, **k: seen_running.append(rig.sync.running) or CatchUp(5, 2, 9)
    assert rig.sync.running == 0
    rig.sync.on_catch_up(REPO, T0)
    assert seen_running == [1] and rig.sync.running == 0
    assert rig.kinds() == [("catch_up", "running"), ("catch_up", "done"), ("reindexed", None)]
    assert rig.events[1] == {"type": "catch_up", "repo_id": REPO, "state": "done", "changed": 5, "deleted": 2}
    assert rig.events[2] == {"type": "reindexed", "repo_id": REPO, "changed": 5, "deleted": 2}


def test_a_catch_up_with_nothing_to_do_publishes_no_reindexed(rig, indexing, caplog):
    with caplog.at_level(logging.INFO, logger="devgraph.agent.sync"):
        rig.sync.on_catch_up(REPO, T0)
    assert rig.kinds() == [("catch_up", "running"), ("catch_up", "done")]
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == f"Checking {REPO} for changes made while DevGraph wasn't watching…"
    assert messages[1].startswith(f"{REPO} is up to date (checked 4 files in ")


def test_a_failed_catch_up_publishes_failed(rig, indexing):
    indexing["catch_up"].side_effect = RuntimeError("neo4j down")
    rig.sync.on_catch_up(REPO, T0)
    assert rig.kinds() == [("catch_up", "running"), ("catch_up", "failed")]
    assert rig.sync.running == 0
    assert rig.requests == [(REPO, T0, FAILURE_RETRY_DELAY_S)]


def test_after_a_git_operation_only_a_find_is_logged_at_info(rig, indexing, caplog):
    with caplog.at_level(logging.INFO, logger="devgraph.agent.sync"):
        rig.sync.on_catch_up(REPO, T0, reason="git")
        indexing["catch_up"].return_value = CatchUp(4, 1, 9)
        rig.sync.on_catch_up(REPO, T0, reason="git")
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        f"{REPO}: found 5 files the live watcher missed after a git operation; updated them"
    ]


def test_counts_come_from_index_paths(rig, monkeypatch, caplog):
    """3 files are due; index_paths indexed 7 (referrers included)."""
    for name in ("a.py", "b.py", "c.py"):
        (rig.root / name).write_text("x = 1\n")
    monkeypatch.setattr(dispatch, "prune_stale_files", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "index_paths", lambda *a, **k: 7)
    with caplog.at_level(logging.INFO, logger="devgraph.agent.sync"):
        rig.sync.on_catch_up(REPO, T0)
    assert rig.events[1]["changed"] == 7
    assert any(r.getMessage().startswith(f"Caught up on {REPO}: 7 files updated, 0 removed (") for r in caplog.records)

    with patch.object(sync, "index_paths", return_value=7):
        rig.sync.on_changes(REPO, {rig.root / "a.py", rig.root / "b.py", rig.root / "c.py"}, set())
    assert rig.events[-1] == {"type": "reindexed", "repo_id": REPO, "changed": 7, "deleted": 0}


def test_an_unknown_repo_is_a_noop(rig, indexing):
    rig.registry.get.return_value = None
    rig.sync.on_changes("gone", {Path("x.py")}, set())
    rig.sync.on_catch_up("gone", T0)
    indexing["index"].assert_not_called()
    indexing["catch_up"].assert_not_called()
    assert rig.events == [] and rig.stamps() == []


def test_running_counts_overlapping_catch_ups(rig, indexing):
    entered, release = threading.Event(), threading.Event()

    def blocking(*a, **k):
        entered.set()
        assert release.wait(5)
        return CatchUp(0, 0, 0)

    indexing["catch_up"].side_effect = blocking
    t = threading.Thread(target=rig.sync.on_catch_up, args=(REPO, T0))
    t.start()
    assert entered.wait(5)
    assert rig.sync.running == 1
    release.set()
    t.join(5)
    assert rig.sync.running == 0
