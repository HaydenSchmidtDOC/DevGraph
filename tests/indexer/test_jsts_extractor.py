"""Unit tests for the JS/TS source-code extractor."""

from devgraph.indexer.jsts.extractor import (
    ExtractionResult,
    extract_js_file,
)


def test_extract_module_classes_functions_imports():
    """Test extracting module, classes, functions, imports, and inheritance."""
    source_code = """
import os from 'os';
import { List } from 'typing';

class BaseService {
  constructor() {}
}

class UserService extends BaseService {
  constructor(dbPath) {
    super();
    this.dbPath = dbPath;
  }

  getUser(userId) {
    return { id: userId };
  }

  createUser(name) {
    return { name: name };
  }
}

function processData(data) {
}
"""
    result = extract_js_file(source_code, "test_module.js", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Module", "test_module.js") in node_names
    assert ("Class", "BaseService") in node_names
    assert ("Class", "UserService") in node_names
    assert ("Function", "constructor") in node_names
    assert ("Function", "getUser") in node_names
    assert ("Function", "createUser") in node_names
    assert ("Function", "processData") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }

    assert ("Module", "test_module.js", "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", "test_module.js", "CONTAINS", "Class", "UserService") in rel_tuples
    assert ("Module", "test_module.js", "CONTAINS", "Function", "processData") in rel_tuples

    assert ("Class", "UserService", "CONTAINS", "Function", "getUser") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "createUser") in rel_tuples

    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples


def test_extract_empty_file():
    """Test extraction of an empty JS file."""
    result = extract_js_file("", "empty_module.js", "test_repo")

    assert len(result.nodes) >= 1
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "empty_module.js"


