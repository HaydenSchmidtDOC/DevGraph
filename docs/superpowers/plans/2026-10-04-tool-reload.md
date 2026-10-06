# Project Tool Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reload a scoped session's `devgraph.tools.yaml` when it changes and tell MCP clients their tool list changed.

**Architecture:** `ProjectToolPlane` (devgraph/mcp/tool_plane.py) owns the served project tools and can `reload_if_changed()` by fingerprint. New devgraph/mcp/tool_reload.py holds a `ToolListNotifier` (middleware-captured connections + subscription bus), a poll loop, and a stdio runner that advertises `tools.listChanged`. `main()` uses the runner instead of `server.run("stdio")`.

**Tech Stack:** Python 3.13, mcp SDK 2.3.0 (pinned; private `_lowlevel_server`, `_subscriptions`, `ctx.session._connection` — each exercised by a test), anyio, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-tool-reload-design.md`

**Working directory:** the repository root of this worktree (branch `epic1/d2-tool-reload`, stacked on `epic1/d1-tool-plane`).

## Global Constraints

- Poll every 2 s (`RELOAD_INTERVAL_S = 2.0`), in the server's event loop, only when the session has a repository.
- Fingerprint: the file's bytes; `"absent"` when missing; `"unreadable:<ExcName>"` on other OSError.
- Reload applies the startup rules exactly (same function), replacing tools_file/served/parameter names/notices.
- Notify only when the served set or a served tool's definition changed.
- `tools.listChanged: true` is advertised only when the session has a repository; a server without one behaves exactly as before (existing tests unchanged).
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A save that makes the file invalid → all project tools disappear with a notice; the next valid save restores them (and notifies both times) — pinned in Task 1/2.
2. An unchanged file across polls (or a touch that doesn't change bytes) → no reload and no notification — pinned in Task 1/2.
3. A reload that raises unexpectedly → the poller logs and keeps polling; the server keeps serving — pinned in Task 2.
4. A tool renamed in the file → old name gone from `tools/list`, new name present — pinned in Task 1.
5. A tool call in flight while a reload happens → the call completes with the old definition (the SDK already holds the function) — reasoned in review, not tested.

---

### Task 1: Reloadable tool plane

**Files:**
- Modify: `devgraph/mcp/tool_plane.py`
- Modify: `devgraph/mcp/server.py` (create the plane in `build_server`)
- Test: `tests/mcp/test_tool_reload.py` (create)

**Interfaces:**
- Produces: `RELOAD_INTERVAL_S = 2.0`; `tools_fingerprint(repo_path) -> bytes | str`; `ToolPlaneStatus.definitions: dict[str, CypherTool]` (not in `to_dict`); `ProjectToolPlane(server, engine, repo, status, *, instrument, annotations)` with `.repo`, `.status`, `reload_if_changed() -> bool`; `build_server` sets `server.devgraph_tool_plane` to the plane.

- [ ] **Step 1: Write the failing tests** — create `tests/mcp/test_tool_reload.py`:

```python
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
```

(Mirror `tests/mcp/test_tool_plane.py` for how `read_resource` content and `list_tools` results are read if these shapes differ; adapt plumbing, never intent.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/mcp/test_tool_reload.py -q` → AttributeError `devgraph_tool_plane`.

- [ ] **Step 3: Implement in `devgraph/mcp/tool_plane.py`:**
  - `RELOAD_INTERVAL_S = 2.0` next to `SESSION_REPO_ENV`.
  - `ToolPlaneStatus`: add `definitions: dict[str, CypherTool] = field(default_factory=dict, repr=False)` (served tool name → its declaration); `to_dict` unchanged.
  - Split `register_project_tools`: move everything after `status = ToolPlaneStatus(repo_id=repo.repo_id, source=source)` into `_serve_repository(server, engine, repo, status, *, instrument, annotations) -> None`, which also records `status.definitions[tool.name] = tool` next to `status.served.append(tool.name)`. `register_project_tools` creates the status and calls it (external behaviour unchanged).
  - Add:

