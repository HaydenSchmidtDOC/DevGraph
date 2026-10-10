"""The CALLS rows every language extractor writes (devgraph/indexer/calls.py).

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md.
"""

from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, call_rows


def rows(pins, bare=False, no_self=False, caller_class=None):
    return [
        (r.to_file, r.properties["confidence"], r.exclude, r.no_self, r.properties.get("caller_class"))
        for r in call_rows("Function", "main", "f", set(pins), bare, caller_class, "app/main.ts", "repo", no_self)
    ]


def test_file_pins_are_resolved_and_a_directory_leaves_them_and_this_file_out():
    assert rows({"lib/b.ts", "lib/a.ts", "lib/"}) == [
        ("lib/a.ts", "resolved", None, False, None),
        ("lib/b.ts", "resolved", None, False, None),
        ("lib/", "package", ["app/main.ts", "lib/a.ts", "lib/b.ts"], False, None),
    ]


def test_a_bare_row_only_when_nothing_resolved():
    assert rows(set(), bare=True) == [(None, "name", None, False, None)]
    assert rows(set(), bare=False) == []
    assert rows({"lib/a.ts"}, bare=True) == [("lib/a.ts", "resolved", None, False, None)]


def test_no_self_rides_only_on_the_bare_row():
    assert rows(set(), bare=True, no_self=True) == [(None, "name", None, True, None)]
    assert rows({"lib/a.ts"}, bare=True, no_self=True) == [("lib/a.ts", "resolved", None, False, None)]


def test_caller_class_is_kept():
    assert rows({"lib/a.ts"}, caller_class="Svc") == [("lib/a.ts", "resolved", None, False, "Svc")]


def test_every_language_has_a_stoplist():
    assert set(STOP_METHODS) == {"python", "js", "java", "kotlin", "csharp", "go", "rust", "cpp"}
    assert {"get", "join", "append"} <= STOP_METHODS["python"]
    assert {"push", "find", "getItem"} <= STOP_METHODS["js"]
    assert {"new", "from"}.isdisjoint(STOP_METHODS["rust"])  # Vec::new is a STOP_TYPES matter
    assert {"Vec", "String", "Box"} <= STOP_TYPES["rust"]
