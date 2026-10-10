"""Kotlin CALLS and IMPORTS resolve as Java's (source roots, imports, typed
receivers) with Kotlin's own shapes: a source tree may leave the root package
out (`src/main/kotlin/data/` holding `com.acme.notes.data`); a top-level
function lives in some file of its package, so an import or an unqualified
call of one names the package's directory; a class may sit in a file named
after something else, so a member call reaches its package's files at package
confidence past its own `.kt` file. An import of a class or function is an
IMPORTS edge to that symbol, whose file is the one declaring it; a wildcard
import, to every Kotlin (and Java) file of the package.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (Java and Kotlin).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.common import pin_matches
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

MAIN = "src/main/kotlin/Main.kt"


def calls(source, file_path):
    result = extract_kotlin_file(textwrap.dedent(source), file_path, "repo")
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def targets(source, caller, callee, file_path=MAIN):
    return {(f, conf) for c, n, f, conf in calls(source, file_path) if (c, n) == (caller, callee)}


def linked(pins, path):
    return any(pin_matches(pin, path) for pin, _conf in pins)


def test_a_top_level_function_import_names_its_package_directory_with_the_root_left_out():
    source = """
        package com.acme.notes

        import com.acme.notes.util.slugify
        import com.acme.notes.report.runReport

        fun main() {
            println("Created ${ slugify("x") }")
            runReport()
            formatStamp(1)
        }
    """
    assert targets(source, "main", "slugify") == {
        ("src/main/kotlin/util/.?.kt", "resolved"), ("/util/.?.kt^t", "package"),
    }
    assert linked(targets(source, "main", "runReport"), "src/main/kotlin/report/Report.kt")
    # The file's own package: its directory (no suffix for the left-out root).
    assert targets(source, "main", "formatStamp") == {("src/main/kotlin/.?.kt", "resolved")}
    assert targets(source, "main", "println") == {("src/main/kotlin/.?.kt", "resolved")}  # no such file function
    result = extract_kotlin_file(textwrap.dedent(source), MAIN, "repo")
    imports = {(r.to_label, r.to_name, r.to_file) for r in result.relationships if r.rel_type == "IMPORTS"}
    assert ("Function", "slugify", "src/main/kotlin/util/.?.kt,.java") in imports


def test_typed_receivers_reach_a_class_in_a_file_named_otherwise():
    source = """
        package com.acme.notes

        import com.acme.notes.data.NoteRepository
        import com.acme.notes.tags.TagSet

        fun main() {
            val repo = NoteRepository()
            repo.create("a")
            val tags = TagSet()
            tags.add("kotlin")
            val sb = StringBuilder()
            sb.append("x")
        }
    """
    create = targets(source, "main", "create")
    assert ("src/main/kotlin/data/NoteRepository.kt", "resolved") in create
    add = targets(source, "main", "add")
    assert linked(add, "src/main/kotlin/tags/Tags.kt")  # TagSet lives in Tags.kt
    assert ("src/main/kotlin/tags/.~?.kt", "package") in add
    assert targets(source, "main", "append") == set()  # a library type


def test_a_primary_constructor_property_and_an_extension_import():
    source = """
        package com.acme.notes.data

        import com.acme.notes.util.Clock
        import com.acme.notes.data.isStale

        class NoteRepository(private val clock: Clock = Clock()) {
            private val notes = mutableListOf<Note>()

            fun create(title: String): Note {
                val note = Note(nextId(), title, clock.now())
                notes.add(note)
                note.isStale(1)
                return note
            }

            private fun nextId() = notes.size + 1
        }
    """
    file_path = "src/main/kotlin/data/NoteRepository.kt"
    assert ("src/main/kotlin/util/Clock.kt", "resolved") in targets(source, "create", "now", file_path)
    assert targets(source, "create", "nextId", file_path) == {(file_path, "resolved")}
    assert targets(source, "create", "add", file_path) == set()  # a stdlib collection: stoplisted
    assert linked(targets(source, "create", "isStale", file_path), "src/main/kotlin/data/NoteExt.kt")


def test_a_test_reaches_its_package_in_main_by_suffix_and_main_never_reaches_tests():
    test = "package com.acme.notes.util\n\nclass TextTest {\n    fun slugifies() { slugify(\"A B\") }\n}\n"
    got = targets(test, "slugifies", "slugify", "src/test/kotlin/util/TextTest.kt")
    assert linked(got, "src/main/kotlin/util/Text.kt")
    main = "package com.acme.notes.export\n\nimport com.acme.notes.util.*\n\nfun line() = truncate(\"x\")\n"
    got = targets(main, "line", "truncate", "src/main/kotlin/export/MarkdownExporter.kt")
    assert linked(got, "src/main/kotlin/util/Text.kt")
    assert not linked(got, "src/test/kotlin/util/TextTest.kt")


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
    repo = f"zz-ktcalls-{uuid.uuid4().hex[:8]}"
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


def _imports(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (a:Module {repo_id: $r})-[x:IMPORTS]->(b {repo_id: $r}) "
        "RETURN a.name AS a, coalesce(b.file, b.name) AS b, x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((r["a"], r["b"], r["c"]) for r in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_a_function_import_follows_the_function_between_files(engine, repo_id, tmp_path):
    """An import of a top-level function is an edge to the file declaring it,
    so moving the function to another file of its package moves the edge,
    with no re-index of the importer."""
    _write(tmp_path, MAIN, "package com.acme.notes\n\nimport com.acme.notes.util.slugify\n\nfun main() { slugify() }\n")
    text = _write(tmp_path, "src/main/kotlin/util/Text.kt", "package com.acme.notes.util\n\nfun slugify() = 1\n")
    other = _write(tmp_path, "src/main/kotlin/util/Clock.kt", "package com.acme.notes.util\n\nclass Clock\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _imports(engine, repo_id) == [(MAIN, "src/main/kotlin/util/Text.kt", "resolved")]
    _equals_fresh(engine, repo_id, tmp_path)

    text.write_text("package com.acme.notes.util\n")
    other.write_text("package com.acme.notes.util\n\nclass Clock\n\nfun slugify() = 1\n")
    index_paths(engine, repo_id, tmp_path, {text, other})
    assert _imports(engine, repo_id) == [(MAIN, "src/main/kotlin/util/Clock.kt", "resolved")]
    _equals_fresh(engine, repo_id, tmp_path)
