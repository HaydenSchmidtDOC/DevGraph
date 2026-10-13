"""Path pins: a relationship row's `to_file` that names a set of files (a
directory, a path suffix, a suffix directory, a recursive prefix) rather than
one, its confidence the pin kind's, and the precedence that gives a file two
pins match one edge from the stronger pin. Unit checks of
devgraph/indexer/common.py, a randomized check that the engine's Cypher
agrees with `pin_matches`, and live writes and relinks per pin kind.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md
(Engine: path pins).
"""

import random
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.graph.schema import MAX_SCOPE_KEYS, pin_kind, scoped_pin, split_pin
from devgraph.indexer import dispatch
from devgraph.indexer.calls import call_rows, import_rows
from devgraph.indexer.common import (
    best_pin,
    module_pin_name,
    name_ref_properties,
    parse_name_ref,
    pin_excludes,
    pin_matches,
)
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

# --- unit ----------------------------------------------------------------------


@pytest.mark.parametrize(("pin", "kind"), [
    (None, "name"), ("", "fileless"), ("a/b.go", "file"), ("a!b.go", "file"), ("a/b/.", "dir"), (".", "dir"),
    ("/a/B.java", "suffix"), ("/a/b/.", "suffix_dir"), ("a/b/", "prefix"), ("a/b/.!_test.go", "dir"),
    ("/a/.!_test.go", "suffix_dir"), ("a/b/.~", "loose_dir"), ("a/b/.~?.go!_test.go", "loose_dir"),
    ("/a/B.java^", "suffix"), ("/a/.?.kt,.java^t", "suffix_dir"), ("a~", "file"), ("a?b.go", "file"),
    # `@` match keys, last; a name starting with "@" (an npm scope) is no flag: a key holds ":".
    ("/.?.cs^@Shop:User,.:User", "suffix_dir"), ("a/b/.@N:+T", "dir"), ("a/@b.go", "file"), ("/@s/x.ts", "suffix"),
    ("a/@s/", "prefix"), ("@s/.", "dir"), ("/a/.?.cs@bad", "suffix"),
])
def test_pin_kinds(pin, kind):
    assert pin_kind(pin) == kind


@pytest.mark.parametrize(("pin", "path", "matches"), [
    ("a/b.go", "a/b.go", True), ("a/b.go", "x/a/b.go", False),
    ("a/b/.", "a/b/c.go", True), ("a/b/.", "a/b/c/d.go", False), ("a/b/.", "x/a/b/c.go", False),
    (".", "c.go", True), (".", "a/c.go", False),
    ("a/b/.!_test.go", "a/b/c_test.go", False), ("a/b/.!_test.go", "a/b/c.go", True),
    ("/a/B.java", "x/a/B.java", True), ("/a/B.java", "a/B.java", True), ("/a/B.java", "xa/B.java", False),
    ("/a/b/.", "x/a/b/c.kt", True), ("/a/b/.", "a/b/c.kt", True), ("/a/b/.", "a/b/c/d.kt", False),
    ("/a/b/.", "xa/b/c.kt", False),
    ("a/b/", "a/b/c/d.py", True), ("a/b/", "a/bc/d.py", False),
    ("", "", True), ("", "a.py", False), (None, "a.py", True),
    # Flags: a language's suffixes, no vendored segment, a package-confidence directory.
    ("a/b/.?.go", "a/b/c.go", True), ("a/b/.?.go", "a/b/c.py", False), ("a/b/.?.go!_test.go", "a/b/c_test.go", False),
    ("/m/.?.kt,.java", "x/m/A.java", True), ("/m/.?.kt,.java", "x/m/A.md", False),
    ("/m/C.java^", "core/m/C.java", True), ("/m/C.java^", "third_party/u/m/C.java", False),
    ("/m/C.java^", "a/vendor/m/C.java", False), ("/m/C.java", "third_party/u/m/C.java", True),
    # Only the source root before the match counts: a package named `vendor` or `test` is fine.
    ("/vendor/C.java^t", "core/vendor/C.java", True), ("/m/C.java^t", "core/src/test/java/m/C.java", False),
    ("/m/C.java^", "core/src/test/java/m/C.java", True), ("/test/.?.kt^t", "src/main/kotlin/test/a.kt", True),
    ("/m/.?.kt^t", "src/test/kotlin/m/a.kt", False), ("/m/.^", "vendor/m/a.kt", False),
    # ^t leaves out conventional test roots only, not any directory named test.
    ("/m/C.java^t", "test/m/C.java", True), ("/m/C.java^t", "tools/test/x/m/C.java", True),
    ("/m/C.java^t", "lib/test/java/m/C.java", False), ("/m/C.java^t", "app/src/androidTest/java/m/C.java", False),
    ("/m/C.java^t", "src/testFixtures/kotlin/m/C.java", False),
    ("a/b/.~", "a/b/c.go", True), ("a/b/.~?.go", "a/b/c/d.go", False),
])
def test_pin_matches(pin, path, matches):
    assert pin_matches(pin, path) is matches


