"""Reloading devgraph.tools.yaml in a running session. Stub engine."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server

ONE = """
    version: 1
    tools:
      - name: list_files
        description: List files.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path
"""

TWO = """
    version: 1
    tools:
      - name: list_files
        description: List files, changed.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path
      - name: count_files
        description: Count files.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) RETURN count(f) AS n
"""


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
        return [], False


def write(repo, text):
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent(text))


def build(tmp_path, monkeypatch, tools=ONE, scoped=True):
    repo = tmp_path / "demo"
    repo.mkdir(exist_ok=True)
    if tools is not None:
        write(repo, tools)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = Repo("demo", repo)
    server = mcp_server.build_server(
        Engine(), Registry([record]), session_repo=record if scoped else None, session_source="env" if scoped else "none"
    )
    return server, repo


def tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def status(server):
    return json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)


def test_an_unchanged_file_is_not_reloaded(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    assert plane.reload_if_changed() is False
    (repo / TOOLS_FILENAME).touch()
    assert plane.reload_if_changed() is False


def test_adding_and_changing_tools_is_served(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    write(repo, TWO)
    assert server.devgraph_tool_plane.reload_if_changed() is True
    listed = tools(server)
    assert {"list_files", "count_files"} <= set(listed)
    assert listed["list_files"].description == "List files, changed."
    assert status(server)["served"] == ["list_files", "count_files"]


def test_a_renamed_tool_replaces_the_old_name(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    write(repo, ONE.replace("list_files", "file_list"))
    assert server.devgraph_tool_plane.reload_if_changed() is True
    listed = tools(server)
    assert "file_list" in listed and "list_files" not in listed


def test_an_invalid_save_serves_nothing_until_fixed(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    (repo / TOOLS_FILENAME).write_text("version: 1\ntools: [oops\n")
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "list_files" not in tools(server)
    current = status(server)
    assert current["served"] == [] and any(TOOLS_FILENAME in n for n in current["notices"])
    write(repo, ONE)
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "list_files" in tools(server)
    assert status(server)["notices"] == []


def test_deleting_the_file_serves_nothing(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    (repo / TOOLS_FILENAME).unlink()
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "list_files" not in tools(server)
    assert status(server)["tools_file"] is None


def test_a_reload_that_changes_bytes_but_not_tools_reports_no_change(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent(ONE) + "# a comment\n")
    assert server.devgraph_tool_plane.reload_if_changed() is False
    assert "list_files" in tools(server)


def test_builtin_names_are_still_refused_after_a_reload(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    write(repo, ONE.replace("list_files", "search_component"))
    server.devgraph_tool_plane.reload_if_changed()
    assert any("search_component" in n and "built-in" in n for n in status(server)["notices"])


def test_the_catalog_follows_a_reload(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    write(repo, TWO)
    server.devgraph_tool_plane.reload_if_changed()
    catalog = json.loads(asyncio.run(server.read_resource("devgraph://tool-catalog"))[0].content)
    assert "count_files" in {entry["name"] for entry in catalog}


def test_without_a_scope_there_is_nothing_to_reload(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, scoped=False)
    assert server.devgraph_tool_plane.repo is None
    assert server.devgraph_tool_plane.reload_if_changed() is False