```python
def tools_fingerprint(repo_path: Path | str) -> bytes | str:
    """What the tools file looks like now: its bytes, 'absent', or 'unreadable:<error>'."""
    try:
        return tools_file_path(Path(repo_path)).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


class ProjectToolPlane:
    """The session's served project tools, reloadable when the tools file changes.

    The scope (`repo`) never changes; only the tools file is re-read, under the
    same rules as at startup.
    """

    def __init__(self, server: Any, engine: Any, repo: Any | None, status: ToolPlaneStatus, *,
                 instrument: Callable[[Callable[..., Any]], Callable[..., Any]], annotations: Any) -> None:
        self.server, self.engine, self.repo, self.status = server, engine, repo, status
        self._instrument, self._annotations = instrument, annotations
        self._fingerprint = tools_fingerprint(repo.path) if repo is not None else None

    def reload_if_changed(self) -> bool:
        """Re-serve the tools file if its bytes changed. True when the served tools changed."""
        if self.repo is None:
            return False
        fingerprint = tools_fingerprint(self.repo.path)
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        before = dict(self.status.definitions)
        for name in self.status.served:
            self.server.remove_tool(name)
        self.status.tools_file = None
        self.status.served.clear()
        self.status.parameter_names.clear()
        self.status.definitions.clear()
        self.status.notices.clear()
        _serve_repository(self.server, self.engine, self.repo, self.status,
                          instrument=self._instrument, annotations=self._annotations)
        return self.status.definitions != before
```

  - (Check `MCPServer.remove_tool`'s exact name/behaviour in `.venv/lib/python3.13/site-packages/mcp/server/mcpserver/server.py`; if it raises for an unknown name, that can't happen here since only served names are removed.)
  - In `server.py` `build_server`, right after `status = register_project_tools(...)`: `server.devgraph_tool_plane = ProjectToolPlane(server, engine, session_repo, status, instrument=_instrument, annotations=_READ_ONLY)` with a one-line comment that `main()` uses it to poll for reloads. Import `ProjectToolPlane`.

- [ ] **Step 4: Run** — `uv run pytest tests/mcp -q` → all pass.
- [ ] **Step 5: Commit** — `git add devgraph/mcp/tool_plane.py devgraph/mcp/server.py tests/mcp/test_tool_reload.py` and `git commit -m "Reload project tools when the tools file changes"`.

---

### Task 2: Notifications and the stdio runner

**Files:**
- Create: `devgraph/mcp/tool_reload.py`
- Modify: `devgraph/mcp/server.py` (`main()`)
- Test: `tests/mcp/test_tool_reload.py` (append)

**Interfaces:**
- Consumes: `ProjectToolPlane`, `RELOAD_INTERVAL_S` (Task 1); `server.devgraph_tool_plane`.
- Produces: `ToolListNotifier(server)` with `async notify()`; `async poll_tool_reloads(plane, notifier, *, interval_s=RELOAD_INTERVAL_S, sleep=anyio.sleep)`; `initialization_options(server, *, tools_changed: bool)`; `async run_stdio(server)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/mcp/test_tool_reload.py`:

```python
import anyio
import pytest
from mcp.client import Client

from devgraph.mcp.tool_reload import ToolListNotifier, initialization_options, poll_tool_reloads


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
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    outcomes = iter([False, RuntimeError("boom"), True])
    monkeypatch.setattr(plane, "reload_if_changed", lambda: (lambda o: (_ for _ in ()).throw(o) if isinstance(o, Exception) else o)(next(outcomes)))
    notified = []

    class Notifier:
        async def notify(self):
            notified.append(1)

    ticks = 0

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise anyio.get_cancelled_exc_class()()

    async def scenario():
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await poll_tool_reloads(plane, Notifier(), sleep=sleep)

    anyio.run(scenario)
    assert notified == [1]
```

Notes for the implementer: the poller test's `reload_if_changed` stub is awkward — replace it with a small helper function (`def step(): o = next(outcomes); if isinstance(o, Exception): raise o; return o`) keeping the intent: one no-change, one exception (logged, poller continues), one change (one notify). If raising the cancelled exception class from a fake `sleep` is awkward under anyio, end the loop another way (e.g. a fake sleep that cancels an enclosing `CancelScope`) — intent: the loop runs three iterations, then stops. Check how `Client(..., message_handler=...)` messages are typed in mcp 2.3.0 (the spike saw `ToolListChangedNotification`) and how `listen()` events are typed (`ToolsListChanged`); adapt assertions' plumbing only.

- [ ] **Step 2: Run to verify they fail** — ImportError for `devgraph.mcp.tool_reload`.

- [ ] **Step 3: Implement** — create `devgraph/mcp/tool_reload.py`:

