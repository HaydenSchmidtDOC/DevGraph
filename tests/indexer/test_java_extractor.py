"""Unit tests for the Java source-code extractor."""

from devgraph.indexer.java.extractor import (
    ExtractionResult,
    extract_java_file,
)


def test_extract_module_classes_methods_imports():
    source_code = """
package com.example.foo;

import com.example.bar.BaseService;
import java.util.List;

class BaseService {
}

class UserService extends BaseService implements Describable {
    UserService(String dbPath) {
        this.dbPath = dbPath;
    }

    public Map getUser(int userId) {
        return null;
    }

    public Map createUser(String name) {
        return null;
    }
}

void processData(List data) {
}
"""
    # (Note: a free-standing method at the end isn't legal Java, so the last
    # snippet is omitted from real assertions below — kept minimal on purpose.)
    source_code = """
package com.example.foo;

import com.example.bar.BaseService;
import java.util.List;

class BaseService {
}

interface Describable {
    void describe();
}

class UserService extends BaseService implements Describable {
    UserService(String dbPath) {
        this.dbPath = dbPath;
    }

    public Map getUser(int userId) {
        return null;
    }

    public Map createUser(String name) {
        return null;
    }

    public void describe() {
    }
}
"""

    result = extract_java_file(source_code, "src/main/java/com/example/foo/UserService.java", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Module", "src/main/java/com/example/foo/UserService.java") in node_names
    assert ("Class", "BaseService") in node_names
    assert ("Class", "Describable") in node_names
    assert ("Class", "UserService") in node_names
    assert ("Function", "UserService") in node_names  # constructor
    assert ("Function", "getUser") in node_names
    assert ("Function", "createUser") in node_names
    assert ("Function", "describe") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }

    module_name = "src/main/java/com/example/foo/UserService.java"
    assert ("Module", module_name, "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", module_name, "CONTAINS", "Class", "UserService") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "getUser") in rel_tuples

    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples
    assert ("Class", "UserService", "EXTENDS", "Class", "Describable") in rel_tuples

    import_targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "com.example.bar.BaseService" in import_targets  # bare dotted, kept for compatibility
    assert "src/main/java/com/example/bar/BaseService.java" in import_targets  # resolved file guess
    assert "java.util.List" in import_targets


def test_extract_empty_file():
    result = extract_java_file("", "Empty.java", "test_repo")
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "Empty.java"


