"""Built-in MCP tools' repo_id: explicit calls unchanged, omitted ones use the session's repository.

Every built-in body is stubbed with a recorder, so these exercise the server layer only:
no Neo4j, no git, no `gh`.
"""

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp.server.mcpserver.utilities.func_metadata import func_metadata

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools as devgraph_tools
from devgraph.mcp.tool_plane import SESSION_REPO_ENV, resolve_session_repo


@dataclass
class Repo:
    repo_id: str
    path: Path
    active: bool = True


class Registry:
    def __init__(self, repos):
        self.repos = repos
        self.list_calls = []

    def list_repos(self, active_only=False):
        self.list_calls.append(active_only)
        return [r for r in self.repos if r.active or not active_only]

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


class Engine:
    def __init__(self):
        self.read_calls = []

    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        self.read_calls.append(dict(parameters))
        return [], False


DICT_PAYLOAD = {"count": 1, "results": [{"name": "X"}], "truncated": False}
LIST_PAYLOAD = [{"name": "X"}]
LIST_TOOLS = {"find_requirements_for", "blame_component"}

MIN_ARGS = {
    "search_component": {"query": "X"},
    "god_nodes": {},
    "find_dependency_cycles": {},
    "find_communities": {},
    "key_nodes": {},
    "list_recent_changes": {"within_commits": 5},
    "trace_request_flow": {"start_endpoint": "X"},
    "get_service_dependencies": {"service_name": "X"},
    "find_callers": {"target_name": "X"},
    "find_related_files": {"component_name": "X"},
    "summarise_repository": {},
    "compare_branches": {"branch_a": "a", "branch_b": "b"},
    "impact_analysis": {"component_name": "X"},
    "impact_analysis_for_diff": {"base_ref": "a", "head_ref": "b"},
    "explain_architecture": {},
    "list_services": {},
    "explain_decision": {"decision_name": "X"},
    "find_requirements_for": {"component_name": "X"},
    "trace_design_rationale": {"component_name": "X"},
    "find_mentions": {"name": "X"},
    "blame_component": {"component_name": "X"},
    "find_related_prs": {"component_name": "X"},
    "issue_history_for": {"component_name": "X"},
    "get_source": {"component_name": "X"},
}


# Where each body receives repo_id: after the engine, or after engine and registry.
REPO_ARG_INDEX = {name: 2 if name in {"impact_analysis_for_diff", "get_source"} else 1 for name in MIN_ARGS}


def payload(name):
    return LIST_PAYLOAD if name in LIST_TOOLS else DICT_PAYLOAD


def structured(name):
    return {"result": LIST_PAYLOAD} if name in LIST_TOOLS else DICT_PAYLOAD


def registered_fn(server, name):
    """The function the SDK registered for `name`. The only place that touches SDK internals.

    Written against mcp 2.3.0's MCPServer, which keeps tools in `_tool_manager._tools`.
    """
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if tools is None:
        pytest.fail("MCPServer internals changed (written against mcp 2.3.0): no _tool_manager._tools")
    return tools[name].fn


@pytest.fixture
def calls(monkeypatch):
    """Stub every built-in body and declared_node_labels; returns the (name, args, kwargs) log."""
    log = []
    for name in MIN_ARGS:
        def recorder(*args, _name=name, **kwargs):
            log.append((_name, args, kwargs))
            return [dict(r) for r in LIST_PAYLOAD] if _name in LIST_TOOLS else {
                **DICT_PAYLOAD, "results": [dict(r) for r in DICT_PAYLOAD["results"]]
            }

        monkeypatch.setattr(devgraph_tools, name, recorder)
    monkeypatch.setattr(devgraph_tools, "declared_node_labels", lambda registry, repo_id: [])
    return log


@pytest.fixture
def engine():
    return Engine()