```python
"""Telling MCP clients that the session's project tools changed.

`ProjectToolPlane.reload_if_changed` re-serves `devgraph.tools.yaml`; this
module polls it and notifies clients: `notifications/tools/list_changed` to
legacy-protocol clients (whose connection is captured by a middleware, since
it is only reachable from a request) and `ToolsListChanged` on the
subscription bus for `subscriptions/listen` clients. Both are sent; each is a
no-op for the other kind of client.

Relies on mcp 2.3.0 internals (`_lowlevel_server`, `_subscriptions`,
`ctx.session._connection`); tests exercise each against the real SDK.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import ToolsListChanged

from devgraph.mcp.tool_plane import RELOAD_INTERVAL_S, ProjectToolPlane

logger = logging.getLogger(__name__)


class ToolListNotifier:
    """Sends tool-list-changed notifications to every client of `server`."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._connections: dict[int, Any] = {}
        server._lowlevel_server.middleware.append(self._capture)

    async def _capture(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        # One connection per client; the per-request session would duplicate notifications.
        connection = getattr(getattr(ctx, "session", None), "_connection", None)
        if connection is not None:
            self._connections[id(connection)] = connection
        return await call_next(ctx)

    async def notify(self) -> None:
        for connection in list(self._connections.values()):
            try:
                await connection.send_tool_list_changed()
            except Exception:
                logger.debug("could not send tools/list_changed", exc_info=True)
        await self._server._subscriptions.publish(ToolsListChanged())


async def poll_tool_reloads(
    plane: ProjectToolPlane,
    notifier: ToolListNotifier,
    *,
    interval_s: float = RELOAD_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    """Reload the tools file whenever it changes, until cancelled."""
    while True:
        await sleep(interval_s)
        try:
            changed = plane.reload_if_changed()
        except Exception:
            logger.warning("reloading project tools failed; keeping the current tools", exc_info=True)
            continue
        if changed:
            await notifier.notify()


def initialization_options(server: Any, *, tools_changed: bool) -> Any:
    return server._lowlevel_server.create_initialization_options(NotificationOptions(tools_changed=tools_changed))


async def run_stdio(server: Any) -> None:
    """`MCPServer.run_stdio_async`, plus project-tool reloads when the session has a repository."""
    plane: ProjectToolPlane = server.devgraph_tool_plane
    scoped = plane.repo is not None
    notifier = ToolListNotifier(server) if scoped else None
    async with stdio_server() as (read_stream, write_stream):
        async with anyio.create_task_group() as tasks:
            if notifier is not None:
                tasks.start_soon(poll_tool_reloads, plane, notifier)
            await server._lowlevel_server.run(
                read_stream, write_stream, initialization_options(server, tools_changed=scoped)
            )
            tasks.cancel_scope.cancel()
```

  - Note: `reload_if_changed` is wrapped in `try/except` in the poller, but if a reload raises midway the tool set may be partial; that's acceptable (logged; the next changed save reloads cleanly) — mention it in the report if you see a better cheap option.
  - In `server.py` `main()`: replace `server.run("stdio")` with `anyio.run(run_stdio, server)` (import `anyio` and `run_stdio`).

- [ ] **Step 4: Run** — `uv run pytest tests/mcp -q` and `uv run pytest -q` → all pass.
- [ ] **Step 5: Commit** — `git add devgraph/mcp/tool_reload.py devgraph/mcp/server.py tests/mcp/test_tool_reload.py` and `git commit -m "Notify MCP clients when project tools change"`.

---

### Task 3: Docs and an end-to-end check

- [ ] **Step 1: Docs** — README "Project tools" section and DEVGRAPH-CLIENT.md project-tools subsection: replace "changes take effect when the MCP session restarts" with: the server checks the file every 2 seconds and serves the new set without a restart, telling the client its tool list changed (clients that support `tools/list_changed` re-list automatically); an invalid save serves no project tools until fixed (see `devgraph://project-tools`). PROJECT_STATUS: shipped bullet; drop "hot reload is not implemented" wording wherever it appears (`grep -rn "restart\|hot reload" README.md DEVGRAPH-CLIENT.md PROJECT_STATUS.md`).
- [ ] **Step 2: Full suite** — `uv run pytest -q`.
- [ ] **Step 3: End-to-end check over a real stdio subprocess** — in the session scratchpad: a git repo with a small Python file and a `devgraph.tools.yaml` declaring `count_modules`; register it in a throwaway registry (`DEVGRAPH_REGISTRY_DB_PATH=<scratch>/r.sqlite3 uv run devgraph add <repo>`). Write a tiny launcher script that builds the server exactly like `main()` but **without** the tray lifecycle calls (`resolve_session_repo` → `build_server` → `anyio.run(run_stdio, server)`), and drive it from a Python client: `Client(StdioServerParameters(command=<venv python>, args=[launcher], env={... DEVGRAPH_MCP_REPO, DEVGRAPH_REGISTRY_DB_PATH, PATH ...}), mode="legacy", message_handler=...)`; check the initialize result advertises `listChanged: true`, list tools, append a second tool to the file, wait ~3 s, confirm the notification arrived and the re-list shows it. Record the output; `devgraph remove` the repo and delete the scratch files.
- [ ] **Step 4: Commit** — `git commit -m "Document project tool reload"`.
