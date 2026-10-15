"""Unit tests for the C# source-code extractor."""

from unittest.mock import MagicMock

from devgraph.indexer.csharp.extractor import (
    ExtractionResult,
    extract_csharp_file,
    index_file,
)
from devgraph.indexer.symbols import EXTRACTORS


def test_extract_module_classes_functions_using():
    """Test extracting module, classes, methods, using-directives, and inheritance."""
    source_code = """
using System;
using MyApp.Data;

namespace MyApp.Services
{
    /// <summary>
    /// Base service class.
    /// </summary>
    public abstract class BaseService
    {
    }

    /// <summary>
    /// User service with inheritance.
    /// </summary>
    public class UserService : BaseService
    {
        public UserService(string dbPath)
        {
        }

        /// <summary>
        /// Retrieve a user.
        /// </summary>
        public Dictionary<string, object> GetUser(int userId)
        {
            return null;
        }

        public Dictionary<string, object> CreateUser(string name)
        {
            return null;
        }
    }
}
"""

    result = extract_csharp_file(source_code, "MyApp/Services/UserService.cs", "test_repo")

    assert isinstance(result, ExtractionResult)

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Module", "MyApp/Services/UserService.cs") in node_names
    assert ("Class", "BaseService") in node_names
    assert ("Class", "UserService") in node_names
    # Constructors are keyed by their name field, which is literally the
    # class name in C# — same Function node identity as any other method.
    assert ("Function", "UserService") in node_names
    assert ("Function", "GetUser") in node_names
    assert ("Function", "CreateUser") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }

    assert ("Module", "MyApp/Services/UserService.cs", "CONTAINS", "Class", "BaseService") in rel_tuples
    assert ("Module", "MyApp/Services/UserService.cs", "CONTAINS", "Class", "UserService") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "UserService") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "GetUser") in rel_tuples
    assert ("Class", "UserService", "CONTAINS", "Function", "CreateUser") in rel_tuples

    assert ("Class", "UserService", "EXTENDS", "Class", "BaseService") in rel_tuples

    # A using names a namespace, no file: the IMPORTS are the classes the
    # file references (none here but library types and its own).
    assert not any(r[2] == "IMPORTS" for r in rel_tuples)


def test_extract_with_attributes():
    """Attributes ([Foo]) are captured in node properties as 'decorators',
    C#'s nearest equivalent to Python decorators."""
    source_code = """
[Obsolete]
public void ExpensiveMethod() {}

[Serializable]
public class DataModel
{
}
"""
    # Wrap in a class since C# has no free functions outside newer
    # top-level-statement files, which this extractor doesn't model.
    source_code = """
public class Container
{
    [Obsolete]
    public void ExpensiveMethod() {}
}

[Serializable]
public class DataModel
{
}
"""

    result = extract_csharp_file(source_code, "attrs.cs", "test_repo")

    function_nodes = [n for n in result.nodes if n.label == "Function"]
    class_nodes = [n for n in result.nodes if n.label == "Class"]

    expensive_method = next((n for n in function_nodes if n.name == "ExpensiveMethod"), None)
    assert expensive_method is not None
    assert "Obsolete" in expensive_method.properties.get("decorators", [])

    data_model = next((n for n in class_nodes if n.name == "DataModel"), None)
    assert data_model is not None
    assert "Serializable" in data_model.properties.get("decorators", [])


def test_extract_nested_classes():
    """Test extraction of nested class declarations."""
    source_code = """
public class OuterClass
{
    public class InnerClass
    {
        public void InnerMethod() {}
    }

    public void OuterMethod() {}
}
"""

    result = extract_csharp_file(source_code, "nested.cs", "test_repo")

    node_names = {(n.label, n.name) for n in result.nodes}

    assert ("Class", "OuterClass") in node_names
    assert ("Class", "InnerClass") in node_names
    assert ("Function", "InnerMethod") in node_names
    assert ("Function", "OuterMethod") in node_names

    rel_tuples = {
        (r.from_label, r.from_name, r.rel_type, r.to_label, r.to_name)
        for r in result.relationships
    }
    assert ("Class", "OuterClass", "CONTAINS", "Class", "InnerClass") in rel_tuples


