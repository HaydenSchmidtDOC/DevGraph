"""MCP answers on a scanned repository against a live Neo4j (audit 7): a route
handler's file-less stub is folded into its real function, datastores and
containers are listed, described and searched like other components, and test
callers come after production ones. Fixture code is fictional."""

import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


FILES = {
    "compose.yaml": """\
        services:
          api:
            build: ./api
          web:
            build: ./web
          db:
            image: postgres:16
          cache:
            image: redis:7
        """,
    "api/main.py": """\
        import redis
        import psycopg2
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/users/{uid}")
        def get_user(uid):
            return load_user(uid)


        def load_user(uid):
            return {"id": uid}
        """,
    "web/app.js": """\
        import express from 'express';
        import { listOrders } from './orders';
        const app = express();
        app.get('/api/users', listUsers);
        app.get('/api/orders', listOrders);
        app.get('/api/ghost', ghostHandler);
        app.get('/api/show', show);
        function listUsers(req, res) { res.json(fetchUsers()); }
        function fetchUsers() { return []; }
        """,
    # The handler a route imports, from another file.
    "web/orders.js": """\
        export function listOrders(req, res) { res.json(loadOrders()); }
        function loadOrders() { return []; }
        """,
    # A `show` the route in app.js never names: not its handler.
    "web/views.js": "export function show() { return 1; }\n",
    # Two methods named like the route's handler, neither of them it.
    "web/admin.js": """\
        import express from 'express';
        const admin = express();
        admin.get('/admin', handle);
        class A { handle() { return 1; } }
        class B { handle() { return 2; } }
        """,
}


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("answers")
    repo_id = f"zz-answers-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, _Registry([_Repo(repo_id, root)])
    engine.delete_repository(repo_id)


def test_the_fixture_has_handler_stubs(engine, repo):
    repo_id, _ = repo
    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $r}) WHERE n.file IS NULL RETURN n.name AS name ORDER BY name", {"r": repo_id}
    )
    assert [row["name"] for row in rows] == ["get_user", "ghostHandler", "handle", "listOrders", "listUsers", "show"]


FOLDED = [("get_user", "api/main.py"), ("listUsers", "web/app.js"), ("listOrders", "web/orders.js")]


@pytest.mark.parametrize(("name", "file"), FOLDED)
def test_get_source_answers_a_handler_with_its_file(engine, repo, name, file):
    repo_id, registry = repo
    result = tools.get_source(engine, registry, repo_id, name)
    assert "status" not in result
    assert result["file"] == file
    assert f"{name}" in result["source"]


@pytest.mark.parametrize(("name", "file"), FOLDED)
def test_describe_node_finds_a_handler_and_folds_in_its_stub(engine, repo, name, file):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, name)
    assert result["status"] == "found"
    assert result["node"]["file"] == file
    assert result["incoming"]["IMPLEMENTS"]["count"] == 1


@pytest.mark.parametrize(("endpoint", "name", "file"), [
    ("GET /api/users", "listUsers", "web/app.js"), ("GET /api/orders", "listOrders", "web/orders.js"),
])
def test_describe_node_lists_an_endpoints_handler_once(engine, repo, endpoint, name, file):
    repo_id, _ = repo
    implements = tools.describe_node(engine, repo_id, endpoint)["outgoing"]["IMPLEMENTS"]
    assert implements["count"] == 1
    assert [(r["name"], r.get("file")) for r in implements["results"]] == [(name, file)]


def test_a_handler_with_no_definition_still_describes_its_stub(engine, repo):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, "ghostHandler")
    assert result["status"] == "found"
    assert "file" not in result["node"] and result["node"]["unresolved_handler"] is True
    assert result["incoming"]["IMPLEMENTS"]["results"][0]["name"] == "GET /api/ghost"


def test_a_stub_is_not_folded_into_an_unrelated_function_of_its_name(engine, repo):
    repo_id, registry = repo
    result = tools.describe_node(engine, repo_id, "show")
    assert result["status"] == "ambiguous"
    by_file = {c.get("file"): c for c in result["candidates"]}
    assert set(by_file) == {None, "web/views.js"}
    assert by_file[None]["unresolved_handler"] is True and "unresolved_handler" not in by_file["web/views.js"]
    assert tools.get_source(engine, registry, repo_id, "show")["status"] == "ambiguous"
    implements = tools.describe_node(engine, repo_id, "GET /api/show")["outgoing"]["IMPLEMENTS"]["results"]
    assert [(r["name"], r.get("file")) for r in implements] == [("show", None)]
    views = tools.describe_node(engine, repo_id, "show", file="web/views.js")
    assert "IMPLEMENTS" not in views["incoming"]
    found = tools.search_component(engine, repo_id, "show")["results"]
    assert sorted(str(r["file"]) for r in found if r["name"] == "show") == ["web/app.js", "web/views.js"]


