"""Unit tests for the Python source-code extractor."""

from devgraph.indexer.python.extractor import (
    ExtractionResult,
    extract_python_file,
)


def test_extract_module_classes_functions_imports():
    """Test extracting module, classes, functions, imports, and inheritance."""
    source_code = """
import os
from typing import List

class BaseService:
    \"\"\"Base service class.\"\"\"
    pass

@dataclass
class UserService(BaseService):
    \"\"\"User service with inheritance.\"\"\"

    def __init__(self, db_path: str):
        self.db_path = db_path

    def get_user(self, user_id: int) -> dict:
        \"\"\"Retrieve a user.\"\"\"
        return {"id": user_id}

    def create_user(self, name: str) -> dict:
        \"\"\"Create a new user.\"\"\"
        return {"name": name}

def process_data(data: List[str]) -> None:
    \"\"\"Module-level function.\"\"\"
    pass
"""

    result = extract_python_file(source_code, "test_module.py", "test_repo")

    assert isinstance(result, ExtractionResult)

    # Check nodes
    node_names = {(n.label, n.name) for n in result.nodes}

    # Module node
    assert ("Module", "test_module.py") in node_names

    # Class nodes
    assert ("Class", "BaseService") in node_names
    assert ("Class", "UserService") in node_names

    # Function nodes
    assert ("Function", "__init__") in node_names
    assert ("Function", "get_user") in node_names
    assert ("Function", "create_user") in node_names
    assert ("Function", "process_data") in node_names

    # Check relationships
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }

    # Module CONTAINS classes
    assert ("Module", "test_module.py", "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", "test_module.py", "CONTAINS", "Class", "UserService") in rel_tuples

    # Module CONTAINS module-level function
    assert ("Module", "test_module.py", "CONTAINS", "Function", "process_data") in rel_tuples

    # Class CONTAINS methods
    assert ("Class", "UserService", "CONTAINS", "Function", "__init__") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "get_user") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "create_user") in rel_tuples

    # Inheritance: UserService EXTENDS BaseService
    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples

    # Module IMPORTS
    assert ("Module", "test_module.py", "IMPORTS", "Module", "os") in rel_tuples
    assert ("Module", "test_module.py", "IMPORTS", "Module", "typing") in rel_tuples


def test_extract_with_decorators():
    """Test that decorators are captured in node properties."""
    source_code = """
from functools import lru_cache

@lru_cache(maxsize=128)
def expensive_function():
    return 42

@dataclass
class DataModel:
    value: int
"""

    result = extract_python_file(source_code, "decorators_module.py", "test_repo")

    # Find the function and class nodes
    function_nodes = [n for n in result.nodes if n.label == "Function"]
    class_nodes = [n for n in result.nodes if n.label == "Class"]

    # Check decorators in properties
    expensive_func = next((n for n in function_nodes if n.name == "expensive_function"), None)
    assert expensive_func is not None
    assert "lru_cache" in expensive_func.properties.get("decorators", [])

    data_model = next((n for n in class_nodes if n.name == "DataModel"), None)
    assert data_model is not None
    assert "dataclass" in data_model.properties.get("decorators", [])


def test_extract_nested_classes():
    """Test extraction of nested class definitions."""
    source_code = """
class OuterClass:
    class InnerClass:
        def inner_method(self):
            pass

    def outer_method(self):
        pass
"""

    result = extract_python_file(source_code, "nested_module.py", "test_repo")

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Class", "OuterClass") in node_names
    assert ("Class", "InnerClass") in node_names
    assert ("Function", "inner_method") in node_names
    assert ("Function", "outer_method") in node_names

    # Check nesting: OuterClass CONTAINS InnerClass
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "OuterClass", "CONTAINS", "Class", "InnerClass") in rel_tuples


def test_extract_empty_file():
    """Test extraction of an empty Python file."""
    source_code = ""

    result = extract_python_file(source_code, "empty_module.py", "test_repo")

    # Should still create a Module node
    assert len(result.nodes) >= 1
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "empty_module.py"


