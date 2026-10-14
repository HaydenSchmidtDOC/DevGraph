"""search_component covers a repository's schema-declared labels."""

import asyncio
import textwrap
from dataclasses import dataclass
from pathlib import Path

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tools import declared_node_labels, search_component

BUILTIN_LABELS = ("Service", "Module", "Class", "Function", "Endpoint")
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

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        return self.run_cypher(query, params), False


@dataclass
class Repo:
    path: Path


class StubRegistry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return self.repos.get(repo_id)


def _searched(query):
    """The labels a search query's per-label branches match, in order."""
    import re

    return tuple(re.findall(r"MATCH \(n:`(\w+)`\)", query))


def test_without_extra_labels_the_builtin_labels_are_searched():
    engine = StubEngine()
    search_component(engine, "demo", "widget")
    assert all(_searched(q) == BUILTIN_LABELS for q, _ in engine.queries)


def test_extra_labels_join_the_label_predicate():
    engine = StubEngine()
    search_component(engine, "demo", "readme", extra_labels=("File", "Folder"))
    assert all(_searched(q) == (*BUILTIN_LABELS, "File", "Folder") for q, _ in engine.queries)


def test_labels_that_are_not_identifiers_are_never_interpolated():
    engine = StubEngine()
    search_component(engine, "demo", "x", extra_labels=("File", "Bad) DETACH DELETE n //"))
    assert all("DETACH" not in q and "n:`File`" in q for q, _ in engine.queries)


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
    assert any("n:`File`" in q for q, _ in engine.queries)
