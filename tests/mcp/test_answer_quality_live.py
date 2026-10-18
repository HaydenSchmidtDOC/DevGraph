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
        const express = require('express');
        const app = express();
        app.get('/api/users', listUsers);
        app.get('/api/ghost', ghostHandler);
        function listUsers(req, res) { res.json(fetchUsers()); }
        function fetchUsers() { return []; }
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
    assert [row["name"] for row in rows] == ["get_user", "ghostHandler", "listUsers"]


@pytest.mark.parametrize(("name", "file"), [("get_user", "api/main.py"), ("listUsers", "web/app.js")])
def test_get_source_answers_a_handler_with_its_file(engine, repo, name, file):
    repo_id, registry = repo
    result = tools.get_source(engine, registry, repo_id, name)
    assert "status" not in result
    assert result["file"] == file
    assert f"{name}" in result["source"]


@pytest.mark.parametrize(("name", "file"), [("get_user", "api/main.py"), ("listUsers", "web/app.js")])
def test_describe_node_finds_a_handler_and_folds_in_its_stub(engine, repo, name, file):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, name)
    assert result["status"] == "found"
    assert result["node"]["file"] == file
    assert result["incoming"]["IMPLEMENTS"]["count"] == 1


def test_describe_node_lists_an_endpoints_handler_once(engine, repo):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, "GET /api/users")
    implements = result["outgoing"]["IMPLEMENTS"]
    assert implements["count"] == 1
    assert [(r["name"], r.get("file")) for r in implements["results"]] == [("listUsers", "web/app.js")]


def test_a_handler_with_no_definition_still_describes_its_stub(engine, repo):
    repo_id, _ = repo
    result = tools.describe_node(engine, repo_id, "ghostHandler")
    assert result["status"] == "found"
    assert "file" not in result["node"]
    assert result["incoming"]["IMPLEMENTS"]["results"][0]["name"] == "GET /api/ghost"


def test_trace_request_flow_lists_each_handler_once(engine, repo):
    repo_id, _ = repo
    result = tools.trace_request_flow(engine, repo_id, "GET /api/users")
    names = [c["name"] for c in result["components"]]
    assert names.count("listUsers") == 1
    assert "fetchUsers" in names
    result = tools.trace_request_flow(engine, repo_id, "GET /api/ghost")
    assert [c["name"] for c in result["components"]].count("ghostHandler") == 1  # a lone stub is kept


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
