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
