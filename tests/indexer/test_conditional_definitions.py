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


def test_conditional_duplicates_are_one_node_the_first_one():
    source = """\
        try:
            import json

            def dumps(value):
                return json.dumps(value)
        except ImportError:
            def helper():
                return 0

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
    # The first definition's lines and properties: the node never spans
    # `helper`, defined between the two.
    assert (props["owner"], props["start_line"], props["end_line"]) == ("", 4, 5)
    assert props["decorators"] == [] and "description" not in props
    # Both bodies' calls come from the one node, and the same text always
    # gives the same node.
    assert ("dumps", "", "fallback", "app/mod.py", None) in _calls(result)
    assert _python(source).to_dict() == result.to_dict()


def test_a_merged_definition_never_contains_one_between():
    from devgraph.indexer.symbols import extract_symbols

    source = textwrap.dedent("""\
        try:
            from fast import Compat
        except ImportError:
            class Compat:
                pass

        def util():
            pass

        if not hasattr(Compat, "x"):
            class Compat(Compat):
                def x(self):
                    pass
        """)
    result = extract_python_file(source, "app/mod.py", "repo")
    (compat,) = [n for n in result.nodes if n.name == "Compat"]
    assert (compat.properties["start_line"], compat.properties["end_line"]) == (4, 5)
    # `class Compat(Compat)` extends no other class of the file (not itself).
    assert [r for r in result.relationships if r.rel_type == "EXTENDS"] == []
    containers = {s.name: s.container for s in extract_symbols("app/mod.py", source)}
    assert containers["util"] is None


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
    assert node.properties["decorators"] == ["property"]
    assert (node.properties["start_line"], node.properties["end_line"]) == (3, 4)


def test_a_function_local_class_is_not_the_module_level_class_of_its_name():
    result = _python("""\
        class User:
            def save(self):
                pass


        def factory():
            class User:
                def save(self):
                    pass
            u = User()
            u.save()
            return u


        class Admin(User):
            def go(self):
                self.save()


        def top():
            User().save()
        """)
    calls = _calls(result)
    assert ("factory", "", "save", "app/mod.py", "factory.User") in calls
    assert ("go", "Admin", "save", "app/mod.py", "User") in calls
    assert ("top", "", "save", "app/mod.py", "User") in calls
    keys = {(n.properties["owner"], tuple(n.properties.get("mkeys", ()))) for n in result.nodes if n.name == "save"}
    # Only the module-level User's save is inherited by Admin.
    assert keys == {("User", ("<Admin",)), ("factory.User", ())}
    (extends,) = [r for r in result.relationships if r.rel_type == "EXTENDS"]
    assert (extends.to_name, extends.to_file, extends.to_owner) == ("User", "app/mod.py", "")


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
