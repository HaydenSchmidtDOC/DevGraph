"""`cross_repo=true` honours DEVGRAPH_ALLOW_CROSS_REPO.

The setting existed and was shown in the dashboard, but nothing enforced it:
search_component(cross_repo=True) answered from every repository in the graph
with the setting off. Now every built-in tool that takes `cross_repo` refuses
it with a plain ToolError unless the setting is on, and when it is on, a
cross-repo query reaches only registered repositories -- never a repository
left in the graph after it was unregistered.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from devgraph.config.settings import Settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return [r for r in self.repos if r.active or not active_only]

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


class _RecordingEngine:
    def __init__(self):
        self.calls = []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.calls.append((query, params))
        return [], False

    def run_cypher(self, query, params=None):
        raise AssertionError("built-in tools must not use the unbounded run_cypher")


def _server(engine, tmp_path, monkeypatch, *, allow, repos=("demo", "other")):
    monkeypatch.setattr(
        mcp_server, "get_settings",
        lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", allow_cross_repo=allow),
    )
    records = []
    for name in repos:
        (tmp_path / name).mkdir(exist_ok=True)
        records.append(_Repo(name, tmp_path / name))
    return mcp_server.build_server(engine, _Registry(records), session_repo=records[0], session_source="env")


def _call(server, tool, arguments):
    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(scenario)


def _cross_repo_tools(server):
    async def scenario():
        return await server.list_tools()

    return {
        t.name: t.input_schema
        for t in anyio.run(scenario)
        if "cross_repo" in (t.input_schema.get("properties") or {})
    }


def _required_args(schema):
    """A placeholder for every required argument, so only cross_repo decides the call."""
    props = schema.get("properties") or {}
    return {k: 1 if props[k].get("type") == "integer" else "X" for k in schema.get("required") or ()}


def test_every_cross_repo_tool_refuses_it_while_the_setting_is_off(tmp_path, monkeypatch):
    engine = _RecordingEngine()
    server = _server(engine, tmp_path, monkeypatch, allow=False)
    tools = _cross_repo_tools(server)
    assert "search_component" in tools and len(tools) > 10
    for tool, schema in tools.items():
        result = _call(server, tool, {**_required_args(schema), "cross_repo": True})
        assert result.is_error is True, tool
        text = result.content[0].text
        assert "DEVGRAPH_ALLOW_CROSS_REPO" in text and "Traceback" not in text, (tool, text)
    assert engine.calls == []


def test_cross_repo_false_is_unaffected_by_the_setting(tmp_path, monkeypatch):
    engine = _RecordingEngine()
    server = _server(engine, tmp_path, monkeypatch, allow=False)
    assert _call(server, "list_services", {"cross_repo": False}).is_error is False
    assert engine.calls


def test_an_enabled_cross_repo_query_is_bound_to_the_registered_repositories(tmp_path, monkeypatch):
    engine = _RecordingEngine()
    server = _server(engine, tmp_path, monkeypatch, allow=True)
    assert _call(server, "list_services", {"cross_repo": True}).is_error is False
    ((query, params),) = engine.calls
    assert "$cross_repo_ids" in query
    assert sorted(params["cross_repo_ids"]) == ["demo", "other"]


# --- live: an unregistered repository's nodes stay out -----------------------


@pytest.fixture
def live_engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    yield engine
    engine.close()


def test_live_cross_repo_search_skips_unregistered_repositories(live_engine, tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    registered, orphan = f"zz-xr-reg-{tag}", f"zz-xr-orphan-{tag}"
    name = f"CrossRepoProbe{tag}"
    try:
        for repo in (registered, orphan):
            live_engine.upsert_node("Class", repo, name)
        server = _server(live_engine, tmp_path, monkeypatch, allow=True, repos=(registered,))
        result = _call(server, "search_component", {"query": name, "cross_repo": True})
        assert result.is_error is False, result.content
        assert {r["repo_id"] for r in result.structured_content["results"]} == {registered}
    finally:
        for repo in (registered, orphan):
            live_engine.delete_repository(repo)
