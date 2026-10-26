"""A Spring route mapped on an interface, against a live Neo4j: its Endpoint
IMPLEMENTS the interface's method and the same-named method of each
controller implementing the interface (apis/routes.py
`spring_interface_pins`), so its handler stub resolved, to one method and
its overrides. The stub stands for nothing of its own: describe_node and
search_component show only the real methods, and trace_request_flow lists
each once, with its file. Two unrelated classes' same-named methods a route
links to still leave the stub visible (tests/mcp/test_answer_quality_live.py).
Fixture code is fictional."""

import textwrap
import uuid

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


API_PATH = "src/main/java/shop/api/OrdersApi.java"
JAVA_PATH = "src/main/java/shop/web/OrdersController.java"
KOTLIN_PATH = "src/main/kotlin/shop/web/LegacyOrders.kt"
FILES = {
    API_PATH: """\
        package shop.api;

        @RequestMapping("/api/v1")
        public interface OrdersApi {
            @GetMapping("/orders/{id}")
            Order getOrder(Long id);
        }
        """,
    JAVA_PATH: """\
        package shop.web;

        import shop.api.OrdersApi;

        @RestController
        public class OrdersController implements OrdersApi {
            public Order getOrder(Long id) { return loadOrder(id); }
            Order loadOrder(Long id) { return null; }
        }
        """,
    KOTLIN_PATH: """\
        package shop.web

        import shop.api.OrdersApi

        @RestController
        class LegacyOrders : OrdersApi {
            override fun getOrder(id: Long): Order = TODO()
        }
        """,
}
ROUTE = "GET /api/v1/orders/{id}"
HANDLERS = [(API_PATH, "OrdersApi"), (KOTLIN_PATH, "LegacyOrders"), (JAVA_PATH, "OrdersController")]


@pytest.fixture(scope="module")
def repo_id(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("spring-interface-handlers")
    repo_id = f"zz-springif-handlers-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def test_the_route_implements_the_interface_method_and_each_override(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Endpoint {repo_id: $r, name: $e})-[:IMPLEMENTS]->(f:Function) "
        "RETURN f.file AS file, f.owner AS owner",
        {"r": repo_id, "e": ROUTE},
    )
    assert sorted((row["file"] or "", row["owner"] or "") for row in rows) == sorted([("", ""), *HANDLERS])


def test_describe_node_lists_only_the_real_methods(engine, repo_id):
    result = tools.describe_node(engine, repo_id, "getOrder")
    assert result["status"] == "ambiguous"
    assert sorted((c.get("file"), c.get("owner"), c.get("unresolved_handler")) for c in result["candidates"]) == [
        (file, owner, None) for file, owner in sorted(HANDLERS)
    ]


def test_search_component_has_no_file_less_handler(engine, repo_id):
    found = [r for r in tools.search_component(engine, repo_id, "getOrder")["results"] if r["name"] == "getOrder"]
    assert sorted(r["file"] for r in found) == sorted(file for file, _ in HANDLERS)


def test_trace_request_flow_lists_each_method_with_its_file(engine, repo_id):
    result = tools.trace_request_flow(engine, repo_id, ROUTE)
    handlers = [(c["file"], c.get("owner")) for c in result["components"] if c["name"] == "getOrder"]
    assert sorted(handlers) == sorted(HANDLERS)
    assert "loadOrder" in [c["name"] for c in result["components"]]
