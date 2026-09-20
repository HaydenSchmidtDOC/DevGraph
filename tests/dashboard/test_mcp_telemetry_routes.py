"""Tests for the dashboard's read-only view of MCP tool-call telemetry.

Unlike tests/dashboard/test_routes.py these need no Neo4j: the endpoints
exercised here read the local telemetry file and the in-memory QueryLog, and
the one Cypher call is served by a stub engine. The point is the boundary
between the two telemetry sources — `/api/mcp-telemetry` is what MCP clients
ran, `/api/query-log` and `/api/query-rate` stay the dashboard console's own.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.mcp import server as mcp_server
from devgraph.registry.store import RepoRegistry


class _StubEngine:
    """Serves the dashboard's Cypher console without a graph behind it."""

    def run_cypher_graph(self, query, params=None):
        return {"data": []}


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: fake)
    registry = RepoRegistry(fake.registry_db_path)
    try:
        yield TestClient(build_app(_StubEngine(), registry, EventBroadcaster()))
    finally:
        registry.close()


def _record(tool: str, ok: bool = True) -> None:
    mcp_server.record_tool_call(tool=tool, repo_id="demo", duration_ms=12.5, ok=ok)


def test_endpoint_returns_recorded_mcp_entries_newest_first(client):
    _record("search_component")
    _record("impact_analysis", ok=False)

    res = client.get("/api/mcp-telemetry")

    assert res.status_code == 200
    entries = res.json()["entries"]
    assert [e["tool"] for e in entries] == ["impact_analysis", "search_component"]
    assert entries[0]["ok"] is False
    assert entries[0]["repo_id"] == "demo"
    assert entries[0]["duration_ms"] == 12.5


def test_endpoint_is_empty_when_nothing_has_been_recorded(client):
    res = client.get("/api/mcp-telemetry")
    assert res.status_code == 200
    assert res.json() == {"entries": []}


def test_limit_is_honoured_and_capped(client):
    for i in range(4):
        _record(f"tool_{i}")

    assert len(client.get("/api/mcp-telemetry", params={"limit": 2}).json()["entries"]) == 2
    # Capped like /query-log: an absurd limit is clamped, not honoured.
    assert len(client.get("/api/mcp-telemetry", params={"limit": 10_000}).json()["entries"]) == 4


def test_query_log_and_query_rate_stay_the_dashboard_consoles_own(client):
    _record("search_component")
    client.post("/api/cypher", json={"query": "RETURN 1", "repo_id": "demo", "record": True})

    entries = client.get("/api/query-log").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["query"] == "RETURN 1"
    assert all(e["query"] != "search_component" for e in entries)

    buckets = client.get("/api/query-rate").json()["buckets"]
    assert sum(b["count"] for b in buckets) == 1

    # ...and the console query never leaks into the MCP view.
    mcp_entries = client.get("/api/mcp-telemetry").json()["entries"]
    assert [e["tool"] for e in mcp_entries] == ["search_component"]
    assert "query" not in mcp_entries[0]


def test_the_store_lives_in_the_devgraph_state_directory(client, tmp_path):
    _record("search_component")
    assert mcp_server.telemetry_path() == Path(tmp_path) / "mcp_telemetry.jsonl"
    assert mcp_server.telemetry_path().exists()
