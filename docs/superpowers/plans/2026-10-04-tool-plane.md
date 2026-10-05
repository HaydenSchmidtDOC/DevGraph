# MCP Tool Plane (Serving Project Tools) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the scoped repository's `devgraph.tools.yaml` Cypher tools as MCP tools, with an injected `repo_id`, read transactions, timeouts and row caps, built-in-name protection, and a status resource.

**Architecture:** `GraphEngine.run_read_cypher` runs one query in a read transaction with a server-side timeout and a row cap. New `devgraph/mcp/tool_plane.py` resolves the session's repository, builds a typed function per tool, registers it on the `MCPServer` with `add_tool`, and keeps a status object. `build_server` takes an optional session repository; `main()` resolves it.

**Tech Stack:** Python 3.13, mcp SDK 2.3.0 (`MCPServer.add_tool`, schema from `inspect.signature`), neo4j driver 6.3 (`READ_ACCESS`, `execute_read`, `unit_of_work(timeout=)`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-tool-plane-design.md`

**Working directory:** the repository root of this worktree (branch `epic1/d1-tool-plane`, stacked on `epic1/c-tools-file`). Live tests need Neo4j at `bolt://127.0.0.1:7687` (`neo4j` / `devgraph-local-dev`) and must run, not skip.

## Global Constraints

- Scope: `DEVGRAPH_MCP_REPO` (repo id or a path inside a registered repo; unmatched → no scope), else process cwd inside a registered repo (deepest), else none. Fixed for the process lifetime.
- Calls set `repo_id` to the session repo after binding declared parameters; declared parameters can never be named `repo_id` (the loader already enforces it).
- Execution: read access mode + `execute_read`, transaction timeout = `timeout_s`, at most `max_rows` rows; envelope `{count, results, truncated}` (+ `notices` when non-empty); strings sanitized with the existing `_sanitize_row`.
- A project tool named like a built-in is never registered (notice). An invalid tools file → no project tools (notice).
- New resource `devgraph://project-tools`; `devgraph://tool-catalog` includes served project tools.
- A server built without a session repository behaves exactly as before (all existing tool-count/catalog tests unchanged).
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A tool whose Cypher writes despite the static check (e.g. a future keyword) → refused by the read transaction, reported as a tool error — pinned in Task 1.
2. A query that matches millions of rows → returns `max_rows` rows and `truncated: true` without materialising the rest — pinned in Task 1.
3. A caller passing `repo_id` as an argument → rejected/ignored by the schema; the session repo is used — pinned in Task 2.
4. `DEVGRAPH_MCP_REPO` naming an unregistered repo → no project tools, notice explains, no fallback to cwd — pinned in Task 2.
5. Nested registered repositories (a repo inside another) → the deepest one wins — pinned in Task 2.

---

### Task 1: Read-only query execution in the engine

**Files:**
- Modify: `devgraph/graph/engine.py` (import `READ_ACCESS`, `unit_of_work` from `neo4j`; new method after `run_cypher`)
- Test: `tests/graph/test_engine_read_cypher.py` (create)

**Interfaces:**
- Produces: `GraphEngine.run_read_cypher(query: str, parameters: dict[str, Any], *, timeout_s: float, max_rows: int) -> tuple[list[dict], bool]` (rows, truncated). Raises the driver's `Neo4jError` subclasses on failure.

- [ ] **Step 1: Write the failing tests**

```python
"""run_read_cypher: read access mode, timeout, row cap. Live Neo4j."""

import pytest
from neo4j.exceptions import Neo4jError

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_read_cypher"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def seed(engine, n):
    engine.run_cypher("UNWIND range(1, $n) AS i CREATE (:ZzItem {repo_id: $r, name: 'i' + i, i: i})", {"n": n, "r": REPO})


def test_returns_rows_under_the_cap(engine):
    seed(engine, 3)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i ORDER BY i", {"repo_id": REPO}, timeout_s=10, max_rows=5
    )
    assert rows == [{"i": 1}, {"i": 2}, {"i": 3}] and truncated is False


def test_caps_rows_and_flags_truncation(engine):
    seed(engine, 10)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i ORDER BY i", {"repo_id": REPO}, timeout_s=10, max_rows=4
    )
    assert [r["i"] for r in rows] == [1, 2, 3, 4] and truncated is True


def test_exactly_the_cap_is_not_truncated(engine):
    seed(engine, 4)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i", {"repo_id": REPO}, timeout_s=10, max_rows=4
    )
    assert len(rows) == 4 and truncated is False


def test_writes_are_refused_in_read_mode(engine):
    with pytest.raises(Neo4jError):
        engine.run_read_cypher("CREATE (:ZzItem {repo_id: $repo_id, name: 'x'})", {"repo_id": REPO}, timeout_s=10, max_rows=5)
    assert engine.run_cypher("MATCH (n:ZzItem {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 0}]


def test_the_timeout_is_enforced(engine):
    slow = "UNWIND range(1, 100000000) AS i WITH i WHERE i % 7 = 3 RETURN count(i) AS c"
    with pytest.raises(Neo4jError):
        engine.run_read_cypher(slow + " // $repo_id", {"repo_id": REPO}, timeout_s=1, max_rows=1)
```

(If the slow query finishes within 1 s on this machine, enlarge the range; the assertion is that a timeout surfaces as a `Neo4jError`.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/graph/test_engine_read_cypher.py -q -rs` → AttributeError (not skips).

- [ ] **Step 3: Implement** — in `devgraph/graph/engine.py` add `READ_ACCESS, unit_of_work` to the `from neo4j import ...` line and, after `run_cypher`:

```python
    def run_read_cypher(
        self, query: str, parameters: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> tuple[list[dict], bool]:
        """Run one user-declared query read-only, bounded in time and rows.

        Read access mode makes the server refuse any write; the transaction
        timeout bounds the time; rows stop being pulled after `max_rows`, and
        the second value says whether more existed. Used by the MCP tool
        plane for `devgraph.tools.yaml` tools.
        """

        @unit_of_work(timeout=timeout_s)
        def work(tx):
            rows: list[dict] = []
            for record in tx.run(query, parameters):
                if len(rows) == max_rows:
                    return rows, True
                rows.append(record.data())
            return rows, False

        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)
```

- [ ] **Step 4: Run** — `uv run pytest tests/graph -q -rs` → all pass, no skips of the new file.
- [ ] **Step 5: Commit** — `git add devgraph/graph/engine.py tests/graph/test_engine_read_cypher.py` and `git commit -m "Run user-declared queries in bounded read transactions"`.

---

### Task 2: The tool plane

**Files:**
- Create: `devgraph/mcp/tool_plane.py`
- Test: `tests/mcp/test_tool_plane.py` (create)

**Interfaces:**
- Consumes: `load_project_tools`, `ProjectToolsError`, `TOOLS_FILENAME`, `INJECTED_PARAMETER` (config/project_tools.py); `builtin_tool_names` (mcp/catalog.py); `_sanitize_row` (mcp/tools.py); `GraphEngine.run_read_cypher` (Task 1).
- Produces: `SESSION_REPO_ENV = "DEVGRAPH_MCP_REPO"`; `resolve_session_repo(registry, env: Mapping[str, str], cwd: Path) -> tuple[RepoRecord | None, str]` (source ∈ `"env"`, `"cwd"`, `"none"`; with an unmatched env value → `(None, "env")` and the caller records a notice); `ToolPlaneStatus` dataclass (`repo_id`, `source`, `tools_file`, `served: list[str]`, `notices: list[str]`, `to_dict()`); `register_project_tools(server, engine, repo, source, *, instrument, annotations) -> ToolPlaneStatus`; `make_tool_function(tool, engine, repo_id, notices) -> Callable`.

- [ ] **Step 1: Write the failing tests** — create `tests/mcp/test_tool_plane.py`:

```python
"""Serving devgraph.tools.yaml tools: scope, schema, calls, notices. Stub engine."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

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
          MATCH (f:File {repo_id: $repo_id}) WHERE f.path STARTS WITH $folder RETURN f.path AS path
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
    payload = result[1] if isinstance(result, tuple) else result
    text = json.dumps(payload, default=str)
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
    with pytest.raises(Exception, match="list_folder"):
        asyncio.run(server.call_tool("list_folder", {"folder": "x"}))


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
```

Notes for the implementer:
- The SDK's `call_tool` return shape and the `Tool` attribute for the input schema (`input_schema` vs `inputSchema`) depend on mcp 2.3.0 — check `tests/mcp/test_tools_cycles.py::test_calling_the_tool_through_the_server_returns_the_envelope` for how existing tests read a call result, and adapt the assertions' plumbing (not their intent) accordingly. Do the same for how a tool error surfaces from `call_tool` (raised vs an `isError` result) — the test's intent is "the failure names the tool".
- `test_a_caller_cannot_override_repo_id`: whatever the SDK does with an extra `repo_id` argument (reject or drop it), any query that runs uses the session repo.
- `server.read_resource(...)` return shape: check how `tests/mcp/test_tools_cycles.py::test_the_catalog_resource_matches_the_registered_tools` reads `devgraph://tool-catalog` and mirror it.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/mcp/test_tool_plane.py -q` → ImportError.

- [ ] **Step 3: Implement** — create `devgraph/mcp/tool_plane.py`:

```python
"""Serving a repository's `devgraph.tools.yaml` tools over MCP.

The session's repository is resolved once at startup (see
`resolve_session_repo`) and never changes, so a session pinned to one
repository can't reach another's tools. Each valid tool becomes an MCP tool
with a typed input schema; a call injects the session's `repo_id` and runs
read-only, bounded by the tool's timeout and row cap
(`GraphEngine.run_read_cypher`).
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from devgraph.config.project_tools import (
    INJECTED_PARAMETER,
    TOOLS_FILENAME,
    CypherTool,
    ProjectToolsError,
    load_project_tools,
    tools_file_path,
)
from devgraph.mcp.catalog import builtin_tool_names

SESSION_REPO_ENV = "DEVGRAPH_MCP_REPO"

_PYTHON_TYPES: dict[str, type] = {"string": str, "integer": int, "float": float, "boolean": bool}


def _resolved(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path)


def _deepest_containing(repos: list[Any], target: Path) -> Any | None:
    target = _resolved(target)
    best, best_depth = None, -1
    for repo in repos:
        root = _resolved(repo.path)
        if target == root or target.is_relative_to(root):
            depth = len(root.parts)
            if depth > best_depth:
                best, best_depth = repo, depth
    return best


def resolve_session_repo(registry: Any, env: Mapping[str, str], cwd: Path) -> tuple[Any | None, str]:
    """The repository this MCP session serves project tools for, and how it was chosen.

    `DEVGRAPH_MCP_REPO` (a repo id, or a path inside a registered repository)
    wins; a value that matches nothing yields no scope rather than falling
    back. Otherwise the process's working directory, if it lies inside a
    registered repository (the deepest one). Otherwise none.
    """
    repos = registry.list_repos(active_only=True)
    pinned = (env.get(SESSION_REPO_ENV) or "").strip()
    if pinned:
        by_id = next((r for r in repos if r.repo_id == pinned), None)
        return (by_id or _deepest_containing(repos, Path(pinned))), "env"
    match = _deepest_containing(repos, cwd)
    return (match, "cwd") if match is not None else (None, "none")


@dataclass
class ToolPlaneStatus:
    repo_id: str | None
    source: str
    tools_file: str | None = None
    served: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": {"repo_id": self.repo_id, "source": self.source},
            "tools_file": self.tools_file,
            "served": list(self.served),
            "notices": list(self.notices),
        }


