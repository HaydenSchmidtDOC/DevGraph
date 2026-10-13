"""C# CALLS and IMPORTS resolve through types and namespaces (the
extractor's tiers): a method of the enclosing class declared in the file
resolves here; a type `T` another file declares is any C# file whose node's
match keys (`mkeys`) hold one of the `Ns:T` the site's namespaces allow (its
enclosing namespaces, its usings, its project's `<Using>` items and `global
using`s, an alias's exact target), at package confidence; a typed receiver's
method also an extension method `Ns:+T` of those namespaces; another part of
a partial class its own `Ns:Class`; a library type links nothing, and an
untyped (or `dynamic`) receiver keeps a bare-name edge unless its method is
stoplisted. A file's IMPORTS are the classes it references, under the same
keys, and the extension methods its typed receivers' calls can reach; never
a `using`d namespace.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (C#).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.graph.schema import MAX_SCOPE_KEYS, split_pin
from devgraph.indexer.common import best_pin
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.resolver_config import ResolverConfig
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

#: Candidate files, each with the match keys of the methods it declares.
FILES = {
    "src/Models/User.cs": ("Shop.Domain:User",),
    "src/Legacy/User.cs": ("Legacy.Accounts:User",),
    "src/Extensions/UserExtensions.cs": ("Shop.Domain:+User", "Shop.Domain:UserExtensions"),
    "src/Utils/Formatting.cs": ("Shop.Text:Formatting",),
    "src/Services/UserService.cs": ("Shop.Domain.Services:UserService",),
    "src/Services/AuditLog.cs": ("Shop.Domain.Services:AuditLog",),
    "src/Orders/Order.cs": ("Shop.Sales:Order",),
    "src/Orders/Order.Pricing.cs": ("Shop.Sales:Order",),
    "src/App/Program.cs": ("Shop.Cli:Program",),
    "src/vendor/Lib/Formatting.cs": ("Shop.Text:Formatting",),
}


def _rows(source, file_path, rel_type="CALLS", config=None):
    result = extract_csharp_file(textwrap.dedent(source), file_path, "repo", config)
    return [r for r in result.relationships if r.rel_type == rel_type]


def reached(source, file_path, caller, callee, candidates=None, config=None):
    """{candidate path: confidence} a CALLS row of `caller` to `callee`
    links, or {"<name>": confidence} for a bare-name row."""
    candidates = FILES if candidates is None else candidates
    rows = [r for r in _rows(source, file_path, config=config) if (r.from_name, r.to_name) == (caller, callee)]
    if any(r.to_file is None for r in rows):
        return {"<name>": rows[0].properties["confidence"]}
    pins = {r.to_file: r.properties["confidence"] for r in rows}
    found = {}
    for path, keys in candidates.items():
        pin = best_pin(pins, path, file_path, keys)
        if pin is not None:
            found[path] = pins[pin]
    return found


def test_a_method_of_the_class_resolves_here_and_another_partial_parts_by_its_key():
    source = """
        namespace Shop.Sales;

        public partial class Order
        {
            public decimal Total() => Subtotal() + Round(Lines());
            private decimal Lines() => this.Count();
            private int Count() => 0;
        }

        public class Invoice
        {
            public void Print() => Header();
        }
    """
    path = "src/Orders/Order.cs"
    assert reached(source, path, "Total", "Lines") == {"src/Orders/Order.cs": "resolved"}
    assert reached(source, path, "Lines", "Count") == {"src/Orders/Order.cs": "resolved"}
    # Declared in another part of the partial class: any part (this file's
    # own part declares no Subtotal, so its pin links no node here).
    assert reached(source, path, "Total", "Subtotal") == {
        "src/Orders/Order.cs": "package", "src/Orders/Order.Pricing.cs": "package",
    }
    # Not partial, nothing declares it: a bare name.
    assert reached(source, path, "Print", "Header") == {"<name>": "name"}


def test_a_base_class_method_is_its_keys():
    source = """
        public class Child : Base
        {
            public void Run() { Help(); base.Stop(); }
        }
    """
    candidates = {"src/Base.cs": (".:Base",), "src/Child.cs": (".:Child",), "src/Other/Base.cs": ("Other:Base",)}
    assert reached(source, "src/Child.cs", "Run", "Help", candidates) == {"src/Base.cs": "package"}
    assert reached(source, "src/Child.cs", "Run", "Stop", candidates) == {"src/Base.cs": "package"}


def test_a_static_call_goes_to_the_types_keys_and_a_library_type_nowhere():
    source = """
        using Shop.Text;

        namespace Shop.Domain;

        public class User
        {
            public string Display() => Formatting.Title(Name) + Shop.Text.Formatting.Mask(Name);
            public bool IsValid() => !string.IsNullOrWhiteSpace(Name) && char.IsUpper(Name[0]);
            public void Log() { Console.WriteLine(Math.Round(1.5)); Assert.Equal(1, 1); DateTime.UtcNow.ToString(); }
        }
    """
    path = "src/Models/User.cs"
    # The vendored copy is left out (`^`).
    assert reached(source, path, "Display", "Title") == {"src/Utils/Formatting.cs": "package"}
    assert reached(source, path, "Display", "Mask") == {"src/Utils/Formatting.cs": "package"}
    for caller, callee in [("IsValid", "IsNullOrWhiteSpace"), ("IsValid", "IsUpper"), ("Log", "WriteLine"),
                           ("Log", "Round"), ("Log", "Equal"), ("Log", "ToString")]:
        assert reached(source, path, caller, callee) == {}, callee
    # Without the using, Formatting is not in scope.
    assert reached(source.replace("using Shop.Text;", ""), path, "Display", "Title") == {}


def test_typed_receivers_go_to_the_types_keys_and_their_extension_methods():
    source = """
        using Shop.Sales;
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
                foreach (var each in _users) { each.Display(); }
            }

            public int Count() => 0;
        }
    """
    path = "src/Services/UserService.cs"
    # User is Shop.Domain.User (an enclosing namespace); its extension methods
    # are those of the namespaces in scope (Shop.Domain's UserExtensions).
    user = {"src/Models/User.cs": "package", "src/Extensions/UserExtensions.cs": "package"}
    assert reached(source, path, "Register", "Display") == user
    assert reached(source, path, "Register", "Initials") == user
    assert reached(source, path, "Register", "Write") == {"src/Services/AuditLog.cs": "package"}
    assert reached(source, path, "Register", "Add") == {}  # List<User> is a library type
    assert reached(source, path, "Register", "Count") == {path: "resolved"}
    orders = {"src/Orders/Order.cs": "package", "src/Orders/Order.Pricing.cs": "package"}
    assert reached(source, path, "Register", "Total") == orders
    assert reached(source, path, "Register", "AddLine") == orders
    # The alias's exact type, a static call.
    assert reached(source, path, "Register", "Parse") == {"src/Legacy/User.cs": "package"}


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


def _keys(row):
    return set(split_pin(row.to_file).scope)


def test_imports_are_the_classes_a_file_references_under_their_keys():
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
    assert {(r.to_label, r.to_name, r.to_file.split("@")[0], r.properties["confidence"]) for r in rows} == {
        ("Class", name, "/.?.cs^", "package") for name in ("User", "Order", "Formatting", "Account", "Base", "IThing")
    }
    by_name = {}
    for row in rows:
        by_name.setdefault(row.to_name, []).append(_keys(row))
    # The alias's exact target, at each split (a type can be nested).
    assert by_name["Account"] == [{"Shop.Domain:Account", "Shop:Domain.Account", ".:Shop.Domain.Account"}]
    assert by_name["Formatting"] == [{"Shop.Text:Formatting", "Shop:Text.Formatting", ".:Shop.Text.Formatting"}]
    # `User` (Legacy.Accounts by the using) and `Shop.Domain.User` (qualified).
    users = sorted(by_name["User"], key=len)
    assert "Legacy.Accounts:User" in users[0] and "Shop.Domain:User" not in users[0]
    assert "Shop.Domain:User" in users[1] and "Legacy.Accounts:User" not in users[1]
    assert {"Shop.Mapping:Order", "Legacy.Accounts:Order", "Xunit:Order", "Shop:Order", ".:Order"} <= by_name["Order"][0]


def test_namespaces_and_global_usings_import_nothing():
    assert _rows("global using Shop.Text;\n", "src/App/GlobalUsings.cs", "IMPORTS") == []
    assert _rows("using Shop.Text;\nnamespace Shop.Cli;\n", "src/App/Empty.cs", "IMPORTS") == []


def test_a_typed_receivers_call_imports_the_extension_methods_it_can_reach():
    source = """
        namespace Shop.Domain.Services;

        public class UserService
        {
            public string Badge(User user) => user.Initials() + Count();
            public int Count() => 0;
            public void Again(UserService other) => other.Count();
        }
    """
    rows = [r for r in _rows(source, "src/Services/UserService.cs", "IMPORTS") if r.to_label == "Function"]
    # `other.Count()` names a method this file declares: no import.
    assert [(r.to_name, _keys(r)) for r in rows] == [
        ("Initials", {"Shop.Domain.Services:+User", "Shop.Domain:+User", "Shop:+User", ".:+User"}),
    ]


# --- match keys --------------------------------------------------------------------


def _mkeys(source, file_path="src/A.cs"):
    """(label, name) -> mkeys of each declaration; a member of a class is
    named with its owner (`Outer.Run`)."""
    result = extract_csharp_file(textwrap.dedent(source), file_path, "repo")
    return {
        (n.label, f"{n.properties['owner']}.{n.name}" if n.properties["owner"] else n.name): n.properties["mkeys"]
        for n in result.nodes if n.label != "Module"
    }


def test_classes_and_functions_carry_their_namespace_and_owner_as_match_keys():
    keys = _mkeys("""
        namespace Shop
        {
            namespace Domain.Models
            {
                public class Outer
                {
                    public Outer() { }
                    public void Run() { }
                    public class Inner { public void Run() { } }
                }

                public static class UserExtensions
                {
                    public static string Initials(this User? user) => "";
                }
            }

            public record Point(int X);
        }

        public struct Global { void Go() { } }
    """)
    assert keys == {
        ("Class", "Outer"): ["Shop.Domain.Models:Outer"],
        ("Function", "Outer.Outer"): ["Shop.Domain.Models:Outer"],
        # Two methods named Run in one file, in two classes: two nodes, each with its own owner's key.
        ("Function", "Outer.Run"): ["Shop.Domain.Models:Outer"],
        ("Function", "Outer.Inner.Run"): ["Shop.Domain.Models:Outer.Inner"],
        ("Class", "Outer.Inner"): ["Shop.Domain.Models:Outer.Inner"],
        ("Class", "UserExtensions"): ["Shop.Domain.Models:UserExtensions"],
        ("Function", "UserExtensions.Initials"): ["Shop.Domain.Models:+User", "Shop.Domain.Models:UserExtensions"],
        ("Class", "Point"): ["Shop:Point"],
        ("Class", "Global"): [".:Global"],
        ("Function", "Global.Go"): [".:Global"],
    }


def test_a_file_scoped_namespace_keys_every_declaration_and_one_name_in_two_namespaces_unions():
    assert _mkeys("namespace Shop.Sales;\npublic partial class Order { void Total() { } }\n") == {
        ("Class", "Order"): ["Shop.Sales:Order"], ("Function", "Order.Total"): ["Shop.Sales:Order"],
    }
    assert _mkeys("namespace A { class User { } }\nnamespace B { class User { } }\n") == {
        ("Class", "User"): ["A:User", "B:User"],
    }


# --- candidate namespaces ------------------------------------------------------------


def _call_keys(source, caller, callee, file_path="src/A.cs", config=None):
    rows = [r for r in _rows(source, file_path, config=config) if (r.from_name, r.to_name) == (caller, callee)]
    return set().union(*(_keys(r) for r in rows if r.to_file and "@" in r.to_file))


def test_a_sites_namespaces_are_its_enclosing_ones_and_its_blocks_usings():
    source = """
        using Top;

        namespace Shop
        {
            using Inside;

            namespace Domain
            {
                class Service { void Run() { Jobs.Go(); Domain.User.Make(); Outer.Inner.Make(); } }
                partial class Outer { void Run2() { Inner.Make(); } }  // Inner: in another part
            }
        }

        namespace Elsewhere
        {
            class Other { void Run() { Jobs.Go(); } }
        }
    """
    assert _call_keys(source, "Run", "Go") >= {"Shop.Domain:Jobs", "Shop:Jobs", ".:Jobs", "Top:Jobs", "Inside:Jobs",
                                               "Elsewhere:Jobs"}
    other = [r for r in _rows(source, "src/A.cs") if r.to_name == "Go"]
    # Inside's using is the Shop block's alone, and relative to Shop too.
    assert {frozenset(_keys(r)) for r in other} == {
        frozenset({"Shop.Domain:Jobs", "Shop:Jobs", ".:Jobs", "Top:Jobs", "Inside:Jobs", "Shop.Inside:Jobs",
                   "Shop.Domain:Service.Jobs"}),
        frozenset({"Elsewhere:Jobs", ".:Jobs", "Top:Jobs", "Elsewhere:Other.Jobs"}),
    }
    # A qualified name, relative to the global namespace and each enclosing one.
    assert {"Domain:User", "Shop.Domain.Domain:User", "Shop.Domain:User"} <= _call_keys(source, "Run", "Make")
    # A nested type, by its enclosing class or written through it.
    assert "Shop.Domain:Outer.Inner" in _call_keys(source, "Run2", "Make")
    assert "Shop.Domain:Outer.Inner" in _call_keys(source, "Run", "Make")


def test_aliases_name_their_exact_target_and_a_using_static_its_type():
    source = """
        using LA = Legacy.Accounts;
        using J = Shop.Jobs;
        using static Shop.Text.Formatting;

        namespace Shop.App;

        class Program { void Main() { LA.User.Parse(); J.Run(); Title(); } }
    """
    assert _call_keys(source, "Main", "Parse") == {"Legacy.Accounts:User", "Legacy:Accounts.User",
                                                    ".:Legacy.Accounts.User"}
    assert _call_keys(source, "Main", "Run") == {"Shop:Jobs", ".:Shop.Jobs"}
    assert _call_keys(source, "Main", "Title") == {"Shop.Text:Formatting", "Shop:Text.Formatting",
                                                    ".:Shop.Text.Formatting"}


def test_a_projects_usings_and_global_usings_are_in_scope(tmp_path):
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n  <ItemGroup>\n    <Using Include="Shop.Models" />\n'
        '    <Using Include="Shop.Jobs" Static="true" />\n  </ItemGroup>\n</Project>\n'
    )
    (tmp_path / "App" / "GlobalUsings.cs").write_text("global using Shop.Text;\nglobal using L = Legacy.User;\n")
    config = ResolverConfig(tmp_path)
    source = "namespace App;\nclass Program { void Main() { Formatting.Title(); User.Make(); L.Parse(); Run(); } }\n"
    assert {"Shop.Text:Formatting", "Shop.Models:Formatting"} <= _call_keys(source, "Main", "Title", "App/P.cs", config)
    assert _call_keys(source, "Main", "Parse", "App/P.cs", config) == {"Legacy:User", ".:Legacy.User"}
    assert _call_keys(source, "Main", "Run", "App/P.cs", config) == {"Shop:Jobs", ".:Shop.Jobs"}
    # A file outside every project has none of them.
    assert "Shop.Text:Formatting" not in _call_keys(source, "Main", "Title", "Scripts/P.cs", config)
    assert reached(source, "Scripts/P.cs", "Main", "Run", {}, config) == {"<name>": "name"}


def test_more_candidate_keys_than_the_cap_fall_back_to_the_class_file_each_group_on_its_own():
    usings = "".join(f"using N{i};\n" for i in range(MAX_SCOPE_KEYS))
    source = usings + "namespace A;\nclass C { void Run(User u) { Jobs.Go(); u.Save(); } }\n"
    rows = _rows(source, "src/C.cs")
    # Over the cap: the class file, as before match keys (never any C# file).
    assert [r.to_file for r in rows if r.to_name == "Go"] == ["/Jobs.cs^"]
    assert sorted(r.to_file for r in rows if r.to_name == "Save") == ["/User.cs^", "/UserExtensions.cs^"]
    # The extension import needs its keys: none over the cap.
    assert [r.to_name for r in _rows(source, "src/C.cs", "IMPORTS") if r.to_label == "Function"] == []
    # Half as many namespaces: two pins, each under the cap on its own.
    usings = "".join(f"using N{i};\n" for i in range(MAX_SCOPE_KEYS // 2 + 2))
    rows = _rows(usings + "namespace A;\nclass C { void Run(User u) { u.Save(); } }\n", "src/C.cs")
    pins = sorted(r.to_file for r in rows if r.to_name == "Save")
    assert len(pins) == 2 and all(pin.startswith("/.?.cs^@") for pin in pins)
    assert {len(split_pin(pin).scope) for pin in pins} == {MAX_SCOPE_KEYS // 2 + 4, MAX_SCOPE_KEYS // 2 + 5}


def test_system_and_microsoft_usings_are_no_candidates():
    source = """
        using System;
        using System.Linq;
        using Microsoft.Extensions.Logging;
        using Shop.Models;

        namespace App;

        class C { void Run() { Jobs.Go(); } }
    """
    assert _call_keys(source, "Run", "Go") == {"App:Jobs", ".:Jobs", "Shop.Models:Jobs", "App:C.Jobs"}


def test_a_nested_type_through_a_using_static_an_alias_or_a_qualified_name():
    source = """
        using static Shop.Outer.Inner;
        using IA = Shop.Outer.Inner;

        namespace App;

        class C { void Run() { Go(); IA.Make(); Shop.Outer.Inner.Stop(); global::Shop.Outer.Inner.Halt(); } }
    """
    for callee in ("Go", "Make", "Stop", "Halt"):
        assert "Shop:Outer.Inner" in _call_keys(source, "Run", callee), callee
    # `global::` is the global namespace's alone.
    assert _call_keys(source, "Run", "Halt") == {"Shop.Outer:Inner", "Shop:Outer.Inner", ".:Shop.Outer.Inner"}
    assert "App.Shop.Outer:Inner" in _call_keys(source, "Run", "Stop")


def test_a_library_type_is_the_leftmost_name_alone_and_a_generic_type_its_simple_name():
    source = """
        namespace App;

        class C { void Run() { Contoso.Directory.UserStore.Get(); Directory.Exists(); Svc<int>.Make(); } }
    """
    assert "Contoso.Directory:UserStore" in _call_keys(source, "Run", "Get")
    assert reached(source, "src/C.cs", "Run", "Exists") == {}
    assert "App:Svc" in _call_keys(source, "Run", "Make")


def test_a_local_function_called_above_its_declaration_links_nothing():
    source = "class C { void Run() { Helper(); void Helper() { } } }\n"
    assert [r for r in _rows(source, "src/C.cs") if r.to_name == "Helper"] == []


def test_an_alias_is_its_namespace_blocks_alone():
    source = """
        namespace A { using J = Shop.Jobs; class C { void Run() { J.Go(); } } }
        namespace B { class D { void Run() { J.Go(); } } }
    """
    keys = [_keys(r) for r in _rows(source, "src/C.cs") if r.to_name == "Go"]
    assert {"Shop:Jobs", "A.Shop:Jobs"} <= keys[0] or {"Shop:Jobs", "A.Shop:Jobs"} <= keys[1]
    assert any("B:J" in each for each in keys)  # in B, J is no alias


def test_verbatim_identifiers_and_a_constraints_extension_key():
    keys = _mkeys("""
        namespace @Shop.Models
        {
            public class @event { void @Run() { } }

            public static class RepoExtensions
            {
                public static void Touch<T>(this T repo) where T : IRepo, new() { }
            }
        }
    """)
    assert keys[("Class", "@event")] == ["Shop.Models:event"]
    assert keys[("Function", "RepoExtensions.Touch")] == ["Shop.Models:+IRepo", "Shop.Models:RepoExtensions"]


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


def test_a_namespace_edit_moves_calls_and_imports_between_same_named_classes(engine, repo_id, tmp_path):
    """Two `User` classes in two namespaces: a caller links the one its
    usings reach. Editing a target's namespace changes its match keys, so
    the edges into it are retracted and relinked; editing the caller's
    using moves its own. Every step equals a fresh scan."""
    user = "namespace {}\n{{\n    public class User {{ public void Display() {{ }} }}\n}}\n"
    models = _write(tmp_path, "Models/User.cs", user.format("Shop.Domain"))
    legacy = _write(tmp_path, "Legacy/User.cs", user.format("Legacy.Accounts"))
    program = _write(tmp_path, "App/Program.cs",
                     "using Shop.Domain;\n\nnamespace App;\n\nclass Program { static void Main(User u) { u.Display(); } }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)

    def targets():
        calls = {e[2] for e in _edges(engine, repo_id, "CALLS") if e[1] == "Main"}
        imports = {e[2] for e in _edges(engine, repo_id, "IMPORTS") if e[0] == "App/Program.cs"}
        return calls, imports

    assert targets() == ({"Models/User.cs"}, {"Models/User.cs"})
    _equals_fresh(engine, repo_id, tmp_path)

    legacy.write_text(user.format("Shop.Domain"))  # now both are Shop.Domain.User
    index_paths(engine, repo_id, tmp_path, {legacy})
    assert targets() == ({"Models/User.cs", "Legacy/User.cs"}, {"Models/User.cs", "Legacy/User.cs"})
    _equals_fresh(engine, repo_id, tmp_path)

    models.write_text(user.format("Other"))
    index_paths(engine, repo_id, tmp_path, {models})
    assert targets() == ({"Legacy/User.cs"}, {"Legacy/User.cs"})
    _equals_fresh(engine, repo_id, tmp_path)

    program.write_text(program.read_text().replace("using Shop.Domain;", "using Other;"))
    index_paths(engine, repo_id, tmp_path, {program})
    assert targets() == ({"Models/User.cs"}, {"Models/User.cs"})
    _equals_fresh(engine, repo_id, tmp_path)


def test_a_library_root_the_files_own_namespace_is_under_stays_a_candidate():
    source = """
        namespace Microsoft.AspNetCore.Http
        {
            using Microsoft.AspNetCore.Routing;
            using System.Text;

            class Endpoint { void Run(RouteData d) { d.Match(); } }
        }
    """
    keys = _call_keys(source, "Run", "Match")
    assert {"Microsoft.AspNetCore.Routing:RouteData", "Microsoft.AspNetCore.Http.Microsoft.AspNetCore.Routing:RouteData",
            "Microsoft.AspNetCore.Http:RouteData"} <= keys
    assert not any(key.startswith("System.") for key in keys)  # another library root still is the framework's


def test_a_project_using_under_the_files_own_library_root_stays_a_candidate(tmp_path):
    (tmp_path / "Http").mkdir()
    (tmp_path / "Http" / "Http.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><Using Include="Microsoft.AspNetCore.Routing" />'
        '<Using Include="Microsoft.Extensions.Logging" /></ItemGroup></Project>'
    )
    config = ResolverConfig(tmp_path)
    source = "namespace Microsoft.AspNetCore.Http;\nclass E { void Run() { RouteData.Make(); } }\n"
    keys = _call_keys(source, "Run", "Make", "Http/E.cs", config)
    assert "Microsoft.AspNetCore.Routing:RouteData" in keys and "Microsoft.Extensions.Logging:RouteData" in keys
    elsewhere = _call_keys(source.replace("Microsoft.AspNetCore.Http", "Shop"), "Run", "Make", "Http/E.cs", config)
    assert not any(key.startswith("Microsoft.") for key in elsewhere)


def test_an_extension_on_a_constrained_type_parameter_skips_constraints_naming_no_type():
    keys = _mkeys("""
        namespace Shop
        {
            public static class RepoExtensions
            {
                public static void Touch<T>(this T repo) where T : class, IRepo { }
                public static void Poke<T>(this T value) where T : struct { }
            }
        }
    """)
    assert keys[("Function", "RepoExtensions.Touch")] == ["Shop:+IRepo", "Shop:RepoExtensions"]
    assert keys[("Function", "RepoExtensions.Poke")] == ["Shop:+T", "Shop:RepoExtensions"]


def test_global_qualified_calls_and_block_usings_stay_absolute():
    source = """
        namespace Shop
        {
            using global::Tools.Text;
            using Models;

            class C { void Run() { global::Util.Go(); Formatting.Trim(); } }
        }
    """
    assert _call_keys(source, "Run", "Go") == {".:Util"}
    keys = _call_keys(source, "Run", "Trim")
    assert "Tools.Text:Formatting" in keys and "Shop.Tools.Text:Formatting" not in keys
    assert {"Models:Formatting", "Shop.Models:Formatting"} <= keys  # a plain block using is relative too