@pytest.fixture
def make_server(tmp_path, monkeypatch, engine):
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", enable_run_cypher=True)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    def make(session_repo=None, session_source="none", registry=None, session_pinned=None):
        if registry is None:
            registry = Registry([session_repo] if session_repo is not None else [])
        return mcp_server.build_server(
            engine, registry, session_repo=session_repo, session_source=session_source, session_pinned=session_pinned
        )

    return make


def demo(tmp_path, repo_id="demo", active=True):
    path = tmp_path / repo_id
    path.mkdir(exist_ok=True)
    return Repo(repo_id, path, active)


def _builtins_with_repo_id():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        settings = Settings(registry_db_path=Path(tmp) / "r.sqlite3", enable_run_cypher=True)
        mp.setattr(mcp_server, "get_settings", lambda: settings)
        from devgraph.config import global_tools

        mp.setattr(global_tools, "_default_path", lambda: Path(tmp) / "no-global" / global_tools.GLOBAL_TOOLS_FILENAME)
        server = mcp_server.build_server(Engine(), Registry([]))
        names = [t.name for t in asyncio.run(server.list_tools())]
        return {
            n for n in names
            if "repo_id" in inspect.signature(inspect.unwrap(registered_fn(server, n))).parameters
        }


BUILTINS_WITH_REPO_ID = _builtins_with_repo_id()
NAMES = sorted(BUILTINS_WITH_REPO_ID)


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))


# ── characterization: explicit calls ───────────────────────────────────────


def test_builtins_with_repo_id_are_exactly_the_24():
    assert len(BUILTINS_WITH_REPO_ID) == 24
    assert "run_cypher" not in BUILTINS_WITH_REPO_ID
    assert set(MIN_ARGS) == BUILTINS_WITH_REPO_ID


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("repo_id", ["other", "demo", ""])
def test_explicit_calls_are_unchanged(name, repo_id, tmp_path, calls, make_server):
    session = demo(tmp_path)
    registry = Registry([session])  # shared, so registry-taking bodies see equal arguments
    servers = [make_server(session, "env", registry), make_server(None, "none", registry)]
    dumps, recorded = [], []
    for server in servers:
        calls.clear()
        result = call(server, name, {"repo_id": repo_id, **MIN_ARGS[name]})
        assert result.is_error is False
        assert result.structured_content == structured(name)
        assert "notices" not in (result.structured_content or {})
        (seen_name, args, kwargs), = calls
        assert seen_name == name and args[REPO_ARG_INDEX[name]] == repo_id
        recorded.append((args, kwargs))
        dumps.append(result.model_dump_json())
    assert recorded[0] == recorded[1]
    assert dumps[0] == dumps[1]


# ── the default ────────────────────────────────────────────────────────────

OPTIONAL_REPO_ID = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Repo Id"}
ENV_NOTICE = "repo_id not given; used this session's repository 'demo' (from DEVGRAPH_MCP_REPO)"
CWD_NOTICE = "repo_id not given; used this session's repository 'demo' (from the server's working directory)"
RESTART_HINT = "pass repo_id explicitly, or restart the MCP server after registering a repository"


def listed_schemas(server):
    return {t.name: t.input_schema if hasattr(t, "input_schema") else t.inputSchema for t in asyncio.run(server.list_tools())}


def call_over_wire(server, name, arguments):
    """What a client receives: a deliberate ToolError becomes an is_error result.

    In process, `MCPServer.call_tool` raises it; the request handler (`_handle_call_tool`
    in mcp 2.3.0) turns it into `CallToolResult(content=[TextContent(str(exc))], is_error=True)`.
    """
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from mcp.types import CallToolResult, TextContent

    try:
        return call(server, name, arguments)
    except ToolError as exc:
        if isinstance(exc, UnexpectedToolError):
            raise
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)


def error_text(result):
    return json.dumps([getattr(c, "text", str(c)) for c in result.content])