def test_package_folder_convention_resolves_source_root():
    """A file at 'src/main/java/com/example/foo/Bar.java' declaring
    'package com.example.foo;' should detect 'src/main/java' as the source
    root, and use it to resolve a sibling-package import to a real file
    guess.
    """
    source_code = """
package com.example.foo;

import com.example.util.Helper;

class Bar {
}
"""
    result = extract_java_file(source_code, "src/main/java/com/example/foo/Bar.java", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/main/java/com/example/util/Helper.java" in targets


def test_static_import_resolves_to_declaring_class_file():
    source_code = """
package com.example.foo;

import static com.example.util.Helper.doStuff;

class Bar {
}
"""
    result = extract_java_file(source_code, "src/main/java/com/example/foo/Bar.java", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/main/java/com/example/util/Helper.java" in targets
    # The member name itself ('doStuff') must not be mistaken for a class file.
    assert "src/main/java/com/example/util/Helper/doStuff.java" not in targets


def test_wildcard_import_has_no_file_guess():
    source_code = """
package com.example.foo;

import com.example.util.*;

class Bar {
}
"""
    result = extract_java_file(source_code, "src/main/java/com/example/foo/Bar.java", "test_repo")
    rels = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    targets = {r.to_name for r in rels}
    assert "com.example.util" in targets  # bare dotted, kept
    assert not any(t.endswith(".java") for t in targets)  # no file guess for a wildcard


def test_mismatched_package_directory_falls_back_to_repo_root_relative():
    """If the file's directory doesn't end with its declared package path,
    the source-root guess falls back to '' (repo-root-relative) rather than
    crashing or producing a nonsensical prefix.
    """
    source_code = """
package com.example.foo;

import com.example.bar.Baz;

class Weird {
}
"""
    result = extract_java_file(source_code, "somewhere/else/Weird.java", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "com/example/bar/Baz.java" in targets


def test_calls_edge_for_bare_method_call():
    source_code = """
class Service {
    void helper() {
    }

    void caller() {
        helper();
    }
}
"""
    result = extract_java_file(source_code, "Service.java", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_this_and_object_qualified_calls():
    source_code = """
class Service {
    void process() {
        this.helper();
        obj.otherHelper();
    }

    void helper() {
    }
}
"""
    result = extract_java_file(source_code, "Service.java", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls
    assert ("process", "otherHelper") in calls


def test_calls_edge_carries_caller_class():
    source_code = """
class Service {
    void process() {
        this.helper();
    }

    void helper() {
    }
}
"""
    result = extract_java_file(source_code, "Service.java", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("process", "helper")] == {"caller_class": "Service"}


def test_calls_not_hoisted_out_of_nested_class():
    source_code = """
class Outer {
    class Inner {
        void innerMethod() {
            deepCall();
        }
    }

    void outerMethod() {
        outerCall();
    }
}
"""
    result = extract_java_file(source_code, "Outer.java", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("innerMethod", "deepCall") in calls
    assert ("outerMethod", "outerCall") in calls
    assert ("outerMethod", "deepCall") not in calls


def test_nested_class_extraction():
    source_code = """
class Outer {
    class Inner {
        void innerMethod() {
        }
    }

    void outerMethod() {
    }
}
"""
    result = extract_java_file(source_code, "Outer.java", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Outer") in node_names
    assert ("Class", "Inner") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Outer", "CONTAINS", "Class", "Inner") in rel_tuples


def test_method_javadoc_extracted_into_description_and_full():
    source_code = """
class Greeter {
    /**
     * Say hello to someone.
     *
     * Longer explanation that should not appear in description.
     */
    void greet(String name) {
    }
}
"""
    result = extract_java_file(source_code, "Greeter.java", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "greet")
    assert func.properties["description"] == "Say hello to someone."
    assert "Longer explanation" in func.properties["docstring_full"]


def test_class_javadoc_extracted():
    source_code = """
/**
 * A simple class.
 */
class Foo {
}
"""
    result = extract_java_file(source_code, "Foo.java", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "A simple class."
    assert cls.properties["docstring_full"] == "A simple class."


def test_non_javadoc_block_comment_is_not_treated_as_docstring():
    source_code = """
/* Not a javadoc comment. */
class Foo {
}
"""
    result = extract_java_file(source_code, "Foo.java", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert "description" not in cls.properties
    assert "docstring_full" not in cls.properties


def test_no_javadoc_means_no_description_property():
    source_code = "class Bare {\n}\n"
    result = extract_java_file(source_code, "Bare.java", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Bare")
    assert "description" not in cls.properties
    assert "docstring_full" not in cls.properties


def test_annotations_captured_as_decorators():
    source_code = """
class Foo {
    @Override
    public void bar() {
    }
}
"""
    result = extract_java_file(source_code, "Foo.java", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "bar")
    assert "Override" in func.properties["decorators"]


def test_interface_and_enum_extracted_as_class_nodes():
    source_code = """
interface Describable {
    void describe();
}

enum Color {
    RED, GREEN, BLUE;
}
"""
    result = extract_java_file(source_code, "Types.java", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Describable") in node_names
    assert ("Class", "Color") in node_names


def test_enum_implements_produces_extends_edge():
    source_code = """
interface Describable {
}

enum Color implements Describable {
    RED, GREEN;
}
"""
    result = extract_java_file(source_code, "Types.java", "test_repo")
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Color", "EXTENDS", "Class", "Describable") in rel_tuples


def test_interface_extends_multiple_produces_extends_edges():
    source_code = """
interface A {
}
interface B {
}
interface C extends A, B {
}
"""
    result = extract_java_file(source_code, "Types.java", "test_repo")
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "C", "EXTENDS", "Class", "A") in rel_tuples
    assert ("Class", "C", "EXTENDS", "Class", "B") in rel_tuples


def test_all_nodes_scoped_to_repo():
    source_code = """
class MyClass {
    void myMethod() {
    }
}
"""
    repo_id = "test_repo_id_123"
    result = extract_java_file(source_code, "MyClass.java", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_function_and_class_get_start_end_line():
    source_code = "class Foo {\n    void method() {\n    }\n}\n"
    result = extract_java_file(source_code, "Foo.java", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    method = next(n for n in result.nodes if n.label == "Function" and n.name == "method")
    assert cls.properties["start_line"] == 1
    assert cls.properties["end_line"] == 4
    assert method.properties["start_line"] == 2
    assert method.properties["end_line"] == 3


def test_constructor_extracted_as_function():
    source_code = """
class Foo {
    Foo(int x) {
    }
}
"""
    result = extract_java_file(source_code, "Foo.java", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "Foo") in node_names
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name) for r in result.relationships
    }
    assert ("Class", "Foo", "CONTAINS", "Function", "Foo") in rel_tuples
