"""Same-named members of one file are separate nodes, told apart by `owner`
(docs/superpowers/specs/2026-10-08-graph-accuracy-design.md, owner identity),
against a live Neo4j: one node per class, `get_source` and `describe_node`
reach each one, calls land on the right one, and an incremental index equals a
fresh scan."""

import tempfile
import textwrap
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import describe_node, find_callers, get_source
from devgraph.registry.store import RepoRegistry
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, live_repo_id, snapshot_diff

MODELS = """\
    class Order:
        def save(self):
            return self.check()

        def check(self):
            return "order"


    class Invoice:
        def save(self):
            self.check()
            return "invoice"

        def check(self):
            return "invoice"


    def archive():
        order = Order()
        order.save()
        Invoice().save()
    """


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
def repo(engine):
    """A registered repository (root, registry, repo_id), deleted afterwards."""
    repo_id = live_repo_id("zz-owner")
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as regdir:
        root = Path(tmp)
        (root / ".git").mkdir()
        registry = RepoRegistry(Path(regdir) / "registry.db")
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        try:
            yield root, registry, repo_id
        finally:
            for each in (repo_id, f"{repo_id}_fresh"):
                engine.delete_repository(each)
            registry.close()


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def nodes(engine, repo_id, name):
    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $r, name: $n}) RETURN n.owner AS owner, n.start_line AS line",
        {"r": repo_id, "n": name},
    )
    return sorted((row["owner"], row["line"]) for row in rows)


def calls(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[:CALLS]->(b:Function) "
        "RETURN coalesce(a.owner, '') + '.' + a.name AS a, b.owner + '.' + b.name AS b",
        {"r": repo_id},
    )
    return sorted((row["a"], row["b"]) for row in rows)


def equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_two_classes_same_named_methods_are_two_nodes_with_their_own_source(engine, repo):
    root, registry, repo_id = repo
    write(root, "shop/models.py", MODELS)
    full_scan(engine, repo_id, root)

    assert nodes(engine, repo_id, "save") == [("Invoice", 10), ("Order", 2)]
    assert nodes(engine, repo_id, "check") == [("Invoice", 14), ("Order", 5)]
    contains = engine.run_cypher(
        "MATCH (c:Class {repo_id: $r})-[:CONTAINS]->(f:Function {name: 'save'}) RETURN c.name AS c, f.owner AS o",
        {"r": repo_id},
    )
    assert sorted((row["c"], row["o"]) for row in contains) == [("Invoice", "Invoice"), ("Order", "Order")]

    ambiguous = get_source(engine, registry, repo_id, "save", file="shop/models.py")
    assert ambiguous["status"] == "ambiguous"
    assert sorted(c["owner"] for c in ambiguous["candidates"]) == ["Invoice", "Order"]
    order = get_source(engine, registry, repo_id, "Order.save")
    assert order["owner"] == "Order" and "return self.check()" in order["source"]
    invoice = get_source(engine, registry, repo_id, "Invoice.save", file="shop/models.py")
    assert invoice["owner"] == "Invoice" and 'return "invoice"' in invoice["source"]

    described = describe_node(engine, repo_id, "Invoice.save")
    assert described["status"] == "found" and described["node"]["owner"] == "Invoice"
    assert [ref["owner"] for ref in described["outgoing"]["CALLS"]["results"]] == ["Invoice"]
    candidate = next(c for c in describe_node(engine, repo_id, "save")["candidates"] if c.get("owner") == "Order")
    again = describe_node(engine, repo_id, **candidate)
    assert again["status"] == "found" and again["node"]["properties"]["start_line"] == 2


def test_calls_reach_the_method_of_the_class_they_name(engine, repo):
    root, _registry, repo_id = repo
    write(root, "shop/models.py", MODELS)
    full_scan(engine, repo_id, root)

    assert calls(engine, repo_id) == [
        (".archive", "Invoice.save"),  # Invoice().save()
        (".archive", "Order.save"),  # order = Order(); order.save()
        ("Invoice.save", "Invoice.check"),  # self.check() in Invoice
        ("Order.save", "Order.check"),  # self.check() in Order
    ]
    # A lookup by the bare name still finds the callers of every `save`.
    assert [row["name"] for row in find_callers(engine, repo_id, "save")["results"]] == ["archive"]


