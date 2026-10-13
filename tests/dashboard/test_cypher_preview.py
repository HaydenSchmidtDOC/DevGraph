"""`POST /api/cypher` with `preview: true` runs read-only. Live Neo4j.

The console's live glow preview fires on every pause in typing, so a write
typed into the box -- a `DETACH DELETE` half-way through being edited -- used
to execute before the user ever pressed Run. Preview requests now run in a
read transaction: the server refuses any write, and the graph is unchanged.
Run (no `preview`) still executes writes, behind the console's confirm.

Both are bounded in time and rows, so a runaway query can't hold the shared
Neo4j or send the browser the whole database.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings
from devgraph.dashboard import layout_store, routes
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


_MANY = "UNWIND range(1, 1000000) AS i RETURN i"
_SLOW = "UNWIND range(1, 1000000000) AS i WITH i WHERE i = -1 RETURN i"


@pytest.mark.parametrize("preview", [False, True], ids=["run", "preview"])
def test_a_large_result_comes_back_truncated_at_the_row_cap(live, preview):
    client, _engine, _repo = live
    body = _post(client, _MANY, preview=preview)
    assert body["errors"] == []
    result = body["results"][0]
    cap = routes._PREVIEW_MAX_ROWS if preview else routes._CONSOLE_MAX_ROWS
    assert len(result["data"]) == cap
    assert result["truncated"] is True


def test_a_result_under_the_cap_is_not_truncated(live):
    client, _engine, _repo = live
    result = _post(client, "UNWIND range(1, 3) AS i RETURN i")["results"][0]
    assert [d["row"] for d in result["data"]] == [[1], [2], [3]]
    assert result["truncated"] is False


@pytest.mark.parametrize("preview", [False, True], ids=["run", "preview"])
def test_a_query_past_its_timeout_is_stopped_with_a_plain_message(live, monkeypatch, preview):
    client, _engine, _repo = live
    monkeypatch.setattr(routes, "_PREVIEW_TIMEOUT_S" if preview else "_CONSOLE_TIMEOUT_S", 0.5)
    body = _post(client, _SLOW, preview=preview)
    assert body["results"] == []
    assert body["errors"] == [{
        "code": "Timeout",
        "message": "The query ran longer than 0.5 s and was stopped. Narrow the MATCH or add a LIMIT.",
    }]


class _Bounds:
    """Records the bounds each console call is given."""

    def __init__(self):
        self.calls = []

    def run_cypher_graph(self, query, params=None, **bounds):
        self.calls.append(("run", bounds))
        return {"columns": [], "data": [], "truncated": False}

    def preview_cypher_graph(self, query, params=None, **bounds):
        self.calls.append(("preview", bounds))
        return {"columns": [], "data": [], "truncated": False}


def test_run_and_preview_get_their_own_timeout_and_row_cap(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
    registry = RepoRegistry(settings.registry_db_path)
    engine = _Bounds()
    try:
        client = TestClient(build_app(engine, registry, EventBroadcaster()))
        _post(client, "RETURN 1")
        _post(client, "RETURN 1", preview=True)
    finally:
        registry.close()
    assert engine.calls == [
        ("run", {"timeout_s": 60.0, "max_rows": 50_000}),
        ("preview", {"timeout_s": 5.0, "max_rows": 5_000}),
    ]