def test_extract_empty_file():
    """Test extraction of an empty C# file."""
    result = extract_csharp_file("", "Empty.cs", "test_repo")

    assert len(result.nodes) >= 1
    module_nodes = [n for n in result.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].name == "Empty.cs"


def test_a_using_imports_no_file():
    """`using MyApp.Services.Foo;` names a namespace, which can live in any
    file: no IMPORTS target, neither the dotted name nor a folder guess."""
    for source in ("using System;\n", "using MyApp.Services.Foo;\n"):
        result = extract_csharp_file(source, "App.cs", "test_repo")
        assert [r for r in result.relationships if r.rel_type == "IMPORTS"] == []


def test_every_class_and_function_carries_match_keys():
    """`mkeys` is written on every Class and Function, sorted, even empty."""
    result = extract_csharp_file("class A { void Run() {} }\nenum E { X }\n", "A.cs", "test_repo")
    assert {(n.label, n.name): n.properties.get("mkeys") for n in result.nodes} == {
        ("Module", "A.cs"): None, ("Class", "A"): [".:A"], ("Function", "Run"): [".:A"],
    }


def test_every_entry_point_reads_the_project_as_index_paths_does(tmp_path):
    """The legacy `index_file` reads the repository's C# projects, so a
    project `<Using>` narrows its pins as in a scan; `symbols.EXTRACTORS`
    keeps only nodes, whose match keys come from the text alone."""
    (tmp_path / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><Using Include="Shop.Text" /></ItemGroup></Project>'
    )
    source = "namespace App;\nclass P { void Main() { Formatting.Title(); } }\n"
    (tmp_path / "P.cs").write_text(source)
    engine = MagicMock()
    index_file(engine, "test_repo", tmp_path / "P.cs", repo_root=tmp_path)
    (rels,), _ = engine.upsert_relationships.call_args
    pins = {r["to_file"] for r in rels if r["rel_type"] == "CALLS"}
    assert len(pins) == 1 and "Shop.Text:Formatting" in pins.pop()
    nodes = {(n.label, n.name, tuple(n.properties["mkeys"])) for n in EXTRACTORS["cs"](source, "P.cs").nodes
             if n.label != "Module"}
    assert nodes == {("Class", "P", ("App:P",)), ("Function", "Main", ("App:P",))}


def test_nodes_carry_their_project_and_path_pins_what_it_sees(tmp_path):
    """A Class and Function carry their project's directory as `proj` ("."
    at the repository root, "" with no project); a path-pinned row of a
    file in a project carries its `visible` projects (its own and what its
    references reach), a row pinned to its own file none, and a file of no
    project none at all."""
    from devgraph.indexer.resolver_config import ResolverConfig

    def project(rel, *references):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>' + "".join(
            f'<ProjectReference Include="{each}" />' for each in references
        ) + "</ItemGroup></Project>")

    project("src/App/App.csproj", r"..\Web\Web.csproj")
    project("src/Web/Web.csproj", "../Core/Core.csproj")
    project("src/Core/Core.csproj")
    source = "namespace App;\nclass P { void Main() { Svc.Run(); Help(); } void Help() {} }\n"

    def extract(rel):
        result = extract_csharp_file(source, rel, "test_repo", ResolverConfig(tmp_path))
        projs = {n.properties.get("proj") for n in result.nodes if n.label != "Module"}
        calls = {r.to_name: r.visible for r in result.relationships if r.rel_type == "CALLS"}
        return projs, calls

    assert extract("src/App/P.cs") == ({"src/App"}, {"Run": ["src/App", "src/Core", "src/Web"], "Help": None})
    assert extract("loose/P.cs") == ({""}, {"Run": None, "Help": None})
    project("Root.csproj")
    assert extract("loose/P.cs") == ({"."}, {"Run": ["."], "Help": None})


def test_all_nodes_scoped_to_repo():
    """Test that all extracted nodes carry the correct repo_id."""
    source_code = """
public class MyClass
{
    public void MyMethod() {}
}
"""

    repo_id = "test_repo_id_123"
    result = extract_csharp_file(source_code, "test_module.cs", repo_id)

    for node in result.nodes:
        assert node.repo_id == repo_id
    for rel in result.relationships:
        assert rel.repo_id == repo_id