@pytest.mark.parametrize(("pin", "keys", "matches"), [
    ("/.?.cs^@N:A", ["N:A"], True), ("/.?.cs^@N:A", ["M:A", "N:A"], True), ("/.?.cs^@N:A,.:A", [".:A"], True),
    ("/.?.cs^@N:A", ["N:B"], False), ("/.?.cs^@N:A", [], False), ("/.?.cs^", [], True), ("/.?.cs^", ["N:B"], True),
])
def test_an_at_flag_matches_a_node_holding_one_of_its_keys(pin, keys, matches):
    assert pin_matches(pin, "x/User.cs", keys) is matches
    assert pin_matches(pin, "x/User.md", keys) is False


def test_at_keys_are_canonical_and_capped():
    assert split_pin("/.?.cs^@b:X,a:Y,b:X").scope == ("a:Y", "b:X")
    assert split_pin("/a/.?.cs!_g.cs@N:T") == split_pin("/a/.?.cs!_g.cs")._replace(scope=("N:T",))
    assert split_pin("/a/.?.cs@N:T").only == (".cs",)  # "@" ends the `?` suffixes
    assert scoped_pin("/.?.cs^", ["b:X", "a:Y", "b:X"]) == "/.?.cs^@a:Y,b:X"
    assert scoped_pin("/.?.cs^", []) == "/.?.cs^"
    keys = [f"N:T{i}" for i in range(MAX_SCOPE_KEYS)]
    assert scoped_pin("/.?.cs^", keys + keys[:5]).count(",") == MAX_SCOPE_KEYS - 1  # 32 once deduplicated
    assert scoped_pin("/.?.cs^", [*keys, "N:X"]) == "/.?.cs^"  # more than 32: the pin alone


def test_best_pin_tests_at_keys_against_the_nodes():
    pins = {"lib/a.cs", "/.?.cs^@N:A"}
    assert best_pin(pins, "x/a.cs", "app.cs", ["N:A"]) == "/.?.cs^@N:A"
    assert best_pin(pins, "x/a.cs", "app.cs", ["N:B"]) is None
    assert best_pin(pins, "lib/a.cs", "app.cs", ["N:A"]) == "lib/a.cs"
    # An `@` exclusion leaves out only the nodes holding its keys.
    assert best_pin({"x/.@N:A", "/.?.cs"}, "x/a.cs", "app.cs", ["N:B"]) == "/.?.cs"
    assert best_pin({"x/.@N:A", "/.?.cs"}, "x/a.cs", "app.cs", ["N:A"]) == "x/.@N:A"


def _cs_rows(pins):
    rows = [r.to_dict() for r in call_rows("Function", "main", "f", set(pins), False, None, "app.cs", "r")]
    for row in rows:
        row["from_file"] = row["origin"] = "app.cs"
    return rows


def test_name_refs_store_each_set_of_at_keys_once():
    """Two call sites on receivers of two types give one call two `@` pins
    with different keys: an entry per key set, each storing its keys once,
    which relink takes together again."""
    from devgraph.indexer.dispatch import _with_scopes_merged

    refs = name_ref_properties(_cs_rows({"a/f.cs", "/.?.cs^@N:A,.:A", "lib/.@N:A,.:A", "/.?.cs^@B:Y"}))["name_refs"]
    head = ["CALLS", "Function", "main", "app.cs", "Function", "f", "", ""]
    assert all(entry.count("N:A") <= 1 for entry in refs)
    assert sorted(parse_name_ref(entry) for entry in refs) == [
        (head, ["/.?.cs^@.:A,N:A", "lib/.@.:A,N:A"], "pin"),
        (head, ["/.?.cs^@B:Y"], "pin"),
        (head, ["a/f.cs"], "pin"),
    ]
    # An entry without keys has no such field, as before.
    assert [len(entry.split("\x1f")) for entry in sorted(refs)] == [11, 11, 10]
    assert _with_scopes_merged([parse_name_ref(entry) for entry in refs]) == [
        (head, ["/.?.cs^@.:A,N:A", "/.?.cs^@B:Y", "a/f.cs", "lib/.@.:A,N:A"], "pin"),
    ]


def test_relink_takes_a_calls_entries_of_different_at_keys_together():
    """A node an exact file pin and a weaker `@` pin of the same call both
    match is relinked once, through the file pin, as the writer's rows left
    it out of the `@` pin's."""
    from devgraph.indexer.dispatch import _relink_name_refs

    refs = name_ref_properties(_cs_rows({"lib/a.cs", "/lib/.@N:A", "/lib/.@N:B"}))["name_refs"]

    class Engine:
        def __init__(self):
            self.rels = []

        def find_name_refs(self, repo_id, names, skip):
            return [("app.cs", refs)]

        def upsert_relationships(self, rels):
            self.rels += rels

    engine = Engine()
    _relink_name_refs(engine, "r", {("Function", "f", "lib/a.cs", ""), ("Function", "f", "lib/b.cs", "")}, set(), {
        ("Function", "f", "lib/a.cs", ""): ("N:A",), ("Function", "f", "lib/b.cs", ""): ("N:B",),
    })
    assert sorted((rel["to_file"], rel["properties"]["confidence"]) for rel in engine.rels) == [
        ("lib/a.cs", "resolved"), ("lib/b.cs", "package"),
    ]