@pytest.mark.parametrize("name", NAMES)
def test_only_repo_id_became_optional(name, tmp_path, make_server):
    server = make_server(demo(tmp_path), "env")
    original = func_metadata(inspect.unwrap(registered_fn(server, name))).arg_model.model_json_schema()
    expected = {**original, "properties": {**original["properties"], "repo_id": OPTIONAL_REPO_ID}}
    required = [r for r in original["required"] if r != "repo_id"]
    expected.pop("required")
    if required:
        expected["required"] = required
    listed = listed_schemas(server)[name]
    assert listed == expected
    assert list(listed["properties"]) == list(original["properties"])


@pytest.mark.parametrize("name", NAMES)
def test_omitted_repo_id_uses_the_session_repo(name, tmp_path, calls, make_server):
    server = make_server(demo(tmp_path), "env")
    result = call(server, name, dict(MIN_ARGS[name]))
    assert result.is_error is False
    (_, args, _), = calls
    assert args[REPO_ARG_INDEX[name]] == "demo"
    if name in LIST_TOOLS:
        assert result.structured_content == {"result": LIST_PAYLOAD}
    else:
        assert result.structured_content == {**DICT_PAYLOAD, "repo_id": "demo", "notices": [ENV_NOTICE]}


@pytest.mark.parametrize("name", sorted(LIST_TOOLS))
def test_defaulted_list_payload_is_unchanged(name, tmp_path, calls, make_server):
    server = make_server(demo(tmp_path), "env")
    explicit = call(server, name, {"repo_id": "demo", **MIN_ARGS[name]})
    defaulted = call(server, name, dict(MIN_ARGS[name]))
    assert explicit.model_dump_json() == defaulted.model_dump_json()
    for result in (explicit, defaulted):
        dumped = result.model_dump_json()
        assert '"repo_id"' not in dumped and '"notices"' not in dumped


def test_session_repo_still_active_defaults_normally(tmp_path, calls, make_server):
    session = demo(tmp_path)
    registry = Registry([session, demo(tmp_path, "other")])
    server = make_server(session, "env", registry)
    registry.list_calls.clear()
    result = call(server, "god_nodes", {})
    assert result.is_error is False
    assert result.structured_content["repo_id"] == "demo"
    assert registry.list_calls == [True]


@pytest.mark.parametrize("still_registered_inactive", [False, True])
def test_removed_session_repo_errors(still_registered_inactive, tmp_path, calls, make_server):
    session = demo(tmp_path)
    registry = Registry([session, demo(tmp_path, "other")])
    server = make_server(session, "env", registry)
    if still_registered_inactive:
        session.active = False
    else:
        registry.repos.remove(session)
    result = call_over_wire(server, "god_nodes", {})
    assert result.is_error is True
    text = error_text(result)
    assert "session repository 'demo' is no longer registered or active" in text
    assert "other (other)" in text and RESTART_HINT in text
    assert calls == []

    registry.list_calls.clear()
    explicit = call(server, "god_nodes", {"repo_id": "demo"})
    assert explicit.is_error is False and explicit.structured_content == DICT_PAYLOAD
    assert registry.list_calls == []


def test_null_repo_id_is_treated_as_omitted(tmp_path, calls, make_server):
    server = make_server(demo(tmp_path), "env")
    result = call(server, "search_component", {"repo_id": None, "query": "X"})
    assert result.is_error is False
    assert result.structured_content == {**DICT_PAYLOAD, "repo_id": "demo", "notices": [ENV_NOTICE]}
    (_, args, _), = calls
    assert args[1] == "demo"


def test_cwd_source_notice(tmp_path, calls, make_server):
    server = make_server(demo(tmp_path), "cwd")
    result = call(server, "god_nodes", {})
    assert result.structured_content["notices"] == [CWD_NOTICE]


def test_run_cypher_is_unchanged(tmp_path, make_server):
    server = make_server(demo(tmp_path), "env")
    original = func_metadata(inspect.unwrap(registered_fn(server, "run_cypher"))).arg_model.model_json_schema()
    listed = listed_schemas(server)["run_cypher"]
    assert listed == original
    assert listed["required"] == ["query"]


