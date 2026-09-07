"""Unit tests for the C++ source-code extractor."""

from devgraph.indexer.cpp.extractor import (
    ExtractionResult,
    extract_cpp_file,
)


def test_extract_module_classes_functions():
    """Test extracting module, classes (including out-of-class method
    definitions), and CONTAINS relationships.
    """
    source_code = """
class Base {
public:
    virtual void foo();
};

class Derived : public Base {
public:
    Derived();
    void bar() {}
};

Derived::Derived() {}

int free_func(int a) {
    return a + 1;
}
"""
    result = extract_cpp_file(source_code, "src/widget.cpp", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Module", "src/widget.cpp") in node_names
    assert ("Class", "Base") in node_names
    assert ("Class", "Derived") in node_names
    assert ("Function", "bar") in node_names
    assert ("Function", "Derived") in node_names  # out-of-class ctor definition
    assert ("Function", "free_func") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Module", "src/widget.cpp", "CONTAINS", "Class", "Base") in rel_tuples
    assert ("Module", "src/widget.cpp", "CONTAINS", "Class", "Derived") in rel_tuples
    assert ("Module", "src/widget.cpp", "CONTAINS", "Function", "free_func") in rel_tuples
    assert ("Class", "Derived", "CONTAINS", "Function", "bar") in rel_tuples
    # Out-of-class 'Derived::Derived() {}' is attributed to Class Derived,
    # not left dangling at Module scope.
    assert ("Class", "Derived", "CONTAINS", "Function", "Derived") in rel_tuples


def test_out_of_class_method_attributed_when_class_not_declared_in_file():
    """A .cpp implementing a class declared only in a header it doesn't
    happen to be extracted alongside still attributes the method to a Class
    node (a stub, per the module docstring's heuristic) rather than dropping
    it or hoisting it to Module scope.
    """
    source_code = """
void Widget::render() {
    draw();
}
"""
    result = extract_cpp_file(source_code, "src/widget.cpp", "test_repo")

    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Widget") in node_names
    assert ("Function", "render") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Widget", "CONTAINS", "Function", "render") in rel_tuples


def test_extends_with_multiple_and_mixed_access_bases():
    """A base-class list with multiple bases and mixed/virtual access
    specifiers produces one EXTENDS edge per base, ignoring the specifiers.
    """
    source_code = """
class A {};
class B {};
class C : public A, private B, public virtual A {
public:
    void m() {}
};
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    extends = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "EXTENDS"}
    assert ("C", "A") in extends
    assert ("C", "B") in extends


def test_export_macro_prefixed_class_still_extracted():
    """`class EXPORT_MACRO Name : public Base { ... }` — a common DLL-export/
    visibility-attribute convention — completely defeats Tree-sitter-cpp's
    grammar without macro expansion (it reads the macro as the class name
    and everything after, including the real name/bases/members, as a bogus
    function_definition). This extractor recovers the class name and base
    classes for this specific misparse shape (not the members — see
    `_recover_macro_prefixed_class`'s docstring for why).
    """
    source_code = """
class MYLIB_API Widget : public Base {
public:
    Widget();
};
"""
    result = extract_cpp_file(source_code, "x.h", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Widget") in node_names
    assert ("Class", "MYLIB_API") not in node_names  # not mistaken for the class name

    extends = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "EXTENDS"}
    assert ("Widget", "Base") in extends


def test_export_macro_prefixed_class_no_base():
    """Same misparse without a base-class list — the declarator field alone
    (not an ERROR-node fragment) carries the real name in this shape.
    """
    source_code = """
class MYLIB_API Widget {
public:
    Widget();
};
"""
    result = extract_cpp_file(source_code, "x.h", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Widget") in node_names


def test_struct_extraction():
    """A `struct` is extracted the same way a `class` is."""
    source_code = """
struct Point {
    int x;
    int y;
};
"""
    result = extract_cpp_file(source_code, "point.h", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Point") in node_names


def test_quoted_include_resolves_same_dir_guess():
    """A quoted `#include` resolves to a same-directory/relative Module-name
    guess (the 'never materializes if wrong' pattern) — see module docstring.
    """
    source_code = '#include "utils.h"\n#include "../common/base.h"\n'
    result = extract_cpp_file(source_code, "src/widget.cpp", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/utils.h" in targets
    assert "common/base.h" in targets


def test_angle_bracket_include_never_resolved():
    """A system/library `#include <...>` produces no IMPORTS edge at all —
    not even a guess — since it has no repo-relative meaning.
    """
    source_code = "#include <vector>\n#include <string>\n"
    result = extract_cpp_file(source_code, "src/widget.cpp", "test_repo")
    imports = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    assert imports == []


def test_include_at_repo_root_has_no_directory_prefix():
    source_code = '#include "utils.h"\n'
    result = extract_cpp_file(source_code, "main.cpp", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert targets == {"utils.h"}


def test_calls_edge_for_bare_function_call():
    source_code = """
void helper() {}

void caller() {
    helper();
}
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_member_call_via_this_and_arrow():
    source_code = """
class Service {
public:
    void process() {
        this->helper();
    }
    void helper() {}
};
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls


def test_calls_edge_carries_caller_class_for_method_body_calls():
    source_code = """
void free_call() {
    helper();
}

class Service {
public:
    void process() {
        helper();
    }
    void helper() {}
};
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("process", "helper")] == {"caller_class": "Service"}
    assert calls_by_pair[("free_call", "helper")] is None


def test_calls_not_hoisted_out_of_nested_class():
    """A call inside a locally-defined nested class's method is attributed
    to that nested scope, not hoisted to the enclosing function/class.
    """
    source_code = """
void outer() {
    class Local {
    public:
        void m() { inner_call(); }
    };
    outer_call();
}
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("outer", "outer_call") in calls
    assert ("outer", "inner_call") not in calls


def test_class_doc_comment_triple_slash():
    source_code = """
/// A simple widget.
class Widget {
public:
    void draw() {}
};
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Widget")
    assert cls.properties["description"] == "A simple widget."
    assert cls.properties["docstring_full"] == "A simple widget."


def test_class_doc_comment_multi_line_triple_slash():
    source_code = """
/// A simple widget.
/// Second line of detail.
class Widget {
public:
    void draw() {}
};
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Widget")
    assert cls.properties["description"] == "A simple widget."
    assert "Second line of detail." in cls.properties["docstring_full"]


def test_function_doc_comment_block_style():
    source_code = """
/**
 * Compute the answer.
 * Longer explanation here.
 */
int compute() {
    return 42;
}
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "compute")
    assert func.properties["description"] == "Compute the answer."
    assert "Longer explanation here." in func.properties["docstring_full"]


