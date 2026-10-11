"""god_nodes against a live Neo4j 5, through the MCP client: the degree query must
parse (no pattern expressions) and max_results must be honoured past 50."""

import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from devgraph.config.settings import Settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server

REPO = f"_smoketest_god_nodes_{uuid.uuid4().hex[:8]}"
LEAVES = 60


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


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
def hub(engine):
    """One Class called by LEAVES functions; each leaf also calls the next, so degrees differ."""
    engine.upsert_node("Class", REPO, "Hub")
    leaves = [f"leaf{k:02d}" for k in range(LEAVES)]
    for leaf in leaves:
        engine.upsert_node("Function", REPO, leaf)
        engine.upsert_relationship("Function", leaf, "CALLS", "Class", "Hub", REPO)
    for a, b in zip(leaves, leaves[1:]):
        engine.upsert_relationship("Function", a, "CALLS", "Function", b, REPO)
    yield REPO
    engine.delete_repository(REPO)


def _call(engine, tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(
        mcp_server, "get_settings",
        lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", allow_cross_repo=bool(arguments.get("cross_repo"))),
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    record = _Repo(REPO, tmp_path / "repo")
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("god_nodes", arguments)

    return anyio.run(scenario)


def test_god_nodes_ranks_by_degree_on_neo4j_5(engine, hub, tmp_path, monkeypatch):
    result = _call(engine, tmp_path, monkeypatch, {"max_results": 3})
    assert result.is_error is False, result.content
    body = result.structured_content
    assert [r["name"] for r in body["results"]][0] == "Hub"
    assert body["results"][0]["degree"] == LEAVES
    assert body["results"][0]["labels"] == ["Class"]
    assert len(body["results"]) == 3
    assert body["truncated"] is True
    assert body["count"] == LEAVES + 1


def test_god_nodes_honours_max_results_past_fifty(engine, hub, tmp_path, monkeypatch):
    result = _call(engine, tmp_path, monkeypatch, {"max_results": 55})
    assert result.is_error is False, result.content
    body = result.structured_content
    assert len(body["results"]) == 55
    assert body["truncated"] is True
    assert {r["repo_id"] for r in body["results"]} == {REPO}


def test_god_nodes_cross_repo_runs(engine, hub, tmp_path, monkeypatch):
    result = _call(engine, tmp_path, monkeypatch, {"cross_repo": True, "max_results": 5})
    assert result.is_error is False, result.content
    assert len(result.structured_content["results"]) == 5


class _RecordingEngine:
    def __init__(self):
        self.queries = []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.queries.append(query)
        return [{"total": 0, "top": []}], False

    def run_cypher(self, query, params=None):
        raise AssertionError("built-in tools must not use the unbounded run_cypher")


def test_cross_repo_god_nodes_ranks_every_repositorys_declared_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", allow_cross_repo=True)
    )
    declared = {"a": ("Runbook",), "b": ("Adr",)}
    monkeypatch.setattr(mcp_server.devgraph_tools, "declared_node_labels", lambda registry, repo_id: declared.get(repo_id, ()))
    for name in declared:
        (tmp_path / name).mkdir()
    records = [_Repo(name, tmp_path / name) for name in declared]
    engine = _RecordingEngine()
    server = mcp_server.build_server(engine, _Registry(records), session_repo=records[0], session_source="env")

    async def scenario(arguments):
        async with Client(server, mode="auto") as client:
            return await client.call_tool("god_nodes", arguments)

    assert anyio.run(scenario, {"cross_repo": True}).is_error is False
    (query,) = engine.queries  # one scan ranks and counts
    assert "`Runbook`" in query and "`Adr`" in query
    engine.queries.clear()
    anyio.run(scenario, {})
    (query,) = engine.queries
    assert "`Runbook`" in query and "`Adr`" not in query
