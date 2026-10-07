"""Built-in MCP tools' repo_id: explicit calls unchanged, omitted ones use the session's repository.

Every built-in body is stubbed with a recorder, so these exercise the server layer only:
no Neo4j, no git, no `gh`.
"""

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools as devgraph_tools


@dataclass
class Repo:
    repo_id: str
    path: Path
    active: bool = True
    name: str = ""


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
    def run_cypher(self, query, params=None):
        return []


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
def make_server(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", enable_run_cypher=True)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    engine = Engine()

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
    return Repo(repo_id, path, active, name=f"{repo_id}-name")


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
