"""Unit tests for the Rust source-code extractor."""

from devgraph.indexer.rust.extractor import (
    ExtractionResult,
    extract_rust_file,
)


def test_extract_module_struct_function():
    """Test extracting module, struct, enum, trait, and free functions."""
    source_code = """
struct Point {
    x: i32,
    y: i32,
}

enum Color {
    Red,
    Green,
}

trait Shape {
    fn area(&self) -> f64;
}

fn free_function(x: i32) -> i32 {
    x
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Module", "src/lib.rs") in node_names
    assert ("Class", "Point") in node_names
    assert ("Class", "Color") in node_names
    assert ("Class", "Shape") in node_names
    assert ("Function", "free_function") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Module", "src/lib.rs", "CONTAINS", "Class", "Point") in rel_tuples
    assert ("Module", "src/lib.rs", "CONTAINS", "Class", "Color") in rel_tuples
    assert ("Module", "src/lib.rs", "CONTAINS", "Class", "Shape") in rel_tuples
    assert ("Module", "src/lib.rs", "CONTAINS", "Function", "free_function") in rel_tuples


def test_struct_enum_trait_kind_property():
    """struct/enum/trait all map to the Class label; properties['kind']
    records which one it originally was."""
    source_code = "struct S;\nenum E { A }\ntrait T {}\n"
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    class_nodes = {n.name: n for n in result.nodes if n.label == "Class"}
    assert class_nodes["S"].properties["kind"] == "struct"
    assert class_nodes["E"].properties["kind"] == "enum"
    assert class_nodes["T"].properties["kind"] == "trait"


def test_impl_methods_attributed_to_struct():
    """Methods inside an inherent `impl Type { .. }` block are attributed
    (CONTAINS) to the struct's Class node, not the Module."""
    source_code = """
struct Counter {
    value: i32,
}

impl Counter {
    fn new() -> Self {
        Counter { value: 0 }
    }

    fn increment(&self) {
        self.value;
    }
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "new") in node_names
    assert ("Function", "increment") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Counter", "CONTAINS", "Function", "new") in rel_tuples
    assert ("Class", "Counter", "CONTAINS", "Function", "increment") in rel_tuples
    # Methods are NOT also directly contained by the Module.
    assert ("Module", "src/lib.rs", "CONTAINS", "Function", "new") not in rel_tuples


def test_impl_trait_for_type_extends():
    """`impl Trait for Type` emits an EXTENDS edge from Class(Type) to
    Class(Trait) -- this extractor's judgment-call mapping for Rust's trait
    implementation onto the graph's generic inheritance relationship."""
    source_code = """
trait Greet {
    fn hello(&self);
}

struct Person;

impl Greet for Person {
    fn hello(&self) {}
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Person", "EXTENDS", "Class", "Greet") in rel_tuples
    # The impl's method is attributed to Person, not Greet.
    assert ("Class", "Person", "CONTAINS", "Function", "hello") in rel_tuples


def test_trait_default_method_attributed_to_trait():
    """A trait's default method (a function_item with a body, inside the
    trait's own declaration_list) is attributed to the trait's own Class
    node; a signature-only method (no body) is not extracted as a
    Function node at all."""
    source_code = """
trait Greet {
    fn hello(&self);

    fn wave(&self) {
        println!("wave");
    }
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "wave") in node_names
    assert ("Function", "hello") not in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Greet", "CONTAINS", "Function", "wave") in rel_tuples


def test_extract_empty_file():
    """Test extraction of an empty Rust file."""
    result = extract_rust_file("", "src/empty.rs", "test_repo")

    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "src/empty.rs"


def test_mod_declaration_resolves_to_file_guesses():
    """`mod foo;` emits both same-repo file-path guesses (foo.rs and
    foo/mod.rs) since only one convention is actually on disk and Tree-sitter
    alone can't tell which; the wrong guess just never materializes an edge
    (upsert_relationship only MATCH-links real existing nodes)."""
    result = extract_rust_file("mod foo;\n", "src/lib.rs", "test_repo")

    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/foo.rs" in targets
    assert "src/foo/mod.rs" in targets


def test_mod_declaration_relative_to_importing_file_directory():
    """A `mod foo;` inside a nested file resolves relative to that file's
    own directory, not the repo root."""
    result = extract_rust_file("mod foo;\n", "src/nested/child.rs", "test_repo")

    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/nested/foo.rs" in targets
    assert "src/nested/foo/mod.rs" in targets


def test_use_crate_path_resolves_under_crate_root():
    """`use crate::foo::bar::Baz;` resolves under the assumed src/ crate
    root, with both readings of the last segment emitted (itself a module,
    or an item defined inside the second-to-last segment's module)."""
    result = extract_rust_file("use crate::foo::bar::Baz;\n", "src/lib.rs", "test_repo")

    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/foo/bar.rs" in targets
    assert "src/foo/bar/mod.rs" in targets
    assert "src/foo/bar/Baz.rs" in targets
    assert "src/foo/bar/Baz/mod.rs" in targets


