"""Reloading devgraph.tools.yaml in a running session. Stub engine."""

import asyncio
import json
import logging
import shutil
import textwrap
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp.client import Client
from mcp.shared.memory import create_client_server_memory_streams

from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tool_plane, tool_reload
from devgraph.mcp.tool_reload import ToolListNotifier, initialization_options, poll_tool_reloads, run_stdio

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


def test_listchanged_is_advertised_only_with_a_scope(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch)
    assert initialization_options(server, tools_changed=True).capabilities.tools.list_changed is True
    assert not initialization_options(server, tools_changed=False).capabilities.tools.list_changed


def test_a_legacy_client_is_told_and_relists(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    notifier = ToolListNotifier(server)
    received = []

    async def handler(message):
        received.append(type(message).__name__)

    async def scenario():
        async with Client(server, mode="legacy", message_handler=handler) as client:
            assert "count_files" not in {t.name for t in (await client.list_tools()).tools}
            write(repo, TWO)
            assert server.devgraph_tool_plane.reload_if_changed() is True
            await notifier.notify()
            await anyio.sleep(0.2)
            assert any("ToolListChanged" in name for name in received)
            assert "count_files" in {t.name for t in (await client.list_tools()).tools}

    anyio.run(scenario)


def test_a_modern_listener_is_told(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    notifier = ToolListNotifier(server)

    async def scenario():
        async with Client(server, mode="auto") as client:
            async with client.listen(tools_list_changed=True) as events:
                write(repo, TWO)
                server.devgraph_tool_plane.reload_if_changed()
                await notifier.notify()
                with anyio.fail_after(3):
                    async for event in events:
                        assert "ToolsListChanged" in type(event).__name__
                        break

    anyio.run(scenario)


def test_the_poller_notifies_only_on_change_and_survives_errors(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    outcomes = iter([False, RuntimeError("boom"), True])

    def step():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(plane, "reload_if_changed", step)
    notified = []

    class Notifier:
        async def notify(self):
            notified.append(1)

    async def scenario():
        with anyio.CancelScope() as scope:
            ticks = 0

            async def sleep(_):
                nonlocal ticks
                ticks += 1
                if ticks > 3:
                    scope.cancel()
                await anyio.sleep(0)

            await poll_tool_reloads(plane, Notifier(), sleep=sleep)
        assert ticks == 4

    anyio.run(scenario)
    assert notified == [1]


def test_a_modern_client_is_not_captured_or_sent_a_legacy_notification(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    notifier = ToolListNotifier(server)
    received = []

    async def handler(message):
        received.append(type(message).__name__)

    async def scenario():
        async with Client(server, mode="auto", message_handler=handler) as client:
            for _ in range(3):
                await client.list_tools()
            write(repo, TWO)
            server.devgraph_tool_plane.reload_if_changed()
            await notifier.notify()
            await anyio.sleep(0.2)

    anyio.run(scenario)
    assert notifier._connections == {}
    assert not any("ToolListChanged" in name for name in received)


def test_a_legacy_client_is_captured_once(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch)
    notifier = ToolListNotifier(server)

    async def scenario():
        async with Client(server, mode="legacy") as client:
            await client.list_tools()
            await client.list_tools()
            assert len(notifier._connections) == 1

    anyio.run(scenario)


def _emptied_after_the_first_read(module, repo, monkeypatch):
    """Make a truncate-then-write save land right after the fingerprint read (identical bytes follow)."""
    real = module.tools_fingerprint
    reads = []

    def racing(path):
        fingerprint = real(path)
        if not reads:
            reads.append(1)
            (repo / TOOLS_FILENAME).write_text("")
        return fingerprint

    monkeypatch.setattr(module, "tools_fingerprint", racing)


def test_a_reload_parses_the_bytes_it_fingerprinted(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    write(repo, TWO)
    _emptied_after_the_first_read(tool_plane, repo, monkeypatch)
    assert plane.reload_if_changed() is True
    assert "count_files" in tools(server)
    write(repo, TWO)  # the save completes with identical bytes
    assert plane.reload_if_changed() is False
    assert "count_files" in tools(server)


def test_a_save_during_a_reload_is_reloaded_next(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    real = tool_plane.tools_fingerprint

    def racing(path):
        fingerprint = real(path)
        write(repo, ONE)  # saved again right after the read
        return fingerprint

    write(repo, TWO)
    monkeypatch.setattr(tool_plane, "tools_fingerprint", racing)
    assert plane.reload_if_changed() is True
    monkeypatch.setattr(tool_plane, "tools_fingerprint", real)
    assert plane.reload_if_changed() is True
    assert "count_files" not in tools(server)


def test_the_startup_load_parses_the_bytes_it_fingerprinted(tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    repo.mkdir()
    write(repo, ONE)
    _emptied_after_the_first_read(mcp_server, repo, monkeypatch)
    server, _ = build(tmp_path, monkeypatch, tools=None)
    assert "list_files" in tools(server)
    assert status(server)["notices"] == []
    write(repo, ONE)
    assert server.devgraph_tool_plane.reload_if_changed() is False


def test_a_root_that_goes_missing_is_noticed_and_cleared_when_it_returns(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch, tools=None)
    plane = server.devgraph_tool_plane
    shutil.rmtree(repo)
    assert plane.reload_if_changed() is False
    assert any("does not exist" in n for n in status(server)["notices"])
    repo.mkdir()
    plane.reload_if_changed()
    assert status(server)["notices"] == []


def test_a_root_missing_at_startup_is_cleared_when_it_returns(tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = Repo("demo", repo)
    server = mcp_server.build_server(Engine(), Registry([record]), session_repo=record, session_source="env")
    assert any("does not exist" in n for n in status(server)["notices"])
    repo.mkdir()
    server.devgraph_tool_plane.reload_if_changed()
    assert status(server)["notices"] == []


def test_a_failed_reload_is_retried(tmp_path, monkeypatch):
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    real = tool_plane._serve_repository
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    write(repo, TWO)
    monkeypatch.setattr(tool_plane, "_serve_repository", flaky)
    try:
        plane.reload_if_changed()
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failure should propagate")
    assert plane.reload_if_changed() is True
    assert "count_files" in tools(server)


def test_a_reload_with_file_notices_warns(tmp_path, monkeypatch, caplog):
    server, repo = build(tmp_path, monkeypatch)
    (repo / TOOLS_FILENAME).write_text("version: 1\ntools: [oops\n")
    with caplog.at_level(logging.WARNING):
        server.devgraph_tool_plane.reload_if_changed()
    assert any(TOOLS_FILENAME in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def _poll_once(server):
    class Notifier:
        async def notify(self):
            pass

    plane = server.devgraph_tool_plane
    plane._fingerprint = None  # force a reload on the first tick
    with anyio.CancelScope() as scope:
        async def sleep(_):
            if getattr(sleep, "done", False):
                scope.cancel()
            sleep.done = True
            await anyio.sleep(0)

        await poll_tool_reloads(plane, Notifier(), sleep=sleep)


def test_a_failed_poll_says_tools_may_be_unavailable(tmp_path, monkeypatch, caplog):
    server, _ = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(plane, "reload_if_changed", boom)
    with caplog.at_level(logging.WARNING):
        asyncio.run(_poll_once(server))
    assert any("unavailable" in r.getMessage() for r in caplog.records)


class _MemoryTransport:
    def __init__(self, streams):
        self._streams = streams

    async def __aenter__(self):
        return self._streams

    async def __aexit__(self, *exc):
        return None


async def _run_stdio_handshake(server, monkeypatch):
    """Serve `run_stdio` over memory streams; return (initialize capabilities, whether run_stdio ended)."""
    finished = anyio.Event()
    async with create_client_server_memory_streams() as (client_streams, server_streams):

        @asynccontextmanager
        async def fake_stdio_server():
            yield server_streams

        monkeypatch.setattr(tool_reload, "stdio_server", fake_stdio_server)

        async def serve():
            await run_stdio(server)
            finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(serve)
            async with Client(_MemoryTransport(client_streams), mode="legacy") as client:
                capabilities = client.server_capabilities
            await client_streams[1].aclose()
            with anyio.fail_after(5):
                await finished.wait()
    return capabilities


def test_run_stdio_advertises_listchanged_for_a_scoped_server_and_ends_with_the_client(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch)
    capabilities = anyio.run(_run_stdio_handshake, server, monkeypatch)
    assert capabilities.tools.list_changed is True


def test_run_stdio_does_not_advertise_listchanged_without_a_scope(tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch, scoped=False)
    capabilities = anyio.run(_run_stdio_handshake, server, monkeypatch)
    assert not capabilities.tools.list_changed
