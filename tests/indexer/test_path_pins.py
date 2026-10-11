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
from devgraph.graph.schema import pin_kind
from devgraph.indexer import dispatch
from devgraph.indexer.calls import call_rows, import_rows
from devgraph.indexer.common import best_pin, module_pin_name, pin_excludes, pin_matches
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

# --- unit ----------------------------------------------------------------------


@pytest.mark.parametrize(("pin", "kind"), [
    (None, "name"), ("", "fileless"), ("a/b.go", "file"), ("a!b.go", "file"), ("a/b/.", "dir"), (".", "dir"),
    ("/a/B.java", "suffix"), ("/a/b/.", "suffix_dir"), ("a/b/", "prefix"), ("a/b/.!_test.go", "dir"),
    ("/a/.!_test.go", "suffix_dir"), ("a/b/.~", "loose_dir"), ("a/b/.~?.go!_test.go", "loose_dir"),
    ("/a/B.java^", "suffix"), ("/a/.?.kt,.java^t", "suffix_dir"), ("a~", "file"), ("a?b.go", "file"),
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


#: Segments that share prefixes, so `a/b/` must not match `a/bc/`.
_SEGMENTS = ["a", "ab", "b", "bc", "m", "vendor", "third_party", "test", "src", "java"]
_NAMES = ["C.java", "a.py", "a_test.go", "x.go", "ab.py", "K.kt"]


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
    return pin


def _random_cases(rng: random.Random, kinds, count: int, loose=True) -> list[tuple[str, list[str]]]:
    """(pin, its exclusions): files and other pins, as `pin_excludes` writes them."""
    return [
        (_random_pin(rng, kinds, loose), sorted({_random_pin(rng) for _ in range(rng.randint(0, 3))}))
        for _ in range(count)
    ]


def _expected(pin: str, exclude: list[str], path: str) -> bool:
    return pin_matches(pin, path) and not any(pin_matches(other, path) for other in exclude)


def test_the_cypher_pin_test_agrees_with_pin_matches(engine):
    """A path pin and its exclusions, sent as `_group_rels_by_triple` sends
    them (`pin`, `ex_files`, `ex_pins`), match in Cypher exactly the paths
    `pin_matches` says."""
    from devgraph.graph.engine import _group_rels_by_triple, _path_pin_conditions

    rng = random.Random(11)
    cases, expected = [], {}
    for i, (pin, exclude) in enumerate(_random_cases(rng, ("dir", "suffix", "suffix_dir", "prefix"), 3000)):
        path = _random_path(rng)
        (rows,) = _group_rels_by_triple([{
            "from_label": "Function", "from_name": "f", "rel_type": "CALLS", "to_label": "Function",
            "to_name": "g", "repo_id": "r", "to_file": pin, "exclude": exclude,
        }]).values()
        cases.append({"i": i, "t": rows[0]["targets"][0], "f": path})
        expected[i] = (pin, exclude, path, _expected(pin, exclude, path))
    rows = engine.run_cypher(
        "UNWIND $cases AS c WITH c, c.t AS t RETURN c.i AS i, "
        + " AND ".join(_path_pin_conditions("t", "c.f")) + " AS m",
        {"cases": cases},
    )
    wrong = [expected[row["i"]][:3] for row in rows if row["m"] is not expected[row["i"]][3]]
    assert len(rows) == len(cases) and wrong == []
    assert 100 < sum(e[3] for e in expected.values()) < 2900  # both outcomes well represented


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
    # graph gets.
    engine.run_cypher("CALL db.clearQueryCaches()")
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
    engine.run_cypher("CALL db.clearQueryCaches()")
    _profiled_upsert(engine, _fan_in(engine, f"{repo_id}-tiny", packages=2, callers=2))  # plans it
    try:
        rows = _fan_in(engine, repo_id, packages=300, callers=30)
        time.sleep(12)
        hits = _profiled_upsert(engine, rows)
    finally:
        engine.delete_repository(f"{repo_id}-tiny")
    assert sum(hits) < 15_000, hits
