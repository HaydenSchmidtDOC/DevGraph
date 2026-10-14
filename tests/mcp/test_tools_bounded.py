"""Built-in tools are bounded: impact traversal depth, and every query read-only
with a timeout that reaches the client as a ToolError."""

import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from neo4j.exceptions import ClientError

from devgraph.config.settings import Settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools
from devgraph.mcp.tools import IMPACT_MAX_DEPTH, impact_analysis


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
def chain(engine):
    """f0 <- f1 <- ... <- f7: f<k> reaches f0 over k CALLS edges."""
    repo_id = f"zz-impact-depth-{uuid.uuid4().hex[:8]}"
    names = [f"f{k}" for k in range(8)]
    for name in names:
        engine.upsert_node("Function", repo_id, name)
    for caller, callee in zip(names[1:], names):
        engine.upsert_relationship("Function", caller, "CALLS", "Function", callee, repo_id)
    yield repo_id
    engine.delete_repository(repo_id)


def test_impact_transitive_dependents_stop_at_the_max_depth(engine, chain):
    assert IMPACT_MAX_DEPTH == 4
    result = impact_analysis(engine, chain, "f0")
    assert [d["name"] for d in result["direct_dependents"]["results"]] == ["f1"]
    transitive = sorted(d["name"] for d in result["transitive_dependents"]["results"])
    assert transitive == ["f2", "f3", "f4"]  # f5 is 5 hops away


def test_impact_queries_carry_no_unbounded_path():
    import inspect

    source = inspect.getsource(tools)
    assert "*2..]" not in source and "*2..}" not in source


def test_builtin_queries_run_read_only_with_a_timeout():
    seen = {}

    class Engine:
        def run_read_cypher(self, query, params, *, timeout_s, max_rows):
            seen.update(timeout_s=timeout_s, max_rows=max_rows)
            return [], False

        def run_cypher(self, query, params=None):
            raise AssertionError("built-in tools must not use the unbounded run_cypher")

    with pytest.raises(ToolError, match="no component named 'f0'"):
        impact_analysis(Engine(), "demo", "f0")
    assert seen["timeout_s"] == tools.BUILTIN_TIMEOUT_S


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


class _SlowEngine:
    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        raise ClientError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="secret")

    def run_cypher(self, query, params=None):
        raise AssertionError("built-in tools must not use the unbounded run_cypher")


