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
        import express from 'express';
        import { listOrders } from './orders';
        const app = express();
        app.get('/orders', listOrders);
        app.post('/orders/:id/cancel', cancelOrder);
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
    "go": (
        "services/shop/main.go", """\
        package main

        import "net/http"

        func main() {
        \tmux := http.NewServeMux()
        \tmux.HandleFunc("GET /orders", listOrders)
        \thttp.ListenAndServe(":8080", mux)
        }
        """,
        "services/shop/orders.go",
        "package main\n\nimport \"net/http\"\n\nfunc listOrders(w http.ResponseWriter, r *http.Request) {}\n",
        "GET /orders", "listOrders", "services/shop/orders.go",
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
    listed = [{k: c[k] for k in ("name", "labels", "repo_id")} for c in flow["components"]]
    assert {"name": handler, "labels": ["Function"], "repo_id": repo_id} in listed
    assert {"name": "shop", "labels": ["Service"], "repo_id": repo_id} in listed
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


def _endpoint_sources(engine, repo_id):
    return sorted(
        (row["name"], tuple(row["sources"] or [row["source"]]))
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r}) RETURN e.name AS name, e.source AS source, e.sources AS sources",
            {"r": repo_id},
        )
    )


def _drop_record(engine, repo_id, module, entry):
    """What overlapping batches of two processes left before the record's
    lock: the file claims `entry` but its Module's record lacks it."""
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: $m}) SET m.shared_claims = [x IN m.shared_claims WHERE x <> $e]",
        {"r": repo_id, "m": module, "e": entry},
    )


ROUTED = "import express from 'express';\nconst app = express();\napp.get('/orders', list);\nfunction list() {}\n"
UNROUTED = "export function list() {}\n"


def test_a_record_that_lost_a_claim_heals_on_a_full_scan(engine, repo_id, tmp_path):
    from devgraph.graph.engine import SHARED_CLAIM_SEP

    routes = write(tmp_path, "web/routes.ts", ROUTED)
    _scan(engine, repo_id, tmp_path)
    assert _endpoint_sources(engine, repo_id) == [("GET /orders", ("web/routes.ts",))]
    _drop_record(engine, repo_id, "web/routes.ts", SHARED_CLAIM_SEP.join(("Endpoint", "GET /orders")))

    # A replace unclaims what the record lists, so the lost claim survives it.
    write(tmp_path, "web/routes.ts", UNROUTED)
    index_paths(engine, repo_id, tmp_path, {routes})
    assert _endpoint_sources(engine, repo_id) == [("GET /orders", ("web/routes.ts",))]

    # A full scan rebuilds the records from the claims first: healed.
    full_scan(engine, repo_id, tmp_path)
    assert _endpoint_sources(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_deleting_a_file_reads_every_claim(engine, repo_id, tmp_path):
    """A delete reads every claim, not the record: one the record lost goes too."""
    from devgraph.graph.engine import SHARED_CLAIM_SEP

    routes = write(tmp_path, "web/routes.ts", ROUTED)
    _scan(engine, repo_id, tmp_path)
    _drop_record(engine, repo_id, "web/routes.ts", SHARED_CLAIM_SEP.join(("Endpoint", "GET /orders")))
    routes.unlink()
    remove_paths(engine, repo_id, tmp_path, {routes})
    assert _endpoint_sources(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_a_full_scan_heals_a_lost_claim_of_a_deleted_file(engine, repo_id, tmp_path):
    """The file is gone before the scan: its prune deletes it, and the claim
    its record lost goes with it."""
    from devgraph.graph.engine import SHARED_CLAIM_SEP

    routes = write(tmp_path, "web/routes.ts", ROUTED)
    write(tmp_path, "web/other.ts", UNROUTED)
    _scan(engine, repo_id, tmp_path)
    _drop_record(engine, repo_id, "web/routes.ts", SHARED_CLAIM_SEP.join(("Endpoint", "GET /orders")))
    routes.unlink()
    full_scan(engine, repo_id, tmp_path)
    assert _endpoint_sources(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_two_writes_of_one_file_take_turns_on_its_record(engine, repo_id, tmp_path):
    """A replace reads its file's record under the Module's lock, so a
    second write waits for the first and unclaims what the first claimed."""
    import threading

    routes = write(tmp_path, "web/routes.ts", UNROUTED)
    _scan(engine, repo_id, tmp_path)
    with engine._driver.session() as session:
        tx = session.begin_transaction()
        tx.run(
            "MATCH (m:Module {repo_id: $r, name: 'web/routes.ts'}) SET m.name = m.name", {"r": repo_id}
        ).consume()
        done = threading.Event()
        write(tmp_path, "web/routes.ts", ROUTED)
        worker = threading.Thread(
            target=lambda: (index_paths(engine, repo_id, tmp_path, {routes}), done.set()), daemon=True
        )
        worker.start()
        # The other write waits on the lock this transaction holds.
        assert not done.wait(2)
        tx.commit()
    worker.join(60)
    assert done.is_set()
    assert _endpoint_sources(engine, repo_id) == [("GET /orders", ("web/routes.ts",))]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def _owners(engine, repo_id):
    return sorted(
        (row["e"], row["s"], tuple(row["o"]))
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[c:CALLS]->(s:Service) RETURN e.name AS e, s.name AS s, c.origins AS o "
            "UNION ALL MATCH (s:Service {repo_id: $r})-[c:USES]->(d) RETURN d.name AS e, s.name AS s, c.origins AS o",
            {"r": repo_id},
        )
    )


def test_a_compose_change_re_owns_the_files_outside_its_batch(engine, repo_id, tmp_path):
    """A compose file's `build:` change alone (the route and client files
    unchanged) moves their Endpoints' CALLS and stores' USES to the Service
    that owns them now, as a fresh scan has them; deleting it drops them."""
    compose = write(tmp_path, "compose.yaml", "services:\n  shop:\n    build: ./services\n")
    write(tmp_path, "services/shop/routes.ts", ROUTED)
    write(tmp_path, "services/shop/db.py", "import redis\n")
    write(tmp_path, "services/admin/main.go", (
        'package main\n\nimport "net/http"\n\nfunc main() {\n\thttp.HandleFunc("GET /admin", h)\n}\n\n'
        "func h(w http.ResponseWriter, r *http.Request) {}\n"
    ))
    _scan(engine, repo_id, tmp_path)
    assert {owner for _e, owner, _o in _owners(engine, repo_id)} == {"shop"}

    for text in (
        "services:\n  shop:\n    build: ./services/shop\n  admin:\n    build: ./services/admin\n",
        "services:\n  shop:\n    build: ./services/shop\n  admin:\n    build: ./services\n",
        "services:\n  shop:\n    image: shop:1\n  admin:\n    build: ./services\n",
    ):
        write(tmp_path, "compose.yaml", text)
        index_paths(engine, repo_id, tmp_path, {compose})
        incremental_equals_fresh(engine, repo_id, tmp_path)
    assert {owner for _e, owner, _o in _owners(engine, repo_id)} == {"admin"}
    compose.unlink()
    remove_paths(engine, repo_id, tmp_path, {compose})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _owners(engine, repo_id) == []