def make_tool_function(tool: CypherTool, engine: Any, repo_id: str, notices: list[str]) -> Callable[..., dict[str, Any]]:
    """A function the SDK can register: its signature is the tool's parameters."""
    from devgraph.mcp.tools import _sanitize_row

    def call(**kwargs: Any) -> dict[str, Any]:
        params = {p.name: kwargs.get(p.name, p.default) for p in tool.parameters}
        params[INJECTED_PARAMETER] = repo_id  # always the session's repository
        try:
            rows, truncated = engine.run_read_cypher(
                tool.cypher, params, timeout_s=tool.timeout_s, max_rows=tool.max_rows
            )
        except Exception as exc:  # the driver raises its own hierarchy
            raise RuntimeError(f"project tool {tool.name!r} failed: {exc}") from exc
        results = [_sanitize_row(row) if isinstance(row, dict) else row for row in rows]
        envelope: dict[str, Any] = {"count": len(results), "results": results, "truncated": truncated}
        if notices:
            envelope["notices"] = list(notices)
        return envelope

    parameters = []
    for p in tool.parameters:
        python_type = _PYTHON_TYPES[p.type]
        if p.required:
            parameters.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, annotation=python_type))
        else:
            parameters.append(
                inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, default=p.default, annotation=python_type | None)
            )
    call.__name__ = tool.name
    call.__qualname__ = tool.name
    call.__doc__ = tool.description
    call.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    call.__annotations__ = {**{p.name: p.annotation for p in parameters}, "return": dict[str, Any]}
    return call