def test_extract_complex_imports():
    """Test extraction of various import styles."""
    source_code = """
import sys
import os
from typing import Dict, List, Optional
from pathlib import Path as PathLib
from . import local_module
from ..parent import parent_module
"""

    result = extract_python_file(source_code, "imports_module.py", "test_repo")

    # Check that imports are captured (note: exact behavior depends on tree-sitter parsing)
    # We should have some IMPORTS relationships
    imports = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    assert len(imports) > 0


def test_relative_import_targets_resolve_to_module_filenames():
    """IMPORTS targets for same-repo relative imports must match how Module
    nodes are actually named (bare '<name>.py') so GraphEngine.upsert_relationship's
    MATCH on both endpoints can find the target node — a prior version of
    this extractor produced targets like '.' or '.helpers' that could never
    match any Module node, so every same-repo IMPORTS edge silently failed
    to materialize (find_related_files' imported_modules was always empty).
    """
    source_code = "from . import utils, helpers\nfrom .db import connect\n"
    result = extract_python_file(source_code, "main.py", "test_repo")

    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "utils.py" in targets
    assert "helpers.py" in targets
    # 'connect' is a name inside db.py, not a module itself — target must be
    # the sibling file db.py, not connect.py.
    assert "db.py" in targets
    assert "connect.py" not in targets


