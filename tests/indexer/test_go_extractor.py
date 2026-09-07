"""Unit tests for the Go source-code extractor."""

from devgraph.indexer.common import ExtractionResult
from devgraph.indexer.go.extractor import extract_go_file


def test_extract_module_structs_functions_imports():
    """Test extracting module, structs, functions, methods, and imports."""
    source_code = """// Package svc does things.
package svc

import (
	"fmt"
)

// BaseService is a base struct.
type BaseService struct {
	Name string
}

// UserService embeds BaseService.
type UserService struct {
	BaseService
	DBPath string
}

// GetUser retrieves a user.
func (u *UserService) GetUser(id int) string {
	return fmt.Sprintf("%d", id)
}

// CreateUser creates a new user.
func (u *UserService) CreateUser(name string) string {
	return name
}

// ProcessData is a module-level function.
func ProcessData(data string) {
}
"""

    result = extract_go_file(source_code, "test_module.go", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Module", "test_module.go") in node_names
    assert ("Class", "BaseService") in node_names
    assert ("Class", "UserService") in node_names
    assert ("Function", "GetUser") in node_names
    assert ("Function", "CreateUser") in node_names
    assert ("Function", "ProcessData") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }

    assert ("Module", "test_module.go", "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", "test_module.go", "CONTAINS", "Class", "UserService") in rel_tuples
    assert ("Module", "test_module.go", "CONTAINS", "Function", "ProcessData") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "GetUser") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "CreateUser") in rel_tuples

    # Struct embedding: UserService EXTENDS BaseService
    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples

    assert ("Module", "test_module.go", "IMPORTS", "Module", "fmt") in rel_tuples


def test_extract_empty_file():
    """Test extraction of an empty Go file."""
    source_code = ""

    result = extract_go_file(source_code, "empty_module.go", "test_repo")

    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "empty_module.go"


