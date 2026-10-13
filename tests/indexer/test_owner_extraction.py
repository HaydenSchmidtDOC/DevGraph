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
    imported class or instance names no owner (the class can inherit it)."""
    result = extract_python_file(textwrap.dedent("""\
        from shop.models import Order, save, client


        def run():
            save()


        def sync():
            Order().save()
            client.save()
        """), "shop/jobs.py", "repo")

    def rows(caller):
        return {
            (r.to_file, r.to_owner) for r in result.relationships
            if r.rel_type == "CALLS" and r.to_name == "save" and r.from_name == caller
        }

    assert ("shop/models.py", "") in rows("run") and ("shop/models.py", None) not in rows("run")
    assert ("shop/models.py", None) in rows("sync") and ("shop/models.py", "Order") not in rows("sync")


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
