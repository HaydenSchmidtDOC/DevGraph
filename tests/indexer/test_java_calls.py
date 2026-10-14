"""Java CALLS and IMPORTS resolve through source roots, imports and types (the
extractor's tiers): a method of the enclosing class (or an in-file base)
resolves here, an inherited one to its base's class file, a static import or
a typed receiver (field, parameter, local, `new T()`, `T.m()`) to its class's
file: anchored under the file's own source root (resolved) and by path suffix
in every other one (package; another Maven module, a test root), never a
vendored copy and, from main code, never a test root. A library type links
nothing; an untyped receiver keeps a bare-name edge unless its method is
stoplisted.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (Java and Kotlin).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.common import pin_matches
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.indexer.java.extractor import extract_java_file
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

APP = "app/src/main/java/com/acme/app/OrderService.java"
REPO = "core/src/main/java/com/acme/core/repo/Repo.java"


def calls(source, file_path):
    """(caller, callee, to_file, confidence) per CALLS row."""
    result = extract_java_file(textwrap.dedent(source), file_path, "repo")
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def targets(source, caller, callee, file_path):
    return {(f, conf) for c, n, f, conf in calls(source, file_path) if (c, n) == (caller, callee)}


def linked(pins, path):
    """Whether one of a call's (pin, confidence) targets matches `path`."""
    return any(pin_matches(pin, path) for pin, _conf in pins)


def test_a_jdk_collection_call_links_nothing_and_never_its_caller():
    source = """
        package com.acme.core.repo;

        import java.util.List;

        public class Repo {
            protected final List<Item> items = new ArrayList<>();

            public void add(Item item) {
                validate(item);
                items.add(item);
                item.getName().isEmpty();
                cache.add(item);
            }

            protected void validate(Item item) {}
        }
    """
    assert targets(source, "add", "validate", REPO) == {(REPO, "resolved")}
    # items is a List, a library type: no edge, least of all Repo.add itself.
    got = extract_java_file(textwrap.dedent(source), REPO, "repo")
    adds = [(r.to_file, r.no_self) for r in got.relationships if r.rel_type == "CALLS" and r.to_name == "add"]
    assert adds == []  # `cache` is untyped, but `add` is stoplisted
    assert targets(source, "add", "isEmpty", REPO) == set()


def test_a_class_in_another_module_resolves_by_suffix_and_never_the_vendored_copy():
    source = """
        package com.acme.app;

        import com.acme.core.model.Item;
        import com.acme.core.repo.AuditedRepo;
        import com.acme.core.util.*;

        public class OrderService {
            private final AuditedRepo repo = new AuditedRepo();

            public void place(String name) {
                Item item = new Item(Strings.slug(name));
                repo.add(item);
                item.getName();
            }
        }
    """
    slug = targets(source, "place", "slug", APP)
    assert ("app/src/main/java/com/acme/core/util/Strings.java", "resolved") in slug  # this root (no file)
    assert linked(slug, "core/src/main/java/com/acme/core/util/Strings.java")  # another module
    assert not linked(slug, "third_party/acme-util/src/main/java/com/acme/core/util/Strings.java")
    assert not linked(slug, "core/src/test/java/com/acme/core/util/Strings.java")  # main never sees tests
    add = targets(source, "place", "add", APP)
    assert linked(add, "core/src/main/java/com/acme/core/repo/AuditedRepo.java")
    assert not linked(add, REPO)  # the field's own type, not its base
    assert all(conf == "package" for pin, conf in add if not pin.startswith("app/"))
    assert linked(targets(source, "place", "getName", APP), "core/src/main/java/com/acme/core/model/Item.java")


def test_wildcard_and_nested_static_imports_name_their_files():
    source = """
        package com.acme.app;

        import static com.acme.core.config.Settings.Defaults.pageSize;
        import com.acme.core.config.Settings.Defaults;
        import com.acme.core.util.*;

        public class App {
            int pages() { return pageSize() + Defaults.pageSize(); }
        }
    """
    file_path = "app/src/main/java/com/acme/app/App.java"
    got = targets(source, "pages", "pageSize", file_path)
    assert linked(got, "core/src/main/java/com/acme/core/config/Settings.java")
    result = extract_java_file(textwrap.dedent(source), file_path, "repo")
    imports = {(r.to_name, r.to_file) for r in result.relationships if r.rel_type == "IMPORTS"}
    assert imports == {
        ("app/src/main/java/com/acme/core/config/Settings.java", None),
        ("Settings.java", "/com/acme/core/config/Settings.java^t"),
        ("app/src/main/java/com/acme/core/util", "app/src/main/java/com/acme/core/util/.?.java"),
        ("util", "/com/acme/core/util/.?.java^t"),
    }