def test_all_nodes_scoped_to_repo():
    """Test that all extracted nodes carry the correct repo_id."""
    source_code = """package foo

type MyStruct struct {
	Value int
}

func (m *MyStruct) MyMethod() {
}

func MyFunction() {
}
"""

    repo_id = "test_repo_id_123"
    result = extract_go_file(source_code, "test_module.go", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_calls_edge_for_bare_function_call():
    """A bare call inside a function body produces a CALLS edge to the target."""
    source_code = """package foo

func helper() {
}

func caller() {
	helper()
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_method_call_via_selector():
    """obj.method() resolves to the method's simple name — no type info, so
    this extractor links by name, not by resolved type.
    """
    source_code = """package foo

type Service struct{}

func (s *Service) Process() {
	s.Helper()
}

func (s *Service) Helper() {
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Process", "Helper") in calls


def test_calls_edge_carries_caller_class_for_method_body_calls():
    """CALLS edges from a method body carry a caller_class property (the
    receiver's struct name); a free function's CALLS edges carry none.
    """
    source_code = """package foo

func freeCall() {
	helper()
}

type Service struct{}

func (s *Service) Process() {
	s.Helper()
}

func (s *Service) Helper() {
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("Process", "Helper")] == {"caller_class": "Service"}
    assert calls_by_pair[("freeCall", "helper")] is None


def test_pointer_and_value_receivers_attribute_to_same_struct():
    """Both `func (s *Service)` and `func (s Service)` attribute their
    method to the same struct name, 'Service'.
    """
    source_code = """package foo

type Service struct{}

func (s *Service) PointerMethod() {
}

func (s Service) ValueMethod() {
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "Service", "CONTAINS", "Function", "PointerMethod") in rel_tuples
    assert ("Class", "Service", "CONTAINS", "Function", "ValueMethod") in rel_tuples


def test_intra_module_import_resolves_to_file_guess():
    """An import path under the repo's own module (per go.mod) gets an
    extra same-repo file-path guess, alongside the bare import path.
    """
    result = extract_go_file(
        'package foo\n\nimport "myproject/pkg/sub"\n',
        "main.go",
        "test_repo",
        module_path="myproject",
    )
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "myproject/pkg/sub" in targets  # bare path, kept for compatibility
    assert "pkg/sub/sub.go" in targets  # same-repo file-path guess


def test_external_import_has_no_extra_guess_when_module_known():
    """When go.mod's module path is known, a genuinely external import
    (outside this repo's module) gets no extra file-path guess — it's
    expected not to resolve, same as Python's third-party-import behavior.
    """
    result = extract_go_file(
        'package foo\n\nimport "github.com/someone/pkg"\n',
        "main.go",
        "test_repo",
        module_path="myproject",
    )
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert targets == {"github.com/someone/pkg"}


def test_import_without_known_module_path_still_gets_a_guess():
    """When go.mod isn't available (module_path=None), every import gets a
    best-effort last-path-segment same-repo guess.
    """
    result = extract_go_file(
        'package foo\n\nimport "path/to/pkg"\n',
        "main.go",
        "test_repo",
    )
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "path/to/pkg" in targets
    assert "pkg/pkg.go" in targets


def test_calls_targets_are_function_label():
    source_code = "package foo\n\nfunc a() {\n\tb()\n}\nfunc b() {\n}\n"
    result = extract_go_file(source_code, "x.go", "test_repo")
    calls_rel = next(r for r in result.relationships if r.rel_type == "CALLS")
    assert calls_rel.to_label == "Function"


def test_function_doc_comment_extracted_into_description_and_full():
    source_code = """package foo

// Greet says hello to someone.
//
// Longer explanation that should not appear in description.
func Greet(name string) string {
	return name
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "Greet")
    assert func.properties["description"] == "Greet says hello to someone."
    assert "Longer explanation" in func.properties["docstring_full"]


def test_struct_doc_comment_extracted():
    source_code = """package foo

// Foo is a simple struct.
type Foo struct {
	Value int
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "Foo is a simple struct."
    assert cls.properties["docstring_full"] == "Foo is a simple struct."


def test_module_doc_comment_extracted_from_package_clause():
    source_code = "// Package foo does the thing.\npackage foo\n"
    result = extract_go_file(source_code, "x.go", "test_repo")
    module = next(n for n in result.nodes if n.label == "Module")
    assert module.properties["description"] == "Package foo does the thing."


def test_no_doc_comment_means_no_description_property():
    source_code = "package foo\n\nfunc bare() {\n}\n"
    result = extract_go_file(source_code, "x.go", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "bare")
    assert "description" not in func.properties
    assert "docstring_full" not in func.properties


def test_blank_line_before_comment_is_not_treated_as_doc_comment():
    """A comment separated from the declaration by a blank line is not its
    doc comment, per Go's convention (must be directly above, no gap).
    """
    source_code = "package foo\n\n// Not a doc comment (blank line follows).\n\nfunc bare() {\n}\n"
    result = extract_go_file(source_code, "x.go", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "bare")
    assert "description" not in func.properties


def test_function_and_struct_get_start_end_line():
    source_code = "package foo\n\ntype Foo struct {\n\tValue int\n}\n\nfunc Bar() {\n}\n"
    result = extract_go_file(source_code, "x.go", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "Bar")
    assert cls.properties["start_line"] == 3
    assert cls.properties["end_line"] == 5
    assert func.properties["start_line"] == 7
    assert func.properties["end_line"] == 8


def test_qualified_type_embedding_extends_by_simple_name():
    """Embedding a struct from another package (`pkg.Other`) still emits an
    EXTENDS edge, keyed by the embedded type's simple name.
    """
    source_code = """package foo

type Wrapper struct {
	pkg.Other
	Value int
}
"""
    result = extract_go_file(source_code, "x.go", "test_repo")
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "Wrapper", "EXTENDS", "Class", "Other") in rel_tuples
