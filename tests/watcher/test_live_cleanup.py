"""Live tests clean up after themselves, including after a killed run.

A run killed before teardown (a timeout, Ctrl+C twice) left its
`zz-watcher-live-*` and `zz-accfuzz-*` repositories, and the constraints of
their per-run labels, in the shared Neo4j, where `devgraph doctor` reported
each one. Their ids now carry the creation time, and the next run's fixtures
sweep any older than an hour. Live Neo4j.
"""

import time
import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from tests.watcher.live_helpers import STALE_LIVE_REPO_S, live_repo_id, sweep_stale_live_repos


@pytest.fixture
def engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    yield engine
    engine.close()


def test_live_repo_ids_are_unique_and_dated():
    a, b = live_repo_id("zz-x"), live_repo_id("zz-x")
    assert a != b
    stamp = int(a.split("-")[2])
    assert abs(stamp - time.time()) < 5


def test_the_sweep_deletes_only_old_repositories_of_its_prefix(engine):
    prefix = f"zz-sweeptest{uuid.uuid4().hex[:6]}"
    label = f"ZzSweep{uuid.uuid4().hex[:8]}"
    old = f"{prefix}-{int(time.time() - STALE_LIVE_REPO_S - 60)}-aaaaaa"
    old_fresh = f"{old}_fresh"
    new = live_repo_id(prefix)
    other = f"zz-othertest-{int(time.time() - STALE_LIVE_REPO_S - 60)}-bbbbbb"
    repos = (old, old_fresh, new, other)
    try:
        for repo in repos:
            engine.upsert_repository(repo, repo, f"/tmp/{repo}")
            engine.upsert_node("Class", repo, "Thing")
        engine.record_applied_schema(old, "sha256:x", [label], [], [f"{label}:slug"])
        engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key IF NOT EXISTS FOR (n:{label}) REQUIRE (n.repo_id, n.slug) IS UNIQUE")

        swept = sweep_stale_live_repos(engine, prefix)

        left = {r["id"] for r in engine.run_cypher(
            "MATCH (n) WHERE n.repo_id IN $ids RETURN DISTINCT n.repo_id AS id", {"ids": list(repos)})}
        assert sorted(swept) == sorted([old, old_fresh])
        assert left == {new, other}
        assert not engine.run_cypher("SHOW CONSTRAINTS YIELD name WHERE name = $n RETURN name", {"n": f"{label.lower()}_repo_key"})
    finally:
        for repo in repos:
            engine.delete_repository(repo)
        engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
