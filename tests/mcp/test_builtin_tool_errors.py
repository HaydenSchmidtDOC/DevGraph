"""A caller's mistake in a built-in tool reaches the client as its own text.

The MCP SDK passes a `ToolError`'s message through but turns any other exception
into a bare `Error executing tool <name>`, so these go over the wire and assert
what the client reads."""

from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server

NOT_COMPUTED = (
    "graph insights have not been computed for this repository yet; the DevGraph agent "
    "computes them after indexing, or run `devgraph insights <repo_id>`"
)


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


class _Engine:
    """A graph with nothing in it and no insights computed."""

    def __init__(self):
        self.queries = []

    def run_cypher(self, query, params=None):
        self.queries.append(query)
        return []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        return self.run_cypher(query, params), False

    def read_insights_summary(self, repo_id):
        return None


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    (tmp_path / "demo").mkdir()
    record = _Repo("demo", tmp_path / "demo")
    engine = _Engine()
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    def run(name, arguments):
        async def scenario():
            async with Client(server, mode="auto") as client:
                return await client.call_tool(name, arguments)

        result = anyio.run(scenario)
        assert result.is_error is True
        (content,) = result.content
        return content.text

    run.engine = engine
    return run


def test_an_unsupported_cycle_relationship_says_which_are_supported(call):
    text = call("find_dependency_cycles", {"relationship": "MENTIONS"})
    assert text == (
        "Error executing tool find_dependency_cycles: unsupported relationship 'MENTIONS' for cycle detection; "
        "supported types are: CALLS, DEPENDS_ON, EXTENDS, IMPORTS, USES"
    )
    assert call.engine.queries == []


def test_the_rejected_input_is_echoed_at_most_100_characters(call):
    text = call("find_dependency_cycles", {"relationship": "zz-" + "x" * 5000})
    assert repr("zz-" + "x" * 97) in text
    assert "x" * 98 not in text
    text = call("key_nodes", {"metric": "y" * 5000})
    assert repr("y" * 100) in text
    assert "y" * 101 not in text


def test_an_unknown_metric_names_the_metrics(call):
    text = call("key_nodes", {"metric": "degree"})
    assert text == "Error executing tool key_nodes: metric must be pagerank or betweenness, not 'degree'"


@pytest.mark.parametrize("name", ["find_communities", "key_nodes"])
def test_insights_not_computed_says_how_to_compute_them(call, name):
    assert call(name, {}) == f"Error executing tool {name}: {NOT_COMPUTED}"


@pytest.mark.parametrize("name", ["find_communities", "key_nodes"])
def test_an_unknown_repo_id_is_named_not_reported_as_uncomputed(call, name):
    assert call(name, {"repo_id": "nope"}) == (
        f"Error executing tool {name}: no such repo_id: 'nope'; run devgraph list to see registered repositories"
    )
