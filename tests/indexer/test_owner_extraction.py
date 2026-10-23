"""Every extractor gives a Class or Function its `owner` (the types it is
declared in), so two same-named members of one file are two nodes, and pins
the ends of its own edges to them (docs/superpowers/specs/2026-10-08-graph-accuracy-design.md,
owner identity)."""

import textwrap

import pytest

from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    heir_keys,
    member_owner,
    own_edges,
    owned_pin,
    split_owned_pin,
)
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file

# (extractor, path, source): classes A and B each with `save` calling a
# same-class helper `load`, and B nesting a class Inner with its own `save`
# where the language nests types.
CASES = {
    "python": (extract_python_file, "app/models.py", """\
        class A:
            def save(self):
                return self.load()

            def load(self):
                return 1


        class B:
            def save(self):
                return self.load()

            def load(self):
                return 2

            class Inner:
                def save(self):
                    return 3
        """),
    "ts": (extract_js_file, "app/models.ts", """\
        class A { save() { return this.load(); } load() { return 1; } }
        class B {
          save = () => this.load();
          load() { return 2; }
        }
        """),
    "java": (extract_java_file, "app/Models.java", """\
        package app;
        class A { int save() { return load(); } int load() { return 1; } }
        class B {
            int save() { return this.load(); }
            int load() { return 2; }
            static class Inner { int save() { return 3; } }
        }
        """),
    "kotlin": (extract_kotlin_file, "app/Models.kt", """\
        package app

        class A {
            fun save(): Int { return this.load() }
            fun load(): Int { return 1 }
        }

        class B {
            fun save(): Int { return this.load() }
            fun load(): Int { return 2 }
            class Inner {
                fun save(): Int { return 3 }
            }
        }
        """),
    "csharp": (extract_csharp_file, "app/Models.cs", """\
        namespace App;
        public class A { public int Save() { return Load(); } public int Load() { return 1; } }
        public class B
        {
            public int Save() { return this.Load(); }
            public int Load() { return 2; }
            public class Inner { public int Save() { return 3; } }
        }
        """),
    "go": (extract_go_file, "app/models.go", """\
        package app

        type A struct{}
        type B struct{}

        func (a A) Save() int { return a.Load() }
        func (a A) Load() int { return 1 }
        func (b *B) Save() int { return b.Load() }
        func (b *B) Load() int { return 2 }
        """),
    "rust": (extract_rust_file, "src/models.rs", """\
        pub struct A;
        pub struct B;
        impl A {
            pub fn save(&self) -> i32 { self.load() }
            pub fn load(&self) -> i32 { 1 }
        }
        impl B {
            pub fn save(&self) -> i32 { self.load() }
            pub fn load(&self) -> i32 { 2 }
        }
        """),
    "cpp": (extract_cpp_file, "app/models.cpp", """\
        class A { public: int save() { return load(); } int load() { return 1; } };
        class B { public: int save(); int load() { return 2; } };
        int B::save() { return load(); }
        """),
}


def _extract(lang):
    extract, path, source = CASES[lang]
    return extract(textwrap.dedent(source), path, "repo"), path


@pytest.mark.parametrize("lang", sorted(CASES))
def test_same_named_members_of_two_classes_are_two_owned_nodes(lang):
    result, path = _extract(lang)
    save = "Save" if lang in ("csharp", "go") else "save"
    owners = sorted(n.properties["owner"] for n in result.nodes if n.label == "Function" and n.name == save)
    assert owners[:2] == ["A", "B"]
    if lang in ("python", "java", "kotlin", "csharp"):
        assert owners == ["A", "B", "B.Inner"]
    assert all("owner" in n.properties for n in result.nodes if n.label in ("Class", "Function"))
    contains = {
        (r.from_name, r.to_owner) for r in result.relationships
        if r.rel_type == "CONTAINS" and r.to_name == save and r.from_file == path
    }
    assert {("A", "A"), ("B", "B")} <= contains


@pytest.mark.parametrize("lang", sorted(CASES))
def test_a_members_calls_come_from_its_own_node(lang):
    result, _path = _extract(lang)
    save = "Save" if lang in ("csharp", "go") else "save"
    sources = {r.from_owner for r in result.relationships if r.rel_type == "CALLS" and r.from_name == save}
    assert {"A", "B"} <= sources


