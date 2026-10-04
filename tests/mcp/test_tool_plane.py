"""Serving devgraph.tools.yaml tools: scope, schema, calls, notices. Stub engine."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tool_plane import SESSION_REPO_ENV, resolve_session_repo

TOOLS = """
    version: 1
    tools:
      - name: list_folder
        description: List the files directly inside a folder.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) WHERE f.path STARTS WITH $folder RETURN f.path AS path, $limit_hint AS hint
        parameters:
          - name: folder
            description: Folder path.
          - name: limit_hint
            type: integer
            required: false
            default: 5
        max_rows: 2
        timeout_s: 7
      - name: search_component
        description: Shadows a built-in.
        cypher: |
          MATCH (n {repo_id: $repo_id}) RETURN n.name AS name
"""


@dataclass
class Repo:
    repo_id: str
    path: Path


class Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


class Engine:
    def __init__(self, rows=None, truncated=False, error=None):
        self.rows, self.truncated, self.error, self.calls = rows or [], truncated, error, []

    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        self.calls.append((query, dict(parameters), timeout_s, max_rows))
        if self.error:
            raise self.error
        return list(self.rows), self.truncated


def build(tmp_path, monkeypatch, engine, tools=TOOLS, repo_id="demo"):
    repo = tmp_path / repo_id
    repo.mkdir(exist_ok=True)
    if tools is not None:
        (repo / TOOLS_FILENAME).write_text(textwrap.dedent(tools))
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = Repo(repo_id, repo)
    return mcp_server.build_server(engine, Registry([record]), session_repo=record, session_source="env"), record


def tool_names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


# ── scope ──────────────────────────────────────────────────────────────────


def test_env_selects_by_repo_id_or_path(tmp_path):
    a, b = Repo("a", tmp_path / "a"), Repo("b", tmp_path / "b")
    for r in (a, b):
        r.path.mkdir()
    registry = Registry([a, b])
    assert resolve_session_repo(registry, {SESSION_REPO_ENV: "b"}, tmp_path) == (b, "env")
    (b.path / "sub").mkdir()
    assert resolve_session_repo(registry, {SESSION_REPO_ENV: str(b.path / "sub")}, tmp_path) == (b, "env")


def test_an_unknown_env_value_never_falls_back_to_cwd(tmp_path):
    a = Repo("a", tmp_path / "a")
    a.path.mkdir()
    assert resolve_session_repo(Registry([a]), {SESSION_REPO_ENV: "nope"}, a.path) == (None, "env")


def test_cwd_picks_the_deepest_registered_repo(tmp_path):
    outer, inner = Repo("outer", tmp_path / "outer"), Repo("inner", tmp_path / "outer" / "inner")
    inner.path.mkdir(parents=True)
    (inner.path / "src").mkdir()
    registry = Registry([outer, inner])
    assert resolve_session_repo(registry, {}, inner.path / "src") == (inner, "cwd")
    assert resolve_session_repo(registry, {}, outer.path) == (outer, "cwd")
    assert resolve_session_repo(registry, {}, tmp_path) == (None, "none")


# ── serving ────────────────────────────────────────────────────────────────


def test_project_tools_are_listed_with_a_typed_schema(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, Engine())
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    tool = tools["list_folder"]
    assert tool.description.startswith("List the files")
    schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
    assert set(schema["properties"]) == {"folder", "limit_hint"}
    assert schema.get("required") == ["folder"]
    assert "repo_id" not in schema["properties"]
    assert tool.annotations.read_only_hint is True


def test_a_call_injects_the_session_repo_and_returns_the_envelope(tmp_path, monkeypatch):
    engine = Engine(rows=[{"path": "a.py\x07"}, {"path": "b.py"}], truncated=True)
    server, _ = build(tmp_path, monkeypatch, engine)
    result = asyncio.run(server.call_tool("list_folder", {"folder": "src"}))
    assert result.is_error is False
    text = json.dumps(result.structured_content, default=str)
    assert '"truncated": true' in text and "a.py" in text and "\\u0007" not in text
    (query, params, timeout_s, max_rows), = engine.calls
    assert params == {"folder": "src", "limit_hint": 5, "repo_id": "demo"}
    assert (timeout_s, max_rows) == (7, 2)


def test_a_caller_cannot_override_repo_id(tmp_path, monkeypatch):
    engine = Engine()
    server, _ = build(tmp_path, monkeypatch, engine)
    try:
        asyncio.run(server.call_tool("list_folder", {"folder": "x", "repo_id": "other"}))
    except Exception:
        pass  # rejecting the unknown argument is fine
    for _, params, _, _ in engine.calls:
        assert params["repo_id"] == "demo"


def test_a_builtin_name_is_not_taken_over(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, Engine())
    status = json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)
    assert "search_component" not in status["served"]
    assert any("search_component" in n and "built-in" in n for n in status["notices"])
    assert status["scope"] == {"repo_id": "demo", "source": "env"}


def test_an_invalid_tools_file_serves_nothing_and_says_why(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, Engine(), tools="version: 1\ntools: [oops\n")
    status = json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)
    assert status["served"] == [] and any(TOOLS_FILENAME in n for n in status["notices"])


def test_a_neo4j_failure_is_a_tool_error_naming_the_tool(tmp_path, monkeypatch):
    from neo4j.exceptions import ClientError

    server, _ = build(tmp_path, monkeypatch, Engine(error=ClientError("Writing in read access mode not allowed")))
    try:
        result = asyncio.run(server.call_tool("list_folder", {"folder": "x"}))
    except Exception as exc:
        assert "list_folder" in str(exc)
        return
    assert result.is_error is True
    assert "list_folder" in json.dumps([getattr(c, "text", str(c)) for c in result.content])


def test_the_catalog_lists_served_project_tools(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, Engine())
    catalog = json.loads(asyncio.run(server.read_resource("devgraph://tool-catalog"))[0].content)
    assert "list_folder" in {entry["name"] for entry in catalog}


def test_without_a_session_repo_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    plain = mcp_server.build_server(Engine(), Registry([]))
    assert "list_folder" not in tool_names(plain)
    status = json.loads(asyncio.run(plain.read_resource("devgraph://project-tools"))[0].content)
    assert status["scope"] == {"repo_id": None, "source": "none"} and status["served"] == []