# ── resolution branches, through the production resolution ────────────────

SHADOW_NOTICE = "ignored: project tool 'search_component' shadows a locked tool; using the fixed implementation"
TOOLS_YAML = """version: 1
tools:
  - name: list_folder
    description: List the files directly inside a folder.
    cypher: |
      MATCH (f:File {repo_id: $repo_id}) WHERE f.path STARTS WITH $folder RETURN f.path AS path
    parameters:
      - name: folder
        description: Folder path.
  - name: search_component
    description: Shadows a built-in.
    cypher: |
      MATCH (n {repo_id: $repo_id}) RETURN n.name AS name
"""


@pytest.fixture
def resolved(make_server):
    """A server whose session comes from the real `resolve_session_repo(registry, env, cwd)`, as in main()."""

    def make(registry, env, cwd):
        session, source = resolve_session_repo(registry, env, cwd)
        return make_server(session, source, registry, env.get(SESSION_REPO_ENV))

    return make


def unscoped_scenario(kind, tmp_path):
    """(registry, env, cwd) for each way a session ends up with no repository."""
    a = demo(tmp_path, "a")
    if kind == "unmatched_pin":
        return Registry([a]), {SESSION_REPO_ENV: "nope"}, a.path
    if kind == "inactive_pin":
        return Registry([a, demo(tmp_path, "old", active=False)]), {SESSION_REPO_ENV: "old"}, a.path
    if kind == "no_cwd_match":
        return Registry([a, demo(tmp_path, "b")]), {}, tmp_path
    if kind == "one_repo":
        return Registry([a]), {}, tmp_path
    if kind == "none_registered":
        return Registry([]), {}, tmp_path
    raise AssertionError(kind)


UNSCOPED = ["unmatched_pin", "inactive_pin", "no_cwd_match", "one_repo", "none_registered"]


def unscoped_error_text(server):
    result = call_over_wire(server, "search_component", {"query": "X"})
    assert result.is_error is True
    return error_text(result)


@pytest.mark.parametrize("kind", UNSCOPED)
def test_every_unscoped_error_has_the_restart_hint_and_runs_nothing(kind, tmp_path, calls, resolved):
    server = resolved(*unscoped_scenario(kind, tmp_path))
    text = unscoped_error_text(server)
    assert "repo_id is required because this session has no repository" in text
    assert RESTART_HINT in text
    assert calls == []


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("unmatched_pin", "DEVGRAPH_MCP_REPO='nope' matches no registered repository"),
        ("inactive_pin", "DEVGRAPH_MCP_REPO='old' names repository 'old', which is registered but inactive"),
        ("no_cwd_match", "the server's working directory is not inside a registered repository and DEVGRAPH_MCP_REPO is unset"),
        ("none_registered", "Active registered repositories: none registered (register one with `devgraph add <path>`)"),
    ],
)
def test_each_no_session_reason_is_stated(kind, expected, tmp_path, calls, resolved):
    assert expected in unscoped_error_text(resolved(*unscoped_scenario(kind, tmp_path)))


def test_unmatched_pin_errors_without_cwd_fallback(tmp_path, calls, resolved):
    text = unscoped_error_text(resolved(*unscoped_scenario("unmatched_pin", tmp_path)))
    assert "DEVGRAPH_MCP_REPO='nope'" in text and "matches no registered repository" in text
    assert "a (" in text
    assert calls == []


def test_inactive_pin_says_inactive_and_does_not_list_it(tmp_path, calls, resolved):
    text = unscoped_error_text(resolved(*unscoped_scenario("inactive_pin", tmp_path)))
    assert "inactive" in text
    assert "a (a)" in text and "old (" not in text


def test_no_cwd_match_names_the_working_directory_and_the_env_var(tmp_path, calls, resolved):
    text = unscoped_error_text(resolved(*unscoped_scenario("no_cwd_match", tmp_path)))
    assert "working directory" in text and "DEVGRAPH_MCP_REPO" in text
    assert "a (a)" in text and "b (b)" in text


