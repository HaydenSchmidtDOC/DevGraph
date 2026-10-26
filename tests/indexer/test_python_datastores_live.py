"""Python datastores read from the syntax tree, against a live Neo4j: a
file under a test folder claims no store, and when a file's only import of
a client moves into a docstring its service's USES of that store goes, an
edit that equals a fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import get_service_dependencies
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-pystores-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def incremental_equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def uses(engine, repo_id, service):
    return {(dep["type"], dep["name"]) for dep in get_service_dependencies(engine, repo_id, service)["dependencies"]}


def stores(engine, repo_id):
    return sorted(
        row["name"]
        for row in engine.run_cypher(
            "MATCH (d {repo_id: $r}) WHERE d:Database OR d:Cache OR d:Queue OR d:VectorStore RETURN d.name AS name",
            {"r": repo_id},
        )
    )


CACHE = """
    import redis

    client = redis.Redis(host="cache")
"""


def test_a_test_folder_and_a_docstring_import_claim_no_store(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  shop:\n    build: ./shop\n")
    cache = write(tmp_path, "shop/cache.py", CACHE)
    write(tmp_path, "shop/tests/test_store.py", "import sqlite3\nconn = sqlite3.connect(':memory:')\n")
    write(tmp_path, "shop/notes.py", '"""Talks to mysql://reports and imports pymongo."""\nHELP = "postgresql://x"\n')
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert stores(engine, repo_id) == ["Redis"]
    assert uses(engine, repo_id, "shop") == {("Cache", "Redis")}

    # The only import moves into a docstring: the service no longer uses Redis.
    write(tmp_path, "shop/cache.py", '"""Example:\n\n    import redis\n    redis.Redis()\n"""\nclient = None\n')
    index_paths(engine, repo_id, tmp_path, {cache})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert uses(engine, repo_id, "shop") == set()
    assert stores(engine, repo_id) == []

    # And comes back.
    write(tmp_path, "shop/cache.py", CACHE)
    index_paths(engine, repo_id, tmp_path, {cache})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert uses(engine, repo_id, "shop") == {("Cache", "Redis")}