def test_plain_comment_is_not_a_doc_comment():
    source_code = """
// just a regular comment, not a doc comment
void plain() {}
"""
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "plain")
    assert "description" not in func.properties
    assert "docstring_full" not in func.properties


def test_all_nodes_scoped_to_repo():
    source_code = """
class MyClass {
public:
    void method() {}
};

void my_function() {}
"""
    repo_id = "test_repo_id_123"
    result = extract_cpp_file(source_code, "x.cpp", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_extract_empty_file():
    result = extract_cpp_file("", "empty.cpp", "test_repo")
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "empty.cpp"


def test_function_and_class_get_start_end_line():
    source_code = "class Foo {\npublic:\n    void method() {}\n};\n"
    result = extract_cpp_file(source_code, "x.cpp", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    method = next(n for n in result.nodes if n.label == "Function" and n.name == "method")
    assert cls.properties["start_line"] == 1
    assert cls.properties["end_line"] == 4
    assert method.properties["start_line"] == 3
    assert method.properties["end_line"] == 3


def test_header_file_extracted_same_as_source_file():
    """The same extractor handles .h/.hpp headers, not just .cpp sources."""
    source_code = """
#ifndef WIDGET_H
#define WIDGET_H

class Widget {
public:
    void draw();
};

#endif
"""
    result = extract_cpp_file(source_code, "widget.h", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Module", "widget.h") in node_names
    assert ("Class", "Widget") in node_names
