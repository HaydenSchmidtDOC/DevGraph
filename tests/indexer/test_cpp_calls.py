"""C++ CALLS and IMPORTS resolve through quoted includes (the extractor's
tiers): an include names the file beside the includer (resolved) and, as a
suffix pin, the same path under an include directory elsewhere (package); a
header's declarations are defined in its same-stem source (`/x/y.cpp`,
package). A function of the file resolves here; any other call goes to the
includes that can declare it: a namespace or class qualifier picks the
includes whose path names it, a typed receiver the include named after its
type (else the type's conventional files); `std::` and STL types link
nothing, and an untyped receiver keeps a bare-name edge unless its method is
stoplisted.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (C++).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.common import best_pin
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.dispatch import full_scan, index_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

FILES = [
    "include/inventory.h", "include/platform.h", "include/util/log.h", "include/util/strings.h",
    "src/inventory.cpp", "src/main.cpp", "src/report.cpp", "src/report.h", "src/net/conn.cpp", "src/net/conn.h",
    "src/net/log.h", "src/util/strings.cpp", "src/platform_posix.cpp", "third_party/acme/util/log.h",
]


def _rows(source, file_path, rel_type="CALLS"):
    result = extract_cpp_file(textwrap.dedent(source), file_path, "repo")
    return [r for r in result.relationships if r.rel_type == rel_type]


def reached(source, file_path, caller, callee, candidates=FILES):
    """{candidate path: confidence} a CALLS row of `caller` to `callee`
    links, or {"<name>": confidence} for a bare-name row."""
    rows = [r for r in _rows(source, file_path) if (r.from_name, r.to_name) == (caller, callee)]
    if any(r.to_file is None for r in rows):
        return {"<name>": rows[0].properties["confidence"]}
    pins = {r.to_file: r.properties["confidence"] for r in rows}
    found = {}
    for path in candidates:
        pin = best_pin(pins, path, file_path)
        if pin is not None:
            found[path] = pins[pin]
    return found


def imported(source, file_path, candidates=FILES):
    rows = _rows(source, file_path, "IMPORTS")
    names = {r.to_name for r in rows if r.to_file is None}
    pins = [r.to_file for r in rows if r.to_file is not None]
    return {path: "resolved" if path in names else "package" for path in candidates
            if path in names or best_pin(pins, path, file_path) is not None}


def test_an_include_names_the_file_beside_it_else_one_under_an_include_directory():
    assert imported('#include "conn.h"\n#include "log.h"\n#include "platform.h"\n#include <string>\n',
                    "src/net/conn.cpp") == {
        "src/net/conn.h": "resolved", "src/net/log.h": "resolved", "include/platform.h": "package",
    }
    assert imported('#include "inventory.h"\n#include "util/log.h"\n#include "net/conn.h"\n', "src/main.cpp") == {
        "include/inventory.h": "package", "include/util/log.h": "package", "src/net/conn.h": "resolved",
    }
    assert imported('#include "../src/report.h"\n', "tests/report_test.cpp") == {"src/report.h": "resolved"}


def test_a_function_of_the_file_resolves_here_and_others_through_the_includes():
    source = """
        #include "report.h"
        #include "net/log.h"

        static bool known() { return true; }

        int main() {
            known();
            print_report();
            log_info("x");
            printf("x");
            return 0;
        }
    """
    assert reached(source, "src/main.cpp", "main", "known") == {"src/main.cpp": "resolved"}
    # Every include can declare it, and a header's declaration is defined in its same-stem source.
    included = {"src/report.h": "resolved", "src/report.cpp": "package", "src/net/log.h": "resolved"}
    assert reached(source, "src/main.cpp", "main", "print_report") == included
    assert reached(source, "src/main.cpp", "main", "log_info") == included
    assert reached("int main() { helper(); }\n", "src/main.cpp", "main", "helper") == {}  # nothing included


def test_a_qualifier_picks_the_includes_whose_path_names_it():
    source = """
        #include "inventory.h"
        #include "util/log.h"
        #include "util/strings.h"

        void Inventory::add(const std::string& name) {
            util::trim(name);
            util::log_info("added");
            std::find(name.begin(), name.end(), 'x');
        }
    """
    path = "src/inventory.cpp"
    assert reached(source, path, "add", "trim") == {
        "include/util/log.h": "package", "include/util/strings.h": "package", "src/util/strings.cpp": "package",
    }
    assert reached(source, path, "add", "log_info") == {
        "include/util/log.h": "package", "include/util/strings.h": "package", "src/util/strings.cpp": "package",
    }
    assert reached(source, path, "add", "find") == {}
    assert reached(source, path, "add", "begin") == {}


def test_typed_receivers_go_to_their_types_include_or_conventional_files():
    source = """
        #include "inventory.h"
        #include "net/conn.h"

        int main() {
            Inventory inv;
            inv.find("bolt");
            net::Connection conn("db.local");
            conn.open();
            std::vector<int> v;
            v.size();
            return 0;
        }
    """
    assert reached(source, "src/main.cpp", "main", "find") == {
        "include/inventory.h": "package", "src/inventory.cpp": "package",
    }
    assert reached(source, "src/main.cpp", "main", "open") == {
        "src/net/conn.h": "resolved", "src/net/conn.cpp": "package",
    }
    assert reached(source, "src/main.cpp", "main", "size") == {}
    # A type no include is named after: its conventional files, and the includes.
    report = '#include "report.h"\n\nvoid print_report(const Inventory& inv) { inv.size(); }\n'
    assert reached(report, "src/report.cpp", "print_report", "size") == {
        "include/inventory.h": "package", "src/inventory.cpp": "package", "src/report.h": "resolved",
        "src/report.cpp": "package",
    }


def test_an_untyped_receiver_keeps_a_bare_row_unless_stoplisted():
    source = """
        class Inventory {
            int size() const { return static_cast<int>(items_.size()); }
            void run() { items_.frobnicate(); this->size(); }
            std::vector<int> items_;
            Widget w_;
        };
    """
    rows = _rows(source, "include/inventory.h")
    assert not any(r.from_name == "size" for r in rows)
    assert reached(source, "include/inventory.h", "run", "frobnicate") == {}  # an STL member
    assert reached(source, "include/inventory.h", "run", "size") == {"include/inventory.h": "resolved"}
    other = "void f(Thing* t) { t->m(); g()->n(); }\n"
    rows = _rows(other, "src/f.cpp")
    assert {(r.to_name, r.to_file, r.no_self) for r in rows if r.to_file is None} == {("n", None, True)}
    assert reached(other, "src/f.cpp", "f", "m", ["include/thing.h", "src/Thing.cpp", "src/thing_util.cpp"]) == {
        "include/thing.h": "package", "src/Thing.cpp": "package",
    }


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
    repo = f"zz-cppcalls-{uuid.uuid4().hex[:8]}"
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


def test_a_definition_moving_from_header_to_source_keeps_its_callers(engine, repo_id, tmp_path):
    """A call through an include follows a function between its header and
    the header's same-stem source; an include of a header that appears
    later links when it does. Every step equals a fresh scan."""
    main = _write(tmp_path, "src/main.cpp", '#include "util/strings.h"\n\nint main() { util::trim(); }\n')
    header = _write(tmp_path, "include/util/strings.h", "namespace util { inline void trim() {} }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert ("src/main.cpp", "main", "include/util/strings.h", "trim", "package") in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    header.write_text("namespace util { void trim(); }\n")
    source = _write(tmp_path, "src/util/strings.cpp", '#include "util/strings.h"\n\nvoid util::trim() {}\n')
    index_paths(engine, repo_id, tmp_path, {header, source})
    assert [e for e in _edges(engine, repo_id, "CALLS") if e[1] == "main"] == [
        ("src/main.cpp", "main", "src/util/strings.cpp", "trim", "package"),
    ]
    _equals_fresh(engine, repo_id, tmp_path)

    main.write_text('#include "util/strings.h"\n#include "util/later.h"\n\nint main() { util::trim(); }\n')
    index_paths(engine, repo_id, tmp_path, {main})
    later = _write(tmp_path, "include/util/later.h", "#pragma once\n")
    index_paths(engine, repo_id, tmp_path, {later})
    assert ("src/main.cpp", "src/main.cpp", "include/util/later.h", "include/util/later.h", "package") in _edges(
        engine, repo_id, "IMPORTS")
    _equals_fresh(engine, repo_id, tmp_path)
