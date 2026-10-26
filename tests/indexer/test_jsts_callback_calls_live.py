"""JS/TS calls inside anonymous callbacks against a live Neo4j: find_callers
names the component holding a `useEffect` body or an `onClick` arrow, and an
edit inside a callback alone equals a fresh `full_scan`. Fixture code is
fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import find_callers
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev"
    )
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-jscallback-{uuid.uuid4().hex[:8]}"
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


def callers(engine, repo_id, name):
    return sorted(row["name"] for row in find_callers(engine, repo_id, name)["results"])


API = """
    export async function fetchOrder(id: string) { return { id }; }
    export async function cancelOrder(id: string) {}
    export async function archiveOrder(id: string) {}
"""

VIEW = """
    import { useEffect, useState } from 'react';
    import { fetchOrder, cancelOrder } from './api';

    export function OrderView({ id }: { id: string }) {
      const [order, setOrder] = useState(null);
      useEffect(() => { fetchOrder(id).then(setOrder); }, [id]);
      return <button onClick={() => cancelOrder(id)}>Cancel</button>;
    }
"""


def test_callback_calls_are_the_components_and_an_edit_inside_one(engine, repo_id, tmp_path):
    write(tmp_path, "src/api.ts", API)
    view = write(tmp_path, "src/OrderView.tsx", VIEW)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "fetchOrder") == ["OrderView"]
    assert callers(engine, repo_id, "cancelOrder") == ["OrderView"]

    # The handler's call changes inside the arrow alone.
    write(
        tmp_path,
        "src/OrderView.tsx",
        VIEW.replace("cancelOrder } from", "archiveOrder } from").replace(
            "() => cancelOrder(id)", "() => archiveOrder(id)"
        ),
    )
    index_paths(engine, repo_id, tmp_path, {view})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "cancelOrder") == []
    assert callers(engine, repo_id, "archiveOrder") == ["OrderView"]
