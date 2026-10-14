"""`symbols`: in-memory symbol extraction and the C3 identity and diff rules."""

import time

import pytest

from devgraph.indexer import symbols
from devgraph.indexer.dispatch import _CODE_ROUTES
from devgraph.indexer.symbols import (
    EXTRACTORS,
    TooManySymbols,
    decode_source,
    diff_symbols,
    extract_symbols,
    language_for,
)


def keys(entries):
    return {(e["kind"], e["container"], e["name"]) for e in entries}


def test_every_code_route_has_an_extractor():
    assert set(EXTRACTORS) == set(_CODE_ROUTES.values())


def test_language_for_routes_by_suffix():
    assert language_for("web/util.ts") == "js"
    assert language_for("src/a.py") == "py"
    assert language_for("geo.hpp") == "cpp"
    assert language_for("README.md") is None
    assert language_for("notes.c") is None
    assert language_for("Makefile") is None


def test_decode_source_normalises_crlf_only():
    assert decode_source(b"a\r\nb\rc\n\xff") == "a\nb\rc\n\xff".replace("\xff", "ÿ")


def test_decode_source_agrees_with_the_indexer():
    from devgraph.indexer.source_text import decode_source as indexer_decode

    latin = "# coding: latin-1\ndef café():\n    pass\n".encode("latin-1")
    assert decode_source(latin, "pkg/a.py") == indexer_decode(latin, python=True)
    cp1252 = "class Café {}\r\n".encode("cp1252")
    assert decode_source(cp1252, "A.java") == "class Café {}\n"


def test_body_slicing_ignores_form_feeds():
    old = "def a():\n    return 1\n\f\ndef b():\n    return 2\n"
    new = "def a():\n    return 10\n\f\ndef b():\n    return 2\n"
    b = next(s for s in extract_symbols("m.py", new) if s.name == "b")
    assert (b.start_line, b.end_line) == (4, 5)
    assert b.body == "def b():\n    return 2"
    added, removed, changed = diff_symbols(extract_symbols("m.py", old), extract_symbols("m.py", new))
    assert added == [] and removed == []
    assert keys(changed) == {("Function", None, "a")}


def test_container_is_not_self():
    got = {(s.kind, s.name): s.container for s in extract_symbols("m.py", "class A: pass\n")}
    assert got == {("Class", "A"): None}
    got = {(s.kind, s.name): s.container for s in extract_symbols("m.py", "class A: \n  def f(self): pass\n")}
    assert got == {("Class", "A"): None, ("Function", "f"): "A"}
    # A one-line method sharing the class's exact lines.
    src = "public class A { void f() {} }\n"
    got = {(s.kind, s.name): s.container for s in extract_symbols("A.java", src)}
    assert got == {("Class", "A"): None, ("Function", "f"): "A"}


def test_innermost_container_and_ordinals():
    src = "class A:\n    class B:\n        def f(self): pass\n        def f(self): pass\n    def f(self): pass\n"
    got = {(s.kind, s.container, s.name, s.ordinal) for s in extract_symbols("m.py", src)}
    assert got == {
        ("Class", None, "A", 0),
        ("Class", "A", "B", 0),
        ("Function", "B", "f", 0),
        ("Function", "B", "f", 1),
        ("Function", "A", "f", 0),
    }


def test_changed_entry_carries_both_sides_lines():
    old = "def f():\n    return 1\n"
    new = "\n" * 5 + "def f():\n    return 2\n"
    added, removed, changed = diff_symbols(extract_symbols("m.py", old), extract_symbols("m.py", new))
    assert added == removed == []
    assert changed == [
        {
            "kind": "Function",
            "name": "f",
            "container": None,
            "start_line": 6,
            "end_line": 7,
            "old_start_line": 1,
            "old_end_line": 2,
        }
    ]


def test_diff_lists_are_sorted_by_line_kind_name():
    new = "def z():\n    pass\nclass Y:\n    pass\ndef a():\n    pass\n"
    added, removed, changed = diff_symbols([], extract_symbols("m.py", new))
    assert [(e["start_line"], e["name"]) for e in added] == [(1, "z"), (3, "Y"), (5, "a")]
    assert removed == changed == []
    assert set(added[0]) == {"kind", "name", "container", "start_line", "end_line"}


def test_equal_range_classes_never_contain_each_other():
    got = {s.name: s.container for s in extract_symbols("A.java", "class A { class B { void f() {} } }\n")}
    assert got == {"A": None, "B": "A", "f": "B"}
    got = {s.name: s.container for s in extract_symbols("a.ts", "class A {} class B {}\n")}
    # Equal line ranges cannot tell nesting from siblings; the earlier one is the outer one, never a cycle.
    assert got == {"A": None, "B": "A"}


def test_container_search_is_not_quadratic(monkeypatch):
    text = "".join(f"class C{i}:\n    def m(self): pass\n" for i in range(20_000))
    result = EXTRACTORS["py"](text, "gen.py")
    monkeypatch.setitem(symbols.EXTRACTORS, "py", lambda source, path: result)  # time our part only
    started = time.perf_counter()
    got = extract_symbols("gen.py", text)
    elapsed = time.perf_counter() - started
    assert len(got) == 40_000
    assert got[-1].container == "C19999"
    assert elapsed < 1.0, elapsed


def test_too_many_symbols_raises_before_the_container_search():
    with pytest.raises(TooManySymbols):
        extract_symbols("m.py", "def a(): pass\ndef b(): pass\ndef c(): pass\n", max_symbols=2)
    assert len(extract_symbols("m.py", "def a(): pass\ndef b(): pass\n", max_symbols=2)) == 2


JAVA_OVERLOADS = "class S {\n  int run(int a) {\n    return 1;\n  }\n  int run(String b) {\n    return 2;\n  }\n}\n"
JAVA_REORDERED = "class S {\n  int run(String b) {\n    return 2;\n  }\n  int run(int a) {\n    return 1;\n  }\n}\n"
CS_OVERLOADS = "class S {\n  int Run(int a) {\n    return 1;\n  }\n  int Run(string b) {\n    return 2;\n  }\n}\n"
CS_INSERTED = (
    "class S {\n  int Run(long c) {\n    return 3;\n  }\n"
    "  int Run(int a) {\n    return 1;\n  }\n  int Run(string b) {\n    return 2;\n  }\n}\n"
)


def test_reordered_overloads_are_not_changed():
    added, removed, changed = diff_symbols(extract_symbols("S.java", JAVA_OVERLOADS), extract_symbols("S.java", JAVA_REORDERED))
    assert added == removed == []
    assert keys(changed) == {("Class", None, "S")}  # the class text moved around; no overload did


def test_overload_inserted_first_is_added_not_changed():
    added, removed, changed = diff_symbols(extract_symbols("S.cs", CS_OVERLOADS), extract_symbols("S.cs", CS_INSERTED))
    assert [(e["name"], e["start_line"]) for e in added] == [("Run", 2)]
    assert removed == []
    assert keys(changed) == {("Class", None, "S")}


def test_duplicates_fall_back_to_order_after_identical_bodies():
    old = "class S {\n  int run(int a) {\n    return 1;\n  }\n  int run(String b) {\n    return 2;\n  }\n}\n"
    new = "class S {\n  int run(int a) {\n    return 10;\n  }\n  int run(String b) {\n    return 2;\n  }\n}\n"
    added, removed, changed = diff_symbols(extract_symbols("S.java", old), extract_symbols("S.java", new))
    assert added == removed == []
    assert [(e["kind"], e["start_line"], e["old_start_line"]) for e in changed] == [("Class", 1, 1), ("Function", 2, 2)]