def test_a_sibling_added_beside_a_same_named_method_equals_a_fresh_scan(engine, repo):
    root, _registry, repo_id = repo
    models = write(root, "shop/models.py", "class Order:\n    def save(self):\n        return 1\n")
    write(root, "shop/jobs.py", """\
        from shop.models import Order


        def run(order: Order):
            order.save()
        """)
    full_scan(engine, repo_id, root)
    equals_fresh(engine, repo_id, root)

    models.write_text(textwrap.dedent(MODELS))
    index_paths(engine, repo_id, root, {models})
    assert nodes(engine, repo_id, "save") == [("Invoice", 10), ("Order", 2)]
    equals_fresh(engine, repo_id, root)

    models.write_text("class Invoice:\n    def save(self):\n        return 2\n")
    index_paths(engine, repo_id, root, {models})
    assert nodes(engine, repo_id, "save") == [("Invoice", 2)]
    equals_fresh(engine, repo_id, root)


@pytest.mark.parametrize(
    ("rel", "source", "name", "owners"),
    [
        ("pkg/names.go", """\
            package pkg

            type Order struct{}
            type Invoice struct{}

            func (o Order) String() string { return "order" }

            func (i *Invoice) String() string { return "invoice" }

            func String() string { return Order{}.String() }
            """, "String", ["", "Invoice", "Order"]),
        ("src/shop.rs", """\
            pub struct Order;
            pub struct Invoice;

            impl Order {
                pub fn new() -> Order { Order }
            }

            impl Invoice {
                pub fn new() -> Invoice { Invoice }
            }
            """, "new", ["Invoice", "Order"]),
        ("src/Shop.java", """\
            package shop;

            class Order { void run() {} }

            class Invoice { void run() {} }
            """, "run", ["Invoice", "Order"]),
        ("src/Shop.kt", """\
            package shop

            interface Task {
                fun go()
            }

            class Job : Task {
                override fun go() {}
            }
            """, "go", ["Job", "Task"]),
        ("src/Shop.cs", """\
            namespace Shop;

            public class Order { public void Run() { } }

            public class Invoice { public void Run() { } }
            """, "Run", ["Invoice", "Order"]),
        ("src/shop.ts", """\
            export class Order { save() { return 1; } }
            export class Invoice { save = () => 2; }
            """, "save", ["Invoice", "Order"]),
        ("src/shop.cpp", """\
            class Order { void save() {} };
            class Invoice { void save() {} };
            """, "save", ["Invoice", "Order"]),
    ],
)
def test_every_language_keeps_same_named_members_of_one_file_apart(engine, repo, rel, source, name, owners):
    root, _registry, repo_id = repo
    write(root, rel, source)
    full_scan(engine, repo_id, root)
    assert [owner for owner, _line in nodes(engine, repo_id, name)] == owners
    equals_fresh(engine, repo_id, root)


def test_an_index_written_before_owners_keeps_its_top_level_nodes(engine, repo):
    """The upgrade rescan gives an old node without `owner` the owner "", so a
    top-level function keeps its node (and its incoming edges) rather than
    getting a second one beside it; a method's old node is replaced."""
    root, _registry, repo_id = repo
    app = write(root, "app.py", "def helper():\n    return 1\n\n\nclass Order:\n    def save(self):\n        return 2\n")
    engine.upsert_nodes([
        {"label": "Function", "repo_id": repo_id, "name": name, "properties": {"file": "app.py", "start_line": 1}}
        for name in ("helper", "save")
    ])
    engine.run_cypher(
        "MATCH (n:Function {repo_id: $r, file: 'app.py'}) REMOVE n.owner SET n.marker = true", {"r": repo_id}
    )
    index_paths(engine, repo_id, root, {app})
    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $r, file: 'app.py'}) RETURN n.name AS name, n.owner AS owner, n.marker AS kept",
        {"r": repo_id},
    )
    assert sorted((row["name"], row["owner"], row["kept"]) for row in rows) == [
        ("helper", "", True), ("save", "Order", None),
    ]
