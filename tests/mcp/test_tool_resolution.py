"""Resolving built-in, global and project tools in one MCP session. Stub engine."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.config import global_tools
from devgraph.config.global_tools import GLOBAL_TOOLS_FILENAME, save_global_tools
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server

# Written before the per-repository opt-in: these tests assume project tools are served.
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
G_LIST = {
    "name": "list_files",
    "description": "Global list.",
    "cypher": "MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path",
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
    def __init__(self, error=None):
        self.error = error

    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        if self.error:
            raise self.error
        return [], False


def store(tmp_path, monkeypatch, tools):
    """Point the global store at tmp_path and write `tools` to it; return its path."""
    path = tmp_path / "home" / GLOBAL_TOOLS_FILENAME
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    save_global_tools(tools)
    return path


def build(tmp_path, monkeypatch, project=None, scoped=True, engine=None):
    repo = tmp_path / "demo"
    repo.mkdir(exist_ok=True)
    if project is not None:
        (repo / TOOLS_FILENAME).write_text(textwrap.dedent(project))
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = Repo("demo", repo)
    server = mcp_server.build_server(
        engine or Engine(), Registry([record]), session_repo=record if scoped else None, session_source="env" if scoped else "none"
    )
    return server, repo


def tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def status(server):
    return json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)


def call(server, name):
    return asyncio.run(server.call_tool(name, {})).structured_content


def test_a_global_tool_is_served_in_a_scoped_session(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch)
    assert "g_count" in tools(server)
    current = status(server)
    assert current["origins"] == {"g_count": "global"}
    assert current["global_tools_file"] == str(path)
    assert set(call(server, "g_count")) == {"count", "results", "truncated"}
    catalog = json.loads(asyncio.run(server.read_resource("devgraph://tool-catalog"))[0].content)
    assert GLOBAL_TOOLS_FILENAME in next(e for e in catalog if e["name"] == "g_count")["note"]


def test_an_unscoped_session_serves_no_global_tools_and_says_why(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch, scoped=False)
    assert "g_count" not in tools(server)
    current = status(server)
    assert current["served"] == [] and current["global_tools_file"] is None
    assert any("global tools" in n and "repository" in n for n in current["notices"])


def test_no_global_store_adds_nothing(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, project=PROJECT)
    current = status(server)
    assert current["origins"] == {"list_files": "project"}
    assert current["global_tools_file"] is None and current["notices"] == []


def test_a_project_tool_overrides_a_global_one(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_LIST, G_COUNT])
    server, _ = build(tmp_path, monkeypatch, project=PROJECT)
    assert tools(server)["list_files"].description == "Project list."
    current = status(server)
    assert current["origins"] == {"list_files": "project (overrides global)", "g_count": "global"}
    assert call(server, "list_files")["notices"] == ["resolved: project override of global tool 'list_files'"]
    assert "notices" not in call(server, "g_count")


def test_a_project_tool_that_cannot_register_falls_back_to_the_global(tmp_path, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    original = MCPServer.add_tool

    def flaky(self, fn, *args, **kwargs):
        if kwargs.get("description") == "Project list.":
            raise ValueError("boom: cannot build schema")
        return original(self, fn, *args, **kwargs)

    monkeypatch.setattr(MCPServer, "add_tool", flaky)
    store(tmp_path, monkeypatch, [G_LIST])
    server, _ = build(tmp_path, monkeypatch, project=PROJECT)
    assert tools(server)["list_files"].description == "Global list."
    current = status(server)
    assert current["origins"] == {"list_files": "global"}
    assert any("could not be served" in n for n in current["notices"])
    (notice,) = call(server, "list_files")["notices"]
    assert notice.startswith("used global tool 'list_files':") and "boom" in notice


def test_a_project_file_that_is_not_yaml_marks_every_global_tool(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_LIST, G_COUNT])
    server, _ = build(tmp_path, monkeypatch, project="version: 1\ntools: [oops\n")
    assert tools(server)["list_files"].description == "Global list."
    for name in ("list_files", "g_count"):
        (notice,) = call(server, name)["notices"]
        assert notice.startswith(f"used global tool '{name}': {TOOLS_FILENAME} is invalid")
        assert "a project tool of this name (if any) can't be served" in notice
        assert str(tmp_path) not in notice


def test_an_invalid_project_file_marks_only_the_global_tools_it_names(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_LIST, G_COUNT])
    invalid = "version: 1\ntools:\n  - name: list_files\n    description: No cypher.\n"
    server, _ = build(tmp_path, monkeypatch, project=invalid)
    assert tools(server)["list_files"].description == "Global list."
    (notice,) = call(server, "list_files")["notices"]
    assert notice.startswith(f"used global tool 'list_files': {TOOLS_FILENAME} is invalid")
    assert str(tmp_path) not in notice
    assert "notices" not in call(server, "g_count")


def _client_error(code):
    from neo4j.exceptions import ClientError

    return ClientError._hydrate_neo4j(code=code, message="secret-host:7687")


def _error_text(server, name):
    try:
        result = asyncio.run(server.call_tool(name, {}))
    except Exception as exc:
        return str(exc)
    assert result.is_error is True
    return json.dumps([getattr(c, "text", str(c)) for c in result.content])


def test_a_global_tool_timeout_names_a_global_tool(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_COUNT])
    engine = Engine(error=_client_error("Neo.ClientError.Transaction.TransactionTimedOut"))
    server, _ = build(tmp_path, monkeypatch, engine=engine)
    text = _error_text(server, "g_count")
    assert "global tool 'g_count' timed out after" in text and "project" not in text


def test_a_global_tool_write_names_global_tools_read_only(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch, engine=Engine(error=_client_error("Neo.ClientError.Request.AccessMode")))
    text = _error_text(server, "g_count")
    assert "global tool 'g_count' tried to write; global tools are read-only" in text and "project" not in text


def test_a_global_tool_with_a_builtin_name_is_ignored(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [{**G_COUNT, "name": "search_component", "description": "Shadow."}])
    server, _ = build(tmp_path, monkeypatch)
    assert tools(server)["search_component"].description != "Shadow."
    current = status(server)
    assert "search_component" not in current["served"]
    assert GLOBAL_SHADOW_NOTICE in current["notices"]


SHADOW_PROJECT = PROJECT.replace("list_files", "search_component")
PROJECT_SHADOW_NOTICE = "ignored: project tool 'search_component' shadows a locked tool; using the fixed implementation"
GLOBAL_SHADOW_NOTICE = "ignored: global tool 'search_component' shadows a locked tool; using the fixed implementation"


def search(server):
    return asyncio.run(server.call_tool("search_component", {"repo_id": "demo", "query": "x"})).structured_content


def test_a_shadowed_builtin_says_so_in_its_response(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, project=SHADOW_PROJECT)
    result = search(server)
    assert {"count", "results", "truncated"} <= set(result)
    assert result["notices"] == [PROJECT_SHADOW_NOTICE]


def test_a_builtin_shadowed_by_a_global_tool_says_so_in_its_response(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [{**G_COUNT, "name": "search_component"}])
    server, _ = build(tmp_path, monkeypatch, project=SHADOW_PROJECT)
    assert search(server)["notices"] == [GLOBAL_SHADOW_NOTICE, PROJECT_SHADOW_NOTICE]
    assert [n for n in status(server)["notices"] if "shadows" in n] == [GLOBAL_SHADOW_NOTICE, PROJECT_SHADOW_NOTICE]


def test_an_invalid_save_keeps_the_shadow_notice(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch, project=SHADOW_PROJECT)
    (repo / TOOLS_FILENAME).write_text("version: 1\ntools: [oops\n")
    server.devgraph_tool_plane.reload_if_changed()
    assert any("last good" in n for n in status(server)["notices"])
    assert search(server)["notices"] == [PROJECT_SHADOW_NOTICE]


def test_the_shadow_notice_follows_a_reload(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [{**G_COUNT, "name": "search_component"}])
    server, _ = build(tmp_path, monkeypatch)
    assert search(server)["notices"] == [GLOBAL_SHADOW_NOTICE]
    save_global_tools([G_COUNT], path)
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "notices" not in search(server)


def test_an_unshadowed_builtin_response_is_unchanged(tmp_path, monkeypatch):
    for scoped in (True, False):
        server, _ = build(tmp_path, monkeypatch, project=PROJECT, scoped=scoped)
        assert set(search(server)) == {"count", "results", "truncated"}


def test_an_unscoped_session_reports_no_shadowing(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [{**G_COUNT, "name": "search_component"}])
    server, _ = build(tmp_path, monkeypatch, scoped=False)
    assert set(search(server)) == {"count", "results", "truncated"}


def test_a_shadowed_builtin_with_another_dict_shape_carries_the_notice(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, project=PROJECT.replace("list_files", "explain_architecture"))
    result = asyncio.run(server.call_tool("explain_architecture", {"repo_id": "demo"})).structured_content
    assert {"services_and_datastores", "endpoints"} <= set(result)
    assert result["notices"] == [
        "ignored: project tool 'explain_architecture' shadows a locked tool; using the fixed implementation"
    ]


def test_a_shadowed_builtin_returning_a_list_is_unchanged(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, project=PROJECT.replace("list_files", "find_requirements_for"))
    args = {"repo_id": "demo", "component_name": "x"}
    result = asyncio.run(server.call_tool("find_requirements_for", args)).structured_content
    assert result == {"result": []}


def test_disabling_the_project_config_keeps_the_global_tools(tmp_path, monkeypatch):
    from devgraph.config import project_switch
    from devgraph.registry.store import RepoRegistry

    store(tmp_path, monkeypatch, [G_LIST, G_COUNT])
    server, repo = build(tmp_path, monkeypatch, project=PROJECT)
    (repo / ".git").mkdir()
    registry = RepoRegistry(tmp_path / "r.sqlite3")
    registry.add_repo(repo, repo_id="demo")
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "r.sqlite3")

    registry.set_project_config_enabled("demo", False)
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert tools(server)["list_files"].description == "Global list."
    assert status(server)["origins"] == {"list_files": "global", "g_count": "global"}
    assert "notices" not in call(server, "list_files")


def test_changing_the_global_store_reloads(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    assert plane.reload_if_changed() is False
    save_global_tools([G_COUNT, G_LIST], path)
    assert plane.reload_if_changed() is True
    assert {"g_count", "list_files"} <= set(tools(server))
    assert status(server)["served"] == ["g_count", "list_files"]


def test_an_invalid_global_store_keeps_the_last_good_global_tools(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    path.write_text('{"version": 1, "tools": [oops')
    assert plane.reload_if_changed() is False
    assert "g_count" in tools(server)
    current = status(server)
    assert current["served"] == ["g_count"]
    assert any(GLOBAL_TOOLS_FILENAME in n and "last good" in n for n in current["notices"])


def test_an_invalid_global_store_at_startup_serves_none(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    path.write_text("not: [valid")
    server, _ = build(tmp_path, monkeypatch)
    assert "g_count" not in tools(server)
    assert any(GLOBAL_TOOLS_FILENAME in n for n in status(server)["notices"])


def test_removing_the_global_store_drops_the_global_tools(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    server, _ = build(tmp_path, monkeypatch)
    path.unlink()
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "g_count" not in tools(server)
    assert status(server)["global_tools_file"] is None


def test_a_project_change_keeps_the_global_tools(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_LIST])
    server, repo = build(tmp_path, monkeypatch, project=PROJECT)
    (repo / TOOLS_FILENAME).unlink()
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert tools(server)["list_files"].description == "Global list."
    assert status(server)["origins"] == {"list_files": "global"}


def test_an_unscoped_load_uses_the_global_fingerprint_it_was_given(tmp_path):
    from devgraph.mcp.tool_plane import register_project_tools

    status = register_project_tools(  # the store is absent now; an empty one was read before
        mcp_server.MCPServer("t"), Engine(), None, "none", instrument=lambda f: f, annotations=None,
        global_fingerprint=b"",
    )
    assert any("global tools" in n for n in status.notices)


BAD_DATE_PROJECT = "version: 1\ntools:\n  - name: list_files\n    description: 2001-13-45\n"
DEEP = "version: 1\ntools: " + "[" * 5000 + "\n"


def test_a_project_file_with_a_bad_date_does_not_stop_the_server(tmp_path, monkeypatch):
    store(tmp_path, monkeypatch, [G_COUNT, G_LIST])
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / TOOLS_FILENAME).write_text(BAD_DATE_PROJECT)
    server, _ = build(tmp_path, monkeypatch)
    listed = tools(server)
    assert "search_component" in listed and {"g_count", "list_files"} <= set(listed)
    current = status(server)
    assert any(TOOLS_FILENAME in n and "malformed YAML" in n for n in current["notices"])
    # The bad file still names list_files, so its global stand-in says why.
    assert call(server, "list_files")["notices"][0].startswith("used global tool 'list_files'")


def test_a_global_store_with_a_bad_date_does_not_stop_the_server(tmp_path, monkeypatch):
    path = store(tmp_path, monkeypatch, [G_COUNT])
    path.write_text('{"version": 1, "tools": [{"name": "g_count", "description": 2001-13-45}]}')
    server, _ = build(tmp_path, monkeypatch, project=PROJECT)
    listed = tools(server)
    assert "search_component" in listed and "list_files" in listed and "g_count" not in listed
    assert any(GLOBAL_TOOLS_FILENAME in n and "malformed YAML" in n for n in status(server)["notices"])


def test_declared_names_never_raises():
    from devgraph.mcp.tool_plane import _declared_names

    assert _declared_names(DEEP.encode()) is None
    assert _declared_names(BAD_DATE_PROJECT.encode()) is None


def test_a_bad_global_store_reload_keeps_the_previous_global_tools(tmp_path, monkeypatch):
    for bad in ('{"version": 1, "tools": [{"name": "g_count", "description": 2001-13-45}]}', DEEP):
        path = store(tmp_path, monkeypatch, [G_COUNT])
        server, _ = build(tmp_path, monkeypatch)
        path.write_text(bad)
        assert server.devgraph_tool_plane.reload_if_changed() is False
        assert "g_count" in tools(server)
        current = status(server)
        assert current["served"] == ["g_count"]
        assert any("last good" in n for n in current["notices"])
