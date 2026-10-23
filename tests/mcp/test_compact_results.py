"""Tool results and resources reach the client as compact JSON. Stub engine, no Neo4j."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.config import global_tools
from devgraph.config.global_tools import GLOBAL_TOOLS_FILENAME, save_global_tools
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools as devgraph_tools

pytestmark = pytest.mark.usefixtures("trusted_project_tools")

PROJECT = """
    version: 1
    tools:
      - name: list_files
        description: Project list.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path
"""
G_COUNT = {
    "name": "g_count",
    "description": "Global count.",
    "cypher": "MATCH (f:File {repo_id: $repo_id}) RETURN count(f) AS n",
}


@dataclass
class Repo:
    repo_id: str
    path: Path
    active: bool = True


class Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


class Engine:
    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        return [{"path": "src/café.py", "n": 2}, {"path": "src/b.py", "n": 1}], False


@pytest.fixture
def server(tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent(PROJECT))
    path = tmp_path / "home" / GLOBAL_TOOLS_FILENAME
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    save_global_tools([G_COUNT])
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = Repo("demo", repo)
    return mcp_server.build_server(Engine(), Registry([record]), session_repo=record, session_source="env")


def _call(server, name, arguments=None):
    return asyncio.run(server.call_tool(name, arguments or {}))


def _assert_compact(result):
    assert result.is_error is False
    (block,) = result.content
    assert "\n" not in block.text and ": " not in block.text and ", " not in block.text
    assert json.loads(block.text) == result.structured_content
    assert block.text == json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
    return block.text


def test_a_builtin_result_is_one_compact_text_block(server, monkeypatch):
    rows = [{"name": "Zoë", "labels": ["Function"], "file": "src/naïve.py"}, {"name": "b", "labels": ["Class"]}]
    monkeypatch.setattr(
        devgraph_tools, "search_component",
        lambda *args, **kwargs: {"count": 2, "results": rows, "truncated": False},
    )
    text = _assert_compact(_call(server, "search_component", {"query": "x"}))
    assert "Zoë" in text and "naïve" in text  # UTF-8 kept, not \u-escaped
    # Key order is the tool's own: the session-default repo_id/notices come after it.
    assert list(json.loads(text))[:3] == ["count", "results", "truncated"]


def test_a_non_json_value_becomes_a_string_in_both_forms(server, monkeypatch):
    from neo4j import time as neo4j_time

    when = neo4j_time.DateTime(2026, 1, 2, 3, 4, 5)
    monkeypatch.setattr(devgraph_tools, "search_component", lambda *a, **k: {"count": 1, "when": when})
    result = _call(server, "search_component", {"query": "x"})
    _assert_compact(result)
    assert result.structured_content["when"] == str(when)


@pytest.mark.parametrize("name", ["list_files", "g_count"])
def test_project_and_global_results_are_compact(server, name):
    text = _assert_compact(_call(server, name))
    assert "café" in text


def test_an_error_passes_through_as_its_plain_message(server, monkeypatch):
    def fail(*args, **kwargs):
        raise ToolError("no such thing")

    monkeypatch.setattr(devgraph_tools, "search_component", fail)
    with pytest.raises(ToolError, match="search_component: no such thing$"):
        _call(server, "search_component", {"query": "x"})


@pytest.mark.parametrize("uri", ["devgraph://project-tools", "devgraph://tool-catalog"])
def test_json_resources_are_compact(server, uri):
    content = asyncio.run(server.read_resource(uri))[0].content
    assert "\n" not in content
    assert content == json.dumps(json.loads(content), separators=(",", ":"), ensure_ascii=False)


def test_the_listed_output_schema_is_unchanged(server):
    tool = {t.name: t for t in asyncio.run(server.list_tools())}["search_component"]
    assert tool.output_schema["type"] == "object"


# Representative result shapes, with fictional data, for the size check below.
def _node(i):
    return {"name": f"handle_request_{i}", "labels": ["Function"], "repo_id": "demo",
            "description": "Handles one request.", "file": f"src/service/module_{i % 7}.py"}


SAMPLES = {
    "search_component": {"count": 50, "results": [_node(i) for i in range(50)], "truncated": True},
    "find_callers": {
        "count": 8, "truncated": False,
        "results": [{"caller": f"run_{i}", "labels": ["Function"], "file": f"src/jobs/j{i}.py",
                     "confidence": "resolved", "owner": "Worker", "line": 10 + i} for i in range(8)],
    },
    "god_nodes": {"count": 20, "truncated": True,
                  "results": [{**_node(i), "degree": 200 - i} for i in range(20)]},
    "describe_node": {
        "status": "found",
        "node": {"label": "Module", "name": "src/service/api.py", "file": "src/service/api.py",
                 "properties": {"path": "src/service/api.py", "language": "python", "loc": 412}},
        "outgoing": {"IMPORTS": {"count": 6, "truncated": False,
                                 "results": [{"label": "Module", "name": f"src/m{i}.py"} for i in range(6)]},
                     "CONTAINS": {"count": 9, "truncated": False,
                                  "results": [{"label": "Function", "name": f"f{i}"} for i in range(9)]}},
        "incoming": {"IMPORTS": {"count": 3, "truncated": False,
                                 "results": [{"label": "Module", "name": f"src/u{i}.py"} for i in range(3)]}},
        "groups_truncated": False,
    },
}


def measure():
    """(tool, pretty-printed chars as the SDK rendered them, compact chars) per sample."""
    import pydantic_core

    return [
        (name, len(pydantic_core.to_json(data, indent=2).decode()), len(mcp_server.compact_json(data)))
        for name, data in SAMPLES.items()
    ]


def test_compact_text_is_much_smaller_than_pretty_printed():
    for name, pretty, compact in measure():
        assert compact <= 0.7 * pretty, (name, pretty, compact)


if __name__ == "__main__":
    for row in measure():
        print("%-18s pretty=%6d compact=%6d (%.0f%%)" % (*row, 100 * row[2] / row[1]))
