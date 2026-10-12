"""`POST /api/cypher` with `preview: true` runs read-only. Live Neo4j.

The console's live glow preview fires on every pause in typing, so a write
typed into the box -- a `DETACH DELETE` half-way through being edited -- used
to execute before the user ever pressed Run. Preview requests now run in a
read transaction: the server refuses any write, and the graph is unchanged.
Run (no `preview`) still executes writes, behind the console's confirm.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings
from devgraph.dashboard import layout_store
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_HOST = {"host": "127.0.0.1:8765"}


@pytest.fixture
def live(tmp_path, monkeypatch):
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    repo = f"zz-preview-{uuid.uuid4().hex[:8]}"
    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
    registry = RepoRegistry(settings.registry_db_path)
    client = TestClient(build_app(engine, registry, EventBroadcaster()))
    try:
        yield client, engine, repo
    finally:
        engine.run_cypher("MATCH (n:ZzPreview {repo_id: $r}) DETACH DELETE n", {"r": repo})
        registry.close()
        engine.close()


def _count(engine, repo):
    return engine.run_cypher("MATCH (n:ZzPreview {repo_id: $r}) RETURN count(n) AS n", {"r": repo})[0]["n"]


def _post(client, query, **extra):
    return client.post("/api/cypher", json={"query": query, **extra}, headers=_HOST).json()


def test_a_preview_create_is_refused_and_writes_nothing(live):
    client, engine, repo = live
    body = _post(client, f"CREATE (:ZzPreview {{repo_id: '{repo}', name: 'x'}})", preview=True)
    assert body["errors"], body
    assert _count(engine, repo) == 0


def test_a_preview_delete_is_refused_and_the_node_survives(live):
    client, engine, repo = live
    engine.run_cypher("CREATE (:ZzPreview {repo_id: $r, name: 'keep'})", {"r": repo})
    body = _post(client, f"MATCH (n:ZzPreview {{repo_id: '{repo}'}}) DETACH DELETE n", preview=True)
    assert body["errors"], body
    assert _count(engine, repo) == 1


def test_a_preview_read_returns_the_graph_shape(live):
    client, engine, repo = live
    engine.run_cypher("CREATE (:ZzPreview {repo_id: $r, name: 'seen'})", {"r": repo})
    body = _post(client, f"MATCH (n:ZzPreview {{repo_id: '{repo}'}}) RETURN n", preview=True)
    assert body["errors"] == []
    nodes = body["results"][0]["data"][0]["graph"]["nodes"]
    assert [n["properties"]["name"] for n in nodes] == ["seen"]


def test_run_without_preview_still_executes_writes(live):
    client, engine, repo = live
    body = _post(client, f"CREATE (:ZzPreview {{repo_id: '{repo}', name: 'x'}})")
    assert body["errors"] == []
    assert _count(engine, repo) == 1