def test_all_nodes_scoped_to_repo():
    """Test that all extracted nodes/relationships carry the correct repo_id."""
    source_code = """
class MyClass {
  myMethod() {}
}

function myFunction() {}
"""
    repo_id = "test_repo_id_123"
    result = extract_js_file(source_code, "test_module.js", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_calls_edge_for_bare_function_call():
    """A bare call inside a function body produces a CALLS edge to the target."""
    source_code = """
function helper() {}

function caller() {
  helper();
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("caller", "helper") in calls


def test_calls_edge_for_method_call_via_this():
    """this.method()/obj.method() resolves to the method's simple name - no
    type info, so it links by name, not by resolved type.
    """
    source_code = """
class Service {
  process() {
    this.helper();
  }

  helper() {}
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("process", "helper") in calls


def test_calls_edge_carries_caller_class_for_method_body_calls():
    """CALLS edges emitted from inside a method body carry a caller_class
    property; edges from module-level/free-function calls carry no such
    property.
    """
    source_code = """
function freeCall() {
  helper();
}

class Service {
  process() {
    this.helper();
  }

  helper() {}
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("process", "helper")] == {"caller_class": "Service"}
    assert calls_by_pair[("freeCall", "helper")] is None


def test_calls_edge_attributed_to_correct_nested_scope():
    """A call inside a nested function is attributed to the nested function,
    not hoisted to the enclosing one.
    """
    source_code = """
function outer() {
  function inner() {
    deepCall();
  }
  outerCall();
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("inner", "deepCall") in calls
    assert ("outer", "outerCall") in calls
    assert ("outer", "deepCall") not in calls


def test_calls_edge_at_module_level():
    """A call made outside any function (top-level script code) is
    attributed to the Module."""
    source_code = """
function setup() {}

setup();
"""
    result = extract_js_file(source_code, "script.js", "test_repo")
    calls = {(r.from_label, r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Module", "script.js", "setup") in calls


def test_calls_not_duplicated_for_method_call():
    """A single call inside a method body must produce exactly one CALLS
    edge, not one per level of statement traversal.
    """
    source_code = """
class Service {
  process() {
    return this.helper();
  }

  helper() {}
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls = [r for r in result.relationships if r.rel_type == "CALLS" and r.to_name == "helper"]
    assert len(calls) == 1


def test_arrow_function_bound_to_const_is_extracted():
    """An arrow function assigned to a const is extracted as a Function node."""
    source_code = """
const arrowFn = () => {
  inner();
};
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "arrowFn") in node_names
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("arrowFn", "inner") in calls


def test_function_expression_bound_to_const_is_extracted():
    source_code = """
const namedExpr = function() {
  helper();
};
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "namedExpr") in node_names


def test_anonymous_arrow_not_bound_to_identifier_is_not_extracted():
    """Scope cut: an inline arrow passed as a callback argument (not bound to
    an identifier) is not extracted as a Function node."""
    source_code = """
function run(cb) {}
run(() => {
  doStuff();
});
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    func_names = {n.name for n in result.nodes if n.label == "Function"}
    assert "run" in func_names
    assert len(func_names) == 1


def test_export_function_and_class_extracted():
    source_code = """
export function exported() {
  helper();
}

export class ExportedClass extends Base {}

export default class DefaultExport {}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "exported") in node_names
    assert ("Class", "ExportedClass") in node_names
    assert ("Class", "DefaultExport") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "ExportedClass", "EXTENDS", "Class", "Base") in rel_tuples


def test_relative_import_targets_include_extension_candidates():
    source_code = """
import { a } from './utils';
import x from '../other/mod';
"""
    result = extract_js_file(source_code, "src/main.js", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "src/utils.js" in targets
    assert "src/utils.ts" in targets
    assert "other/mod.js" in targets


def test_relative_import_resolving_to_repo_root_has_no_dangling_dot_extension():
    """A relative specifier that walks all the way up to the repo root
    (e.g. `require('../..')` from a file two directories deep) must not
    produce a bare '.js'/'.jsx'/etc target (an empty basename plus
    extension) - only the 'index.<ext>' directory-import candidates are
    meaningful for an empty resolved base path.
    """
    result = extract_js_file("const app = require('../..');\n", "examples/mvc/index.js", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert ".js" not in targets
    assert "index.js" in targets


def test_bare_import_resolves_to_node_modules_guess():
    result = extract_js_file("import lodash from 'lodash';\n", "app.js", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "node_modules/lodash" in targets


def test_require_import_resolves_relative_target():
    result = extract_js_file("const utils = require('./utils');\n", "app.js", "test_repo")
    targets = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
    assert "utils.js" in targets


def test_require_call_does_not_also_produce_calls_edge():
    """require(...) is captured as IMPORTS, not as a CALLS edge to a
    (nonexistent) 'require' Function node."""
    result = extract_js_file("const utils = require('./utils');\n", "app.js", "test_repo")
    calls = [r for r in result.relationships if r.rel_type == "CALLS"]
    assert not any(r.to_name == "require" for r in calls)


def test_jsdoc_docstring_extracted_for_function():
    source_code = """
/**
 * Say hello to someone.
 *
 * Longer explanation that should not appear in description.
 */
function greet(name) {
  return `hello ${name}`;
}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "greet")
    assert func.properties["description"] == "Say hello to someone."
    assert "Longer explanation" in func.properties["docstring_full"]


def test_jsdoc_docstring_extracted_for_class():
    source_code = """
/**
 * A simple class.
 */
class Foo {}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "A simple class."


def test_jsdoc_docstring_on_exported_class():
    """JSDoc precedes the `export` keyword, not the class declaration itself."""
    source_code = """
/**
 * Exported doc.
 */
export class Foo {}
"""
    result = extract_js_file(source_code, "x.js", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "Exported doc."


def test_no_jsdoc_means_no_description_property():
    source_code = "function bare() {}\n"
    result = extract_js_file(source_code, "x.js", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "bare")
    assert "description" not in func.properties
    assert "docstring_full" not in func.properties


def test_function_and_class_get_start_end_line():
    source_code = "class Foo {\n  method() {\n    return 1;\n  }\n}\n"
    result = extract_js_file(source_code, "x.js", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    method = next(n for n in result.nodes if n.label == "Function" and n.name == "method")
    assert cls.properties["start_line"] == 1
    assert cls.properties["end_line"] == 5
    assert method.properties["start_line"] == 2
    assert method.properties["end_line"] == 4


def test_calls_targets_are_function_label():
    source_code = "function a() {\n  b();\n}\nfunction b() {}\n"
    result = extract_js_file(source_code, "x.js", "test_repo")
    calls_rel = next(r for r in result.relationships if r.rel_type == "CALLS")
    assert calls_rel.to_label == "Function"


def test_typescript_class_extends_and_implements():
    """TS 'implements' does not produce an EXTENDS edge; 'extends' does."""
    source_code = """
class Widget extends Base implements Renderable {
  render() {}
}
"""
    result = extract_js_file(source_code, "x.ts", "test_repo")
    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "Widget", "EXTENDS", "Class", "Base") in rel_tuples
    assert not any(r.rel_type == "EXTENDS" and r.to_name == "Renderable" for r in result.relationships)


def test_typescript_type_annotations_do_not_break_extraction():
    source_code = """
class Repo<T> {
  find(id: number): T | undefined {
    return this.lookup(id);
  }

  lookup(id: number): T | undefined {
    return undefined;
  }
}
"""
    result = extract_js_file(source_code, "x.ts", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "Repo") in node_names
    assert ("Function", "find") in node_names
    assert ("Function", "lookup") in node_names
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("find", "lookup") in calls


def test_tsx_file_extracts_classes_and_functions():
    source_code = """
function Component() {
  return helper();
}
"""
    result = extract_js_file(source_code, "x.tsx", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Function", "Component") in node_names