def test_each_pin_leaves_out_what_a_stronger_pin_matches():
    pins = {"lib/", "/a.py", "lib/.", "lib/a.py", "/m/."}
    assert pin_excludes(pins, "app.py") == {
        "lib/a.py": [],
        "lib/.": ["lib/a.py"],
        "/a.py": ["lib/.", "lib/a.py"],
        "/m/.": ["/a.py", "lib/.", "lib/a.py"],
        "lib/": ["/a.py", "/m/.", "app.py", "lib/.", "lib/a.py"],
    }
    assert best_pin(pins, "lib/a.py", "app.py") == "lib/a.py"
    assert best_pin(pins, "lib/b.py", "app.py") == "lib/."
    assert best_pin(pins, "x/a.py", "app.py") == "/a.py"
    assert best_pin(pins, "x/m/c.py", "app.py") == "/m/."
    assert best_pin(pins, "lib/sub/c.py", "app.py") == "lib/"
    assert best_pin(pins, "other/c.py", "app.py") is None
    assert best_pin({"lib/"}, "lib/x.py", "lib/x.py") is None  # a prefix never links its writer


def test_a_loose_directory_ranks_after_a_directory_at_package_confidence():
    rows = call_rows("Function", "main", "f", {"a/.~?.go", "a/.?.go", "a/f.go", "/a/f.go"}, False, None, "m.go", "r")
    assert [(r.to_file, r.properties["confidence"], r.exclude) for r in rows] == [
        ("a/f.go", "resolved", None),
        ("a/.?.go", "resolved", ["a/f.go"]),
        ("a/.~?.go", "package", ["a/.?.go", "a/f.go"]),
        ("/a/f.go", "package", ["a/.?.go", "a/.~?.go", "a/f.go"]),
    ]


def test_call_rows_take_their_pin_kinds_confidence():
    rows = call_rows("Function", "main", "f", {"lib/", "/a.py", "lib/.", "lib/a.py"}, False, None, "app.py", "r")
    assert [(r.to_file, r.properties["confidence"], r.exclude) for r in rows] == [
        ("lib/a.py", "resolved", None),
        ("lib/.", "resolved", ["lib/a.py"]),
        ("/a.py", "package", ["lib/.", "lib/a.py"]),
        ("lib/", "package", ["/a.py", "app.py", "lib/.", "lib/a.py"]),
    ]


def test_a_module_pin_seeks_its_directory_its_directory_name_or_basename():
    pins = ("a/b/.", ".", "a/b/.!_test.go", "/a/B.java", "/a/b/.?.java^")
    assert [module_pin_name(p) for p in pins] == ["a/b", "", "a/b", "B.java", "b"]
    with pytest.raises(ValueError):
        module_pin_name("a/b/")


def test_import_rows_collapse_a_files_imports():
    rows = import_rows("app/A.java", "r", {"core/m/Item.java"}, {"/m/Item.java", "core/m/."})
    assert [(r.to_name, r.to_file, (r.properties or {}).get("confidence"), r.exclude) for r in rows] == [
        ("core/m/Item.java", None, None, None),
        ("core/m", "core/m/.", "resolved", ["app/A.java", "core/m/Item.java"]),
        ("Item.java", "/m/Item.java", "package", ["app/A.java", "core/m/.", "core/m/Item.java"]),
    ]


# --- the engine's Cypher agrees with pin_matches ----------------------------------


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


#: Segments that share prefixes, so `a/b/` must not match `a/bc/`; names
#: starting with "@" (an npm scope), which a pin's `@` flag must not take.
_SEGMENTS = ["a", "ab", "b", "bc", "m", "vendor", "third_party", "test", "src", "java", "@s"]
_NAMES = ["C.java", "a.py", "a_test.go", "x.go", "ab.py", "K.kt", "@x.ts"]
#: Match keys, for a pin's `@` flag and a node's `mkeys`.
_KEYS = ["N:A", "N:B", ".:A", "N.M:+A"]


def _random_path(rng: random.Random) -> str:
    return "/".join([rng.choice(_SEGMENTS) for _ in range(rng.randint(0, 3))] + [rng.choice(_NAMES)])


def _random_pin(rng: random.Random, kinds=("file", "dir", "suffix", "suffix_dir", "prefix"), loose=True) -> str:
    folder = "/".join(rng.choice(_SEGMENTS) for _ in range(rng.randint(0, 2)))
    name = rng.choice(_NAMES)
    kind = rng.choice(kinds)
    pin = {
        "file": f"{folder}/{name}" if folder else name,
        "dir": f"{folder}/." if folder else ".",
        "suffix": f"/{folder}/{name}" if folder else f"/{name}",
        "suffix_dir": f"/{folder}/." if folder else "/a/.",
        "prefix": f"{folder}/" if folder else "b/",
    }[kind]
    if kind != "file":
        # Flags, in their order: `~` (on a directory), `?` suffixes, `^`, `!` suffix.
        if kind == "dir" and loose and rng.random() < 0.3:
            pin += "~"
        if rng.random() < 0.3:
            pin += "?" + ",".join(rng.sample([".go", ".kt", ".java", ".py"], rng.randint(1, 2)))
        if rng.random() < 0.3:
            pin += rng.choice(["^", "^t"])
        if rng.random() < 0.3:
            pin += rng.choice(["!_test.go", "!.py"])
        if rng.random() < 0.3:
            pin += "@" + ",".join(rng.sample(_KEYS, rng.randint(1, 2)))
    return pin


