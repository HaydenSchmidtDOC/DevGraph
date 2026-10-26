"""Routes whose handlers share a name, against a live Neo4j (audit 9 item 2):
every route's handler stub is one file-less Function per name, so three
Spring controllers' `list` methods share one stub. Each route resolved to its
own `list`, so the stub stands for them all and is folded away:
trace_request_flow lists the traced route's `list` once, with its file and
owner, and describe_node and search_component show only the real methods. A
stub some route resolved to nothing stays visible as an unresolved handler.
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


def _controller(name):
    return f"""\
        package shop;

        @RestController
        @RequestMapping("/{name}")
        public class {name.title()}Controller {{
          @GetMapping public void list() {{ }}
          @PostMapping public void create() {{ }}
        }}
        """


CONTROLLERS = ("users", "orders", "items")
FILES = {
    **{f"src/main/java/shop/{n.title()}Controller.java": _controller(n) for n in CONTROLLERS},
    # A route whose `create` resolves to no function: its stub stays unresolved.
    "web/app.js": """\
        import express from 'express';
        const app = express();
        app.post('/legacy', create);
        """,
}


@pytest.fixture(scope="module")
def repo_id(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("shared-handlers")
    repo_id = f"zz-shared-handlers-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def test_the_fixture_has_one_shared_stub_per_name(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $r}) WHERE n.file IS NULL "
        "RETURN n.name AS name, COUNT { (n)<-[:IMPLEMENTS]-(:Endpoint) } AS routes ORDER BY name",
        {"r": repo_id},
    )
    assert [(row["name"], row["routes"]) for row in rows] == [("create", 4), ("list", 3)]


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_trace_request_flow_lists_the_traced_routes_handler_once(engine, repo_id, controller):
    endpoint = f"GET /{controller}"
    result = tools.trace_request_flow(engine, repo_id, endpoint)
    handlers = [c for c in result["components"] if c["name"] == "list"]
    owner = f"{controller.title()}Controller"
    assert handlers == [{
        "name": "list", "labels": ["Function"], "repo_id": repo_id,
        "file": f"src/main/java/shop/{owner}.java", "owner": owner,
    }]
    assert [e for e in result["edges"] if e["type"] == "IMPLEMENTS"] == [
        {"type": "IMPLEMENTS", "from": endpoint, "to": "list"}
    ]


def test_describe_node_lists_only_the_real_handlers(engine, repo_id):
    result = tools.describe_node(engine, repo_id, "list")
    assert result["status"] == "ambiguous"
    assert sorted((c.get("file"), c.get("unresolved_handler")) for c in result["candidates"]) == [
        (f"src/main/java/shop/{n.title()}Controller.java", None) for n in sorted(CONTROLLERS)
    ]


def test_an_endpoint_describes_its_own_handler_only(engine, repo_id):
    implements = tools.describe_node(engine, repo_id, "GET /orders")["outgoing"]["IMPLEMENTS"]
    assert implements["count"] == 1
    assert [(r["name"], r.get("file")) for r in implements["results"]] == [
        ("list", "src/main/java/shop/OrdersController.java")
    ]


def test_search_component_has_no_file_less_handler(engine, repo_id):
    found = [r for r in tools.search_component(engine, repo_id, "list")["results"] if r["name"] == "list"]
    assert sorted(r["file"] for r in found) == [
        f"src/main/java/shop/{n.title()}Controller.java" for n in sorted(CONTROLLERS)
    ]


def test_a_stub_some_route_left_unresolved_stays_visible(engine, repo_id):
    result = tools.describe_node(engine, repo_id, "create")
    unresolved = [c for c in result["candidates"] if c.get("unresolved_handler")]
    assert len(result["candidates"]) == 4 and [c.get("file") for c in unresolved] == [None]
    trace = tools.trace_request_flow(engine, repo_id, "POST /legacy")
    assert [(c["name"], c["file"]) for c in trace["components"] if c["name"] == "create"] == [("create", None)]
    # The stub's other routes each still trace to their own method.
    users = tools.trace_request_flow(engine, repo_id, "POST /users")
    assert [(c["name"], c["file"]) for c in users["components"] if c["name"] == "create"] == [
        ("create", "src/main/java/shop/UsersController.java")
    ]


@pytest.mark.parametrize("tool", [tools.find_callers, tools.impact_analysis])
def test_ambiguous_targets_leave_the_folded_stub_out(engine, repo_id, tool):
    ambiguous = tool(engine, repo_id, "list")["ambiguous_targets"]
    assert ambiguous["count"] == 3
    assert sorted(r["file"] for r in ambiguous["results"]) == [
        f"src/main/java/shop/{n.title()}Controller.java" for n in sorted(CONTROLLERS)
    ]
