"""Shared nodes (Datastore, Endpoint, ...) are attributed to their first
source, whatever order the files claim them in (watcher spec W10)."""

import json
import threading
import uuid

import pytest

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine
from devgraph.graph.schema import RESERVED_NODE_PROPERTIES
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


def test_claims_is_reserved_beside_sources():
    assert {"source", "sources", "claims"} <= RESERVED_NODE_PROPERTIES


def test_concurrent_claims_on_one_node_keep_every_claim(engine, repo_ids):
    repo_id = repo_ids[0]
    sources = [f"f{i:02d}.py" for i in range(20)]
    errors = []

    def worker(mine):
        try:
            for source in mine:
                claim(engine, repo_id, source, library=f"lib-{source}")
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(sources[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    after = node(engine, repo_id)
    assert after["sources"] == sources
    assert sorted(json.loads(after["claims"])) == sources
    assert after["source"] == "f00.py" and after["library"] == "lib-f00.py"


def _write_insight_during(monkeypatch, engine, repo_id, step):
    """While `step` runs, a second session sets insight_pagerank on the node once,
    in the middle of the claim transaction (after its read)."""
    writer: list[threading.Thread] = []
    original = engine_module._claims_of

    def claims_of(props):
        if not writer:
            thread = threading.Thread(target=lambda: engine.run_cypher(
                "MATCH (n:Database {repo_id: $r, name: 'PostgreSQL'}) SET n.insight_pagerank = 0.5", {"r": repo_id}
            ))
            writer.append(thread)
            thread.start()
            thread.join(timeout=0.5)  # blocks on the node's lock when the read took it
        return original(props)

    monkeypatch.setattr(engine_module, "_claims_of", claims_of)
    step()
    writer[0].join()


def test_a_concurrent_insight_write_survives_a_claim(engine, repo_ids, monkeypatch):
    repo_id = repo_ids[0]
    claim(engine, repo_id, "b.py", library="psycopg")
    _write_insight_during(monkeypatch, engine, repo_id, lambda: claim(engine, repo_id, "a.py", library="sqlalchemy"))
    after = node(engine, repo_id)
    assert after["insight_pagerank"] == 0.5
    assert after["source"] == "a.py"


def test_a_concurrent_insight_write_survives_an_unclaim(engine, repo_ids, monkeypatch):
    repo_id = repo_ids[0]
    claim(engine, repo_id, "b.py", library="psycopg")
    claim(engine, repo_id, "a.py", library="sqlalchemy")
    _write_insight_during(monkeypatch, engine, repo_id, lambda: engine.delete_nodes_by_source_file(repo_id, "a.py"))
    after = node(engine, repo_id)
    assert after["insight_pagerank"] == 0.5
    assert after["source"] == "b.py"
