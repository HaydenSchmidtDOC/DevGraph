"""An Express route with an empty path (`router.get('', h)`) is the route
at its router's mount, as `'/'` is (jsts/extractor.py `_express_routes`),
in the router's own file and under another file's mount, whose edits equal
a fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.jsts.extractor import extract_js_file
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def _routes(code, file_path="src/app.js"):
    result = extract_js_file(textwrap.dedent(code), file_path, "repo")
    endpoints = sorted(n.name for n in result.nodes if n.label == "Endpoint")
    implements = sorted(
        (r.from_name, r.to_name) for r in result.relationships if r.rel_type == "IMPLEMENTS" and r.to_file
    )
    return endpoints, implements


def test_an_empty_path_is_the_routers_mount():
    endpoints, implements = _routes("""\
        const express = require('express');
        const app = express();
        const r = express.Router();
        r.get('', listOrders);
        r.post('', createOrder);
        app.get('', home);
        app.use('/orders', r);
        function listOrders(req, res) {}
        function createOrder(req, res) {}
        function home(req, res) {}
    """)
    assert endpoints == ["GET /", "GET /orders", "POST /orders"]
    assert implements == [("GET /", "home"), ("GET /orders", "listOrders"), ("POST /orders", "createOrder")]


def test_a_koa_named_route_still_needs_its_path():
    # `router.get('name', '/x', h)` is Koa-router's named route; a bare name with no path is none.
    endpoints, _ = _routes("""\
        const Router = require('@koa/router');
        const router = new Router();
        router.get('orders', '/orders', listOrders);
        router.get('orders', listOrders);
        function listOrders(ctx) {}
    """)
    assert endpoints == ["GET ?/orders (src/app.js)"]


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
    repo = f"zz-exempty-{uuid.uuid4().hex[:8]}"
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


def _implemented(engine, repo_id):
    return sorted(
        (row["endpoint"], row["handler"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f) WHERE f.file IS NOT NULL "
            "RETURN e.name AS endpoint, f.name AS handler",
            {"r": repo_id},
        )
    )


ORDERS = """\
    const express = require('express');
    const router = express.Router();
    router.get('', listOrders);
    function listOrders(req, res) {}
    module.exports = router;
"""

APP = """\
    const express = require('express');
    const orders = require('./orders');
    const app = express();
    app.use('/orders', orders);
"""


def test_an_empty_path_under_another_files_mount(engine, repo_id, tmp_path):
    write(tmp_path, "web/orders.js", ORDERS)
    app = write(tmp_path, "web/app.js", APP)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [("GET /orders", "listOrders")]

    write(tmp_path, "web/app.js", APP.replace("'/orders'", "'/purchases'"))
    index_paths(engine, repo_id, tmp_path, {app})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [("GET /purchases", "listOrders")]