def test_calls_edge_for_bare_method_call():
    """A bare call inside a method body produces a CALLS edge to the target."""
    source_code = """
public class C
{
    public void Helper() {}

    public void Caller()
    {
        Helper();
    }
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Caller", "Helper") in calls


def test_calls_edge_for_method_call_via_this():
    """this.Method()/obj.Method() resolves to the method's simple name — no
    type info is available, so this links by name, not by resolved type."""
    source_code = """
public class Service
{
    public void Process()
    {
        this.Helper();
    }

    public void Helper() {}
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Process", "Helper") in calls


def test_calls_edge_carries_caller_class():
    """CALLS edges from a method body carry a caller_class property (the
    enclosing class's name)."""
    source_code = """
public class Service
{
    public void Process()
    {
        this.Helper();
    }

    public void Helper() {}
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    calls_by_pair = {
        (r.from_name, r.to_name): r.properties for r in result.relationships if r.rel_type == "CALLS"
    }
    assert calls_by_pair[("Process", "Helper")] == {"caller_class": "Service", "confidence": "resolved"}


def test_calls_edge_not_hoisted_into_local_function():
    """A call inside a local function is not hoisted to the enclosing method
    (the walk stops descending at local_function_statement, same treatment
    Python gives a nested def)."""
    source_code = """
public class C
{
    public void Outer()
    {
        void LocalFn()
        {
            DeepCall();
        }
        OuterCall();
    }
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    calls = {(r.from_name, r.to_name) for r in result.relationships if r.rel_type == "CALLS"}
    assert ("Outer", "OuterCall") in calls
    assert ("Outer", "DeepCall") not in calls


def test_calls_targets_are_function_label():
    source_code = """
public class C
{
    public void A() { B(); }
    public void B() {}
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    calls_rel = next(r for r in result.relationships if r.rel_type == "CALLS")
    assert calls_rel.to_label == "Function"


def test_method_xml_doc_extracted_into_description_and_full():
    source_code = """
public class C
{
    /// <summary>
    /// Say hello to someone.
    /// </summary>
    public string Greet(string name)
    {
        return name;
    }
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "Greet")
    assert func.properties["description"] == "Say hello to someone."
    assert "Say hello to someone." in func.properties["docstring_full"]


def test_class_xml_doc_extracted():
    source_code = """
/// <summary>
/// A simple class.
/// </summary>
public class Foo
{
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    assert cls.properties["description"] == "A simple class."


def test_no_doc_comment_means_no_description_property():
    source_code = """
public class C
{
    public void Bare() {}
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "Bare")
    assert "description" not in func.properties
    assert "docstring_full" not in func.properties


def test_non_doc_comment_is_not_treated_as_docstring():
    """A plain `//` comment (not `///`) preceding a declaration must not be
    picked up as an XML doc comment."""
    source_code = """
public class C
{
    // just a regular comment, not a doc comment
    public void Bare() {}
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    func = next(n for n in result.nodes if n.label == "Function" and n.name == "Bare")
    assert "description" not in func.properties


def test_class_extends_and_implements_both_emit_extends():
    """Base class AND implemented interfaces both produce EXTENDS edges —
    tree-sitter-c-sharp's base_list doesn't distinguish them, so (like
    Python's name-based CALLS) this over-links rather than under-links."""
    source_code = """
public class Foo : BaseClass, IDisposable, IComparable
{
}
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    extends_targets = {r.to_name for r in result.relationships if r.rel_type == "EXTENDS"}
    assert extends_targets == {"BaseClass", "IDisposable", "IComparable"}


def test_struct_and_record_produce_class_nodes():
    source_code = """
public struct PointStruct
{
    public int X;
}

public record PersonRecord(string Name, int Age);
"""
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    node_names = {(n.label, n.name) for n in result.nodes}
    assert ("Class", "PointStruct") in node_names
    assert ("Class", "PersonRecord") in node_names


def test_function_and_class_get_start_end_line():
    source_code = "public class Foo\n{\n    public void Method() {}\n}\n"
    result = extract_csharp_file(source_code, "x.cs", "test_repo")
    cls = next(n for n in result.nodes if n.label == "Class" and n.name == "Foo")
    method = next(n for n in result.nodes if n.label == "Function" and n.name == "Method")
    assert cls.properties["start_line"] == 1
    assert cls.properties["end_line"] == 4
    assert method.properties["start_line"] == 3
