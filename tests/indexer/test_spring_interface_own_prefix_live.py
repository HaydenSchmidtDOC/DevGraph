"""A Spring controller that gains or loses a `@RequestMapping` of its own
against a live Neo4j (java/extractor.py `_spring_routes`): with one it no
longer implements its interface's routes, whose paths are not its, and
each edit equals a fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
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
    repo = f"zz-springown-{uuid.uuid4().hex[:8]}"
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

@RequestMapping("/api")
public interface OrdersApi {
    @GetMapping("/orders/{id}")
    Order getOrder(Long id);
}
"""
CONTROLLER = """\
package shop.web;

import shop.api.OrdersApi;

@RestController{own}
public class OrdersController implements OrdersApi {
    public Order getOrder(Long id) { return null; }
}
"""
CONTROLLER_PATH = "src/main/java/shop/web/OrdersController.java"


def _controller_routes(engine, repo_id):
    return {e for e, file, _owner in _implements(engine, repo_id) if file == CONTROLLER_PATH}


def test_a_controller_with_its_own_prefix_implements_no_interface_route(engine, repo_id, tmp_path):
    write(tmp_path, "src/main/java/shop/api/OrdersApi.java", API)
    controller = write(tmp_path, CONTROLLER_PATH, CONTROLLER.replace("{own}", ""))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _controller_routes(engine, repo_id) == {"GET /api/orders/{id}"}

    write(tmp_path, CONTROLLER_PATH, CONTROLLER.replace("{own}", '\n@RequestMapping("/orders")'))
    index_paths(engine, repo_id, tmp_path, {controller})
    assert _controller_routes(engine, repo_id) == set()
    incremental_equals_fresh(engine, repo_id, tmp_path)

    write(tmp_path, CONTROLLER_PATH, CONTROLLER.replace("{own}", ""))
    index_paths(engine, repo_id, tmp_path, {controller})
    assert _controller_routes(engine, repo_id) == {"GET /api/orders/{id}"}
    incremental_equals_fresh(engine, repo_id, tmp_path)
