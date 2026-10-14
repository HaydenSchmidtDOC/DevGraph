"""GET/POST /api/repos/{repo_id}/insights with stub engine and registry."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devgraph.analytics import insights
from devgraph.dashboard.routes import build_router

COMMUNITIES = [{"community": i, "label": f"pkg{i}", "size": 20 - i} for i in range(12)]
TOP = [{"name": "hub", "labels": ["Class"], "file": "a.py", "score": 0.5, "community": 0}]


class StubEngine:
    def __init__(self, computed=True, fail=False):
        self.computed = computed
        self.fail = fail
        self.writes = []

    def read_insights_summary(self, repo_id):
        if self.fail:
            raise RuntimeError("Neo4jError: SECRET connection refused")
        if not self.computed:
            return None
        return {"computed_at": "2026-10-01T00:00:00+00:00", "node_count": 40, "community_count": 12,
                "modularity": 0.41, "communities": json.dumps(COMMUNITIES)}

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        return [dict(TOP[0], name=f"hub-{params['limit']}")], False

    def load_insight_graph(self, repo_id, types):
        if self.fail:
            raise RuntimeError("Neo4jError: SECRET")
        nodes = [{"id": n, "name": n, "labels": ["Function"], "file": f"{n}.py"} for n in "ab"]
        return nodes, [{"source": "a", "target": "b", "type": "CALLS"}]

    def write_insights(self, repo_id, rows, summary):
        self.writes.append((repo_id, rows, summary))
        self.computed = True


class StubRegistry:
    def get(self, repo_id):
        return object() if repo_id == "demo" else None


class Events:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def client(engine, events=None):
    app = FastAPI()
    app.include_router(build_router(engine, StubRegistry(), events or Events()))
    return TestClient(app)


def test_not_computed_yet():
    response = client(StubEngine(computed=False)).get("/api/repos/demo/insights")
    assert response.status_code == 200 and response.json() == {"computed": False}


def test_computed_payload_is_trimmed_for_the_card():
    body = client(StubEngine()).get("/api/repos/demo/insights").json()
    assert body["computed"] is True
    assert body["community_count"] == 12 and body["node_count"] == 40 and body["modularity"] == 0.41
    assert body["communities"] == COMMUNITIES[:8]
    assert body["key_nodes"][0]["name"] == "hub-6" and body["bridges"][0]["name"] == "hub-6"


def test_unknown_repo_is_404_for_both_methods():
    c = client(StubEngine())
    assert c.get("/api/repos/nope/insights").status_code == 404
    assert c.post("/api/repos/nope/insights").status_code == 404


def test_graph_failure_is_503_without_driver_text():
    for method in ("get", "post"):
        response = getattr(client(StubEngine(fail=True)), method)("/api/repos/demo/insights")
        assert response.status_code == 503
        assert response.json() == {"detail": "graph unavailable"}
        assert "SECRET" not in response.text


def test_recompute_writes_publishes_and_returns_the_payload():
    engine, events = StubEngine(computed=False), Events()
    response = client(engine, events).post("/api/repos/demo/insights")
    assert response.status_code == 200 and response.json()["computed"] is True
    assert len(engine.writes) == 1
    assert events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]


def test_recompute_while_a_run_holds_the_lock_is_409():
    engine = StubEngine()
    with insights._repo_lock("demo"):
        response = client(engine).post("/api/repos/demo/insights")
    assert response.status_code == 409
    assert engine.writes == []


def test_recompute_rejects_cross_site_requests():
    engine = StubEngine()
    response = client(engine).post("/api/repos/demo/insights", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    response = client(engine).post("/api/repos/demo/insights", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert engine.writes == []