def test_this_super_and_an_in_file_base_chain():
    source = """
        package com.acme.core.repo;

        public class AuditedRepo extends Repo {
            @Override
            public void add(Item item) { super.add(item); }
            public void addAll(Item i) { add(i); this.add(i); }
            public void check(Item item) { validate(item); }
        }
    """
    file_path = "core/src/main/java/com/acme/core/repo/AuditedRepo.java"
    assert targets(source, "addAll", "add", file_path) == {(file_path, "resolved")}
    sup = targets(source, "add", "add", file_path)
    assert (REPO, "resolved") in sup and not linked(sup, file_path)
    assert (REPO, "resolved") in targets(source, "check", "validate", file_path)


def test_an_untyped_receiver_keeps_a_bare_row_unless_stoplisted():
    source = """
        package a;

        class A {
            void run() { helper().go(); x.size(); System.out.println(1); }
        }
    """
    got = calls(source, "src/a/A.java")
    assert ("run", "go", None, "name") in got
    assert not any(n in ("size", "println") for _c, n, _f, _conf in got)


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
    repo = f"zz-javacalls-{uuid.uuid4().hex[:8]}"
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
        "RETURN coalesce(a.file, a.name) AS a, coalesce(b.file, b.name) AS b, b.name AS bn, x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((r["a"], r["b"], r["bn"], r["c"]) for r in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_an_import_of_a_class_not_there_yet_links_when_another_module_adds_it(engine, repo_id, tmp_path):
    """S2: the class file a suffix pin names appears later in another module,
    a wildcard import's package gains a file, and the class moves between
    modules: each step equals a fresh scan, with no re-index of the importer."""
    app = "app/src/main/java/com/acme/app/App.java"
    _write(tmp_path, app, textwrap.dedent("""
        package com.acme.app;

        import com.acme.core.model.Item;
        import com.acme.core.util.*;

        class App {
            void run(Item item) { item.getName(); Log.info(); }
        }
    """))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _edges(engine, repo_id, "CALLS") == []

    item = _write(tmp_path, "core/src/main/java/com/acme/core/model/Item.java",
                  "package com.acme.core.model;\n\npublic class Item { public String getName() { return \"\"; } }\n")
    log = _write(tmp_path, "core/src/main/java/com/acme/core/util/Log.java",
                 "package com.acme.core.util;\n\npublic class Log { public static void info() {} }\n")
    index_paths(engine, repo_id, tmp_path, {item, log})
    assert (app, item.relative_to(tmp_path).as_posix(), "getName", "package") in _edges(engine, repo_id, "CALLS")
    imports = {(a, b, c) for a, b, _bn, c in _edges(engine, repo_id, "IMPORTS")}
    assert imports == {
        (app, "core/src/main/java/com/acme/core/model/Item.java", "package"),
        (app, "core/src/main/java/com/acme/core/util/Log.java", "package"),
    }
    _equals_fresh(engine, repo_id, tmp_path)

    # Item moves to another module; the vendored copy never links.
    item.unlink()
    remove_paths(engine, repo_id, tmp_path, {item})
    moved = _write(tmp_path, "lib/src/main/java/com/acme/core/model/Item.java",
                   "package com.acme.core.model;\n\npublic class Item { public String getName() { return \"\"; } }\n")
    vendored = _write(tmp_path, "third_party/x/src/main/java/com/acme/core/model/Item.java",
                      "package com.acme.core.model;\n\npublic class Item { public String getName() { return \"\"; } }\n")
    index_paths(engine, repo_id, tmp_path, {moved, vendored})
    got = _edges(engine, repo_id, "CALLS")
    assert (app, "lib/src/main/java/com/acme/core/model/Item.java", "getName", "package") in got
    assert not any(b.startswith("third_party/") and a == app for a, b, _bn, _c in got)
    _equals_fresh(engine, repo_id, tmp_path)


def test_a_test_root_is_a_conventional_one_not_any_test_directory():
    """Main code's suffix pins leave out `src/test/...` roots but not a
    directory that merely is named `test`; code under a test root may reach
    other test roots."""
    from devgraph.indexer.jvm import JvmLayout

    assert JvmLayout("core/src/test/java/a/B.java", "a").flag == "^"
    assert JvmLayout("test/a/B.java", "a").flag == "^t"
    assert JvmLayout("tools/test/src/main/java/a/B.java", "a").flag == "^t"
    assert JvmLayout("third_party/x/src/main/java/a/B.java", "a").flag == ""