def _random_keys(rng: random.Random) -> list[str] | None:
    """A node's `mkeys`: none at all (an older node), or some of `_KEYS`."""
    return None if rng.random() < 0.2 else sorted(rng.sample(_KEYS, rng.randint(0, 2)))


def _random_cases(rng: random.Random, kinds, count: int, loose=True) -> list[tuple[str, list[str]]]:
    """(pin, its exclusions): files and other pins, as `pin_excludes` writes them."""
    return [
        (_random_pin(rng, kinds, loose), sorted({_random_pin(rng) for _ in range(rng.randint(0, 3))}))
        for _ in range(count)
    ]


def _expected(pin: str, exclude: list[str], path: str, keys=()) -> bool:
    keys = keys or ()
    return pin_matches(pin, path, keys) and not any(pin_matches(other, path, keys) for other in exclude)


def test_the_cypher_pin_test_agrees_with_pin_matches(engine):
    """A path pin and its exclusions, sent as `_group_rels_by_triple` sends
    them (`pin`, `ex_files`, `ex_pins`), match in Cypher exactly the nodes
    (paths and match keys) `pin_matches` says."""
    from devgraph.graph.engine import _group_rels_by_triple, _path_pin_conditions

    rng = random.Random(11)
    cases, expected = [], {}
    for i, (pin, exclude) in enumerate(_random_cases(rng, ("dir", "suffix", "suffix_dir", "prefix"), 8000)):
        path, keys = _random_path(rng), _random_keys(rng)
        (rows,) = _group_rels_by_triple([{
            "from_label": "Function", "from_name": "f", "rel_type": "CALLS", "to_label": "Function",
            "to_name": "g", "repo_id": "r", "to_file": pin, "exclude": exclude,
        }]).values()
        cases.append({"i": i, "t": rows[0]["targets"][0], "f": path, "mkeys": keys})
        expected[i] = (pin, exclude, path, keys, _expected(pin, exclude, path, keys))
    rows = engine.run_cypher(
        "UNWIND $cases AS c WITH c, c.t AS t RETURN c.i AS i, "
        + " AND ".join(_path_pin_conditions("t", "c.f", "c")) + " AS m",
        {"cases": cases},
    )
    wrong = [expected[row["i"]][:4] for row in rows if row["m"] is not expected[row["i"]][4]]
    assert len(rows) == len(cases) and wrong == []
    assert 100 < sum(e[4] for e in expected.values()) < 7900  # both outcomes well represented
    # ... for the `@` flag (its path matching, its keys deciding) and the "@" names too.
    scoped = [
        e[4] for e in expected.values() if split_pin(e[0]).scope and _expected(e[0].rsplit("@", 1)[0], *e[1:4])
    ]
    assert 15 < sum(scoped) < len(scoped) - 15
    assert sum(e[4] for e in expected.values() if "@" in e[2]) > 30


def test_module_pins_seek_by_directory_and_basename_as_pin_matches_says(engine, repo_id):
    """Random directory, suffix-directory and suffix pins (with exclusions)
    on IMPORTS rows link exactly the Modules `pin_matches` names, through
    the `dir`, `dir_name` and `basename` seeks."""
    from devgraph.indexer.common import module_pin_name

    rng = random.Random(5)
    paths = sorted({_random_path(rng) for _ in range(300)})
    engine.upsert_nodes(
        [{"label": "Module", "repo_id": repo_id, "name": p, "properties": {"source_file": p}} for p in paths]
        + [{"label": "Module", "repo_id": repo_id, "name": "origin.kt", "properties": {"source_file": "origin.kt"}}]
    )
    wrong, linked = [], 0
    for pin, exclude in _random_cases(rng, ("dir", "suffix", "suffix_dir"), 120, loose=False):
        engine.run_cypher("MATCH (:Module {repo_id: $r, name: 'origin.kt'})-[x:IMPORTS]->() DELETE x", {"r": repo_id})
        engine.upsert_relationships([{
            "from_label": "Module", "from_name": "origin.kt", "rel_type": "IMPORTS", "to_label": "Module",
            "to_name": module_pin_name(pin), "repo_id": repo_id, "to_file": pin, "exclude": exclude,
        }])
        got = {
            row["n"] for row in engine.run_cypher(
                "MATCH (:Module {repo_id: $r, name: 'origin.kt'})-[:IMPORTS]->(b) RETURN b.name AS n", {"r": repo_id}
            )
        }
        want = {p for p in [*paths, "origin.kt"] if _expected(pin, exclude, p)}
        linked += bool(want)
        if got != want:
            wrong.append((pin, exclude, sorted(got ^ want)))
    assert wrong == [] and linked > 20


# --- live writes and relinks per pin kind -----------------------------------------


