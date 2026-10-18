"""Built-in tools never answer a mistake with a plausible empty result: an unknown
repository, an unknown name or an out-of-range number is an error the client can
read, a phantom row is never listed, and an off source says so. Every case goes
over the in-memory MCP client against a live Neo4j and asserts the visible text."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from devgraph.config.settings import Settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server

REPO = f"_smoketest_answers_{uuid.uuid4().hex[:8]}"


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True
    mentions_enabled: bool = False
    pr_source_enabled: bool = False
    issue_source_enabled: bool = False


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.upsert_node("Function", REPO, "validate_token", {"file": "auth/tokens.py"})
    test_engine.upsert_node("Service", REPO, "AuthService", {"file": "compose.yml"})
    test_engine.upsert_node("Module", REPO, "auth/tokens.py")
    test_engine.upsert_node("DesignDecision", REPO, "ADR-001", {"title": "Use tokens"})
    test_engine.upsert_node("Endpoint", REPO, "GET /users/<id>")
    test_engine.upsert_node("Endpoint", REPO, "DELETE /users/<id>")
    test_engine.upsert_relationship("Endpoint", "GET /users/<id>", "CALLS", "Service", "AuthService", REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def _make_client(engine, tmp_path, monkeypatch, **sources):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", allow_cross_repo=True))
    record = _Repo(REPO, tmp_path, **sources)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    def call(name, arguments, *, error):
        async def scenario():
            async with Client(server, mode="auto") as c:
                return await c.call_tool(name, arguments)

        result = anyio.run(scenario)
        assert result.is_error is error, result.content
        (content,) = result.content
        return content.text

    return call


@pytest.fixture
def client(engine, tmp_path, monkeypatch):
    return _make_client(engine, tmp_path, monkeypatch)


@pytest.fixture
def sourced_client(engine, tmp_path, monkeypatch):
    return _make_client(engine, tmp_path, monkeypatch, pr_source_enabled=True, issue_source_enabled=True)


def _error(client, name, arguments):
    return client(name, arguments, error=True)


def _answer(client, name, arguments):
    return json.loads(client(name, arguments, error=False))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("summarise_repository", {}),
        ("search_component", {"query": "auth"}),
        ("impact_analysis", {"component_name": "validate_token"}),
        ("list_services", {}),
        ("explain_architecture", {}),
    ],
)
def test_an_unknown_repo_id_is_an_error(client, tool, arguments):
    text = _error(client, tool, {"repo_id": "no_such_repo", **arguments})
    assert "no such repo_id: 'no_such_repo'" in text


def test_impact_analysis_on_an_unknown_component_suggests_names(client):
    text = _error(client, "impact_analysis", {"component_name": "validate"})
    assert f"no component named 'validate' in repository '{REPO}'" in text
    assert "name='validate_token'" in text


def test_get_service_dependencies_on_an_unknown_service_suggests_names(client):
    text = _error(client, "get_service_dependencies", {"service_name": "AuthServ"})
    assert f"no service named 'AuthServ' in repository '{REPO}'" in text
    assert "name='AuthService'" in text


def test_explain_decision_on_an_unknown_name_suggests_names(client):
    text = _error(client, "explain_decision", {"decision_name": "ADR-00"})
    assert f"no design decision named 'ADR-00' in repository '{REPO}'" in text
    assert "name='ADR-001'" in text


def test_a_known_decision_is_still_explained(client):
    body = _answer(client, "explain_decision", {"decision_name": "ADR-001"})
    assert body["title"] == "Use tokens"


def test_trace_design_rationale_lists_no_phantom_rows(client):
    body = _answer(client, "trace_design_rationale", {"component_name": "auth/tokens.py"})
    assert body["requirements"] == [] and body["notes"] == []


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("list_recent_changes", {"within_commits": 0}, "within_commits must be at least 1, not 0"),
        ("search_component", {"query": "auth", "max_results": 0}, "max_results must be at least 1, not 0"),
        ("search_component", {"query": "auth", "modified_within_commits": -2},
         "modified_within_commits must be at least 1, not -2"),
        ("find_callers", {"target_name": "x", "max_results": -1}, "max_results must be at least 1, not -1"),
        ("god_nodes", {"max_results": 0}, "max_results must be at least 1, not 0"),
        ("find_dependency_cycles", {"max_length": 1}, "max_length must be at least 2, not 1"),
        ("find_communities", {"members_per_community": 0}, "members_per_community must be at least 1, not 0"),
        ("describe_node", {"name": "validate_token", "max_per_type": 0}, "max_per_type must be at least 1, not 0"),
        ("impact_analysis_for_diff", {"base_ref": "HEAD", "head_ref": "HEAD", "max_results": 0},
         "max_results must be at least 1, not 0"),
    ],
)
def test_out_of_range_numbers_are_errors(client, tool, arguments, message):
    assert message in _error(client, tool, arguments)


def test_find_mentions_says_when_mentions_are_off(client):
    body = _answer(client, "find_mentions", {"name": "validate_token"})
    assert body["results"] == []
    assert body["notice"] == f"Mentions ingestion is off for {REPO}; enable it with `devgraph mentions {REPO} enable`"


def test_trace_request_flow_takes_a_bare_path_for_any_method(client):
    bare = _answer(client, "trace_request_flow", {"start_endpoint": "/users/<id>"})
    names = {c["name"] for c in bare["components"]}
    assert {"GET /users/<id>", "DELETE /users/<id>", "AuthService"} <= names
    get = _answer(client, "trace_request_flow", {"start_endpoint": "GET /users/<id>"})
    assert {c["name"] for c in get["components"]} == {"GET /users/<id>", "AuthService"}
    assert get["edges"] == [{"type": "CALLS", "from": "GET /users/<id>", "to": "AuthService"}]


def test_trace_request_flow_on_an_unknown_endpoint_is_an_error(client):
    text = _error(client, "trace_request_flow", {"start_endpoint": "/user"})
    assert f"no endpoint named '/user' in repository '{REPO}'" in text
    assert "GET /users/<id>" in text


def test_a_cross_repo_miss_says_where_its_suggestions_come_from(client):
    text = _error(client, "impact_analysis", {"component_name": "validate", "cross_repo": True})
    assert "no component named 'validate' in any repository." in text
    assert f"Similar names in repository '{REPO}': label='Function', name='validate_token'" in text


@pytest.mark.parametrize(
    "tool", ["find_callers", "find_related_files", "find_requirements_for", "trace_design_rationale"]
)
def test_a_misspelled_component_is_an_error_with_suggestions(client, tool):
    key = "target_name" if tool == "find_callers" else "component_name"
    text = _error(client, tool, {key: "validate_tokne"})
    assert f"no component named 'validate_tokne' in repository '{REPO}'" in text
    text = _error(client, tool, {key: "validate"})
    assert "name='validate_token'" in text


def test_a_known_name_with_no_answers_is_still_empty(client):
    assert _answer(client, "find_callers", {"target_name": "validate_token"})["count"] == 0
    assert _answer(client, "find_requirements_for", {"component_name": "validate_token"})["count"] == 0
    related = _answer(client, "find_related_files", {"component_name": "validate_token"})
    assert related["related_components"]["count"] == 0


@pytest.mark.parametrize("tool", ["find_related_prs", "issue_history_for"])
def test_an_unknown_file_for_prs_and_issues_is_an_error(sourced_client, tool):
    text = _error(sourced_client, tool, {"component_name": "auth/token.py"})
    assert f"no file named 'auth/token.py' in repository '{REPO}'" in text
    assert "name='auth/tokens.py'" in _error(sourced_client, tool, {"component_name": "auth/token"})
    assert _answer(sourced_client, tool, {"component_name": "auth/tokens.py"})["count"] == 0


def test_an_unknown_entity_type_names_the_valid_ones(client):
    text = _error(client, "list_recent_changes", {"within_commits": 5, "entity_type": "Funktion"})
    assert "unknown entity_type 'Funktion'; use one of:" in text and "Function" in text