def test_a_stub_with_two_possible_handlers_stays_visible(engine, repo):
    repo_id, registry = repo
    result = tools.get_source(engine, registry, repo_id, "handle")
    assert result["status"] == "ambiguous"
    assert sorted((c.get("file") or "", c.get("owner") or "") for c in result["candidates"]) == [
        ("", ""), ("web/admin.js", "A"), ("web/admin.js", "B"),
    ]
    # The route's reference links to both methods, so the stub stands for neither.
    implements = tools.describe_node(engine, repo_id, "GET /admin")["outgoing"]["IMPLEMENTS"]["results"]
    assert ("handle", None) in [(r["name"], r.get("file")) for r in implements]


def _consistent(result):
    names = [c["name"] for c in result["components"]]
    assert all(e["from"] in names and e["to"] in names for e in result["edges"])
    return names


@pytest.mark.parametrize(("endpoint", "handler", "callee"), [
    ("GET /api/users", "listUsers", "fetchUsers"), ("GET /api/orders", "listOrders", "loadOrders"),
])
def test_trace_request_flow_lists_each_handler_once(engine, repo, endpoint, handler, callee):
    repo_id, _ = repo
    result = tools.trace_request_flow(engine, repo_id, endpoint)
    names = _consistent(result)
    assert names.count(handler) == 1 and callee in names
    assert [e for e in result["edges"] if e["type"] == "IMPLEMENTS"] == [
        {"type": "IMPLEMENTS", "from": endpoint, "to": handler}
    ]


@pytest.mark.parametrize(("endpoint", "handler", "listed"), [
    ("GET /api/ghost", "ghostHandler", 1), ("GET /api/show", "show", 1),
    ("GET /admin", "handle", 3),  # the stub and both methods the route's reference links to
])
def test_trace_request_flow_keeps_an_unresolved_handler(engine, repo, endpoint, handler, listed):
    repo_id, _ = repo
    result = tools.trace_request_flow(engine, repo_id, endpoint)
    assert _consistent(result).count(handler) == listed
    assert {"type": "IMPLEMENTS", "from": endpoint, "to": handler} in result["edges"]


def test_the_fixture_has_datastores_and_containers(engine, repo):
    repo_id, _ = repo
    rows = engine.run_cypher(
        "MATCH (n) WHERE n.repo_id = $r AND (n:Cache OR n:Database OR n:Container) "
        "RETURN labels(n)[0] AS label, n.name AS name ORDER BY label, name",
        {"r": repo_id},
    )
    found = {(row["label"], row["name"]) for row in rows}
    assert {("Cache", "Redis"), ("Database", "PostgreSQL"), ("Container", "redis"), ("Container", "postgres")} <= found


def test_explain_architecture_lists_a_cache(engine, repo):
    repo_id, _ = repo
    services = tools.explain_architecture(engine, repo_id)["services_and_datastores"]["results"]
    uses = {row["service"]: sorted(row["uses"]) for row in services}
    assert uses["api"] == ["PostgreSQL", "Redis"]


def test_summarise_repository_counts_caches(engine, repo):
    repo_id, _ = repo
    summary = tools.summarise_repository(engine, repo_id)
    assert summary["cache_count"] == 1
    assert summary["database_count"] == 1


def test_describe_node_finds_a_cache_without_a_label(engine, repo):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, "Redis")
    assert result["status"] == "found"
    assert result["node"]["label"] == "Cache"
    assert any(ref["name"] == "api" for ref in result["incoming"]["USES"]["results"])


@pytest.mark.parametrize(
    ("query", "expected"),
    [("redis", {("Cache", "Redis"), ("Container", "redis")}),
     ("postgres", {("Database", "PostgreSQL"), ("Container", "postgres")})],
)
def test_search_component_finds_datastores_and_containers(engine, repo, query, expected):
    repo_id, _ = repo
    result = tools.search_component(engine, repo_id, query)
    assert expected <= {(r["labels"][0], r["name"]) for r in result["results"]}