@pytest.mark.parametrize("lang", ["python", "ts", "java", "kotlin", "csharp", "go", "rust"])
def test_a_call_into_a_class_of_the_file_names_its_owner(lang):
    """`self.load()` (`this.load()`, `b.Load()` ...) in B links B's `load`,
    not A's."""
    result, path = _extract(lang)
    save, load = ("Save", "Load") if lang in ("csharp", "go") else ("save", "load")
    targets = {
        (r.from_owner, r.to_owner) for r in result.relationships
        if r.rel_type == "CALLS" and r.from_name == save and r.to_name == load and r.to_file == path
    }
    assert targets == {("A", "A"), ("B", "B")}


def test_member_owner_follows_classes_and_functions():
    assert member_owner("Module", "app.py", None) == ""
    assert member_owner("Class", "A", "") == "A"
    assert member_owner("Class", "Inner", "A") == "A.Inner"
    assert member_owner("Function", "save", "A") == "A.save"
    assert member_owner("Function", "main", "") == "main"


def test_owned_pins_round_trip():
    assert split_owned_pin(owned_pin("a.py", "A.Inner")) == ("a.py", "A.Inner")
    assert split_owned_pin(owned_pin("a.py", "")) == ("a.py", "")
    assert owned_pin("a.py", None) == "a.py"
    assert split_owned_pin("pkg/") == ("pkg/", None)


def test_own_edges_fills_in_an_owner_only_the_file_decides():
    result = ExtractionResult(
        nodes=[
            GraphNode("Class", "r", "A", {"file": "a.py", "owner": ""}),
            GraphNode("Function", "r", "go", {"file": "a.py", "owner": "A"}),
            GraphNode("Function", "r", "save", {"file": "a.py", "owner": "A"}),
            GraphNode("Function", "r", "save", {"file": "a.py", "owner": "B"}),
        ],
        relationships=[
            GraphRelationship("Function", "go", "CALLS", "Function", "x", "r"),
            GraphRelationship("Function", "save", "CALLS", "Function", "x", "r"),
            GraphRelationship("Class", "A", "CONTAINS", "Function", "go", "r", to_file="a.py"),
        ],
    )
    go, save, contains = own_edges(result, "a.py").relationships
    assert (go.from_file, go.from_owner) == ("a.py", "A")
    assert (save.from_file, save.from_owner) == ("a.py", None)  # two `save`s: the extractor says which
    assert (contains.from_owner, contains.to_owner) == ("", "A")


def test_nested_functions_are_owned_by_their_enclosing_functions():
    result, _path = extract_python_file(textwrap.dedent("""\
        def save():
            def helper():
                return 0
            return helper()


        class A:
            def save(self):
                def helper():
                    return 1
                return helper()

            def helper(self):
                return 2
        """), "app/m.py", "repo"), None
    owners = sorted((n.name, n.properties["owner"]) for n in result.nodes if n.label == "Function")
    assert owners == [("helper", "A"), ("helper", "A.save"), ("helper", "save"), ("save", ""), ("save", "A")]


def test_kotlin_extension_functions_are_owned_by_their_receiver():
    result = extract_kotlin_file("package k\n\nclass A\nclass B\n\nfun A.save() {}\nfun B?.save() {}\nfun save() {}\n",
                                 "k/Ext.kt", "repo")
    assert sorted(n.properties["owner"] for n in result.nodes if n.name == "save") == ["", "A", "B"]


def test_cpp_specialisation_qualifier_and_nested_class_owners():
    result = extract_cpp_file(textwrap.dedent("""\
        namespace ui {
        class Outer { class Inner { void run() {} }; };
        void Outer::Inner::stop() {}
        }
        template<> class Stack<int> { void push(int v) {} };
        void Stack<int>::pop() {}
        void lib::Widget::paint() {}
        """), "src/ui.cpp", "repo")
    owners = {(n.label, n.name): n.properties.get("owner") for n in result.nodes if n.properties.get("file")}
    assert owners[("Class", "Inner")] == "Outer"
    assert owners[("Function", "run")] == owners[("Function", "stop")] == "Outer.Inner"
    assert owners[("Class", "Stack")] == "" and owners[("Function", "push")] == owners[("Function", "pop")] == "Stack"
    assert owners[("Function", "paint")] == "lib.Widget"  # `lib` is no namespace of this file


def test_a_cross_file_call_names_the_owner_its_resolver_knows():
    """Python: an imported function is top-level (""); a method of an
    imported class is the class's (its own or one it inherits from a class
    of its file, which the heir keys there tell), but under a package
    directory owner-less; a method of an imported instance names no owner."""
    result = extract_python_file(textwrap.dedent("""\
        from shop.models import Order, save, client


        def run():
            save()


        def sync():
            Order().save()


        def push():
            client.save()
        """), "shop/jobs.py", "repo")

    def rows(caller):
        return {
            (r.to_file, r.to_owner) for r in result.relationships
            if r.rel_type == "CALLS" and r.to_name == "save" and r.from_name == caller
        }

    assert ("shop/models.py", "") in rows("run") and ("shop/models.py", None) not in rows("run")
    assert ("shop/models.py", "Order") in rows("sync") and ("shop/models.py", None) not in rows("sync")
    assert ("shop/models/", None) in rows("sync")
    assert ("shop/models.py", None) in rows("push")


