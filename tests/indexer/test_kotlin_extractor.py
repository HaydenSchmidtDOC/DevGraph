"""Unit tests for the Kotlin source-code extractor."""

from devgraph.indexer.kotlin.extractor import (
    ExtractionResult,
    extract_kotlin_file,
)


def test_extract_module_classes_functions_imports():
    source_code = """
package com.example.foo

import com.example.bar.BaseService
import java.util.List

class BaseService {
}

interface Describable {
    fun describe()
}

class UserService : BaseService(), Describable {
    fun getUser(userId: Int): Map<Any, Any>? {
        return null
    }

    fun createUser(name: String): Map<Any, Any>? {
        return null
    }

    override fun describe() {
    }
}

fun processData(data: List<Any>) {
}
"""

    result = extract_kotlin_file(
        source_code, "src/main/java/com/example/foo/UserService.kt", "test_repo"
    )

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Module", "src/main/java/com/example/foo/UserService.kt") in node_names
    assert ("Class", "BaseService") in node_names
    assert ("Class", "Describable") in node_names
    assert ("Class", "UserService") in node_names
    assert ("Function", "getUser") in node_names
    assert ("Function", "createUser") in node_names
    assert ("Function", "describe") in node_names
    assert ("Function", "processData") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }

    module_name = "src/main/java/com/example/foo/UserService.kt"
    assert ("Module", module_name, "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", module_name, "CONTAINS", "Class", "UserService") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "getUser") in rel_tuples

    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples
    assert ("Class", "UserService", "EXTENDS", "Class", "Describable") in rel_tuples

    import_targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "com.example.bar.BaseService" in import_targets  # bare dotted, kept
    assert "src/main/java/com/example/bar/BaseService.kt" in import_targets  # resolved file guess
    assert "java.util.List" in import_targets


def test_extract_empty_file():
    result = extract_kotlin_file("", "Empty.kt", "test_repo")
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "Empty.kt"


def test_package_folder_convention_resolves_source_root():
    """A file at 'src/main/java/com/example/foo/Bar.kt' declaring
    'package com.example.foo' should detect 'src/main/java' as the source
    root and use it to resolve a sibling-package import to a real file guess.
    """
    source_code = """
package com.example.foo

import com.example.util.Helper

class Bar {
}
"""
    result = extract_kotlin_file(
        source_code, "src/main/java/com/example/foo/Bar.kt", "test_repo"
    )
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/main/java/com/example/util/Helper.kt" in targets


def test_wildcard_import_has_no_file_guess():
    source_code = """
package com.example.foo

import com.example.util.*

class Bar {
}
"""
    result = extract_kotlin_file(
        source_code, "src/main/java/com/example/foo/Bar.kt", "test_repo"
    )
    rels = [r for r in result.relationships if r.rel_type == "IMPORTS"]
    targets = {r.to_name for r in rels}
    assert "com.example.util" in targets  # bare dotted, kept
    assert not any(t.endswith(".kt") for t in targets)  # no file guess for a wildcard


def test_mismatched_package_directory_falls_back_to_repo_root_relative():
    """If the file's directory doesn't end with its declared package path,
    the source-root guess falls back to '' (repo-root-relative)."""
    source_code = """
package com.example.foo

import com.example.bar.Baz

class Weird {
}
"""
    result = extract_kotlin_file(source_code, "somewhere/else/Weird.kt", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "com/example/bar/Baz.kt" in targets


def test_calls_edge_for_bare_function_call():
    source_code = """
class Service {
    fun helper() {
    }

    fun caller() {
        helper()
    }
}
"""
    result = extract_kotlin_file(source_code, "Service.kt", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_qualified_calls():
    source_code = """
class Service {
    fun process() {
        this.helper()
        obj.otherHelper()
        System.currentTimeMillis()
    }

    fun helper() {
    }
}
"""
    result = extract_kotlin_file(source_code, "Service.kt", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls
    assert ("process", "otherHelper") in calls
    assert ("process", "currentTimeMillis") in calls


def test_calls_edge_carries_caller_class():
    source_code = """
class Service {
    fun process() {
        this.helper()
    }

    fun helper() {
    }
}
"""
    result = extract_kotlin_file(source_code, "Service.kt", "test_repo")
    calls = [
        r for r in result.relationships if r.rel_type == "CALLS" and r.to_name == "helper"
    ]
    assert calls
    assert calls[0].properties == {"caller_class": "Service"}


def test_data_class_and_enum_and_object_map_to_class():
    source_code = """
package com.example.foo

data class Point(val x: Int, val y: Int)

enum class Color { RED, GREEN, BLUE }

object Registry {
    fun lookup(): String = ""
}
"""
    result = extract_kotlin_file(source_code, "src/main/java/com/example/foo/T.kt", "test_repo")
    class_names = {n.name for n in result.nodes if n.label == "Class"}
    assert "Point" in class_names
    assert "Color" in class_names
    assert "Registry" in class_names


def test_extension_function_extracted_as_function():
    source_code = """
package com.example.foo

fun String.shout(): String = this.uppercase()
"""
    result = extract_kotlin_file(source_code, "src/main/java/com/example/foo/Ext.kt", "test_repo")
    func_names = {n.name for n in result.nodes if n.label == "Function"}
    assert "shout" in func_names


def test_nested_scope_calls_attributed_to_nested_function():
    source_code = """
package com.example.foo

fun outer() {
    inner()
    fun nested() {
        deep()
    }
}
"""
    result = extract_kotlin_file(source_code, "src/main/java/com/example/foo/N.kt", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("outer", "inner") in calls
    # `deep` is attributed to `nested`, not `outer`.
    assert ("nested", "deep") in calls
    assert ("outer", "deep") not in calls