@pytest.mark.parametrize("tool", ["list_recent_changes", "find_mentions"])
def test_cache_is_a_known_entity_type(engine, repo, tool):
    repo_id, _ = repo
    if tool == "list_recent_changes":
        result = tools.list_recent_changes(engine, repo_id, within_commits=1, entity_type="Cache")
    else:
        result = tools.find_mentions(engine, repo_id, "Redis", label="Cache")
    assert result["count"] == 0


CALLER_FILES = {
    "core/scan.py": (
        "from core.util import util\nfrom tests.fakes import fake\n\n\ndef work():\n    return util() + fake()\n"
    ),
    "core/util.py": "def util():\n    return 1\n",
    "tests/fakes.py": "def fake():\n    return 0\n",
    "app/cli.py": "from core.scan import work\n\n\ndef run():\n    return work()\n\n\ndef rerun():\n    return work()\n",
    "tests/test_work.py": "from core.scan import work\n\n\n" + "".join(
        f"def test_case_{i:02d}():\n    assert work()\n\n\n" for i in range(20)
    ),
}


@pytest.fixture(scope="module")
def callers_repo(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("callers")
    repo_id = f"zz-answers-callers-{uuid.uuid4().hex[:8]}"
    for rel, text in CALLER_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def test_find_callers_lists_production_callers_before_tests(engine, callers_repo):
    result = tools.find_callers(engine, callers_repo, "work", max_results=5)
    assert result["count"] == 22 and result["test_count"] == 20
    assert [(r["name"], r["file"]) for r in result["results"][:2]] == [("rerun", "app/cli.py"), ("run", "app/cli.py")]
    assert all(r["file"] == "tests/test_work.py" for r in result["results"][2:])


def test_find_callers_can_leave_tests_out(engine, callers_repo):
    result = tools.find_callers(engine, callers_repo, "work", include_tests=False)
    assert [r["name"] for r in result["results"]] == ["rerun", "run"]
    assert result["count"] == 2 and result["test_count"] == 20 and result["truncated"] is False


def test_impact_analysis_lists_production_dependents_before_tests(engine, callers_repo):
    result = tools.impact_analysis(engine, callers_repo, "work", max_results=5)
    direct = result["direct_dependents"]
    assert direct["count"] == 22 and direct["test_count"] == 20
    assert sorted(r["name"] for r in direct["results"][:2]) == ["rerun", "run"]
    assert result["risk_level"] == "high"
    without = tools.impact_analysis(engine, callers_repo, "work", include_tests=False)["direct_dependents"]
    assert sorted(r["name"] for r in without["results"]) == ["rerun", "run"] and without["test_count"] == 20


def test_impact_analysis_lists_each_group_by_name(engine, callers_repo):
    results = tools.impact_analysis(engine, callers_repo, "work", max_results=50)["direct_dependents"]["results"]
    assert [r["name"] for r in results] == ["rerun", "run", *(f"test_case_{i:02d}" for i in range(20))]


def test_test_count_is_exact_when_the_row_cap_cuts_the_callers(engine, callers_repo, monkeypatch):
    monkeypatch.setattr(tools, "LIST_ROW_LIMIT", 5)
    result = tools.find_callers(engine, callers_repo, "work", max_results=50)
    assert len(result["results"]) == 5 and result["truncated"] is True
    assert [r["name"] for r in result["results"][:2]] == ["rerun", "run"]
    assert result["test_count"] == 20


def test_find_related_files_lists_test_code_last(engine, callers_repo):
    result = tools.find_related_files(engine, callers_repo, "work")
    related = result["related_components"]
    assert related["results"] == ["util", "fake"] and related["test_count"] == 1
    assert result["containing_modules"]["test_count"] == 0
    without = tools.find_related_files(engine, callers_repo, "work", include_tests=False)["related_components"]
    assert without["results"] == ["util"] and without["count"] == 1 and without["test_count"] == 1


def test_the_cypher_test_path_rule_agrees_with_python(engine):
    from devgraph.test_paths import TEST_PATH_CYPHER, is_test_path

    from tests.test_test_paths import CASES

    paths = [path for path, _ in CASES if path]
    rows = engine.run_cypher("UNWIND $paths AS p RETURN p, p =~ $pattern AS test", {"paths": paths, "pattern": TEST_PATH_CYPHER})
    assert {row["p"]: row["test"] for row in rows} == {p: is_test_path(p) for p in paths}