def test_one_registered_repo_is_listed_not_picked(tmp_path, calls, resolved):
    text = unscoped_error_text(resolved(*unscoped_scenario("one_repo", tmp_path)))
    assert "a (a)" in text
    assert calls == []


def test_no_registered_repos_points_at_devgraph_add(tmp_path, calls, resolved):
    text = unscoped_error_text(resolved(*unscoped_scenario("none_registered", tmp_path)))
    assert "none registered" in text and "devgraph add" in text
    assert RESTART_HINT in text


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("kind", UNSCOPED)
def test_explicit_wins_over_every_unscoped_branch(kind, name, tmp_path, calls, resolved, make_server):
    registry, env, cwd = unscoped_scenario(kind, tmp_path)
    unscoped = resolved(registry, env, cwd)
    baseline = make_server(None, "none", registry)
    dumps, recorded = [], []
    for server in (unscoped, baseline):
        calls.clear()
        result = call(server, name, {"repo_id": "a", **MIN_ARGS[name]})
        assert result.is_error is False
        assert result.structured_content == structured(name)
        assert "notices" not in (result.structured_content or {})
        (seen_name, args, kwargs), = calls
        assert seen_name == name and args[REPO_ARG_INDEX[name]] == "a"
        recorded.append((args, kwargs))
        dumps.append(result.model_dump_json())
    assert recorded[0] == recorded[1]
    assert dumps[0] == dumps[1]


def nested(tmp_path):
    outer, inner = demo(tmp_path, "outer"), Repo("inner", tmp_path / "outer" / "inner")
    (inner.path / "src").mkdir(parents=True)
    return outer, inner, Registry([outer, inner])


def defaulted_repo(server, calls):
    calls.clear()
    result = call(server, "search_component", {"query": "X"})
    assert result.is_error is False
    (_, args, _), = calls
    assert args[1] == result.structured_content["repo_id"]
    return result.structured_content["repo_id"]


def test_nested_registered_repos_default_to_the_deepest(tmp_path, calls, resolved):
    outer, inner, registry = nested(tmp_path)
    assert defaulted_repo(resolved(registry, {}, inner.path / "src"), calls) == "inner"
    assert defaulted_repo(resolved(registry, {}, outer.path), calls) == "outer"
    assert defaulted_repo(resolved(registry, {SESSION_REPO_ENV: str(inner.path / "src")}, tmp_path), calls) == "inner"


def test_an_unregistered_nested_repo_defaults_to_the_registered_outer(tmp_path, calls, resolved):
    outer = demo(tmp_path, "outer")
    sub = outer.path / "vendor" / "sub"
    (sub / ".git").mkdir(parents=True)
    assert defaulted_repo(resolved(Registry([outer]), {}, sub), calls) == "outer"


# ── composition with the project plane ─────────────────────────────────────


def test_disabled_project_config_does_not_gate_the_default(tmp_path, calls, resolved, monkeypatch):
    from devgraph.mcp import tool_plane

    monkeypatch.setattr(tool_plane, "project_config_enabled", lambda _path: False)
    session = demo(tmp_path)
    (session.path / "devgraph.tools.yaml").write_text(TOOLS_YAML)
    server = resolved(Registry([session]), {}, session.path)
    assert json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)["served"] == []
    assert defaulted_repo(server, calls) == "demo"


def test_an_untrusted_tools_file_does_not_gate_the_default(tmp_path, calls, resolved):
    session = demo(tmp_path)
    (session.path / "devgraph.tools.yaml").write_text(TOOLS_YAML)
    server = resolved(Registry([session]), {}, session.path)
    assert "list_folder" not in {t.name for t in asyncio.run(server.list_tools())}
    assert defaulted_repo(server, calls) == "demo"