@pytest.fixture
def repo_id(engine):
    repo = f"zz-pins-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text="def f():\n    return 0\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _inject(monkeypatch, calls=(), imports=(), files=()):
    """app.py's `main` calls `f` through `calls` (pins) and app.py imports
    `files` by path and `imports` (Module pins), whatever its text."""
    real = dispatch.extract_python_file

    def extract(content, rel_path, repo_id):
        result = real(content, rel_path, repo_id)
        if rel_path == "app.py":
            for rel in call_rows("Function", "main", "f", set(calls), False, None, "app.py", repo_id):
                rel.from_file = rel.origin = "app.py"
                result.relationships.append(rel)
            for rel in import_rows("app.py", repo_id, set(files), set(imports)):
                rel.origin = "app.py"
                result.relationships.append(rel)
        return result

    monkeypatch.setattr(dispatch, "extract_python_file", extract)


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _calls(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Function {repo_id: $r, name: 'main'})-[c:CALLS]->(b) RETURN b.file AS file, c.confidence AS conf",
        {"r": repo_id},
    )
    return sorted((row["file"], row["conf"]) for row in rows)


def _imports(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Module {repo_id: $r, name: 'app.py'})-[c:IMPORTS]->(b) RETURN b.name AS name, c.confidence AS conf",
        {"r": repo_id},
    )
    return sorted((row["name"], row["conf"]) for row in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


@pytest.mark.parametrize(("pins", "before", "added", "after"), [
    pytest.param(["lib/."], ["lib/a.py", "lib/sub/b.py", "other/lib/c.py", "a.py"], "lib/n.py",
                 [("lib/a.py", "resolved"), ("lib/n.py", "resolved")], id="dir"),
    pytest.param(["."], ["a.py", "lib/b.py"], "n.py", [("a.py", "resolved"), ("n.py", "resolved")], id="root dir"),
    pytest.param(["lib/.!_test.py"], ["lib/a.py", "lib/a_test.py"], "lib/n_test.py", [("lib/a.py", "resolved")],
                 id="dir less a suffix"),
    pytest.param(["/m/Item.py"], ["x/m/Item.py", "m/Item.py", "x/m/Other.py", "xm/Item.py"], "y/z/m/Item.py",
                 [("m/Item.py", "package"), ("x/m/Item.py", "package"), ("y/z/m/Item.py", "package")], id="suffix"),
    pytest.param(["/m/."], ["x/m/a.py", "m/b.py", "x/m/s/c.py", "xm/d.py"], "q/m/n.py",
                 [("m/b.py", "package"), ("q/m/n.py", "package"), ("x/m/a.py", "package")], id="suffix dir"),
    pytest.param(["lib/a.py", "lib/.", "/a.py", "lib/"], ["lib/a.py", "lib/b.py", "x/a.py", "lib/s/c.py", "o/c.py"],
                 "lib/s/a.py",
                 [("lib/a.py", "resolved"), ("lib/b.py", "resolved"), ("lib/s/a.py", "package"),
                  ("lib/s/c.py", "package"), ("x/a.py", "package")], id="precedence"),
])
def test_a_path_pinned_call_writes_and_relinks(engine, repo_id, tmp_path, monkeypatch, pins, before, added, after):
    _inject(monkeypatch, calls=pins)
    _write(tmp_path, "app.py", "def main():\n    return 0\n")
    paths = {rel: _write(tmp_path, rel) for rel in before}
    _scan(engine, repo_id, tmp_path)
    linked = [(rel, conf) for rel, conf in after if rel != added]
    assert _calls(engine, repo_id) == linked
    _equals_fresh(engine, repo_id, tmp_path)

    new = _write(tmp_path, added)
    index_paths(engine, repo_id, tmp_path, {new})
    assert _calls(engine, repo_id) == after
    _equals_fresh(engine, repo_id, tmp_path)

    gone = paths[before[0]]
    gone.unlink()
    remove_paths(engine, repo_id, tmp_path, {gone})
    assert _calls(engine, repo_id) == [(rel, conf) for rel, conf in after if rel != before[0]]
    _equals_fresh(engine, repo_id, tmp_path)


#: Each caller file's `main` calls `f` through these pins (and app.py also
#: imports it so): `@` pins (two.py's from two call sites on receivers of
#: two types), a bare name, an exact file beside a weaker `@` pin.
_KEYED_CALLERS = {
    "app.py": {scoped_pin("lib/.", ["N:A"])},
    "two.py": {scoped_pin("lib/.", ["N:A"]), scoped_pin("lib/.", ["N:B"])},
    "bare.py": set(),
    "exact.py": {"lib/a.py", scoped_pin("/lib/.", ["N:A"])},
}


def _inject_match_keys(monkeypatch) -> dict:
    """A `# mkeys k1,k2` first line gives a file's Functions those match
    keys (and every lib/ file's none, [], while the returned settings'
    `empty` is true); the `_KEYED_CALLERS` files call (and app.py imports)
    `f`."""
    real = dispatch.extract_python_file
    settings = {"empty": False}

    def extract(content, rel_path, repo_id):
        result = real(content, rel_path, repo_id)
        keys = None
        if content.startswith("# mkeys "):
            keys = sorted(set(content.splitlines()[0][len("# mkeys "):].split(",")))
        elif settings["empty"] and rel_path.startswith("lib/"):
            keys = []
        if keys is not None:
            for node in result.nodes:
                if node.label == "Function":
                    node.properties["mkeys"] = keys
        pins = _KEYED_CALLERS.get(rel_path)
        if pins is not None:
            sources = [("CALLS", ("Function", "main"))]
            if rel_path == "app.py":
                sources.append(("IMPORTS", ("Module", rel_path)))
            for kind, source in sources:
                for rel in call_rows(*source, "f", set(pins), not pins, None, rel_path, repo_id, rel_type=kind):
                    rel.origin = rel_path
                    rel.from_file = rel_path if kind == "CALLS" else None
                    result.relationships.append(rel)
        return result

    monkeypatch.setattr(dispatch, "extract_python_file", extract)
    return settings


def _keyed_links(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x:CALLS|IMPORTS]->(b:Function {name: 'f'}) "
        "RETURN coalesce(a.file, a.name) AS a, type(x) AS t, b.file AS f, x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((row["a"], row["t"], row["f"], row["c"]) for row in rows)


def _f(keys=None):
    return (f"# mkeys {keys}\n" if keys else "") + "def f():\n    return 0\n"


def test_a_nodes_new_match_keys_retract_and_relink_its_edges(engine, repo_id, tmp_path, monkeypatch):
    """When an edit moves N:A from lib/a.py's `f` to lib/b.py's, the edges
    into both go and are written again under the new keys: `@` pins (two
    sites with different keys among them), a bare name and an exact file,
    from callers outside the batch or in it, as a fresh scan has them. A
    node written without keys loses the ones it had. A commit's MODIFIES
    edge survives."""
    _inject_match_keys(monkeypatch)
    monkeypatch.setattr("devgraph.graph.engine.RETRACT_NODES", 1)  # a transaction per node
    callers = {rel: _write(tmp_path, rel, "def main():\n    return 0\n") for rel in _KEYED_CALLERS}
    a = _write(tmp_path, "lib/a.py", _f("N:A,.:A"))
    b = _write(tmp_path, "lib/b.py", _f("N:B"))
    _scan(engine, repo_id, tmp_path)
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'lib/a.py'}) CREATE (:Commit {repo_id: $r, name: 'c1'})-[:MODIFIES]->(m)",
        {"r": repo_id},
    )
    fixed = [
        ("bare.py", "CALLS", "lib/a.py", "name"), ("bare.py", "CALLS", "lib/b.py", "name"),
        ("exact.py", "CALLS", "lib/a.py", "resolved"),  # the file pin, whatever a's keys
    ]

    def keyed(app, two):
        # exact.py's `@` pin links only b: a is its stronger file pin's.
        return sorted(fixed + [("app.py", t, f, "resolved") for f in app for t in ("CALLS", "IMPORTS")]
                      + [("two.py", "CALLS", f, "resolved") for f in two]
                      + [("exact.py", "CALLS", "lib/b.py", "package") for f in app if f == "lib/b.py"])

    assert _keyed_links(engine, repo_id) == keyed(["lib/a.py"], ["lib/a.py", "lib/b.py"])
    _equals_fresh(engine, repo_id, tmp_path)

    # Callers outside the batch: relinked from their name_refs.
    _write(tmp_path, "lib/a.py", _f("N:C"))
    _write(tmp_path, "lib/b.py", _f("N:A"))
    index_paths(engine, repo_id, tmp_path, {a, b})
    assert _keyed_links(engine, repo_id) == keyed(["lib/b.py"], ["lib/b.py"])
    _equals_fresh(engine, repo_id, tmp_path)

    # A caller in the batch: written again by pass 2.
    _write(tmp_path, "lib/a.py", _f(".:A,N:A"))
    _write(tmp_path, "app.py", "def main():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {a, callers["app.py"]})
    assert _keyed_links(engine, repo_id) == keyed(["lib/a.py", "lib/b.py"], ["lib/a.py", "lib/b.py"])
    _equals_fresh(engine, repo_id, tmp_path)

    # Written without keys: it has none, and no `@` pin links it.
    _write(tmp_path, "lib/a.py", _f())
    index_paths(engine, repo_id, tmp_path, {a})
    assert _keyed_links(engine, repo_id) == keyed(["lib/b.py"], ["lib/b.py"])
    _equals_fresh(engine, repo_id, tmp_path)
    assert engine.run_cypher(
        "MATCH (n:Function {repo_id: $r, name: 'f', file: 'lib/a.py'}) RETURN n.mkeys AS k", {"r": repo_id}
    ) == [{"k": None}]
    assert engine.run_cypher(
        "MATCH (:Commit {repo_id: $r, name: 'c1'})-[x:MODIFIES]->(:Module {name: 'lib/a.py'}) RETURN count(x) AS n",
        {"r": repo_id},
    ) == [{"n": 1}]


@pytest.mark.parametrize("fails", ["keys", "pass 2", "settle"])
def test_a_batch_that_fails_after_writing_match_keys_rekeys_again(engine, repo_id, tmp_path, monkeypatch, fails):
    """A batch that dies once the first node's keys are written (and its
    edges gone), in pass 2, or just before its rekeyed nodes are settled
    leaves them `mkeys_stale`: running it again retracts and relinks them,
    as a fresh scan has them."""
    from devgraph.graph import engine as engine_module

    _inject_match_keys(monkeypatch)
    monkeypatch.setattr(engine_module, "RETRACT_NODES", 1)
    for rel in _KEYED_CALLERS:
        _write(tmp_path, rel, "def main():\n    return 0\n")
    a = _write(tmp_path, "lib/a.py", _f("N:A,.:A"))
    b = _write(tmp_path, "lib/b.py", _f("N:B"))
    _scan(engine, repo_id, tmp_path)

    _write(tmp_path, "lib/a.py", _f())
    _write(tmp_path, "lib/b.py", _f("N:A"))
    with monkeypatch.context() as patch:
        if fails == "keys":
            real, calls = engine_module._run, []

            def flaky(session, query, **params):
                if "mkeys_stale = CASE" in query:
                    calls.append(query)
                    if len(calls) == 2:
                        raise RuntimeError("down")
                return real(session, query, **params)

            patch.setattr(engine_module, "_run", flaky)
        else:
            method = "upsert_relationships" if fails == "pass 2" else "settle_match_keys"
            patch.setattr(type(engine), method, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
        with pytest.raises(RuntimeError):
            index_paths(engine, repo_id, tmp_path, {a, b})
    assert engine.run_cypher(
        "MATCH (n:Function {repo_id: $r, name: 'f'}) WHERE n.mkeys_stale RETURN n.file AS f", {"r": repo_id}
    )
    index_paths(engine, repo_id, tmp_path, {a, b})
    assert ("app.py", "CALLS", "lib/b.py", "resolved") in _keyed_links(engine, repo_id)
    _equals_fresh(engine, repo_id, tmp_path)


def test_keys_that_still_match_the_same_write_no_edge_again(engine, repo_id, tmp_path, monkeypatch):
    """A node stored without keys and written with none ([]) or back keeps
    every edge into it: the keys match the same nodes either way. The
    property follows, as a fresh scan has it."""
    settings = _inject_match_keys(monkeypatch)
    for rel in _KEYED_CALLERS:
        _write(tmp_path, rel, "def main():\n    return 0\n")
    a = _write(tmp_path, "lib/a.py", _f())
    _write(tmp_path, "lib/b.py", _f("N:A"))
    _scan(engine, repo_id, tmp_path)

    def edges():
        return engine.run_cypher(
            "MATCH ()-[x:CALLS|IMPORTS]->(n:Function {repo_id: $r, name: 'f', file: 'lib/a.py'}) "
            "RETURN elementId(x) AS x ORDER BY x",
            {"r": repo_id},
        )

    def keys():
        return engine.run_cypher(
            "MATCH (n:Function {repo_id: $r, name: 'f', file: 'lib/a.py'}) RETURN n.mkeys AS k", {"r": repo_id}
        )

    before = edges()
    assert len(before) == 2 and keys() == [{"k": None}]  # bare.py's and exact.py's
    for empty, stored in ((True, []), (False, None)):
        settings["empty"] = empty
        index_paths(engine, repo_id, tmp_path, {a})
        assert edges() == before and keys() == [{"k": stored}]
        _equals_fresh(engine, repo_id, tmp_path)


def test_module_pins_import_by_directory_and_suffix_and_relink(engine, repo_id, tmp_path, monkeypatch):
    """A not-yet-existing class imported by suffix appears once another
    module adds it (S2), and an anchored directory's new file is imported."""
    _inject(monkeypatch, files={"lib/a.py"}, imports={"lib/.", "/m/Item.py"})
    _write(tmp_path, "app.py", "def main():\n    return 0\n")
    _write(tmp_path, "lib/a.py")
    _write(tmp_path, "lib/b.py")
    _write(tmp_path, "lib/sub/c.py")
    _scan(engine, repo_id, tmp_path)
    assert _imports(engine, repo_id) == [("lib/a.py", None), ("lib/b.py", "resolved")]
    _equals_fresh(engine, repo_id, tmp_path)

    item = _write(tmp_path, "core/src/m/Item.py")
    index_paths(engine, repo_id, tmp_path, {item})
    assert _imports(engine, repo_id) == [("core/src/m/Item.py", "package"), ("lib/a.py", None), ("lib/b.py", "resolved")]
    _equals_fresh(engine, repo_id, tmp_path)

    # A file matched by a path pin and by name keeps the by-name edge alone.
    a = tmp_path / "lib/a.py"
    a.unlink()
    remove_paths(engine, repo_id, tmp_path, {a})
    _write(tmp_path, "lib/a.py")
    index_paths(engine, repo_id, tmp_path, {a, _write(tmp_path, "lib/n.py")})
    assert _imports(engine, repo_id) == [
        ("core/src/m/Item.py", "package"), ("lib/a.py", None), ("lib/b.py", "resolved"), ("lib/n.py", "resolved"),
    ]
    _equals_fresh(engine, repo_id, tmp_path)


def test_modules_carry_their_directory_and_basename(engine, repo_id, tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "pkg/sub/b.py")
    _scan(engine, repo_id, tmp_path)
    rows = engine.run_cypher(
        "MATCH (m:Module {repo_id: $r}) RETURN m.name AS name, m.dir AS dir, m.dir_name AS dir_name, "
        "m.basename AS base ORDER BY name",
        {"r": repo_id},
    )
    assert [(r["name"], r["dir"], r["dir_name"], r["base"]) for r in rows] == [
        ("a.py", "", "", "a.py"), ("pkg/sub/b.py", "pkg/sub", "sub", "b.py"),
    ]


def _fan_in(engine, repo_id, packages: int, callers: int) -> list[dict]:
    """`packages` package directories each declaring `Get` (twice, and in a
    test file), and the CALLS rows of a `main` pinned to `callers` of them
    (a loose directory, a directory and a prefix pin each)."""
    engine.upsert_nodes(
        [{"label": "Module", "repo_id": repo_id, "name": "main.go", "properties": {"source_file": "main.go"}}]
        + [{"label": "Function", "repo_id": repo_id, "name": "main", "properties": {"file": "main.go"}}]
        + [
            {"label": "Function", "repo_id": repo_id, "name": "Get", "properties": {"file": f"p{i}/{f}"}}
            for i in range(packages) for f in ("a.go", "b.go", "a_test.go")
        ]
    )
    rows = [
        rel.to_dict()
        for i in range(callers)
        for pin in (f"p{i}/.~?.go!_test.go", f"p{i}/.?.go", f"p{i}/")
        for rel in call_rows("Function", "main", "Get", {pin}, False, None, "main.go", repo_id)
    ]
    for row in rows:
        row["from_file"] = row["origin"] = "main.go"
    return rows


def _profiled_upsert(engine, rows) -> list[int]:
    """Upsert `rows`, PROFILEd: the db hits of each query."""
    from neo4j import ManagedTransaction

    hits: list[int] = []
    real = ManagedTransaction.run

    def profiled(self, query, *args, **kwargs):
        result = real(self, "PROFILE " + query, *args, **kwargs)
        list(result)

        def total(plan):
            return plan.get("dbHits", 0) + sum(total(child) for child in plan.get("children", []))

        hits.append(total(result.consume().profile))
        return result

    ManagedTransaction.run = profiled
    try:
        engine.upsert_relationships(rows)
    finally:
        ManagedTransaction.run = real
    return hits


def test_a_directory_pin_seeks_its_files_not_every_node_of_the_name(engine, repo_id):
    """S10 fan-in: with 300 packages each declaring `Get`, rows pinned to 30
    package directories (`pK/.~?.go`, `pK/.?.go`, a `pK/` prefix) cost what
    their own targets cost, not what every `Get` of the repository does: the
    seek is a prefix range on the (repo_id, name, file) index. Bounded on
    PROFILE db hits, so it holds on a noisy CI machine."""
    engine.init_schema()  # the (repo_id, name, file) index this seeks, on any database
    rows = _fan_in(engine, repo_id, packages=300, callers=30)
    # A plan cached by an earlier test of the same row shape, made while the
    # label held a few nodes, can satisfy the index hint with `file IS NOT
    # NULL` and filter the file afterwards (Neo4j replans it once its
    # statistics move, see the next test); this one profiles the plan this
    # graph gets. Clearing the caches alone isn't enough: the replan can use
    # index statistics sampled before these rows (a CI failure did);
    # prepareForReplanning resamples the indexes, then clears the caches.
    engine.run_cypher("CALL db.prepareForReplanning()")
    hits = _profiled_upsert(engine, rows)
    linked = engine.run_cypher(
        "MATCH (:Function {repo_id: $r, name: 'main'})-[:CALLS]->(b) RETURN count(b) AS n", {"r": repo_id}
    )[0]["n"]
    assert linked == 30 * 3  # a.go and b.go of each, and its test file through the prefix
    # Every Get of the repository is 900 nodes; 90 rows over them all would be
    # about 80k hits. Seeking each row's own directory stays well under that.
    assert sum(hits) < 15_000, hits


def test_a_directory_pin_plan_cached_on_a_tiny_graph_is_replanned_once_it_grows(engine, repo_id):
    """A repository starts empty: the first saves plan the pinned-CALLS query
    on a few nodes, where the index hint may be met by seeking `(repo_id,
    name)` with `file IS NOT NULL` (about 1 in 2 fresh databases did). That
    costs what the query cost before the prefix seek, never more, and Neo4j
    replans a cached query whose statistics have moved once its minimum
    replan interval (`dbms.cypher.min_replan_interval`, 10 s) has passed:
    the same query, its cache never cleared, then seeks the prefix range."""
    import time

    engine.init_schema()
    engine.run_cypher("CALL db.prepareForReplanning()")
    _profiled_upsert(engine, _fan_in(engine, f"{repo_id}-tiny", packages=2, callers=2))  # plans it
    try:
        rows = _fan_in(engine, repo_id, packages=300, callers=30)
        time.sleep(12)
        hits = _profiled_upsert(engine, rows)
    finally:
        engine.delete_repository(f"{repo_id}-tiny")
    assert sum(hits) < 15_000, hits
