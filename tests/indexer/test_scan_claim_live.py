"""One full scan of a repository at a time across processes (dispatch
`_claim_full_scan`): a `devgraph rescan` in one process and the agent's
catch-up or index upgrade in another wait for each other instead of scanning
together. Live: the claim is on the Repository node in Neo4j."""

import logging
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devgraph.agent import schema_rescan
from devgraph.graph.engine import GraphEngine
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import INDEX_FORMAT, catch_up, full_scan_elsewhere

URI, USER, PASSWORD = "bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev"

#: A CLI-style full scan, slowed so the test can act while it runs; it writes
#: `marker` once its claim is held and the index unstamped.
_SCANNER = textwrap.dedent(
    """
    import logging, sys, time
    from pathlib import Path
    from devgraph.graph.engine import GraphEngine
    from devgraph.indexer import dispatch

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    repo_id, root, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    real = dispatch.index_paths

    def slow(*args, **kwargs):
        marker.write_text("scanning")
        time.sleep(4)
        return real(*args, **kwargs)

    dispatch.index_paths = slow
    engine = GraphEngine(sys.argv[4], sys.argv[5], sys.argv[6])
    try:
        print("indexed", dispatch.full_scan(engine, repo_id, root))
    finally:
        engine.close()
    """
)


@pytest.fixture
def engine():
    live = GraphEngine(URI, USER, PASSWORD)
    try:
        live.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield live
    live.close()


