"""search_component covers a repository's schema-declared labels."""

import asyncio
import textwrap
from dataclasses import dataclass
from pathlib import Path

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tools import declared_node_labels, search_component

BUILTIN_PREDICATE = "(n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint)"
SCHEMA = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
"""


class StubEngine:
    def __init__(self):
        self.queries = []

    def run_cypher(self, query, params=None):
        self.queries.append((query, params or {}))
        return []


@dataclass
class Repo:
    path: Path


class StubRegistry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return self.repos.get(repo_id)


def test_without_extra_labels_the_query_is_unchanged():
    engine = StubEngine()
    search_component(engine, "demo", "widget")
    assert BUILTIN_PREDICATE in engine.queries[0][0]


def test_extra_labels_join_the_label_predicate():
    engine = StubEngine()
    search_component(engine, "demo", "readme", extra_labels=("File", "Folder"))
    query = engine.queries[0][0]
    assert "(n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint OR n:File OR n:Folder)" in query


def test_labels_that_are_not_identifiers_are_never_interpolated():
    engine = StubEngine()
    search_component(engine, "demo", "x", extra_labels=("File", "Bad) DETACH DELETE n //"))
    query = engine.queries[0][0]
    assert "DETACH" not in query and "n:File" in query


def test_declared_labels_come_from_the_repo_schema(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    registry = StubRegistry({"demo": Repo(tmp_path)})
    assert declared_node_labels(registry, "demo") == ("File",)


def test_unknown_repo_missing_or_invalid_schema_declare_nothing(tmp_path):
    assert declared_node_labels(StubRegistry({}), "demo") == ()
    assert declared_node_labels(StubRegistry({"demo": Repo(tmp_path)}), "demo") == ()
    (tmp_path / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    assert declared_node_labels(StubRegistry({"demo": Repo(tmp_path)}), "demo") == ()
    assert declared_node_labels(None, "demo") == ()


def test_the_mcp_tool_searches_declared_labels(tmp_path, monkeypatch):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    engine = StubEngine()
    server = mcp_server.build_server(engine, StubRegistry({"demo": Repo(tmp_path)}))
    asyncio.run(server.call_tool("search_component", {"repo_id": "demo", "query": "readme"}))
    assert any("n:File" in q for q, _ in engine.queries)
