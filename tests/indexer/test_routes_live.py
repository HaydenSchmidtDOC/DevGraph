"""Non-Python routes against a live Neo4j (devgraph/indexer/apis/routes.py): a
service's Express, Spring, ASP.NET Core or Go routes are Endpoints that
`explain_architecture` lists under their compose service and
`trace_request_flow` follows to their handler, and an edit equals a fresh
`full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.mcp.tools import explain_architecture, trace_request_flow
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
    repo = f"zz-routes-{uuid.uuid4().hex[:8]}"
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


COMPOSE = """\
    services:
      shop:
        build: ./services/shop
"""

# One fixture per framework: (route file, its text, another file, its text,
# the endpoint the route file maps, its handler and the handler's file).
FRAMEWORKS = {
    "express": (
        "services/shop/routes.ts", """\
        import { Router } from 'express';
        import { listOrders } from './orders';
        const router = Router();
        router.get('/orders', listOrders);
        router.post('/orders/:id/cancel', cancelOrder);
        function cancelOrder(req, res) {}
        """,
        "services/shop/orders.ts", "export function listOrders(req, res) {}\n",
        "GET /orders", "listOrders", "services/shop/orders.ts",
    ),
    "spring-java": (
        "services/shop/src/main/java/shop/OrderController.java", """\
        package shop;

        @RestController
        public class OrderController {
            @GetMapping("/orders")
            public String list() { return new OrderService().all(); }
        }
        """,
        "services/shop/src/main/java/shop/OrderService.java",
        "package shop;\n\npublic class OrderService {\n    public String all() { return \"\"; }\n}\n",
        "GET /orders", "list", "services/shop/src/main/java/shop/OrderController.java",
    ),
    "spring-kotlin": (
        "services/shop/src/main/kotlin/shop/OrderController.kt", """\
        package shop

        @RestController
        class OrderController(private val orders: OrderService) {
            @PostMapping("/orders")
            fun place(): String = orders.all()
        }
        """,
        "services/shop/src/main/kotlin/shop/OrderService.kt",
        "package shop\n\nclass OrderService {\n    fun all(): String = \"\"\n}\n",
        "POST /orders", "place", "services/shop/src/main/kotlin/shop/OrderController.kt",
    ),
    "aspnet": (
        "services/shop/Controllers/OrdersController.cs", """\
        namespace Shop.Controllers;

        [ApiController]
        public class OrdersController : ControllerBase
        {
            [HttpDelete("/orders")]
            public IActionResult Clear() => Ok();
        }
        """,
        "services/shop/Program.cs", "var app = WebApplication.Create(args);\napp.Run();\n",
        "DELETE /orders", "Clear", "services/shop/Controllers/OrdersController.cs",
    ),
}


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _handlers(engine, repo_id, endpoint):
    return sorted(
        (row["file"] or "", row["name"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r, name: $e})-[:IMPLEMENTS]->(f:Function) "
            "RETURN f.file AS file, f.name AS name",
            {"r": repo_id, "e": endpoint},
        )
    )


@pytest.mark.parametrize("framework", sorted(FRAMEWORKS))
def test_routes_reach_the_architecture_tools(engine, repo_id, tmp_path, framework):
    route_file, route_text, other_file, other_text, endpoint, handler, handler_file = FRAMEWORKS[framework]
    write(tmp_path, "compose.yaml", COMPOSE)
    routes = write(tmp_path, route_file, route_text)
    other = write(tmp_path, other_file, other_text)
    _scan(engine, repo_id, tmp_path)

    endpoints = explain_architecture(engine, repo_id)["endpoints"]["results"]
    assert [entry["calls"] for entry in endpoints] == ["shop"]
    assert endpoint in endpoints[0]["endpoints"]
    flow = trace_request_flow(engine, repo_id, endpoint)
    assert {"name": handler, "labels": ["Function"], "repo_id": repo_id} in flow["components"]
    assert {"name": "shop", "labels": ["Service"], "repo_id": repo_id} in flow["components"]
    # The handler's own Function, and the file-less stub.
    assert _handlers(engine, repo_id, endpoint) == [("", handler), (handler_file, handler)]

    # The other file goes and comes back, then the route file loses every
    # route: incremental equals fresh after each.
    other.unlink()
    remove_paths(engine, repo_id, tmp_path, {other})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    write(tmp_path, other_file, other_text)
    index_paths(engine, repo_id, tmp_path, {other})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _handlers(engine, repo_id, endpoint) == [("", handler), (handler_file, handler)]
    write(tmp_path, route_file, "\n".join(
        line for line in textwrap.dedent(route_text).splitlines() if endpoint.split()[1] not in line
    ))
    index_paths(engine, repo_id, tmp_path, {routes})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert all(endpoint not in entry["endpoints"] for entry in explain_architecture(engine, repo_id)["endpoints"]["results"])
