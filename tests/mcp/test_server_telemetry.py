"""Tests for MCP tool-call telemetry: the registration chokepoint in
build_server and the local JSONL store behind it.

Neo4j-free by construction — every tool here runs against a stub engine, so
these exercise the instrumentation and the store rather than the graph
queries (tests/mcp/test_server.py and tests/mcp/test_tools.py already cover
those against live Neo4j, and are deliberately left untouched).
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server


class _StubEngine:
    """`run_cypher` is the only GraphEngine method the tools used here reach."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises

    def run_cypher(self, cypher, params=None):
        if self.raises is not None:
            raise self.raises
        return []


class _StubRegistry:
    """build_server only stows the registry away for tools needing a repo root."""


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point the telemetry store at a throwaway state directory."""
    fake = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: fake)
    return fake


def _build(engine=None):
    return mcp_server.build_server(engine or _StubEngine(), _StubRegistry())


def test_a_tool_call_records_exactly_one_successful_entry(settings):
    server = _build()

    result = asyncio.run(server.call_tool("list_services", {"repo_id": "demo"}))
    assert result.is_error is False

    entries = mcp_server.read_tool_telemetry(100)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "list_services"
    assert entry["repo_id"] == "demo"
    assert entry["ok"] is True
    assert entry["duration_ms"] >= 0


def test_a_failing_tool_call_is_recorded_as_failed(settings):
    server = _build(_StubEngine(raises=RuntimeError("neo4j is down")))

    try:
        asyncio.run(server.call_tool("list_services", {"repo_id": "demo"}))
    except Exception:
        # How the SDK surfaces a tool error is its business; what matters
        # here is that the failure was recorded rather than dropped.
        pass

    entries = mcp_server.read_tool_telemetry(100)
    assert len(entries) == 1
    assert entries[0]["tool"] == "list_services"
    assert entries[0]["ok"] is False


def test_the_original_exception_propagates_unchanged(settings):
    boom = RuntimeError("neo4j is down")

    def failing_tool(repo_id: str) -> dict:
        raise boom

    with pytest.raises(RuntimeError) as excinfo:
        mcp_server._instrument(failing_tool)("demo")

    assert excinfo.value is boom
    assert [e["ok"] for e in mcp_server.read_tool_telemetry(100)] == [False]


def test_no_arguments_cypher_or_results_are_recorded(settings):
    distinctive_argument = "zz-not-a-real-component-name-zz"
    server = _build()

    asyncio.run(
        server.call_tool(
            "search_component", {"repo_id": "demo", "query": distinctive_argument}
        )
    )

    raw = mcp_server.telemetry_path().read_text(encoding="utf-8")
    assert distinctive_argument not in raw
    assert "MATCH" not in raw
    assert "results" not in raw
    assert json.loads(raw.strip()).keys() == {"ts", "tool", "repo_id", "duration_ms", "ok"}


# Runs in its own interpreter, resolving the store from the environment the
# way a real MCP server process spawned by a client would.
_WRITER = (
    "import sys\n"
    "from devgraph.mcp.server import record_tool_call\n"
    "record_tool_call(tool=sys.argv[1], repo_id='demo', duration_ms=1.0, ok=True)\n"
)


def test_two_independent_writer_processes_both_land_in_the_store(settings):
    env = {**os.environ, "DEVGRAPH_REGISTRY_DB_PATH": str(settings.registry_db_path)}
    repo_root = Path(__file__).resolve().parents[2]

    for name in ("first_writer", "second_writer"):
        proc = subprocess.run(
            [sys.executable, "-c", _WRITER, name],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr

    assert {e["tool"] for e in mcp_server.read_tool_telemetry(100)} == {
        "first_writer",
        "second_writer",
    }


def test_the_store_is_bounded_and_keeps_the_newest_entries(settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "_TELEMETRY_MAX_ENTRIES", 3)
    monkeypatch.setattr(mcp_server, "_TELEMETRY_TRIM_AT_BYTES", 1)

    for i in range(6):
        mcp_server.record_tool_call(tool=f"tool_{i}", repo_id="demo", duration_ms=0.0, ok=True)

    entries = mcp_server.read_tool_telemetry(100)
    assert [e["tool"] for e in entries] == ["tool_5", "tool_4", "tool_3"]


def test_an_unwritable_store_does_not_change_the_tool_call(settings):
    # A directory where the store's file belongs: every write attempt fails.
    mcp_server.telemetry_path().mkdir()
    server = _build()

    result = asyncio.run(server.call_tool("list_services", {"repo_id": "demo"}))

    assert result.is_error is False
    assert result.structured_content == {"count": 0, "results": [], "truncated": False}
    assert mcp_server.read_tool_telemetry(100) == []


def test_a_corrupt_store_does_not_change_the_tool_call(settings):
    mcp_server.telemetry_path().write_text('not json\n{"half": \n', encoding="utf-8")
    server = _build()

    result = asyncio.run(server.call_tool("list_services", {"repo_id": "demo"}))

    assert result.is_error is False
    assert result.structured_content == {"count": 0, "results": [], "truncated": False}
    # Unparseable lines are skipped, the new record is still readable.
    assert [e["tool"] for e in mcp_server.read_tool_telemetry(100)] == ["list_services"]


def test_instrumentation_leaves_the_registered_tool_surface_unchanged(settings):
    server = _build()

    tools = asyncio.run(server.list_tools())

    assert len(tools) == 21
    search = next(t for t in tools if t.name == "search_component")
    assert search.description.startswith("Search for components")
    assert search.annotations.read_only_hint is True
