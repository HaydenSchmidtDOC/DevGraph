"""Spring mappings on an interface against a live Neo4j
(devgraph/indexer/apis/routes.py `spring_interface_pins`): a controller in
another file implementing the interface IMPLEMENTS its routes, and adding,
editing, unhooking and deleting the controller equals a fresh `full_scan`.
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
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
    repo = f"zz-springif-{uuid.uuid4().hex[:8]}"
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


def _implements(engine, repo_id):
    return {
        (row["e"], row["file"], row["owner"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f:Function) WHERE f.file IS NOT NULL "
            "RETURN e.name AS e, f.file AS file, f.owner AS owner",
            {"r": repo_id},
        )
    }


API = """\
package shop.api;

@RequestMapping("/api/v1")
public interface OrdersApi {
    @GetMapping("/orders/{id}")
    Order getOrder(Long id);
}
"""
CONTROLLER = """\
package shop.web;

import shop.api.OrdersApi;

@RestController
public class OrdersController implements OrdersApi {
    public Order getOrder(Long id) { return null; }
}
"""
KOTLIN = """\
package shop.web

@RestController
class LegacyOrders : OrdersApi {
    override fun getOrder(id: Long): Order = TODO()
}
"""
API_PATH = "src/main/java/shop/api/OrdersApi.java"
CONTROLLER_PATH = "src/main/java/shop/web/OrdersController.java"
KOTLIN_PATH = "src/main/kotlin/shop/web/LegacyOrders.kt"
ROUTE = "GET /api/v1/orders/{id}"


def test_a_controller_implementing_an_interface_implements_its_routes(engine, repo_id, tmp_path):
    write(tmp_path, API_PATH, API)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _implements(engine, repo_id) == {(ROUTE, API_PATH, "OrdersApi")}

    # The controller added after the interface's file was indexed.
    controller = write(tmp_path, CONTROLLER_PATH, CONTROLLER)
    index_paths(engine, repo_id, tmp_path, {controller})
    assert _implements(engine, repo_id) == {
        (ROUTE, API_PATH, "OrdersApi"), (ROUTE, CONTROLLER_PATH, "OrdersController"),
    }
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # Edited, then no longer implementing the interface, then again.
    write(tmp_path, CONTROLLER_PATH, CONTROLLER.replace("return null;", "return new Order();"))
    index_paths(engine, repo_id, tmp_path, {controller})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    write(tmp_path, CONTROLLER_PATH, CONTROLLER.replace(" implements OrdersApi", ""))
    index_paths(engine, repo_id, tmp_path, {controller})
    assert _implements(engine, repo_id) == {(ROUTE, API_PATH, "OrdersApi")}
    incremental_equals_fresh(engine, repo_id, tmp_path)
    write(tmp_path, CONTROLLER_PATH, CONTROLLER)
    index_paths(engine, repo_id, tmp_path, {controller})
    assert (ROUTE, CONTROLLER_PATH, "OrdersController") in _implements(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # A Kotlin controller of the same interface, then the Java one deleted.
    kotlin = write(tmp_path, KOTLIN_PATH, KOTLIN)
    index_paths(engine, repo_id, tmp_path, {kotlin})
    assert (ROUTE, KOTLIN_PATH, "LegacyOrders") in _implements(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)
    controller.unlink()
    remove_paths(engine, repo_id, tmp_path, {controller})
    assert _implements(engine, repo_id) == {(ROUTE, API_PATH, "OrdersApi"), (ROUTE, KOTLIN_PATH, "LegacyOrders")}
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The interface's mapping edited.
    api = tmp_path / API_PATH
    write(tmp_path, API_PATH, API.replace("/orders/{id}", "/orders/{orderId}"))
    index_paths(engine, repo_id, tmp_path, {api})
    assert (ROUTE.replace("{id}", "{orderId}"), KOTLIN_PATH, "LegacyOrders") in _implements(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)
