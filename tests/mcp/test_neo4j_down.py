"""With Neo4j down, an MCP call fails within seconds with an error saying what to
do, instead of retrying for the driver's default 30 s and returning its raw error."""

import time
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import AuthError, ServiceUnavailable

from devgraph.config.settings import Settings
from devgraph.graph.engine import EngineClosed
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools

#: Nothing listens here (the discard port), so every connection is refused.
CLOSED = "bolt://127.0.0.1:9"


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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("find_callers", {"target_name": "save"}), ("describe_node", {"name": "save"}), ("summarise_repository", {})],
)
def test_a_call_with_neo4j_down_fails_fast_and_says_how_to_fix_it(tmp_path, monkeypatch, tool, arguments):
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=CLOSED)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-neo4j-down", tmp_path)
    engine = mcp_server.mcp_engine(settings)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool(tool, arguments)

    started = time.monotonic()
    try:
        result = anyio.run(scenario)
    finally:
        engine.close()
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, f"{tool} took {elapsed:.1f} s with Neo4j down"
    assert result.is_error is True
    (content,) = result.content
    assert f"Neo4j isn't reachable at {CLOSED}; start it" in content.text
    assert "devgraph doctor" in content.text


def test_a_refused_login_names_the_settings():
    error = tools.neo4j_unavailable(AuthError("unauthorized"), CLOSED)
    assert f"Neo4j at {CLOSED} refused the username or password" in str(error)
    assert "DEVGRAPH_NEO4J_PASSWORD" in str(error)


def test_other_errors_are_not_reported_as_neo4j_down():
    assert tools.neo4j_unavailable(ValueError("x"), CLOSED) is None
    assert tools.neo4j_unavailable(EngineClosed("closing"), CLOSED) is None
    assert tools.neo4j_unavailable(ServiceUnavailable("down"), CLOSED) is not None