def test_a_timeout_reaches_the_client_as_a_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    (tmp_path / "demo").mkdir()
    record = _Repo("demo", tmp_path / "demo")
    server = mcp_server.build_server(_SlowEngine(), _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("impact_analysis", {"component_name": "f0"})

    result = anyio.run(scenario)
    assert result.is_error is True
    (content,) = result.content
    assert content.text == (
        f"Error executing tool impact_analysis: query timed out after {tools.BUILTIN_TIMEOUT_S} s; narrow the request"
    )


#: The hub fixture: layers of callers above `HUBS` same-named `get` functions.
WIDTH, DEPTH, HUBS, FAN = 200, 5, 20, 5


@pytest.fixture
def hub(engine):
    """`HUBS` functions all named `get`, each called by every node of layer 1;
    every node of layer k+1 calls `FAN` random nodes of layer k (seeded). The
    shape of a common name in a real repository: few distinct dependents, very
    many paths to them. Yields the repo id and each dependent's hop distance."""
    import random

    repo_id = f"zz-impact-hub-{uuid.uuid4().hex[:8]}"
    rnd = random.Random(7)
    layers = [[f"l{k}_{i}" for i in range(WIDTH)] for k in range(1, DEPTH + 1)]
    edges = sorted({(a, rnd.choice(below)) for below, above in zip(layers, layers[1:]) for a in above for _ in range(FAN)})
    engine.run_cypher(
        "UNWIND $names AS name CREATE (:Function {repo_id: $r, name: name, file: name + '.py'})",
        {"r": repo_id, "names": [n for layer in layers for n in layer]},
    )
    engine.run_cypher(
        "UNWIND range(1, $hubs) AS i CREATE (:Function {repo_id: $r, name: 'get', file: 'get' + i + '.py'})",
        {"r": repo_id, "hubs": HUBS},
    )
    engine.run_cypher(
        "MATCH (a:Function {repo_id: $r}), (g:Function {repo_id: $r, name: 'get'}) "
        "WHERE a.name STARTS WITH 'l1_' CREATE (a)-[:CALLS]->(g)",
        {"r": repo_id},
    )
    engine.run_cypher(
        "UNWIND $edges AS e MATCH (a:Function {repo_id: $r, name: e[0]}), (b:Function {repo_id: $r, name: e[1]}) "
        "CREATE (a)-[:CALLS]->(b)",
        {"r": repo_id, "edges": edges},
    )
    distance = {n: 1 for n in layers[0]}
    callers: dict[str, set[str]] = {}
    for a, b in edges:
        callers.setdefault(b, set()).add(a)
    frontier = set(layers[0])
    for hop in range(2, IMPACT_MAX_DEPTH + 1):
        frontier = {a for b in frontier for a in callers.get(b, ())} - distance.keys()
        distance.update(dict.fromkeys(frontier, hop))
    yield repo_id, distance
    engine.delete_repository(repo_id)


def test_impact_on_a_hub_is_fast_and_counts_distinct_dependents(engine, hub):
    import time

    repo_id, distance = hub
    started = time.monotonic()
    result = impact_analysis(engine, repo_id, "get", max_results=5000)
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"impact_analysis took {elapsed:.2f}s on the hub fixture"
    assert result["direct_dependents"]["count"] == WIDTH
    assert result["risk_level"] == "high"
    transitive = {d["name"] for d in result["transitive_dependents"]["results"]}
    assert transitive == {n for n, hop in distance.items() if hop >= 2}
    assert not any(n.startswith("l5_") for n in transitive)  # five hops away


def test_impact_for_diff_on_a_hub_is_fast(engine, hub, tmp_path):
    import subprocess
    import time

    from devgraph.mcp.tools import impact_analysis_for_diff
    from devgraph.registry.store import RepoRegistry

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")
    repo_id, distance = hub
    (root / "hub.py").write_text("def get():\n    return 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (root / "hub.py").write_text("def get():\n    return 2\n")
    git("commit", "-q", "-am", "change")
    engine.run_cypher(
        "MATCH (f:Function {repo_id: $r, name: 'get', file: 'get1.py'}) SET f.file = 'hub.py'",
        {"r": repo_id},
    )
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    try:
        registry.add_repo(root, repo_id=repo_id)
        started = time.monotonic()
        result = impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD", max_results=5000)
        elapsed = time.monotonic() - started
    finally:
        registry.close()
    assert elapsed < 3.0, f"impact_analysis_for_diff took {elapsed:.2f}s on the hub fixture"
    assert result["changed_components"] == ["get"]
    assert result["direct_dependents"]["count"] == WIDTH
    assert result["transitive_dependents"]["count"] == sum(1 for hop in distance.values() if hop >= 2)


class _InsightsEngine:
    """Insights computed; every query must be a bounded read."""

    def __init__(self, error=None):
        self.error = error
        self.timeouts = []

    def read_insights_summary(self, repo_id):
        return {"computed_at": "2026-10-01T00:00:00+00:00", "node_count": 2, "community_count": 1,
                "modularity": 0.1, "communities": '[{"community": 0, "label": "core", "size": 2}]'}

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.timeouts.append(timeout_s)
        if self.error is not None:
            raise self.error
        return [], False

    def run_cypher(self, query, params=None):
        raise AssertionError("insight tools must not use the unbounded run_cypher")


class _Known:
    def get(self, repo_id):
        return object()


@pytest.mark.parametrize("tool", [tools.key_nodes, tools.find_communities])
def test_insight_tools_read_with_a_timeout(tool):
    engine = _InsightsEngine()
    tool(engine, _Known(), "demo")
    assert engine.timeouts and all(t == tools.BUILTIN_TIMEOUT_S for t in engine.timeouts)


@pytest.mark.parametrize("tool", [tools.key_nodes, tools.find_communities])
def test_an_insight_timeout_is_a_tool_error(tool):
    from mcp.server.mcpserver.exceptions import ToolError

    timed_out = ClientError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="slow")
    with pytest.raises(ToolError, match="query timed out after"):
        tool(_InsightsEngine(error=timed_out), _Known(), "demo")
