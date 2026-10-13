"""Same-named members of one file are separate nodes, told apart by `owner`
(docs/superpowers/specs/2026-10-08-graph-accuracy-design.md, owner identity),
against a live Neo4j: one node per class, `get_source` and `describe_node`
reach each one, calls land on the right one, and an incremental index equals a
fresh scan."""

import tempfile
import textwrap
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from neo4j.exceptions import Neo4jError

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.graph.schema import FILE_SCOPED_LABELS
from devgraph.indexer.dispatch import catch_up, full_scan, index_outdated, index_paths
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


def _constraints(engine):
    rows = engine.run_cypher(
        "SHOW CONSTRAINTS YIELD name, properties WHERE name CONTAINS '_repo_name_file' RETURN name, properties"
    )
    return {row["name"]: tuple(row["properties"]) for row in rows}


def test_a_database_an_older_version_built_migrates_on_start_and_rescan(engine, repo):
    """The upgrade path from index format 10: the old (repo_id, name, file)
    constraints and owner-less nodes. Two processes starting at once (the
    agent and the MCP server) both run init_schema, from a state where a
    previous start dropped one old constraint and was cut short before
    creating its new one; then the automatic rescan (catch_up of an outdated
    index) gives every node its owner. The constraints are shared by the
    whole database, so they are put back whatever happens."""
    root, _registry, repo_id = repo
    write(root, "shop/models.py", "def helper():\n    return 1\n\n\nclass Order:\n    def save(self):\n        return 2\n")
    write(root, "shop/jobs.py", "from shop.models import helper\n\n\ndef run():\n    return helper()\n")
    full_scan(engine, repo_id, root)
    engine.run_cypher(
        "MATCH (n:Function|Class {repo_id: $r}) WHERE n.file IS NOT NULL REMOVE n.owner SET n.marker = true",
        {"r": repo_id},
    )
    engine.set_index_format(repo_id, 10)
    try:
        for label in FILE_SCOPED_LABELS:
            engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_name_file_owner IF EXISTS")
            try:
                engine.run_cypher(
                    f"CREATE CONSTRAINT {label.lower()}_repo_name_file IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE (n.repo_id, n.name, n.file) IS UNIQUE"
                )
            except Neo4jError as exc:  # another repository's same-file siblings
                pytest.skip(f"the old constraint can't be created over the shared database's data: {exc}")
        # A start cut short: one old constraint already dropped, its new one not created.
        engine.run_cypher("DROP CONSTRAINT function_repo_name_file IF EXISTS")
        assert "function_repo_name_file_owner" not in _constraints(engine)

        errors = []
        engines = [GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev") for _ in range(2)]

        def start(each):
            try:
                each.init_schema()
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=start, args=(each,)) for each in engines]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for each in engines:
            each.close()
        assert errors == []
        assert {name: props for name, props in _constraints(engine).items() if name.endswith(("_file", "_owner"))} == {
            f"{label.lower()}_repo_name_file_owner": ("repo_id", "name", "file", "owner") for label in FILE_SCOPED_LABELS
        }

        # The file gains a sibling `save` while the old index waits for its rescan.
        write(root, "shop/models.py", MODELS.replace("    def archive():", "    def helper():\n        return 1\n\n\n    def archive():"))
        assert index_outdated(engine, repo_id)
        catch_up(engine, repo_id, root, since=datetime.now(timezone.utc))
        assert not index_outdated(engine, repo_id)
        rows = engine.run_cypher(
            "MATCH (n:Function {repo_id: $r, file: 'shop/models.py'}) RETURN n.name AS name, n.owner AS owner, "
            "n.marker AS kept",
            {"r": repo_id},
        )
        assert sorted((row["name"], row["owner"], row["kept"]) for row in rows) == [
            ("archive", "", None), ("check", "Invoice", None), ("check", "Order", None),
            ("helper", "", True),  # a top-level function keeps its node
            ("save", "Invoice", None), ("save", "Order", None),  # the old `save` is replaced, one node per owner
        ]
        engine.run_cypher("MATCH (n:Function|Class {repo_id: $r}) REMOVE n.marker", {"r": repo_id})
        equals_fresh(engine, repo_id, root)
    finally:
        for label in FILE_SCOPED_LABELS:
            engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_name_file IF EXISTS")
        engine.init_schema()
