"""C# CALLS and IMPORTS resolve through types (the extractor's tiers): a
method of the enclosing class declared in the file resolves here; a type `T`
another file declares is its class file `/T.cs` by suffix (package
confidence: a class need not live in a file named after it, and two
namespaces can each hold a `User.cs`), a typed receiver's method also its
extension class file `/TExtensions.cs`; a library type links nothing, and an
untyped (or `dynamic`) receiver keeps a bare-name edge unless its method is
stoplisted. A file's IMPORTS are the classes it references, wherever in a C#
file they are declared, never a `using`d namespace.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (C#).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.common import best_pin
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.dispatch import full_scan, index_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

FILES = [
    "src/Models/User.cs", "src/Legacy/User.cs", "src/Extensions/UserExtensions.cs", "src/Utils/Formatting.cs",
    "src/Services/UserService.cs", "src/Orders/Order.cs", "src/Orders/Order.Pricing.cs", "src/App/Program.cs",
    "src/vendor/Lib/Formatting.cs",
]


def _rows(source, file_path, rel_type="CALLS"):
    result = extract_csharp_file(textwrap.dedent(source), file_path, "repo")
    return [r for r in result.relationships if r.rel_type == rel_type]


def reached(source, file_path, caller, callee, candidates=FILES):
    """{candidate path: confidence} a CALLS row of `caller` to `callee`
    links, or {"<name>": confidence} for a bare-name row."""
    rows = [r for r in _rows(source, file_path) if (r.from_name, r.to_name) == (caller, callee)]
    if any(r.to_file is None for r in rows):
        return {"<name>": rows[0].properties["confidence"]}
    pins = {r.to_file: r.properties["confidence"] for r in rows}
    found = {}
    for path in candidates:
        pin = best_pin(pins, path, file_path)
        if pin is not None:
            found[path] = pins[pin]
    return found


def test_a_method_of_the_class_resolves_here_and_a_partial_ones_by_name():
    source = """
        namespace Shop.Sales;

        public partial class Order
        {
            public decimal Total() => Subtotal() + Round(Lines());
            private decimal Lines() => this.Count();
            private int Count() => 0;
        }
    """
    path = "src/Orders/Order.cs"
    assert reached(source, path, "Total", "Lines") == {"src/Orders/Order.cs": "resolved"}
    assert reached(source, path, "Lines", "Count") == {"src/Orders/Order.cs": "resolved"}
    # Declared in another part of the partial class, or inherited: a bare name.
    assert reached(source, path, "Total", "Subtotal") == {"<name>": "name"}


def test_a_base_class_method_is_its_class_file():
    source = """
        public class Child : Base
        {
            public void Run() { Help(); base.Stop(); }
        }
    """
    assert reached(source, "src/Child.cs", "Run", "Help", ["src/Base.cs", "src/Child.cs"]) == {
        "src/Base.cs": "package"
    }
    assert reached(source, "src/Child.cs", "Run", "Stop", ["src/Base.cs", "src/Child.cs"]) == {
        "src/Base.cs": "package"
    }


def test_a_static_call_goes_to_the_types_class_file_and_a_library_type_nowhere():
    source = """
        namespace Shop.Domain;

        public class User
        {
            public string Display() => Formatting.Title(Name) + Shop.Text.Formatting.Mask(Name);
            public bool IsValid() => !string.IsNullOrWhiteSpace(Name) && char.IsUpper(Name[0]);
            public void Log() { Console.WriteLine(Math.Round(1.5)); Assert.Equal(1, 1); }
        }
    """
    path = "src/Models/User.cs"
    assert reached(source, path, "Display", "Title") == {"src/Utils/Formatting.cs": "package"}
    assert reached(source, path, "Display", "Mask") == {"src/Utils/Formatting.cs": "package"}
    for caller, callee in [("IsValid", "IsNullOrWhiteSpace"), ("IsValid", "IsUpper"), ("Log", "WriteLine"),
                           ("Log", "Round"), ("Log", "Equal")]:
        assert reached(source, path, caller, callee) == {}, callee


def test_typed_receivers_go_to_the_types_class_and_extension_files():
    source = """
        using LegacyUser = Legacy.Accounts.User;

        namespace Shop.Domain.Services;

        public class UserService
        {
            private readonly List<User> _users = new();
            private readonly AuditLog _log;

            public void Register(User user)
            {
                user.Display();
                user.Initials();
                _log.Write("x");
                _users.Add(user);
                var service = new UserService();
                service.Count();
                Order order = Make();
                order.Total();
                new Order().AddLine(1);
                LegacyUser.Parse("x");
            }

            public int Count() => 0;
        }
    """
    path = "src/Services/UserService.cs"
    users = {"src/Models/User.cs": "package", "src/Legacy/User.cs": "package"}
    # The files the pins match (whichever of them declares the method gets the edge).
    with_extensions = {**users, "src/Extensions/UserExtensions.cs": "package"}
    assert reached(source, path, "Register", "Display") == with_extensions
    assert reached(source, path, "Register", "Initials") == with_extensions
    assert reached(source, path, "Register", "Write", ["src/Services/AuditLog.cs"]) == {
        "src/Services/AuditLog.cs": "package"
    }
    assert reached(source, path, "Register", "Add") == {}  # List<User> is a library type
    assert reached(source, path, "Register", "Count") == {path: "resolved"}
    assert reached(source, path, "Register", "Total") == {"src/Orders/Order.cs": "package"}
    assert reached(source, path, "Register", "AddLine") == {"src/Orders/Order.cs": "package"}
    assert reached(source, path, "Register", "Parse") == users  # the alias's type, a static call


def test_an_untyped_or_dynamic_receiver_keeps_a_bare_row_off_its_caller():
    source = """
        public static class DynamicBridge
        {
            public static string Show(dynamic account, string s) => account.Display() + s.Trim() + x?.Run();
        }
    """
    rows = [r for r in _rows(source, "src/Interop/DynamicBridge.cs") if r.from_name == "Show"]
    assert {(r.to_name, r.to_file, r.properties["confidence"], r.no_self) for r in rows} == {
        ("Display", None, "name", True), ("Run", None, "name", True),
    }


def test_imports_are_the_classes_a_file_references():
    source = """
        using Legacy.Accounts;
        using Xunit;
        using static Shop.Text.Formatting;
        using Alias = Shop.Domain.Account;

        namespace Shop.Mapping;

        public static class UserMapper
        {
            public static Shop.Domain.User ToDomain(User legacy, List<Order> orders) => new Shop.Domain.User(Mask(""));
            public static Alias Describe(UserMapper m, string s, T t) => null;
        }

        public class Pair<T> : Base<T>, IThing { }
    """
    rows = _rows(source, "src/Mapping/Mappers.cs", "IMPORTS")
    assert {(r.to_label, r.to_name, r.to_file, r.properties["confidence"]) for r in rows} == {
        ("Class", name, "/.?.cs^", "package") for name in ("User", "Order", "Formatting", "Account", "Base", "IThing")
    }


def test_namespaces_and_global_usings_import_nothing():
    assert _rows("global using Shop.Text;\n", "src/App/GlobalUsings.cs", "IMPORTS") == []
    assert _rows("using Shop.Text;\nnamespace Shop.Cli;\n", "src/App/Empty.cs", "IMPORTS") == []


# --- live --------------------------------------------------------------------------


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
    repo = f"zz-cscalls-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _edges(engine, repo_id, rel_type):
    rows = engine.run_cypher(
        f"MATCH (a {{repo_id: $r}})-[x:{rel_type}]->(b {{repo_id: $r}}) "
        "RETURN coalesce(a.file, a.name) AS a, a.name AS an, coalesce(b.file, b.name) AS b, b.name AS bn, "
        "x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((r["a"], r["an"], r["b"], r["bn"], r["c"]) for r in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_a_class_moving_between_files_keeps_its_importers_and_callers(engine, repo_id, tmp_path):
    """IMPORTS follow a class into any C# file; CALLS follow it into its
    class file. Every step equals a fresh scan."""
    program = _write(tmp_path, "App/Program.cs",
                     "class Program { static void Main() { Orders.Make(); } }\n")
    _write(tmp_path, "Core/Orders.cs", "public partial class Orders { public static void Make() {} }\n")
    pricing = _write(tmp_path, "Core/Pricing.cs", "public class Pricing { }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert ("App/Program.cs", "App/Program.cs", "Core/Orders.cs", "Orders", "package") in _edges(
        engine, repo_id, "IMPORTS")
    assert ("App/Program.cs", "Main", "Core/Orders.cs", "Make", "package") in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    # A second part of the partial class, in a file not named after it.
    pricing.write_text("public partial class Orders { public static void Price() {} }\n")
    program.write_text("class Program { static void Main() { Orders.Make(); Orders.Price(); } }\n")
    index_paths(engine, repo_id, tmp_path, {pricing, program})
    imports = _edges(engine, repo_id, "IMPORTS")
    assert {e[2] for e in imports if e[0] == "App/Program.cs"} == {"Core/Orders.cs", "Core/Pricing.cs"}
    _equals_fresh(engine, repo_id, tmp_path)