def register_project_tools(
    server: Any,
    engine: Any,
    repo: Any | None,
    source: str,
    *,
    instrument: Callable[[Callable[..., Any]], Callable[..., Any]],
    annotations: Any,
) -> ToolPlaneStatus:
    """Register the session repository's tools on `server`; report what happened."""
    if repo is None:
        status = ToolPlaneStatus(repo_id=None, source=source)
        if source == "env":
            status.notices.append(
                f"{SESSION_REPO_ENV}={os.environ.get(SESSION_REPO_ENV, '')!r} matches no registered repository; "
                f"no project tools are served"
            )
        return status

    status = ToolPlaneStatus(repo_id=repo.repo_id, source=source)
    try:
        declared = load_project_tools(repo.path)
    except ProjectToolsError as exc:
        status.notices.append(f"invalid {TOOLS_FILENAME}; no project tools are served: {str(exc).splitlines()[0]}")
        return status
    if declared is None:
        return status

    status.tools_file = str(tools_file_path(repo.path))
    builtin = builtin_tool_names()
    for tool in declared.tools:
        if tool.name in builtin:
            status.notices.append(
                f"ignored: project tool {tool.name!r} has the name of a built-in tool; using the built-in"
            )
    for tool in declared.tools:
        if tool.name in builtin:
            continue
        fn = instrument(make_tool_function(tool, engine, repo.repo_id, status.notices))
        server.add_tool(fn, name=tool.name, description=tool.description, annotations=annotations)
        status.served.append(tool.name)
    return status
