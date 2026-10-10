"""Every code extractor pins the sources of its own edges and stamps itself as their writer.

See `devgraph.indexer.common.own_edges` and the graph-accuracy design (G1).
"""

import importlib

import pytest

from devgraph.indexer.common import own_edges
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file

REPO = "repo"

# (module, extract function, file, source). Each source has `main` calling
# `helper`, a subtype of `Base`, and, where the language has one, a nested function.
_SOURCES = {
    "python": (
        "extract_python_file",
        "src/app.py",
        "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n\n\ndef helper():\n    pass\n\n\n"
        "def main():\n    def inner():\n        pass\n    helper()\n",
    ),
    "jsts": (
        "extract_js_file",
        "src/app.ts",
        "class Base {}\nclass Sub extends Base {}\nfunction helper() {}\n"
        "function main() {\n  function inner() {}\n  helper();\n}\n",
    ),
    "java": (
        "extract_java_file",
        "src/Sub.java",
        "class Base {}\nclass Sub extends Base {\n  void helper() {}\n  void main() { helper(); }\n}\n",
    ),
    "csharp": (
        "extract_csharp_file",
        "src/Sub.cs",
        "class Base {}\nclass Sub : Base {\n  void helper() {}\n  void main() { helper(); }\n}\n",
    ),
    "cpp": (
        "extract_cpp_file",
        "src/app.cpp",
        "class Base {};\nclass Sub : public Base {};\nvoid helper() {}\nint main() { helper(); return 0; }\n",
    ),
    "go": (
        "extract_go_file",
        "src/app.go",
        "package main\n\ntype Base struct{}\n\ntype Sub struct {\n\tBase\n}\n\nfunc helper() {}\n\n"
        "func main() {\n\thelper()\n}\n",
    ),
    "rust": (
        "extract_rust_file",
        "src/app.rs",
        "trait Base {}\nstruct Sub;\nimpl Base for Sub {}\nfn helper() {}\n"
        "fn main() {\n    fn inner() {}\n    helper();\n}\n",
    ),
    "kotlin": (
        "extract_kotlin_file",
        "src/App.kt",
        "open class Base\nclass Sub : Base()\nfun helper() {}\nfun main() {\n    fun inner() {}\n    helper()\n}\n",
    ),
}


def _rel_key(rel):
    return (rel.from_label, rel.from_name, rel.rel_type, rel.to_label, rel.to_name)


@pytest.mark.parametrize("lang", sorted(_SOURCES))
def test_edges_owned_by_the_parsed_file(lang, monkeypatch):
    module = importlib.import_module(f"devgraph.indexer.{lang}.extractor")
    function_name, path, source = _SOURCES[lang]
    extract = getattr(module, function_name)

    result = extract(source, path, REPO)
    with monkeypatch.context() as patched:
        patched.setattr(module, "own_edges", lambda result, file_path: result, raising=False)
        raw = extract(source, path, REPO)

    owned = {(n.label, n.name) for n in result.nodes if n.properties.get("file") == path}
    edges = {_rel_key(rel): rel for rel in result.relationships}
    assert any(k[2] == "CALLS" and k[1] == "main" and k[4] == "helper" for k in edges)
    assert any(k[2] == "EXTENDS" and k[1] == "Sub" and k[4] == "Base" for k in edges)
    for rel in result.relationships:
        assert rel.origin == path, rel
        if (
            rel.rel_type in ("CALLS", "EXTENDS", "CONTAINS")
            and rel.from_label in ("Function", "Class")
            and (rel.from_label, rel.from_name) in owned
        ):
            assert rel.from_file == path, rel
    assert [rel.to_file for rel in result.relationships] == [rel.to_file for rel in raw.relationships]


def test_unowned_sources_stay_bare():
    python = extract_python_file("def helper():\n    pass\n\n\nhelper()\n", "m.py", REPO)
    module_calls = [r for r in python.relationships if r.rel_type == "CALLS" and r.from_label == "Module"]
    assert module_calls and all(r.from_file is None for r in module_calls)

    rust = extract_rust_file("impl Display for Foo {}\n", "conv.rs", REPO)
    (extends,) = [r for r in rust.relationships if r.rel_type == "EXTENDS"]
    assert (extends.from_name, extends.from_file, extends.origin) == ("Foo", None, "conv.rs")

    cpp = extract_cpp_file("void baz() {}\n\nvoid Foo::bar() { baz(); }\n", "foo.cpp", REPO)
    (call,) = [r for r in cpp.relationships if r.rel_type == "CALLS"]
    assert (call.from_name, call.from_file) == ("bar", "foo.cpp")
    (stub,) = [n for n in cpp.nodes if n.label == "Class" and n.name == "Foo"]
    assert "file" not in stub.properties
    # Known gap (design: out of scope): the stub's CONTAINS keeps the
    # from_file the extractor already sets, which the stub doesn't have.
    (contains,) = [r for r in cpp.relationships if r.rel_type == "CONTAINS" and r.from_name == "Foo"]
    assert contains.from_file == "foo.cpp"


def test_own_edges_keeps_an_already_set_from_file():
    from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

    result = ExtractionResult(
        nodes=[GraphNode("Function", REPO, "f", {"file": "a.py"})],
        relationships=[
            GraphRelationship("Function", "f", "CALLS", "Function", "g", REPO, from_file="elsewhere.py"),
            GraphRelationship("Module", "a.py", "CALLS", "Function", "f", REPO),
        ],
    )
    own_edges(result, "a.py")
    assert [(r.from_file, r.origin) for r in result.relationships] == [("elsewhere.py", "a.py"), (None, "a.py")]
    assert result.relationships[0].to_dict()["origin"] == "a.py"
