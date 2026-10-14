"""Go CALLS and IMPORTS resolve through packages, receivers and types (the
extractor's tiers): a function of the file resolves here, any other
unqualified call to its package's directory, `pkg.F()` to the imported
package's directory, a method on a typed receiver (a parameter, a local, a
struct field, what a package's function returned) to its type's file or
package at package confidence; an untyped receiver keeps a bare-name edge
unless its method is stoplisted, and an external package links nothing. An
import resolves by the longest in-repository module path (every go.mod, local
`replace`s of go.mod and go.work) to the package's directory, tests left out.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (Go).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import (
    full_scan,
    index_paths,
    remove_paths,
    sync_resolver_config,
)
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.resolver_config import ResolverConfig
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def _config(tmp_path, files=None):
    files = files or {"go.mod": "module example.com/shop/v2\n"}
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return ResolverConfig(tmp_path)


def calls(source, file_path="main.go", config=None):
    """(caller, callee, to_file, confidence) per CALLS row."""
    result = extract_go_file(textwrap.dedent(source), file_path, "repo", config)
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def targets(source, caller, callee, file_path="main.go", config=None):
    return {(f, conf) for c, n, f, conf in calls(source, file_path, config) if (c, n) == (caller, callee)}


def imports(source, file_path, config):
    result = extract_go_file(textwrap.dedent(source), file_path, "repo", config)
    return {(r.to_name, r.to_file, (r.properties or {}).get("confidence")) for r in result.relationships
            if r.rel_type == "IMPORTS"}


def test_a_function_of_the_file_resolves_here_and_any_other_to_its_package():
    source = """
        package main

        func main() { run(); migrate(); len(nil) }
        func run() {}
    """
    assert targets(source, "main", "run") == {("main.go", "resolved")}
    # Another file of the package: its directory, a test file left out.
    assert targets(source, "main", "migrate") == {(".?.go!_test.go", "resolved")}
    assert targets(source, "main", "len") == set()  # a builtin
    in_dir = "package store\n\nfunc Get() { normalize() }\n"
    assert targets(in_dir, "Get", "normalize", "internal/store/memory.go") == {
        ("internal/store/.?.go!_test.go", "resolved")
    }


def test_a_test_file_reaches_its_package_and_an_external_test_package_only_its_tests():
    internal = "package store\n\nfunc TestGet() { New() }\n"
    assert targets(internal, "TestGet", "New", "internal/store/memory_test.go") == {
        ("internal/store/.?.go", "resolved")
    }
    external = "package cache_test\n\nfunc TestWarm() { helper() }\n"
    assert targets(external, "TestWarm", "helper", "cache/cache_test.go") == {("cache/.?_test.go", "resolved")}


def test_a_qualified_call_goes_to_the_imported_package_and_an_external_one_links_nothing(tmp_path):
    config = _config(tmp_path)
    source = """
        package main

        import (
            "fmt"
            "example.com/shop/v2/money"
            "github.com/go-chi/chi/v5"
            m2 "example.com/shop/v2/money"
        )

        func main() {
            fmt.Println(money.Format(1), m2.Format(2))
            r := chi.NewRouter()
            r.Get("/")
        }
    """
    assert targets(source, "main", "Format", config=config) == {("money/.?.go!_test.go", "resolved")}
    assert targets(source, "main", "Println", config=config) == set()
    assert targets(source, "main", "NewRouter", config=config) == set()
    assert targets(source, "main", "Get", config=config) == set()  # chi's Router, a typed external receiver


def test_typed_receivers_resolve_to_their_types_package(tmp_path):
    config = _config(tmp_path)
    source = """
        package cache

        import "example.com/shop/v2/internal/store"

        type Cache struct {
            s *store.Store
        }

        func New() *Cache { return &Cache{} }

        func (c *Cache) Get(key string) int {
            v, _ := c.s.Get(key)
            c.Warm()
            return v
        }

        func (c *Cache) Warm() {
            s := store.New()
            s.Put("k", 0)
            x := New()
            x.Get("k")
            var y Cache
            y.Reset()
            z := &store.Store{}
            z.Put("z", 1)
        }
    """
    loose = "internal/store/.~?.go!_test.go"
    assert targets(source, "Get", "Get", "cache/cache.go", config) == {(loose, "package")}  # never Cache.Get
    assert targets(source, "Get", "Warm", "cache/cache.go", config) == {("cache/cache.go", "resolved")}
    assert targets(source, "Warm", "Put", "cache/cache.go", config) == {(loose, "package")}
    # An in-file function's result type, and a type declared here whose
    # method is in another file of the package.
    assert targets(source, "Warm", "Get", "cache/cache.go", config) == {("cache/cache.go", "resolved")}
    assert targets(source, "Warm", "Reset", "cache/cache.go", config) == {("cache/.~?.go!_test.go", "package")}


def test_a_package_named_unlike_its_directory_is_found_through_the_import_it_must_be(tmp_path):
    config = _config(tmp_path)
    source = """
        package main

        import "example.com/shop/v2/internal/legacydb"

        func migrate() {
            conn := db.Open("x")
            defer conn.Close()
        }
    """
    assert targets(source, "migrate", "Open", config=config) == {("internal/legacydb/.?.go!_test.go", "resolved")}
    assert targets(source, "migrate", "Close", config=config) == {("internal/legacydb/.~?.go!_test.go", "package")}


def test_a_renamed_import_binds_only_when_the_unknown_qualifiers_match_it_one_for_one(tmp_path):
    """Two unknown qualifiers (`db` and a package-level `logger` declared in
    another file) for one renamed import: neither is known to be it, so
    both reach its directory only at package confidence."""
    config = _config(tmp_path)
    source = """
        package main

        import "example.com/shop/v2/internal/legacydb"

        func migrate() {
            db.Open("x")
            logger.Info("migrating")
        }
    """
    loose = {("internal/legacydb/.~?.go!_test.go", "package")}
    assert targets(source, "migrate", "Open", config=config) == loose
    assert targets(source, "migrate", "Info", config=config) == loose
    # A local or a parameter is never an unknown qualifier.
    one = """
        package main

        import "example.com/shop/v2/internal/legacydb"

        func migrate(cfg Config) {
            cfg.Load()
            x := 1
            db.Open(x)
        }
    """
    assert targets(one, "migrate", "Open", config=config) == {("internal/legacydb/.?.go!_test.go", "resolved")}


def test_a_version_directory_is_the_package_name_unless_it_is_the_modules_major_version(tmp_path):
    config = _config(tmp_path)
    source = """
        package main

        import (
            "example.com/shop/v2"
            "example.com/shop/v2/apis/apps/v1"
            "github.com/go-chi/chi/v5"
        )

        func main() {
            v1.Register()
            shop.Start()
            chi.NewRouter()
            logger.Info()
        }
    """
    # apis/apps/v1 is `package v1`, a directory of the module: used as v1,
    # not a renamed import an unknown qualifier (logger) could be.
    assert targets(source, "main", "Register", config=config) == {("apis/apps/v1/.?.go!_test.go", "resolved")}
    # The module's own /v2 is its major version: the root package is used by its path name.
    assert targets(source, "main", "Start", config=config) == {(".?.go!_test.go", "resolved")}
    assert targets(source, "main", "NewRouter", config=config) == set()
    assert targets(source, "main", "Info", config=config) == {(None, "name")}


def test_an_untyped_receiver_keeps_a_bare_row_unless_its_method_is_stoplisted():
    source = """
        package main

        func Run(items map[string]int) {
            for _, w := range workers {
                w.Run()
                w.Close()
            }
        }
    """
    got = extract_go_file(textwrap.dedent(source), "main.go", "repo")
    rows = [(r.to_name, r.to_file, r.properties["confidence"], r.no_self) for r in got.relationships
            if r.rel_type == "CALLS"]
    assert rows == [("Run", None, "name", True)]


def test_imports_resolve_by_the_longest_module_path_tests_left_out(tmp_path):
    config = _config(tmp_path, {
        "go.mod": "module example.com/shop/v2\n",
        "tools/go.mod": "module example.com/shop/tools\n\nreplace (\n\texample.com/lib => ../third/lib\n)\n",
        "go.work": "go 1.22\n\nuse (\n\t.\n\t./tools\n)\n\nreplace example.com/ext v1.0.0 => ./vendor/ext\n",
    })
    source = """
        package main

        import (
            "fmt"
            "example.com/shop/v2/cache"
            "example.com/shop/tools/internal/codegen"
            "example.com/shop/v2"
            "example.com/lib/x"
            "example.com/ext"
        )
    """
    assert imports(source, "tools/cmd/gen/main.go", config) == {
        ("cache", "cache/.?.go!_test.go", "resolved"),
        ("tools/internal/codegen", "tools/internal/codegen/.?.go!_test.go", "resolved"),
        ("", ".?.go!_test.go", "resolved"),
        ("third/lib/x", "third/lib/x/.?.go!_test.go", "resolved"),
        ("vendor/ext", "vendor/ext/.?.go!_test.go", "resolved"),
    }
    assert imports(source, "main.go", None) == set()  # no configuration: every import external


# --- live --------------------------------------------------------------------------


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-gocalls-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _edges(engine, repo_id, rel_type):
    rows = engine.run_cypher(
        f"MATCH (a {{repo_id: $r}})-[x:{rel_type}]->(b {{repo_id: $r}}) "
        "RETURN coalesce(a.file, a.name) AS a, a.name AS an, coalesce(b.file, b.name) AS b, b.name AS bn, "
        "x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((r["a"], r["an"], r["b"], r["bn"], r["c"]) for r in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_a_nested_go_mod_moves_a_package_and_a_moved_function_relinks(engine, repo_id, tmp_path):
    """Adding a nested go.mod gives its directory a module path of its own;
    removing it gives the package back to the root module. A function moved
    within its package keeps its callers. Every step equals a fresh scan."""
    _write(tmp_path, "go.mod", "module example.com/app\n")
    main = _write(tmp_path, "main.go", 'package main\n\nimport "example.com/app/lib"\n\nfunc main() { lib.Do() }\n')
    _write(tmp_path, "lib/a.go", "package lib\n\nfunc Do() { help() }\n")
    b = _write(tmp_path, "lib/b.go", "package lib\n\nfunc help() {}\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    calls_before = _edges(engine, repo_id, "CALLS")
    assert ("main.go", "main", "lib/a.go", "Do", "resolved") in calls_before
    assert ("lib/a.go", "Do", "lib/b.go", "help", "resolved") in calls_before
    _equals_fresh(engine, repo_id, tmp_path)

    # help moves from b.go to c.go: Do's call follows it.
    b.write_text("package lib\n")
    c = _write(tmp_path, "lib/c.go", "package lib\n\nfunc help() {}\n")
    index_paths(engine, repo_id, tmp_path, {b, c})
    assert ("lib/a.go", "Do", "lib/c.go", "help", "resolved") in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    # lib/ becomes its own module: example.com/app/lib is no longer in the repository.
    nested = _write(tmp_path, "lib/go.mod", "module example.com/lib\n")
    sync_resolver_config(engine, repo_id, tmp_path, {nested})
    assert not any(e[0] == "main.go" for e in _edges(engine, repo_id, "CALLS"))
    _equals_fresh(engine, repo_id, tmp_path)
    main.write_text('package main\n\nimport "example.com/lib"\n\nfunc main() { lib.Do() }\n')
    index_paths(engine, repo_id, tmp_path, {main})
    assert ("main.go", "main", "lib/a.go", "Do", "resolved") in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    nested.unlink()
    remove_paths(engine, repo_id, tmp_path, {nested})
    sync_resolver_config(engine, repo_id, tmp_path, {nested})
    assert not any(e[0] == "main.go" for e in _edges(engine, repo_id, "CALLS")), _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)


def test_the_index_upgrade_replaces_bare_go_calls(engine, repo_id, tmp_path, monkeypatch):
    """A graph indexed at format 6 has bare Go CALLS with no confidence and
    Modules without `dir_name`; catch_up's automatic upgrade (a full_scan)
    leaves the resolved edges and records both."""
    from datetime import datetime, timezone

    from devgraph.indexer import dispatch
    from devgraph.indexer.common import GraphRelationship

    _write(tmp_path, "go.mod", "module example.com/app\n")
    _write(tmp_path, "main.go", 'package main\n\nimport "example.com/app/lib"\n\nfunc main() { lib.Do() }\n')
    _write(tmp_path, "lib/a.go", "package lib\n\nfunc Do() {}\n")
    _write(tmp_path, "other/b.go", "package other\n\nfunc Do() {}\n")
    real = dispatch.extract_go_file

    def old_extract(content, rel_path, repo_id, config=None):
        # Format 6: every Go call a bare name, no IMPORTS resolution.
        result = real(content, rel_path, repo_id)
        result.relationships = [r for r in result.relationships if r.rel_type not in ("CALLS", "IMPORTS")]
        if rel_path == "main.go":
            result.relationships.append(GraphRelationship(
                from_label="Function", from_name="main", rel_type="CALLS", to_label="Function", to_name="Do",
                repo_id=repo_id, from_file=rel_path, origin=rel_path,
            ))
        return result

    with monkeypatch.context() as old:
        old.setattr(dispatch, "extract_go_file", old_extract)
        provision_repository_schema(engine, tmp_path)
        engine.upsert_repository(repo_id, repo_id, str(tmp_path))
        full_scan(engine, repo_id, tmp_path)
    engine.run_cypher("MATCH (m:Module {repo_id: $r}) REMOVE m.dir_name", {"r": repo_id})
    engine.set_index_format(repo_id, 6)
    calls = [e for e in _edges(engine, repo_id, "CALLS") if e[1] == "main"]
    assert calls == [("main.go", "main", "lib/a.go", "Do", None), ("main.go", "main", "other/b.go", "Do", None)]
    assert dispatch.index_outdated(engine, repo_id)

    dispatch.catch_up(engine, repo_id, tmp_path, since=datetime.now(timezone.utc))
    assert [e for e in _edges(engine, repo_id, "CALLS") if e[1] == "main"] == [
        ("main.go", "main", "lib/a.go", "Do", "resolved"),
    ]
    assert not dispatch.index_outdated(engine, repo_id)
    (row,) = engine.run_cypher("MATCH (m:Module {repo_id: $r, name: 'lib/a.go'}) RETURN m.dir_name AS d", {"r": repo_id})
    assert row["d"] == "lib"
    _equals_fresh(engine, repo_id, tmp_path)
