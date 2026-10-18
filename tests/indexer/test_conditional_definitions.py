"""Definitions under compound statements, classes defined in functions and
Java enum members are Functions and Classes with their owners, and their
calls are their own (audit 7, item 1). Fixture code is fictional."""

import textwrap

import pytest

from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.python.extractor import extract_python_file


def _python(source: str, path: str = "app/mod.py"):
    return extract_python_file(textwrap.dedent(source), path, "repo")


def _symbols(result) -> set[tuple[str, str, str]]:
    return {(n.label, n.name, n.properties["owner"]) for n in result.nodes if n.label in ("Class", "Function")}


def _calls(result) -> set[tuple[str, str, str, str | None, str | None]]:
    return {
        (r.from_name, r.from_owner, r.to_name, r.to_file, r.to_owner)
        for r in result.relationships if r.rel_type == "CALLS"
    }


# Each compound statement, holding a def at module, class and function level.
COMPOUNDS = {
    "if": "if FLAG:\n{body}\nelif OTHER:\n    pass\nelse:\n    pass\n",
    "elif": "if FLAG:\n    pass\nelif OTHER:\n{body}\n",
    "else": "if FLAG:\n    pass\nelse:\n{body}\n",
    "try": "try:\n{body}\nexcept ImportError:\n    pass\n",
    "except": "try:\n    pass\nexcept ImportError:\n{body}\n",
    "try-else": "try:\n    pass\nexcept ImportError:\n    pass\nelse:\n{body}\n",
    "finally": "try:\n    pass\nfinally:\n{body}\n",
    "with": "with lock:\n{body}\n",
    "for": "for item in items:\n{body}\n",
    "for-else": "for item in items:\n    pass\nelse:\n{body}\n",
    "while": "while running:\n{body}\n",
    "match": "match mode:\n    case 1:\n{body2}\n",
    "nested": "if FLAG:\n    try:\n        with lock:\n{body3}\n    except OSError:\n        pass\n",
}

DEF = "def inner():\n    return helper()"


def _wrap(template: str, text: str, indent: int) -> str:
    def at(depth):
        return textwrap.indent(text, " " * depth)

    return textwrap.indent(
        template.format(body=at(4), body2=at(8), body3=at(12)), " " * indent,
    )


@pytest.mark.parametrize("kind", sorted(COMPOUNDS))
def test_a_def_under_a_compound_statement_is_its_scopes_own(kind):
    template = COMPOUNDS[kind]
    source = (
        "def helper():\n    return 1\n\n\n"
        + _wrap(template, DEF, 0)
        + "\n\nclass K:\n" + _wrap(template, DEF.replace("()", "(self)", 1), 4)
        + "\n\ndef outer():\n" + _wrap(template, DEF, 4) + "    return inner()\n"
    )
    result = extract_python_file(source, "app/mod.py", "repo")
    assert {("Function", "inner", ""), ("Function", "inner", "K"), ("Function", "inner", "outer")} <= _symbols(result)
    calls = _calls(result)
    # The calls inside each are its own, resolved in the file.
    for owner in ("", "K", "outer"):
        assert ("inner", owner, "helper", "app/mod.py", None) in calls
    # A call to a conditional def of the enclosing scope resolves in the file.
    assert ("outer", "", "inner", "app/mod.py", None) in calls


def test_a_class_defined_in_a_function_and_its_methods_are_owned_by_it():
    result = _python("""\
        def helper():
            return 1


        def test_it():
            class Fake:
                def run(self):
                    return self.go()

                def go(self):
                    return helper()

            if True:
                class Later:
                    pass
            return Fake().run()
        """)
    assert {
        ("Class", "Fake", "test_it"), ("Function", "run", "test_it.Fake"), ("Function", "go", "test_it.Fake"),
        ("Class", "Later", "test_it"),
    } <= _symbols(result)
    calls = _calls(result)
    assert ("run", "test_it.Fake", "go", "app/mod.py", "test_it.Fake") in calls
    assert ("go", "test_it.Fake", "helper", "app/mod.py", None) in calls
    assert ("test_it", "", "run", "app/mod.py", "test_it.Fake") in calls
    contains = {(r.from_name, r.from_owner, r.to_name, r.to_owner) for r in result.relationships
                if r.rel_type == "CONTAINS"}
    assert ("test_it", "", "Fake", "test_it") in contains
    assert ("Fake", "test_it", "run", "test_it.Fake") in contains


def test_conditional_duplicates_are_one_node_spanning_both():
    source = """\
        try:
            import json

            def dumps(value):
                return json.dumps(value)
        except ImportError:
            @cached
            def dumps(value):
                \"\"\"The fallback.\"\"\"
                return fallback(value)


        def fallback(value):
            return str(value)
        """
    result = _python(source)
    nodes = [n for n in result.nodes if n.name == "dumps"]
    assert len(nodes) == 1
    props = nodes[0].properties
    assert (props["owner"], props["start_line"], props["end_line"]) == ("", 4, 10)
    assert props["decorators"] == ["cached"]
    assert props["description"] == "The fallback."
    # Both bodies' calls come from the one node, and the same text always
    # gives the same node.
    assert ("dumps", "", "fallback", "app/mod.py", None) in _calls(result)
    assert _python(source).to_dict() == result.to_dict()


def test_a_property_and_its_setter_are_one_node():
    result = _python("""\
        class K:
            @property
            def value(self):
                return 1

            @value.setter
            def value(self, new):
                pass
        """)
    (node,) = [n for n in result.nodes if n.name == "value"]
    assert node.properties["decorators"] == ["property", "value.setter"]
    assert (node.properties["start_line"], node.properties["end_line"]) == (3, 8)


def test_a_from_imported_conditional_def_is_a_top_level_target():
    result = _python("""\
        from app.compat import dumps

        def save():
            return dumps(1)
        """, "app/use.py")
    assert ("save", "", "dumps", "app/compat.py", "") in _calls(result)


def test_java_enum_methods_and_constant_bodies_are_the_enums_members():
    result = extract_java_file(textwrap.dedent("""\
        package app;

        public enum Status {
            ACTIVE(label()) {
                @Override
                boolean isActive() { return check(); }
            },
            DONE("done");

            Status(String name) {}

            boolean isActive() { return false; }

            static Status parse(String s) { return valueOf(s); }

            static String label() { return "active"; }

            boolean check() { return true; }

            enum Inner { A; void go() { parse("x"); } }
        }
        """), "app/Status.java", "repo")
    assert {
        ("Class", "Status", ""), ("Function", "Status", "Status"), ("Function", "isActive", "Status"),
        ("Function", "parse", "Status"), ("Function", "label", "Status"), ("Function", "check", "Status"),
        ("Class", "Inner", "Status"), ("Function", "go", "Status.Inner"),
    } <= _symbols(result)
    calls = _calls(result)
    assert ("isActive", "Status", "check", "app/Status.java", "Status") in calls
    # A constant's arguments are the enum's own calls.
    assert ("Status", "", "label", "app/Status.java", "Status") in calls
    assert ("go", "Status.Inner", "parse", "app/Status.java", "Status") in calls