```

(If `server.add_tool` in mcp 2.3.0 needs different keyword names, read `.venv/lib/python3.13/site-packages/mcp/server/mcpserver/server.py` around `def add_tool` and adapt.)

- [ ] **Step 4: Wire into `build_server`** (devgraph/mcp/server.py):
  - Signature: `def build_server(engine, registry=None, *, session_repo=None, session_source="none") -> MCPServer`; document the two new parameters.
  - After all built-in `@server.tool` registrations (after the `run_cypher` block, before the resources): `status = register_project_tools(server, engine, session_repo, session_source, instrument=_instrument, annotations=_READ_ONLY)`.
  - New resource `devgraph://project-tools` (name `devgraph-project-tools`, title "DevGraph project tools", description: which repository this session serves `devgraph.tools.yaml` tools for, how it was chosen, which tools are served, and notices about ignored or invalid declarations; `mime_type="application/json"`) returning `json.dumps(status.to_dict(), indent=2)`.
  - `tool_catalog()` appends `{"name": n, "identifier_kind": "project tool parameters", "envelope": True, "phase": None, "note": f"from {TOOLS_FILENAME} in {status.repo_id}"}` for each `n in status.served`.
  - `main()`: `session_repo, source = resolve_session_repo(registry, os.environ, Path.cwd())` and pass both to `build_server` (import `os`/`Path` if missing).

- [ ] **Step 5: Run** — `uv run pytest tests/mcp -q` → all pass (existing tool-count tests unchanged).
- [ ] **Step 6: Commit** — `git add devgraph/mcp/tool_plane.py devgraph/mcp/server.py tests/mcp/test_tool_plane.py` and `git commit -m "Serve project tools to MCP sessions scoped to a repository"`.

---

### Task 3: Docs and verification

- [ ] **Step 1: Docs**
  - README "Project tools (preview)": replace "DevGraph does not serve these tools yet" with how serving works: the session's repository (`DEVGRAPH_MCP_REPO`, else the server's working directory, else none), the injected `repo_id`, read-only transaction with the tool's timeout and row cap, the `{count, results, truncated}` envelope, built-in names never taken over, the `devgraph://project-tools` resource, and that changes to the file take effect when the MCP session restarts (hot reload is not implemented yet). Give the Claude Code command to pin a project: run in the repository, `claude mcp add devgraph -e DEVGRAPH_MCP_REPO=<repo_id> -- "<venv python>" -m devgraph.mcp.server`.
  - DEVGRAPH-CLIENT.md: one short subsection pointing to `devgraph://project-tools` and the env var.
  - PROJECT_STATUS: shipped bullet; update the tools-file bullet ("inert") accordingly.
- [ ] **Step 2: Full suite** — `uv run pytest -q`.
- [ ] **Step 3: Manual check** — in the session scratchpad, create a git repo with a small Python file and a `devgraph.tools.yaml` declaring a `count_modules` tool (`MATCH (m:Module {repo_id: $repo_id}) RETURN count(m) AS n`); register it with a throwaway registry (`DEVGRAPH_REGISTRY_DB_PATH=<scratch>/r.sqlite3 uv run devgraph add <repo>`); then in Python build the server exactly like `main()` does with `DEVGRAPH_MCP_REPO=<repo_id>` and call `count_modules` via `asyncio.run(server.call_tool(...))`; record the result; `devgraph remove` the repo and delete the scratch files.
- [ ] **Step 4: Commit** — `git commit -m "Document serving project tools"`.
