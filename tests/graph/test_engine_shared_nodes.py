"""Shared nodes (Datastore, Endpoint, ...) are attributed to their first
source, whatever order the files claim them in (watcher spec W10)."""

import json
import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths

_RUN = uuid.uuid4().hex[:8]


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_ids(engine):
    ids = [f"_smoketest_shared_{_RUN}_{i}" for i in range(3)]
    yield ids
    for repo_id in ids:
        engine.delete_repository(repo_id)


def claim(engine, repo_id, source, **props):
    engine.upsert_nodes([{
        "label": "Database", "repo_id": repo_id, "name": "PostgreSQL", "properties": {"source": source, **props},
    }])


def node(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (n:Database {repo_id: $r, name: 'PostgreSQL'}) RETURN properties(n) AS p", {"r": repo_id}
    )
    assert len(rows) <= 1
    if not rows:
        return None
    return {k: v for k, v in rows[0]["p"].items() if k != "repo_id"}


def test_two_claim_orders_give_identical_nodes(engine, repo_ids):
    first, second, _ = repo_ids
    claim(engine, first, "b.py", library="psycopg")
    claim(engine, first, "a.py", library="sqlalchemy")
    claim(engine, second, "a.py", library="sqlalchemy")
    claim(engine, second, "b.py", library="psycopg")

    one, two = node(engine, first), node(engine, second)
    assert one == two
    assert one["source"] == "a.py"
    assert one["sources"] == ["a.py", "b.py"]
    assert one["library"] == "sqlalchemy"
    assert json.loads(one["claims"]) == {
        "a.py": {"library": "sqlalchemy", "source": "a.py"},
        "b.py": {"library": "psycopg", "source": "b.py"},
    }


def test_unclaim_of_the_minimum_reattributes_then_deletes(engine, repo_ids):
    repo_id = repo_ids[0]
    claim(engine, repo_id, "b.py", library="psycopg")
    claim(engine, repo_id, "a.py", library="sqlalchemy", pool="only-a")

    engine.delete_nodes_by_source_file(repo_id, "a.py")
    after = node(engine, repo_id)
    assert after["source"] == "b.py"
    assert after["library"] == "psycopg"
    assert after["sources"] == ["b.py"]
    assert "pool" not in after

    engine.delete_nodes_by_source_file(repo_id, "b.py")
    assert node(engine, repo_id) is None


def test_unclaim_of_a_non_minimum_keeps_the_attribution(engine, repo_ids):
    repo_id = repo_ids[0]
    claim(engine, repo_id, "a.py", library="sqlalchemy")
    claim(engine, repo_id, "b.py", library="psycopg", pool="only-b")

    engine.delete_nodes_by_source_file(repo_id, "b.py")
    after = node(engine, repo_id)
    assert after["source"] == "a.py"
    assert after["library"] == "sqlalchemy"
    assert after["sources"] == ["a.py"]
    assert "pool" not in after


def test_legacy_node_keeps_every_source_and_follows_the_min_rule(engine, repo_ids):
    repo_id = repo_ids[0]
    # The pre-W10 shape: last writer's properties, `sources` in claim order, no `claims`.
    engine.run_cypher(
        "CREATE (n:Database {repo_id: $r, name: 'PostgreSQL', source: 'c.py', library: 'psycopg', "
        "sources: ['c.py', 'b.py']})",
        {"r": repo_id},
    )
    claim(engine, repo_id, "d.py", library="redis-py")
    after = node(engine, repo_id)
    assert after["sources"] == ["b.py", "c.py", "d.py"]
    assert after["source"] == "b.py"
    assert after["library"] == "psycopg"

    claim(engine, repo_id, "a.py", library="sqlalchemy")
    after = node(engine, repo_id)
    assert after["sources"] == ["a.py", "b.py", "c.py", "d.py"]
    assert after["source"] == "a.py"
    assert after["library"] == "sqlalchemy"


def test_incremental_rename_matches_a_fresh_scan(engine, repo_ids, tmp_path):
    live, fresh, _ = repo_ids
    (tmp_path / "a.py").write_text("import psycopg\n")
    (tmp_path / "c.py").write_text("import psycopg2\n")
    engine.upsert_repository(live, live, str(tmp_path))
    full_scan(engine, live, tmp_path)

    # Rename the lower-sorted file, as a watcher batch delivers it: index, then remove.
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    index_paths(engine, live, tmp_path, {tmp_path / "b.py"})
    remove_paths(engine, live, tmp_path, {tmp_path / "a.py"})

    engine.upsert_repository(fresh, fresh, str(tmp_path))
    full_scan(engine, fresh, tmp_path)

    after = node(engine, live)
    assert after == node(engine, fresh)
    assert after["source"] == "b.py"
    assert after["library"] == "psycopg"
    assert after["sources"] == ["b.py", "c.py"]