def test_use_external_crate_is_skipped():
    """`use std::...` (and any other non-`crate::`-rooted use) is skipped
    entirely -- no same-repo guess is attempted for external crates."""
    result = extract_rust_file("use std::collections::HashMap;\n", "src/lib.rs", "test_repo")

    imports = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    assert imports == []


def test_use_list_resolves_each_name():
    """`use crate::a::{b, c};` resolves each named import separately."""
    result = extract_rust_file("use crate::a::{b, c};\n", "src/lib.rs", "test_repo")

    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/a/b.rs" in targets
    assert "src/a/c.rs" in targets


def test_calls_edge_for_bare_function_call():
    """A bare call inside a function body produces a CALLS edge."""
    source_code = """
fn helper() {}

fn caller() {
    helper();
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_method_call_via_self():
    """self.method()/obj.method() resolves to the method's simple name --
    structural, not type-resolved (no type info available from Tree-sitter
    alone), same trade-off the Python extractor makes."""
    source_code = """
struct Service;

impl Service {
    fn process(&self) {
        self.helper();
    }

    fn helper(&self) {}
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls


def test_calls_edge_for_associated_function_via_scoped_path():
    """`Foo::new()` resolves to the last path segment ('new'), the
    associated-function equivalent of Python's dotted method-call resolution."""
    source_code = """
struct Foo;

impl Foo {
    fn new() -> Self { Foo }
}

fn make() -> Foo {
    Foo::new()
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("make", "new") in calls


def test_calls_edge_carries_caller_class_for_method_body_calls():
    """CALLS edges from inside an impl method body carry a caller_class
    property; edges from a free function carry no such property."""
    source_code = """
fn free_call() {
    helper();
}

struct Service;

impl Service {
    fn process(&self) {
        self.helper();
    }

    fn helper(&self) {}
}

fn helper() {}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls_by_pair = {(r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"}
    assert calls_by_pair[("process", "helper")] == {"caller_class": "Service"}
    assert calls_by_pair[("free_call", "helper")] is None


def test_calls_edge_at_module_level():
    """A call made outside any function (e.g. in a static initializer) is
    attributed to the Module."""
    source_code = """
fn setup() -> i32 { 0 }

static X: i32 = setup();
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls = {(r.from_label, r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Module", "src/lib.rs", "setup") in calls


def test_calls_not_hoisted_across_impl_boundary():
    """A call inside one method isn't attributed to a sibling method or to
    the enclosing struct's other scope."""
    source_code = """
struct Foo;

impl Foo {
    fn a(&self) {
        target_a();
    }

    fn b(&self) {
        target_b();
    }
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("a", "target_a") in calls
    assert ("b", "target_b") in calls
    assert ("a", "target_b") not in calls
    assert ("b", "target_a") not in calls


def test_doc_comment_extraction_for_struct_and_function():
    """`///` doc comments immediately preceding an item populate
    description/docstring_full, Rust's equivalent of a docstring."""
    source_code = '''
/// A point in 2D space.
///
/// Has an x and y coordinate.
struct Point {
    x: i32,
}

/// Adds two numbers.
fn add(a: i32, b: i32) -> i32 {
    a + b
}
'''
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    point_node = next(n for n in result.nodes if n.name == "Point")
    assert point_node.properties["description"] == "A point in 2D space."
    assert "Has an x and y coordinate." in point_node.properties["docstring_full"]

    add_node = next(n for n in result.nodes if n.name == "add")
    assert add_node.properties["description"] == "Adds two numbers."


def test_module_doc_comment_uses_inner_doc_markers():
    """`//!` inner doc comments at the top of the file become the Module
    node's docstring; they must not be misattributed to whatever item
    happens to follow them (see _leading_doc_comment vs _extract_module_doc)."""
    source_code = '''//! Crate root module.

/// Doc for Foo, not the crate.
struct Foo;
'''
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")

    module_node = next(n for n in result.nodes if n.label == "Module")
    assert module_node.properties["description"] == "Crate root module."

    foo_node = next(n for n in result.nodes if n.name == "Foo")
    assert foo_node.properties["description"] == "Doc for Foo, not the crate."


def test_all_nodes_scoped_to_repo():
    """All extracted nodes/relationships carry the provided repo_id."""
    source_code = """
struct MyStruct;

impl MyStruct {
    fn my_method(&self) {}
}

fn my_function() {}
"""
    repo_id = "test_repo_id_123"
    result = extract_rust_file(source_code, "src/lib.rs", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_start_end_line_properties():
    """Class/Function nodes carry 1-indexed start_line/end_line, matching
    the Python extractor's node shape."""
    source_code = """fn top() {
    inner_call();
}
"""
    result = extract_rust_file(source_code, "src/lib.rs", "test_repo")
    func_node = next(n for n in result.nodes if n.name == "top")
    assert func_node.properties["start_line"] == 1
    assert func_node.properties["end_line"] == 3
