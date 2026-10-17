"""The dashboard with Neo4j down (audit 6, item 9): the repository list still
comes from the registry, and every graph route says Neo4j isn't reachable in
the MCP tools' wording, as a 503, never a bare 500 or the driver's text."""

import subprocess

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import AuthError, ServiceUnavailable

from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.registry.store import RepoRegistry

URI = "bolt://127.0.0.1:7699"
RAW = "Couldn't connect to 127.0.0.1:7699 (resolved to ('127.0.0.1:7699',)): Failed to establish connection to ResolvedIPv4Address"


class DownEngine:
    """Every graph call fails as the driver does with nothing listening."""

    uri = URI

    def __init__(self, exc=None):
        self.exc = exc or ServiceUnavailable(RAW)

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self.exc

        return fail


@pytest.fixture
def registry(tmp_path):
    reg = RepoRegistry(tmp_path / "registry.sqlite3")
    for name in ("alpha", "beta"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        reg.add_repo(repo, repo_id=name)
    yield reg
    reg.close()


def client(registry, engine=None):
    return TestClient(build_app(engine or DownEngine(), registry, EventBroadcaster()), base_url="http://127.0.0.1")


def test_the_repository_list_still_answers_from_the_registry(registry):
    res = client(registry).get("/api/repos")
    assert res.status_code == 200
    body = res.json()
    assert [r["repo_id"] for r in body["repos"]] == ["alpha", "beta"]
    assert all(r["node_count"] is None for r in body["repos"])
    assert body["graph_unavailable"].startswith(f"Neo4j isn't reachable at {URI}; start Neo4j")
    assert "ResolvedIPv4Address" not in res.text


@pytest.mark.parametrize("path", ["/api/repos/alpha/summary", "/api/repos/alpha/schema", "/api/repos/alpha/search?q=x"])
def test_graph_routes_answer_503_with_the_mcp_wording(registry, path):
    res = client(registry).get(path)
    assert res.status_code == 503
    assert res.json()["detail"].startswith(f"Neo4j isn't reachable at {URI}")
    assert "ResolvedIPv4Address" not in res.text


def test_the_console_names_the_problem_not_the_driver_error(registry):
    res = client(registry).post("/api/cypher", json={"query": "RETURN 1 AS ok"})
    assert res.status_code == 503
    error = res.json()["errors"][0]
    assert error["code"] == "Neo4jUnavailable"
    assert error["message"].startswith(f"Neo4j isn't reachable at {URI}") and "podman start devgraph-neo4j" in error["message"]
    assert "ResolvedIPv4Address" not in res.text


def test_a_refused_login_names_the_settings(registry):
    c = client(registry, DownEngine(AuthError("The client is unauthorized")))
    assert "DEVGRAPH_NEO4J_PASSWORD" in c.get("/api/repos").json()["graph_unavailable"]
    assert "DEVGRAPH_NEO4J_PASSWORD" in c.post("/api/cypher", json={"query": "RETURN 1"}).json()["errors"][0]["message"]