@pytest.mark.parametrize("extract, path, source", [
    (extract_python_file, "app/s.py", """\
        from app.models import User


        def register(u: User):
            u.save()


        def make():
            u = User()
            u.save()
            User().save()
        """),
    (extract_js_file, "app/s.ts", """\
        import { User } from './models';
        import Account from './account';

        class Svc {
          constructor(private repo: User) {}
          sync() { this.repo.save(); }
        }
        export function register(u: User) { u.save(); }
        export function make() { const u = new User(); u.save(); new Account().save(); }
        """),
])
def test_a_typed_receiver_of_an_imported_class_names_its_owner(extract, path, source):
    """A receiver typed by an annotation, a constructor or a field, and a
    constructed one, name the class as the owner of the method's every
    file pin (a package or barrel prefix none); a default import names its
    file's default-exported class (`default`)."""
    result = extract(textwrap.dedent(source), path, "repo")
    rows = [r for r in result.relationships if r.rel_type == "CALLS" and r.to_name == "save"]
    assert rows
    for row in rows:
        expected = None if row.to_file.endswith("/") else "default" if "account" in row.to_file else "User"
        assert row.to_owner == expected, row


def test_heir_keys_name_the_classes_inheriting_a_member_from_the_nearest_base():
    assert heir_keys([
        ("Base", "Base", [], ["save", "check"]), ("Mid", "Mid", ["Base"], ["check"]),
        ("User", "User", ["Mid", "Missing"], []), ("Admin", "Admin", ["User"], ["save"]),
        ("Local", None, ["Base"], []),
    ]) == {
        ("save", "Base"): ["<Mid", "<User"], ("check", "Mid"): ["<Admin", "<User"],
    }


@pytest.mark.parametrize("lang", ["python", "ts", "java"])
def test_an_inherited_member_carries_its_heirs_keys(lang):
    extract, path, source = {
        "python": (extract_python_file, "app/m.py",
                   "class Base:\n    def save(self):\n        pass\n\n\nclass User(Base):\n    pass\n"),
        "ts": (extract_js_file, "app/m.ts", "class Base { save() {} }\nclass User extends Base {}\n"),
        "java": (extract_java_file, "app/m/User.java",
                 "package m;\nclass Base { void save() {} }\npublic class User extends Base {}\n"),
    }[lang]
    result = extract(source, path, "repo")
    (save,) = [n for n in result.nodes if n.label == "Function" and n.name == "save"]
    assert save.properties["owner"] == "Base" and save.properties["mkeys"] == ["<User"]


def test_a_route_implements_its_handlers_owner():
    from devgraph.indexer.apis.extractor import APIExtractor

    source = textwrap.dedent("""\
        @app.get("/items")
        def search():
            return []


        class Views:
            @router.get("/users")
            def search(self):
                return []
        """)
    result = APIExtractor("repo").extract_from_source(source, "api/routes.py")
    pinned = {(r.source_name, r.to_owner) for r in result.relationships if r.to_file == "api/routes.py"}
    assert pinned == {("GET /items", ""), ("GET /users", "Views")}


def _heir_open(result):
    (module,) = [n for n in result.nodes if n.label == "Module"]
    return module.properties["heir_open"]


def _function_keys(result):
    return {(n.name, n.properties["owner"]): n.properties.get("mkeys") for n in result.nodes if n.label == "Function"}


def test_python_heir_keys_follow_c3_in_a_diamond():
    """`class D(B, C)` where only C overrides A's `f`: C3 puts C before A,
    so D inherits C.f; JS and Java keep the depth-first walk."""
    classes = [("A", "A", [], ["f"]), ("B", "B", ["A"], []), ("C", "C", ["A"], ["f"]), ("D", "D", ["B", "C"], [])]
    assert heir_keys(classes, c3=True) == {("f", "A"): ["<B"], ("f", "C"): ["<D"]}
    assert heir_keys(classes) == {("f", "A"): ["<B", "<D"]}
    keys = _function_keys(extract_python_file(textwrap.dedent("""\
        class A:
            def f(self):
                pass


        class B(A):
            pass


        class C(A):
            def f(self):
                pass


        class D(B, C):
            pass
        """), "m.py", "repo"))
    assert keys[("f", "C")] == ["<D"] and keys[("f", "A")] == ["<B"]


