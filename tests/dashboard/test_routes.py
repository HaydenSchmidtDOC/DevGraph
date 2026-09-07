"""Tests for the dashboard's /api/* routes via FastAPI's TestClient.

Reuses the seeded-graph fixture pattern from tests/mcp/test_tools.py: a real
GraphEngine against the local test Neo4j (bolt://127.0.0.1:7687), seeded
with two repos to also verify repo_id scoping holds through this second
entry point into the same engine.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def engine():
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    test_engine.verify_connectivity()
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def seeded_graph(engine):
    engine.upsert_repository("dash_repo_a", "Dash Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", "dash_repo_a", "UserService")
    engine.upsert_node("Service", "dash_repo_a", "AuthService")
    engine.upsert_node("Module", "dash_repo_a", "auth.py")
    engine.upsert_node("Class", "dash_repo_a", "AuthHandler")
    engine.upsert_relationship("Service", "UserService", "CALLS", "Service", "AuthService", "dash_repo_a")
    engine.upsert_relationship("Module", "auth.py", "CONTAINS", "Class", "AuthHandler", "dash_repo_a")

    engine.upsert_repository("dash_repo_b", "Dash Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", "dash_repo_b", "NotificationService")

    yield engine

    engine.delete_repository("dash_repo_a")
    engine.delete_repository("dash_repo_b")


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.sqlite3")
        yield reg
        reg.close()


def _init_git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture
def client(seeded_graph, registry):
    # add_repo requires a real (empty is fine) git repo on disk and
    # slugifies its name into a repo_id -- register two throwaway repos
    # under explicit repo_ids matching the fixture graph's seeded data
    # ("dash_repo_a"/"dash_repo_b") rather than letting it derive one from
    # a temp directory name.
    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        _init_git_repo(Path(dir_a))
        _init_git_repo(Path(dir_b))
        record_a = registry.add_repo(dir_a, repo_id="dash_repo_a")
        record_b = registry.add_repo(dir_b, repo_id="dash_repo_b")
        assert record_a.repo_id == "dash_repo_a"
        assert record_b.repo_id == "dash_repo_b"

        events = EventBroadcaster()
        app = build_app(seeded_graph, registry, events)
        yield TestClient(app)


def test_list_repos(client):
    res = client.get("/api/repos")
    assert res.status_code == 200
    repo_ids = {r["repo_id"] for r in res.json()}
    assert {"dash_repo_a", "dash_repo_b"} <= repo_ids
    repo_a = next(r for r in res.json() if r["repo_id"] == "dash_repo_a")
    assert repo_a["node_count"] >= 4  # 2 services + 1 module + 1 class


def test_summary_counts_known_seeded_data(client):
    res = client.get("/api/repos/dash_repo_a/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["nodes_by_label"]["Service"] == 2
    assert body["nodes_by_label"]["Module"] == 1
    assert body["nodes_by_label"]["Class"] == 1
    assert body["relationships_by_type"]["CALLS"] == 1
    assert body["relationships_by_type"]["CONTAINS"] == 1


def test_summary_does_not_leak_cross_repo(client):
    res = client.get("/api/repos/dash_repo_a/summary")
    body = res.json()
    assert "NotificationService" not in body["nodes_by_label"]
    assert body["nodes_by_label"].get("Service") == 2  # not 3


def test_graph_endpoint_shape(client):
    res = client.get("/api/repos/dash_repo_a/graph")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"nodes", "edges"}
    assert len(body["nodes"]) >= 4
    node = body["nodes"][0]
    assert set(node["data"].keys()) == {"id", "label", "name"}
    edge = body["edges"][0]
    assert set(edge["data"].keys()) == {"id", "source", "target", "type"}


def test_graph_endpoint_label_filter(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"label": "Service"})
    assert res.status_code == 200
    body = res.json()
    assert all(n["data"]["label"] == "Service" for n in body["nodes"])


def test_graph_endpoint_unknown_label_rejected(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"label": "NotARealLabel"})
    assert res.status_code == 400


def test_graph_endpoint_limit_capping(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"limit": 1})
    assert res.status_code == 200
    assert len(res.json()["nodes"]) <= 1


def test_search_endpoint(client):
    res = client.get("/api/repos/dash_repo_a/search", params={"q": "Auth"})
    assert res.status_code == 200
    names = [r["name"] for r in res.json()["results"]]
    assert "AuthService" in names
    assert "NotificationService" not in names


def test_unknown_repo_id_404s(client):
    for path in (
        "/api/repos/does-not-exist/summary",
        "/api/repos/does-not-exist/graph",
        "/api/repos/does-not-exist/search?q=x",
    ):
        res = client.get(path)
        assert res.status_code == 404


def test_cypher_endpoint_returns_neo4j_http_shaped_graph(client):
    res = client.post(
        "/api/cypher",
        json={"query": "MATCH (n:Service {repo_id: 'dash_repo_a'}) RETURN n ORDER BY n.name"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["errors"] == []
    data = body["results"][0]["data"]
    assert len(data) == 2
    names = {n["properties"]["name"] for row in data for n in row["graph"]["nodes"]}
    assert names == {"AuthService", "UserService"}


def test_cypher_endpoint_reports_errors_without_raising(client):
    res = client.post("/api/cypher", json={"query": "NOT VALID CYPHER"})
    assert res.status_code == 200
    body = res.json()
    assert body["results"] == []
    assert body["errors"][0]["message"]


def test_cypher_endpoint_rejects_empty_query(client):
    res = client.post("/api/cypher", json={"query": "  "})
    assert res.status_code == 400


def test_cypher_endpoint_only_logs_when_record_true(client):
    client.post("/api/cypher", json={"query": "RETURN 1", "record": False})
    assert client.get("/api/query-log").json()["entries"] == []

    client.post("/api/cypher", json={"query": "RETURN 1", "repo_id": "dash_repo_a", "record": True})
    entries = client.get("/api/query-log").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["query"] == "RETURN 1"
    assert entries[0]["repo_id"] == "dash_repo_a"
    assert entries[0]["ok"] is True


def test_query_rate_endpoint_shape(client):
    client.post("/api/cypher", json={"query": "RETURN 1", "record": True})
    res = client.get("/api/query-rate", params={"span": 3600, "interval": 60})
    assert res.status_code == 200
    buckets = res.json()["buckets"]
    assert len(buckets) >= 2
    assert sum(b["count"] for b in buckets) == 1


def test_mcp_tools_endpoint_has_real_descriptions(client):
    res = client.get("/api/mcp-tools")
    assert res.status_code == 200
    tools = {t["name"]: t for t in res.json()}
    assert "search_component" in tools
    assert tools["search_component"]["description"]
    # enable_run_cypher defaults to False -- the escape-hatch tool shouldn't
    # be advertised unless it's actually registered on the MCP server.
    assert "run_cypher" not in tools


def test_git_log_endpoint_on_empty_repo_returns_empty_list(client):
    # The client fixture's registered repos are `git init`-only, no commits.
    res = client.get("/api/repos/dash_repo_a/git-log")
    assert res.status_code == 200
    assert res.json() == []


def test_git_status_endpoint_shape(client):
    res = client.get("/api/repos/dash_repo_a/git-status")
    assert res.status_code == 200
    body = res.json()
    assert "branch" in body
    assert body["uncommitted"] == []  # nothing was ever added/committed


def test_git_endpoints_unknown_repo_404s(client):
    assert client.get("/api/repos/does-not-exist/git-log").status_code == 404
    assert client.get("/api/repos/does-not-exist/git-status").status_code == 404


def test_settings_endpoint_never_exposes_password(client):
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert "neo4j_password" not in body
    assert body["neo4j_uri"] == "bolt://127.0.0.1:7687"
