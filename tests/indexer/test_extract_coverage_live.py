"""Extraction gaps of audit 7 (items 1, 3 and 6) against a live Neo4j:
`find_callers` lists a caller defined under a `try:` or in an object
literal, and a def moving into or out of one equals a fresh `full_scan`;
`list_endpoints` reports a FastAPI and a Flask file's routes as written.
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import find_callers, list_endpoints
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-coverage-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def incremental_equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _callers(engine, repo_id, name):
    return sorted((row["name"], row["file"]) for row in find_callers(engine, repo_id, name)["results"])


SCANNER = """\
    def full_scan(root):
        return root
"""

ROUTES = """\
    from app.scanner import full_scan


    def build_router():
        def register(root):
            try:
                def scan_and_stamp():
                    return full_scan(root)
            except RuntimeError:
                return None
            return scan_and_stamp()
        return register
"""


def test_a_caller_defined_under_a_try_is_a_caller(engine, repo_id, tmp_path):
    write(tmp_path, "app/scanner.py", SCANNER)
    routes = write(tmp_path, "app/routes.py", ROUTES)
    _scan(engine, repo_id, tmp_path)
    assert ("scan_and_stamp", "app/routes.py") in _callers(engine, repo_id, "full_scan")
    assert _callers(engine, repo_id, "scan_and_stamp") == [("register", "app/routes.py")]

    # The def moves out of the try, then back in: incremental equals fresh.
    write(tmp_path, "app/routes.py", ROUTES.replace("            try:\n", "            if True:\n").replace(
        "            except RuntimeError:\n                return None\n", ""))
    index_paths(engine, repo_id, tmp_path, {routes})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert ("scan_and_stamp", "app/routes.py") in _callers(engine, repo_id, "full_scan")
    write(tmp_path, "app/routes.py", ROUTES)
    index_paths(engine, repo_id, tmp_path, {routes})
    incremental_equals_fresh(engine, repo_id, tmp_path)


FASTAPI = """\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/users/{uid}")
    def get_user(uid):
        \"\"\"Mirrors @app.get("/docs-only").\"\"\"
        return uid


    @app.post("/users")
    def create_user():
        return None
"""

FLASK = """\
    from flask import Flask

    app = Flask(__name__)


    # @app.route("/commented")
    @app.route("/login", methods=["GET", "POST"])
    def login():
        return "ok"
"""


def test_list_endpoints_reports_python_routes_as_written(engine, repo_id, tmp_path):
    write(tmp_path, "api/main.py", FASTAPI)
    write(tmp_path, "web/app.py", FLASK)
    _scan(engine, repo_id, tmp_path)
    rows = list_endpoints(engine, repo_id, max_results=50)["results"]
    assert sorted((row["name"], row["framework"]) for row in rows) == [
        ("GET /login", "Flask"), ("GET /users/{uid}", "FastAPI"), ("POST /login", "Flask"),
        ("POST /users", "FastAPI"),
    ]
    by_name = {row["name"]: row for row in rows}
    assert by_name["POST /users"]["handlers"] == [{"name": "create_user", "file": "api/main.py", "owner": ""}]


SERVICE = """\
    import { repo } from './repo';

    export const userService = {
      async getUser(id) { return repo.find(id); },
      deleteUser: async (id) => repo.remove(id),
    };
"""

PAGE = """\
    import { userService } from './service';
    const lambda = require('./lambda');

    export function showUser(id) {
      return userService.getUser(id);
    }

    export function trigger(event) {
      return lambda.handler(event);
    }
"""

LAMBDA = """\
    exports.handler = async (event) => event;
"""


def test_object_literal_and_exported_functions_have_callers(engine, repo_id, tmp_path):
    write(tmp_path, "src/repo.ts", "export const repo = { find(id) { return id; }, remove(id) { return id; } };\n")
    service = write(tmp_path, "src/service.ts", SERVICE)
    write(tmp_path, "src/page.ts", PAGE)
    write(tmp_path, "src/lambda.js", LAMBDA)
    _scan(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "getUser") == [("showUser", "src/page.ts")]
    assert _callers(engine, repo_id, "find") == [("getUser", "src/service.ts")]
    assert _callers(engine, repo_id, "handler") == [("trigger", "src/page.ts")]

    # The service loses its object literal, then gets it back: incremental equals fresh.
    write(tmp_path, "src/service.ts", "export const userService = null;\n")
    index_paths(engine, repo_id, tmp_path, {service})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    write(tmp_path, "src/service.ts", SERVICE)
    index_paths(engine, repo_id, tmp_path, {service})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "getUser") == [("showUser", "src/page.ts")]