def test_cross_repo_without_repo_id_defaults_when_scoped(tmp_path, calls, resolved):
    session = demo(tmp_path)
    server = resolved(Registry([session]), {}, session.path)
    result = call(server, "search_component", {"query": "X", "cross_repo": True})
    assert result.structured_content["repo_id"] == "demo"
    (_, args, _), = calls
    assert args[1] == "demo" and args[3] is True


def test_cross_repo_without_repo_id_errors_when_unscoped(tmp_path, calls, resolved):
    server = resolved(*unscoped_scenario("no_cwd_match", tmp_path))
    result = call_over_wire(server, "search_component", {"query": "X", "cross_repo": True})
    assert result.is_error is True and RESTART_HINT in error_text(result)
    assert calls == []


@pytest.mark.usefixtures("trusted_project_tools")
def test_shadow_and_default_notices_coexist(tmp_path, calls, resolved):
    session = demo(tmp_path)
    (session.path / "devgraph.tools.yaml").write_text(TOOLS_YAML)
    server = resolved(Registry([session]), {}, session.path)
    result = call(server, "search_component", {"query": "X"})
    # The default notice comes first: which repository answered matters most.
    assert result.structured_content["notices"] == [CWD_NOTICE, SHADOW_NOTICE]


@pytest.mark.usefixtures("trusted_project_tools")
def test_both_planes_use_the_same_session_repo(tmp_path, calls, resolved, engine):
    session = demo(tmp_path)
    (session.path / "devgraph.tools.yaml").write_text(TOOLS_YAML)
    server = resolved(Registry([session]), {}, session.path)
    builtin = call(server, "search_component", {"query": "X"})
    project = call(server, "list_folder", {"folder": "src"})
    assert project.is_error is False
    (params,) = engine.read_calls
    assert builtin.structured_content["repo_id"] == params["repo_id"] == "demo"


@pytest.mark.usefixtures("trusted_project_tools")
def test_a_tools_file_reload_keeps_the_default(tmp_path, calls, resolved):
    session = demo(tmp_path)
    tools_file = session.path / "devgraph.tools.yaml"
    tools_file.write_text(TOOLS_YAML)
    server = resolved(Registry([session]), {}, session.path)
    tools_file.write_text(TOOLS_YAML.replace("Folder path.", "Changed."))
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert defaulted_repo(server, calls) == "demo"


# ── telemetry ──────────────────────────────────────────────────────────────


def telemetry_lines():
    path = mcp_server.telemetry_path()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_a_defaulted_call_records_no_repo_id(tmp_path, calls, make_server):
    server = make_server(demo(tmp_path), "env")
    call(server, "god_nodes", {})
    (line,) = telemetry_lines()
    assert set(json.loads(line)) == set(mcp_server._TELEMETRY_FIELDS)
    assert "demo" not in line


def test_an_unscoped_error_records_a_failure(tmp_path, calls, resolved):
    server = resolved(*unscoped_scenario("no_cwd_match", tmp_path))
    call_over_wire(server, "god_nodes", {})
    (line,) = telemetry_lines()
    assert json.loads(line)["ok"] is False


# ── instructions ───────────────────────────────────────────────────────────

CROSS_REPO_RULE = "pass cross_repo=true only when the user explicitly wants results across multiple registered repositories"


@pytest.mark.parametrize("source, named", [("env", "DEVGRAPH_MCP_REPO"), ("cwd", "the server's working directory")])
def test_scoped_instructions_name_the_session_repo_and_source(source, named, tmp_path, make_server):
    instructions = make_server(demo(tmp_path), source).instructions
    assert "'demo'" in instructions and named in instructions
    assert "defaults to that repo only" not in instructions
    assert CROSS_REPO_RULE in instructions


def test_unscoped_instructions_require_repo_id(tmp_path, make_server):
    registry = Registry([demo(tmp_path, "alpha")])
    instructions = make_server(None, "none", registry).instructions
    assert "repo_id" in instructions and "devgraph list" in instructions
    assert "alpha" not in instructions and "demo" not in instructions
    assert "defaults to that repo only" not in instructions
    assert CROSS_REPO_RULE in instructions