def test_absolute_import_targets_are_bare_module_names():
    """Absolute imports (stdlib/third-party/top-level) keep their bare name as
    the target — they're expected not to resolve to a same-repo Module node,
    unlike relative imports.
    """
    result = extract_python_file("import os\nimport requests\n", "main.py", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert targets == {"os", "requests"}


def test_all_nodes_scoped_to_repo():
    """Test that all extracted nodes carry the correct repo_id."""
    source_code = """
class MyClass:
    def my_method(self):
        pass

def my_function():
    pass
"""

    repo_id = "test_repo_id_123"
    result = extract_python_file(source_code, "test_module.py", repo_id)

    # All nodes should have the provided repo_id
    for node in result.nodes:
        assert node.repo_id == repo_id

    # All relationships should have the provided repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_calls_edge_for_bare_function_call():
    """A bare call inside a function body produces a CALLS edge to the target."""
    source_code = """
def helper():
    pass

def caller():
    helper()
"""
    result = extract_python_file(source_code, "x.py", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_method_call_via_self():
    """self.method()/obj.method() resolves to the method's simple name — this
    extractor has no type info, so it links by name, not by resolved type.
    """
    source_code = """
class Service:
    def process(self):
        self.helper()

    def helper(self):
        pass
"""
    result = extract_python_file(source_code, "x.py", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls


def test_calls_edge_carries_caller_class_for_method_body_calls():
    """CALLS edges emitted from inside a method body carry a caller_class
    property (the enclosing class's name) so find_callers can optionally
    narrow results via scope_to_class; edges from module-level/free-function
    calls carry no such property (Implementation Plan #3, Item 2).
    """
    source_code = """
def free_call():
    helper()

class Service:
    def process(self):
        self.helper()

    def helper(self):
        pass
"""
    result = extract_python_file(source_code, "x.py", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("process", "helper")] == {"caller_class": "Service"}
    assert calls_by_pair[("free_call", "helper")] is None


def test_calls_edge_attributed_to_correct_nested_scope():
    """A call inside a nested function is attributed to the nested function,
    not hoisted to the enclosing one.
    """
    source_code = """
def outer():
    def inner():
        deep_call()
    outer_call()
"""
    result = extract_python_file(source_code, "x.py", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("inner", "deep_call") in calls
    assert ("outer", "outer_call") in calls
    assert ("outer", "deep_call") not in calls


def test_calls_edge_at_module_level():
    """A call made outside any function (top-level script code) is
    attributed to the Module."""
    source_code = """
def setup():
    pass

setup()
"""
    result = extract_python_file(source_code, "script.py", "test_repo")
    calls = {(r.from_label, r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Module", "script.py", "setup") in calls


def test_from_import_names_exclude_the_module_name():
    """Regression test: tree_sitter's Python bindings return a fresh Node
    wrapper on every child_by_field_name() call, so `child is module_node`
    never matches even for the same underlying tree node — this silently
    corrupted every from-import's name list ('from typing import List'
    produced imported_names=['typing', 'List'] instead of ['List']) since
    the original Tree-sitter migration. Fixed by comparing byte spans.
    """
    result = extract_python_file("from typing import List\n", "x.py", "test_repo")
    imports_rels = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    # Exactly one edge (one imported name), not two (module name duplicated in).
    assert len(imports_rels) == 1


def test_dotted_absolute_import_resolves_to_same_repo_file_path():
    """'from services.api_gateway.clients import Foo' — a same-repo absolute
    dotted import (the common real-world style, not dot-relative) — must
    ALSO emit a same-repo-file-path target so it can resolve to the actual
    Module node, alongside the original bare-dotted-name target kept for
    compatibility. Confirmed necessary: RAG4 (a real ~1300-node repo) uses
    this style exclusively and had zero resolvable same-repo IMPORTS edges
    before this fix.
    """
    result = extract_python_file(
        "from services.api_gateway.clients import Foo\n", "services/api_gateway/main.py", "test_repo"
    )
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "services.api_gateway.clients" in targets  # kept for compatibility
    assert "services/api_gateway/clients.py" in targets  # new: resolvable target


def test_dotted_import_statement_also_resolves():
    """Same guess applies to bare 'import X.Y.Z' (not just from-imports)."""
    result = extract_python_file("import shared.utils\n", "app.py", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "shared/utils.py" in targets


def test_single_segment_import_has_no_extra_file_guess():
    """A bare single-word import ('import os') shouldn't gain an extra
    file-path guess — there's no dotted structure to reinterpret.
    """
    result = extract_python_file("import os\n", "app.py", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert targets == {"os"}


def test_calls_targets_are_function_label():
    source_code = "def a():\n    b()\ndef b():\n    pass\n"
    result = extract_python_file(source_code, "x.py", "test_repo")
    calls_rel = next(r for r in result.relationships if r.rel_type == "CALLS")
    assert calls_rel.to_label == "Function"


def test_function_docstring_extracted_into_description_and_full():
    source_code = '''
def greet(name: str) -> str:
    """Say hello to someone.

    Longer explanation that should not appear in description.
    """
    return f"hello {name}"
'''
    result = extract_python_file(source_code, "x.py", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "greet")
    assert func.properties["description"] == "Say hello to someone."
    assert "Longer explanation" in func.properties["docstring_full"]


def test_class_docstring_extracted():
    source_code = '''
class Foo:
    """A simple class."""
    pass
'''
    result = extract_python_file(source_code, "x.py", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "A simple class."
    assert cls.properties["docstring_full"] == "A simple class."


def test_module_docstring_extracted():
    source_code = '"""Module summary line."""\nx = 1\n'
    result = extract_python_file(source_code, "x.py", "test_repo")
    module = next(n for n in result.nodes if n.label == "Module")
    assert module.properties["description"] == "Module summary line."


def test_no_docstring_means_no_description_property():
    source_code = "def bare():\n    pass\n"
    result = extract_python_file(source_code, "x.py", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "bare")
    assert "description" not in func.properties
    assert "docstring_full" not in func.properties


def test_long_docstring_summary_is_truncated():
    long_line = "This is a very long summary line " * 10
    source_code = f'def f():\n    """{long_line}"""\n    pass\n'
    result = extract_python_file(source_code, "x.py", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "f")
    assert len(func.properties["description"]) <= 120
    assert func.properties["description"].endswith("...")
    assert len(func.properties["docstring_full"]) > 120


def test_function_and_class_get_start_end_line():
    source_code = "class Foo:\n    def method(self):\n        pass\n"
    result = extract_python_file(source_code, "x.py", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    method = next(n for n in result.nodes if n.label == "Function" and n.name == "method")
    assert cls.properties["start_line"] == 1
    assert cls.properties["end_line"] == 3
    assert method.properties["start_line"] == 2
    assert method.properties["end_line"] == 3