def test_a_subscripted_base_is_its_value():
    """`class User(Base[int])` and `typing.Generic[T]` keep their base."""
    result = extract_python_file(textwrap.dedent("""\
        import typing


        class Base:
            def save(self):
                pass


        class User(Base[int], typing.Generic[T]):
            pass
        """), "m.py", "repo")
    assert ("Base", "typing.Generic") == tuple(
        r.to_name for r in result.relationships if r.rel_type == "EXTENDS" and r.from_name == "User"
    )
    assert "<User" in _function_keys(result)[("save", "Base")]


def test_same_named_python_classes_keep_their_own_heirs():
    """Two nested `Meta`s, one of base `Base`: only that one inherits `save`."""
    keys = _function_keys(extract_python_file(textwrap.dedent("""\
        class Base:
            def save(self):
                pass


        class A:
            class Meta(Base):
                pass


        class B:
            class Meta:
                def run(self):
                    pass
        """), "m.py", "repo"))
    assert keys[("save", "Base")] == ["<A.Meta"]
    assert keys[("run", "B.Meta")] is None


def test_python_aliases_and_instances_are_heirs_of_their_class():
    """`Registry = _Registry()` and `UserModel = User` hold their class's
    members' keys; `Other = make()`, which names no class of the file, and
    a class with a base outside it, are its file's open classes, so a call
    through them links what an owner-less pin did."""
    result = extract_python_file(textwrap.dedent("""\
        from x import Model


        class _Registry:
            def register(self):
                pass


        class User:
            def save(self):
                pass


        class Remote(Model):
            def own(self):
                pass


        def helper():
            pass


        Registry = _Registry()
        UserModel = User
        Other = make()
        """), "m.py", "repo")
    keys = _function_keys(result)
    assert keys[("register", "_Registry")] == ["<Registry"]
    assert keys[("save", "User")] == ["<UserModel"]
    assert keys[("own", "Remote")] is None and keys[("helper", "")] is None
    assert _heir_open(result) == ["Other", "Remote"]


@pytest.mark.parametrize("source, name, owner, key", [
    ("export class UserServiceImpl { find() {} }\nexport const UserService = new UserServiceImpl();\n",
     "find", "UserServiceImpl", "<UserService"),
    ("class Client { get() {} }\nexport default new Client();\n", "get", "Client", "<default"),
    ("class Impl { go() {} }\nexport { Impl as default };\n", "go", "Impl", "<default"),
    ("class Base { start() {} }\nexport default class extends Base {}\n", "start", "Base", "<default"),
    ("class Svc { run() {} }\nmodule.exports = Svc;\n", "run", "Svc", "<default"),
    ("class User { save() {} }\nexport const UserModel = User;\n", "save", "User", "<UserModel"),
    # Naming no class of the file: an open class of it.
    ("export const Api = makeApi();\nexport function get() {}\n", "get", "", "Api"),
    ("export default function Page() {}\nfunction load() {}\n", "load", "", "default"),
])
def test_js_aliases_instances_and_default_exports_are_heirs(source, name, owner, key):
    result = extract_js_file(source, "web/m.ts", "repo")
    if key.startswith("<"):
        assert key in _function_keys(result)[(name, owner)]
    else:
        assert key in _heir_open(result)


def test_open_keys_skip_nested_classes_type_aliases_and_exception_bases():
    """Only a top-level class or alias is open; a type expression, a literal
    or a `TypeVar` is no alias; a builtin exception base supplies no
    sibling's member."""
    result = extract_python_file(textwrap.dedent("""\
        from typing import Literal, TypeVar
        from lib import Model


        class Error(ValueError):
            def code(self):
                pass


        class Holder:
            class Inner(Model):
                pass


        Mode = Literal["a", "b"]
        Either = Error | Holder
        T2 = TypeVar("T2")
        Kind = NewType("Kind", int)


        def helper():
            pass
        """), "m.py", "repo")
    assert _heir_open(result) == ["Kind"]


def test_owner_matches_falls_back_to_an_open_class_its_file_names_no_member_of():
    from devgraph.indexer.common import owner_matches

    assert owner_matches(["Remote"], "", (), ["Remote"], [("", ())])
    # A Function of the name Remote has, or inherits, takes the link.
    assert not owner_matches(["Remote"], "", (), ["Remote"], [("", ()), ("Remote", ())])
    assert not owner_matches(["Remote"], "", (), ["Remote"], [("", ()), ("Base", ("<Remote",))])
    assert not owner_matches(["Remote"], "", (), [], [("", ())])
