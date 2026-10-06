"""find_communities and key_nodes: stub engines, no Neo4j."""

import asyncio
import json

import pytest

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tools import find_communities, key_nodes

COMMUNITIES = [
    {"community": 0, "label": "auth", "size": 3},
    {"community": 1, "label": "billing", "size": 3},
    {"community": 2, "label": "misc", "size": 1},
]


class StubEngine:
    def __init__(self, summary="computed", rows=None):
        self.summary = summary
        self.rows = rows if rows is not None else []
        self.queries = []

    def read_insights_summary(self, repo_id):
        if self.summary is None:
            return None
        return {"computed_at": "2026-10-01T00:00:00+00:00", "node_count": 7, "community_count": 3,
                "modularity": 0.36, "communities": json.dumps(COMMUNITIES)}

    def run_cypher(self, query, params=None):
        self.queries.append((query, params or {}))
        return self.rows


def test_communities_with_members_for_the_shown_ones_only():
    members = [{"community": 0, "members": [{"name": "login\x07", "labels": ["Function"], "file": "auth/a.py", "pagerank": 0.4}]},
               {"community": 1, "members": []}]
    engine = StubEngine(rows=members)
    result = find_communities(engine, "demo", max_results=2, members_per_community=3)
    assert result["count"] == 3 and result["truncated"] is True
    assert [r["label"] for r in result["results"]] == ["auth", "billing"]
    assert result["results"][0]["top_members"][0]["name"] == "login"  # control char stripped
    assert result["results"][1]["top_members"] == []
    (query, params), = engine.queries
    assert params["communities"] == [0, 1] and params["k"] == 3


def test_members_per_community_is_clamped():
    engine = StubEngine(rows=[])
    find_communities(engine, "demo", members_per_community=500)
    assert engine.queries[0][1]["k"] == 20
    find_communities(engine, "demo", members_per_community=0)
    assert engine.queries[1][1]["k"] == 1


def test_never_computed_is_an_error_that_names_the_command():
    with pytest.raises(ValueError, match="devgraph insights"):
        find_communities(StubEngine(summary=None), "demo")
    with pytest.raises(ValueError, match="devgraph insights"):
        key_nodes(StubEngine(summary=None), "demo")


def test_computed_but_empty_is_an_empty_envelope_not_an_error():
    class Empty(StubEngine):
        def read_insights_summary(self, repo_id):
            return {"computed_at": "x", "node_count": 0, "community_count": 0, "modularity": 0.0, "communities": "[]"}

    assert find_communities(Empty(), "demo") == {"count": 0, "results": [], "truncated": False}
    assert key_nodes(Empty(), "demo") == {"count": 0, "results": [], "truncated": False}


@pytest.mark.parametrize(("metric", "prop"), [("pagerank", "insight_pagerank"), ("BETWEENNESS", "insight_betweenness")])
def test_key_nodes_queries_the_allow_listed_property(metric, prop):
    engine = StubEngine(rows=[{"name": "hub", "labels": ["Class"], "file": "a.py", "score": 0.5, "community": 0}])
    result = key_nodes(engine, "demo", metric=metric, max_results=5)
    assert result["count"] == 1 and result["results"][0]["name"] == "hub"
    (query, params), = engine.queries
    assert f"n.{prop}" in query and params["repo_id"] == "demo"


@pytest.mark.parametrize("metric", ["degree", "pagerank; MATCH (x) DETACH DELETE x", "", None, 3])
def test_key_nodes_rejects_other_metrics_without_echoing_them(metric):
    engine = StubEngine()
    with pytest.raises(ValueError) as excinfo:
        key_nodes(engine, "demo", metric=metric)
    assert "DETACH" not in str(excinfo.value)
    assert engine.queries == []


class _StubRegistry:
    def get(self, repo_id):
        return None


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: fake)
    return mcp_server.build_server(StubEngine(rows=[]), _StubRegistry())


def test_both_tools_are_registered_read_only_and_catalogued(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("find_communities", "key_nodes"):
        assert tools[name].annotations.read_only_hint is True
    catalog = json.loads(asyncio.run(server.read_resource("devgraph://tool-catalog"))[0].content)
    assert {"find_communities", "key_nodes"} <= {entry["name"] for entry in catalog}
