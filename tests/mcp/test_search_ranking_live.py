"""search_component and the dashboard search against a live Neo4j: an exact name
match comes first however many substring matches there are, every result names
its file, a file-less stub never shadows a real node, and count is honest."""

import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from devgraph.config.settings import Settings
from devgraph.dashboard import queries
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server

REPO = f"_smoketest_search_rank_{uuid.uuid4().hex[:8]}"
FILLERS = 300


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
def seeded(engine):
    """FILLERS functions containing `get` (none starting with it) written first, then
    the function named exactly `get`, a file-less handler stub of the same name, a
    lone stub, and two `invoice` functions in different files."""
    nodes = [
        {"label": "Function", "repo_id": REPO, "name": f"x_get_{i:03d}", "properties": {"file": f"f{i % 7}.py"}}
        for i in range(FILLERS)
    ]
    nodes += [
        {"label": "Function", "repo_id": REPO, "name": "get", "properties": {"file": "api/client.py"}},
        {"label": "Function", "repo_id": REPO, "name": "get", "properties": {"file": "", "type": "handler"}},
        {"label": "Function", "repo_id": REPO, "name": "orphan_handler", "properties": {"file": "", "type": "handler"}},
        {"label": "Function", "repo_id": REPO, "name": "invoice", "properties": {"file": "a/invoice.py"}},
        {"label": "Function", "repo_id": REPO, "name": "invoice", "properties": {"file": "b/invoice.py"}},
    ]
    engine.upsert_nodes(nodes)
    yield engine
    engine.delete_repository(REPO)


def _call(engine, tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = _Repo(REPO, tmp_path)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("search_component", arguments)

    result = anyio.run(scenario)
    assert result.is_error is False, result.content
    return result.structured_content


def test_the_exact_match_comes_first_past_two_hundred_substring_matches(seeded, tmp_path, monkeypatch):
    body = _call(seeded, tmp_path, monkeypatch, {"query": "get", "max_results": 5})
    first = body["results"][0]
    assert (first["name"], first["file"]) == ("get", "api/client.py")
    assert body["count"] == FILLERS + 1
    assert body["truncated"] is True
    assert not body.get("count_is_lower_bound")


def test_a_stub_is_dropped_when_a_real_node_has_its_name(seeded, tmp_path, monkeypatch):
    body = _call(seeded, tmp_path, monkeypatch, {"query": "get", "max_results": 200})
    gets = [r for r in body["results"] if r["name"] == "get"]
    assert gets == [{**gets[0], "file": "api/client.py"}]
    assert body["count"] == FILLERS + 1  # the stub is not counted either


def test_a_lone_stub_is_kept(seeded, tmp_path, monkeypatch):
    body = _call(seeded, tmp_path, monkeypatch, {"query": "orphan_handler"})
    assert [r["name"] for r in body["results"]] == ["orphan_handler"]


def test_same_named_results_are_told_apart_by_file(seeded, tmp_path, monkeypatch):
    body = _call(seeded, tmp_path, monkeypatch, {"query": "invoice"})
    assert sorted(r["file"] for r in body["results"]) == ["a/invoice.py", "b/invoice.py"]
    assert body["count"] == 2 and body["truncated"] is False


def test_count_is_a_lower_bound_when_index_matches_fill_the_page(seeded, tmp_path, monkeypatch):
    # Every filler starts with `x`: the index stage alone fills the page, so the
    # substring scan that would count the rest never runs.
    body = _call(seeded, tmp_path, monkeypatch, {"query": "x", "max_results": 3})
    assert [r["name"] for r in body["results"]] == ["x_get_000", "x_get_001", "x_get_002"]
    assert body["truncated"] is True
    assert body["count_is_lower_bound"] is True
    assert body["count"] > 3


def test_the_dashboard_search_ranks_the_exact_match_first(seeded):
    rows = queries.search_components(seeded, REPO, "get", 15)
    assert (rows[0]["name"], rows[0]["file"]) == ("get", "api/client.py")
    assert len(rows) == 15
    assert not any(r["name"] == "get" and not r["file"] for r in rows)


@pytest.fixture
def snake(engine):
    """An exact snake_case name, written last, behind 250 names that share its first
    eleven characters and sort before it (`user_servic000` < `user_service`)."""
    repo = f"{REPO}_snake"
    nodes = [
        {"label": "Function", "repo_id": repo, "name": f"user_servic{i:03d}", "properties": {"file": "s.py"}}
        for i in range(250)
    ]
    nodes.append({"label": "Function", "repo_id": repo, "name": "user_service", "properties": {"file": "svc.py"}})
    engine.upsert_nodes(nodes)
    yield engine, repo
    engine.delete_repository(repo)


def _call_repo(engine, repo, tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = _Repo(repo, tmp_path)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("search_component", arguments)

    result = anyio.run(scenario)
    assert result.is_error is False, result.content
    return result.structured_content


def test_an_exact_snake_case_name_comes_first_past_two_hundred_prefix_matches(snake, tmp_path, monkeypatch):
    engine, repo = snake
    body = _call_repo(engine, repo, tmp_path, monkeypatch, {"query": "user_service", "max_results": 5})
    assert (body["results"][0]["name"], body["results"][0]["file"]) == ("user_service", "svc.py")
    assert queries.search_components(engine, repo, "user_service", 5)[0]["name"] == "user_service"


def test_max_results_is_capped(seeded, tmp_path, monkeypatch):
    from devgraph.mcp.tools import SEARCH_MAX_RESULTS

    body = _call(seeded, tmp_path, monkeypatch, {"query": "get", "max_results": 100_000})
    assert len(body["results"]) == SEARCH_MAX_RESULTS
    assert body["count"] == FILLERS + 1 and body["truncated"] is True