@pytest.fixture
def repo(tmp_path, engine):
    repo_id = f"zz-scan-claim-{uuid.uuid4().hex[:12]}"
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    for i in range(5):
        (root / "pkg" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    engine.upsert_repository(repo_id, repo_id, str(root))  # as registering it does
    yield repo_id, root
    engine.delete_repository(repo_id)


@dataclass
class _Repo:
    repo_id: str
    path: Path
    docs_path: str | None = None
    mentions_enabled: bool = False
    watch_enabled: bool = True
    last_indexed: str | None = "2026-10-01T00:00:00+00:00"


class _Registry:
    def __init__(self, repo):
        self.repo = repo

    def list_repos(self, active_only=False):
        return [self.repo]

    def get(self, repo_id):
        return self.repo

    def mark_indexed(self, repo_id, at=None):
        pass


def test_a_claim_is_refused_while_fresh_and_taken_over_once_stale(engine, repo):
    repo_id, _root = repo
    assert engine.claim_full_scan(repo_id, "1@a/x", 90) == (True, None)
    assert engine.claim_full_scan(repo_id, "1@a/x", 90) == (True, None)  # its own: again granted
    assert engine.claim_full_scan(repo_id, "2@b/y", 90) == (False, "1@a/x")
    assert engine.claim_full_scan(repo_id, "2@b/y", 90, replace="1@a/x") == (True, None)  # a holder known gone
    assert engine.beat_full_scan(repo_id, "1@a/x") is False  # taken over: its scan stops
    assert engine.full_scan_holder(repo_id, 90) == "2@b/y"
    assert full_scan_elsewhere(engine, repo_id) == "2@b/y"
    time.sleep(1.2)
    assert engine.full_scan_holder(repo_id, 1) is None  # silent for over a second
    assert engine.claim_full_scan(repo_id, "3@c/z", 1) == (True, None)  # taken over once stale
    engine.release_full_scan(repo_id, "2@b/y")  # no longer its claim: nothing changes
    assert engine.full_scan_holder(repo_id, 90) == "3@c/z"
    assert engine.beat_full_scan(repo_id, "3@c/z") is True
    engine.release_full_scan(repo_id, "3@c/z")
    assert engine.full_scan_holder(repo_id, 90) is None


def test_a_removed_repository_is_not_claimed_scanned_or_created_again(engine, repo):
    repo_id, root = repo
    engine.delete_repository(repo_id)
    assert engine.claim_full_scan(repo_id, "1@a/x", 90) == (False, None)
    assert dispatch.full_scan(engine, repo_id, root) == 0
    assert repo_id not in engine.graph_repo_ids()


def test_closing_the_engine_releases_a_claim_a_cut_short_scan_holds(engine, repo, caplog):
    """The agent shutting down mid-scan: the engine refuses new sessions
    once closing, so the scan's own release fails; the close released it."""
    repo_id, _root = repo
    closing = GraphEngine(URI, USER, PASSWORD)
    with caplog.at_level(logging.WARNING):
        with dispatch.full_scan_turn(repo_id, engine=closing):
            assert engine.full_scan_holder(repo_id, 90) == dispatch._SCAN_OWNER
            closing.close()
            assert engine.full_scan_holder(repo_id, 90) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert repo_id not in dispatch._scan_claims


def test_a_claim_left_by_an_ended_process_on_this_host_is_taken_over_at_once(engine, repo, monkeypatch):
    repo_id, root = repo
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    holder = f"{int(ended.stdout)}@{socket.gethostname()}/0123abcd"
    engine.set_index_format(repo_id, 0)  # its scan was cut short
    assert engine.claim_full_scan(repo_id, holder, 90) == (True, None)
    monkeypatch.setattr(dispatch, "FULL_SCAN_POLL_S", 5.0)
    started = time.monotonic()
    result = catch_up(engine, repo_id, root, datetime.now(timezone.utc))
    assert time.monotonic() - started < 4  # no wait for its heartbeat to go stale
    assert result.indexed == 5
    assert engine.full_scan_holder(repo_id, 90) is None


def test_the_agent_does_not_scan_a_repository_another_process_is_scanning(engine, repo, tmp_path, monkeypatch, caplog):
    """A `devgraph rescan`-style full scan runs in a subprocess; meanwhile the
    agent's schema scheduler pass and its catch-up see the index outdated.
    Neither scans: the pass skips, the catch-up waits, then finds the index
    current. Exactly one full scan runs, with no deadlock retried."""
    repo_id, root = repo
    engine.set_index_format(repo_id, INDEX_FORMAT)  # indexed before: the catch-up is otherwise incremental
    scans = []

    def counting(real):
        def scan(*args, **kwargs):
            scans.append(args[1])
            return real(*args, **kwargs)
        return scan

    monkeypatch.setattr(dispatch, "full_scan", counting(dispatch.full_scan))
    monkeypatch.setattr(schema_rescan, "full_scan", counting(schema_rescan.full_scan))
    monkeypatch.setattr(dispatch, "FULL_SCAN_POLL_S", 0.2)
    marker, log = tmp_path / "scanning", tmp_path / "scanner.log"
    with log.open("w") as out:  # not a pipe: unread, a full one would block the scan
        scanner = subprocess.Popen(
            [sys.executable, "-c", _SCANNER, repo_id, str(root), str(marker), URI, USER, PASSWORD],
            stdout=out, stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert scanner.poll() is None, log.read_text()
            assert time.monotonic() < deadline, "the subprocess scan never started"
            time.sleep(0.05)
        assert dispatch.index_outdated(engine, repo_id)  # unstamped while it runs
        assert full_scan_elsewhere(engine, repo_id)

        scheduler = schema_rescan.SchemaRescanScheduler(engine, _Registry(_Repo(repo_id, root)))
        with caplog.at_level(logging.INFO):
            assert scheduler.run_once() == []
            waited = []
            result = catch_up(
                engine, repo_id, root, datetime.now(timezone.utc) - timedelta(hours=1),
                while_waiting=lambda: waited.append(1) or True,
            )
        scanner.wait(timeout=120)
    finally:
        if scanner.poll() is None:
            scanner.kill()
            scanner.wait()
    output = log.read_text()
    assert scanner.returncode == 0, output
    assert "indexed 5" in output
    assert scans == [] and waited  # it waited for the other scan, then had none to run
    assert result.checked == 5  # an incremental catch-up, not a full scan
    assert not dispatch.index_outdated(engine, repo_id)
    assert engine.full_scan_holder(repo_id, 90) is None  # released
    assert "DeadlockDetected" not in output + caplog.text


def test_a_crashed_scans_stale_claim_is_taken_over(engine, repo, monkeypatch):
    """A `devgraph rescan` killed mid-scan leaves its claim and an unstamped
    index; once its heartbeat is stale the agent's catch-up scans in full."""
    repo_id, root = repo
    engine.set_index_format(repo_id, 0)
    assert engine.claim_full_scan(repo_id, "999@elsewhere/dead", 90) == (True, None)
    monkeypatch.setattr(dispatch, "FULL_SCAN_STALE_S", 1.0)
    monkeypatch.setattr(dispatch, "FULL_SCAN_POLL_S", 0.2)
    started = time.monotonic()
    result = catch_up(engine, repo_id, root, datetime.now(timezone.utc))
    assert time.monotonic() - started >= 0.8  # waited for the heartbeat to go stale
    assert result.indexed == 5
    assert not dispatch.index_outdated(engine, repo_id)
    assert engine.full_scan_holder(repo_id, 90) is None
