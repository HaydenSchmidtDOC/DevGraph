"""Graph accuracy against a live Neo4j: edge sources are pinned, and a file's re-index
retracts the edges it no longer writes and nothing else.

See docs/superpowers/specs/2026-10-08-graph-accuracy-design.md (G1, G2, G3). Every
scenario ends in "incremental equals a fresh `full_scan`" or "`full_scan`
equals the expected graph".
"""

import textwrap
from datetime import datetime, timezone
import uuid
from pathlib import Path

import pytest

from devgraph.graph.engine import _UNCLAIM_EDGE, GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.apis import extractor as apis_extractor
from devgraph.indexer.apis.extractor import Relationship
from devgraph.indexer.common import GraphRelationship
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

_TOKEN = uuid.uuid4().hex[:8]
RUNBOOK = f"ZzAccRunbook{_TOKEN}"


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        cleanup.run_cypher(f"DROP CONSTRAINT {RUNBOOK.lower()}_repo_key IF EXISTS")
        cleanup.run_cypher(f"DROP INDEX {RUNBOOK.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


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
    repo = f"zz-accuracy-{uuid.uuid4().hex[:8]}"
    ids = [repo, f"{repo}_fresh", f"{repo}-other", f"{repo}-other_fresh"]
    for each in ids:
        engine.delete_repository(each)
    yield repo
    for each in ids:
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def scan(engine, repo_id, root, **options):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, **options)


def incremental_equals_fresh(engine, repo_id, root, mentions_enabled=False, docs_path=None):
    expected = fresh_snapshot(engine, repo_id, root, mentions_enabled=mentions_enabled, docs_path=docs_path)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def edges(engine, repo_id, rel_type):
    """The snapshot's edges of one type: (label, name, file, type, label, name, file, origins),
    without their other properties."""
    return [edge[:8] for edge in graph_snapshot(engine, repo_id)[1] if edge[3] == rel_type]


# --- origins -----------------------------------------------------------------


def _calls(engine, repo_id, origin):
    engine.upsert_relationships([{
        "from_label": "Function", "from_name": "f", "rel_type": "CALLS", "to_label": "Function",
        "to_name": "g", "repo_id": repo_id, "origin": origin,
    }])


def _origins(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Function {repo_id: $repo_id, name: 'f'})-[r:CALLS]->() RETURN r.origins AS o", {"repo_id": repo_id}
    )
    return [row["o"] for row in rows]


def _unclaim(engine, repo_id, file_name):
    engine.run_cypher(
        "MATCH (:Function {repo_id: $repo_id, name: 'f'})-[r:CALLS]->() " + _UNCLAIM_EDGE,
        {"repo_id": repo_id, "f": file_name},
    )


def test_origins_are_a_sorted_set(engine, repo_id):
    engine.upsert_nodes([
        {"label": "Function", "repo_id": repo_id, "name": name, "properties": {}} for name in ("f", "g")
    ])
    for origin in ("b.py", "a.py", "b.py", None):
        _calls(engine, repo_id, origin)
    assert _origins(engine, repo_id) == [["a.py", "b.py"]]

    _unclaim(engine, repo_id, "a.py")
    assert _origins(engine, repo_id) == [["b.py"]]
    _unclaim(engine, repo_id, "b.py")
    assert _origins(engine, repo_id) == []

    _calls(engine, repo_id, None)
    assert _origins(engine, repo_id) == [None]
    _unclaim(engine, repo_id, "any.py")
    assert _origins(engine, repo_id) == []


# --- code ----------------------------------------------------------------------

APP = """\
    def helper():
        return 1


    def main():
        return helper()
"""


def app_and_worker(root):
    write(root, "src/app.py", APP)
    write(root, "src/worker.py", "def main():\n    return 2\n")


def test_same_named_functions_keep_their_own_calls(engine, repo_id, tmp_path):
    app_and_worker(tmp_path)
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "CALLS") == [
        ("Function", "main", "src/app.py", "CALLS", "Function", "helper", "src/app.py", ("src/app.py",)),
    ]

    write(tmp_path, "src/worker.py", "def main():\n    return 3\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "src/worker.py"})
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_same_named_classes_keep_their_own_bases(engine, repo_id, tmp_path):
    write(tmp_path, "a.py", "class K(Base):\n    pass\n")
    write(tmp_path, "b.py", "class K:\n    pass\n")
    write(tmp_path, "base.py", "class Base:\n    pass\n")
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == [
        ("Class", "K", "a.py", "EXTENDS", "Class", "Base", "base.py", ("a.py",)),
    ]
    incremental_equals_fresh(engine, repo_id, tmp_path)


# Spec G1 row 1 in every language: a same-named function in another file
# never takes the caller's edge, on a full scan or a re-index of either file.
STEALING = {
    "python": ("a.py", "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n",
               "b.py", "def main():\n    return {}\n"),
    "typescript": ("a.ts", "function helper() {\n  return 1;\n}\n\nfunction main() {\n  return helper();\n}\n",
                   "b.ts", "function main() {{\n  return {};\n}}\n"),
    "csharp": ("A.cs", "class A {\n  static int Helper() { return 1; }\n  static int Main() { return Helper(); }\n}\n",
               "B.cs", "class B {{\n  static int Main() {{ return {}; }}\n}}\n"),
    "cpp": ("a.cpp", "int helper() { return 1; }\n\nint main() { return helper(); }\n",
            "b.cpp", "int main() {{ return {}; }}\n"),
    "java": ("A.java", "class A {\n  static int helper() { return 1; }\n  static int main() { return helper(); }\n}\n",
             "B.java", "class B {{\n  static int main() {{ return {}; }}\n}}\n"),
    "rust": ("a.rs", "fn helper() -> i32 {\n    1\n}\n\nfn main() -> i32 {\n    helper()\n}\n",
             "b.rs", "fn main() -> i32 {{\n    {}\n}}\n"),
    "go": ("a.go", "package a\n\nfunc helper() int {\n\treturn 1\n}\n\nfunc main() int {\n\treturn helper()\n}\n",
           "b.go", "package a\n\nfunc main() int {{\n\treturn {}\n}}\n"),
    "kotlin": ("a.kt", "fun helper(): Int = 1\n\nfun main(): Int = helper()\n",
               "b.kt", "fun main(): Int = {}\n"),
}


@pytest.mark.parametrize("language", sorted(STEALING))
def test_no_cross_file_stealing(engine, repo_id, tmp_path, language):
    a, a_text, b, b_text = STEALING[language]
    (tmp_path / a).write_text(a_text)
    (tmp_path / b).write_text(b_text.format(2))
    scan(engine, repo_id, tmp_path)
    (only,) = edges(engine, repo_id, "CALLS")  # main@a -> helper@a, and nothing from b's main
    assert (only[0], only[2], only[4], only[6], only[7]) == ("Function", a, "Function", a, (a,))
    assert only[1].lower() == "main" and only[5].lower() == "helper"

    (tmp_path / b).write_text(b_text.format(3))
    index_paths(engine, repo_id, tmp_path, {tmp_path / b})
    index_paths(engine, repo_id, tmp_path, {tmp_path / a})
    assert edges(engine, repo_id, "CALLS") == [only]
    incremental_equals_fresh(engine, repo_id, tmp_path)


M_BEFORE = """\
    import pkg.util


    class Base:
        pass


    class K(Base):
        pass


    def helper():
        pass


    def main():
        helper()
"""
M_AFTER = """\
    class Base:
        pass


    class K:
        pass


    def helper():
        pass


    def main():
        pass
"""


def _removed_edges(engine, repo_id):
    return [
        edge for rel_type in ("IMPORTS", "EXTENDS", "CALLS") for edge in edges(engine, repo_id, rel_type)
        if edge[1] in ("m.py", "K", "main")
    ]


def test_removed_call_base_and_import_are_retracted(engine, repo_id, tmp_path):
    write(tmp_path, "pkg/util.py", "X = 1\n")
    write(tmp_path, "m.py", M_BEFORE)
    scan(engine, repo_id, tmp_path)
    assert len(_removed_edges(engine, repo_id)) == 3

    write(tmp_path, "m.py", M_AFTER)
    index_paths(engine, repo_id, tmp_path, {tmp_path / "m.py"})
    assert _removed_edges(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The same edit, healed by a full_scan alone.
    write(tmp_path, "m.py", M_BEFORE)
    scan(engine, repo_id, tmp_path)
    assert len(_removed_edges(engine, repo_id)) == 3
    write(tmp_path, "m.py", M_AFTER)
    scan(engine, repo_id, tmp_path)
    assert _removed_edges(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_full_scan_heals_a_polluted_graph(engine, repo_id, tmp_path):
    app_and_worker(tmp_path)
    scan(engine, repo_id, tmp_path)
    # The wrong edge bug 1 wrote, and a stale one out of a surviving node,
    # both without origins as an older index left them.
    engine.run_cypher(
        "MATCH (a:Function {repo_id: $r, name: 'main', file: 'src/worker.py'}) "
        "MATCH (b:Function {repo_id: $r, name: 'helper', file: 'src/app.py'}) "
        "MATCH (c:Function {repo_id: $r, name: 'main', file: 'src/app.py'}) "
        "MERGE (a)-[:CALLS]->(b) MERGE (b)-[:CALLS]->(c)",
        {"r": repo_id},
    )
    assert len(edges(engine, repo_id, "CALLS")) == 3

    scan(engine, repo_id, tmp_path)
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_foreign_impl_edge_survives_owner_reindex(engine, repo_id, tmp_path):
    write(tmp_path, "foo.rs", "pub struct Foo;\n")
    write(tmp_path, "display.rs", "pub trait Display {}\n")
    write(tmp_path, "conv.rs", "impl Display for Foo {}\n")
    scan(engine, repo_id, tmp_path)
    impl_edge = ("Class", "Foo", "foo.rs", "EXTENDS", "Class", "Display", "display.rs", ("conv.rs",))
    assert edges(engine, repo_id, "EXTENDS") == [impl_edge]

    write(tmp_path, "foo.rs", "// Foo.\npub struct Foo;\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "foo.rs"})
    assert edges(engine, repo_id, "EXTENDS") == [impl_edge]
    incremental_equals_fresh(engine, repo_id, tmp_path)


# --- docs notes ----------------------------------------------------------------


def note(root, rel, front):
    return write(root, rel, f"---\n{textwrap.dedent(front).strip()}\n---\n# Note\n")


ADR_LINKED = """
    type: design_decision
    id: ADR-1
    links: [src/app.py]
"""
ADR_UNLINKED = """
    type: design_decision
    id: ADR-1
"""
DOCUMENTED = ("Module", "src/app.py", "src/app.py", "DOCUMENTED_BY", "DesignDecision", "ADR-1", "docs/adr-1.md",
              ("docs/adr-1.md",))


def test_docs_note_edge_survives_code_reindex(engine, repo_id, tmp_path):
    write(tmp_path, "src/app.py", APP)
    note(tmp_path, "docs/adr-1.md", ADR_LINKED)
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]

    write(tmp_path, "src/app.py", APP + "\n\n# Touched.\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "src/app.py"}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")


def test_removed_note_links_are_retracted(engine, repo_id, tmp_path):
    write(tmp_path, "src/app.py", APP)
    adr = note(tmp_path, "docs/adr-1.md", ADR_LINKED)
    later = note(tmp_path, "docs/adr-2.md", "type: design_decision\nid: ADR-2\nsupersedes: ADR-1")
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]
    assert len(edges(engine, repo_id, "SUPERSEDES")) == 1

    note(tmp_path, "docs/adr-1.md", ADR_UNLINKED)
    index_paths(engine, repo_id, tmp_path, {adr}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")

    note(tmp_path, "docs/adr-2.md", "type: design_decision\nid: ADR-2")
    index_paths(engine, repo_id, tmp_path, {later}, docs_path="docs")
    assert edges(engine, repo_id, "SUPERSEDES") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")

    # A legacy edge (no origins) the note no longer writes goes on a full_scan.
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'src/app.py'}) MATCH (d:DesignDecision {repo_id: $r, name: 'ADR-1'}) "
        "MERGE (m)-[:DOCUMENTED_BY]->(d)",
        {"r": repo_id},
    )
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")


def test_doc_note_replace_is_repo_scoped(engine, repo_id, tmp_path):
    other = f"{repo_id}-other"
    roots = {repo_id: tmp_path / "one", other: tmp_path / "two"}
    for repo, root in roots.items():
        write(root, "src/app.py", APP)
        note(root, "docs/adr-1.md", ADR_LINKED)
        scan(engine, repo, root, docs_path="docs")
    others_before = edges(engine, other, "DOCUMENTED_BY")
    assert others_before == [DOCUMENTED]

    adr = note(roots[repo_id], "docs/adr-1.md", ADR_UNLINKED)
    index_paths(engine, repo_id, roots[repo_id], {adr}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    assert edges(engine, other, "DOCUMENTED_BY") == others_before
    for repo, root in roots.items():
        incremental_equals_fresh(engine, repo, root, docs_path="docs")


# --- edges a re-index must keep --------------------------------------------------

ROUTED_APP = """\
    from fastapi import FastAPI

    app = FastAPI()


    def helper():
        return 1


    @app.get("/items")
    def list_items():
        return helper()
"""


def test_reindex_keeps_incoming_and_foreign_edges(engine, repo_id, tmp_path):
    write(tmp_path, "devgraph.schema.yaml", f"""\
        version: 1
        node_types:
          - label: {RUNBOOK}
            key: [path]
            metadata: [{{name: path}}]
            source: {{provider: docs, paths: ["runbooks/*.md"]}}
        relationships:
          - type: ZZ_RUNS
            provider: docs
            from: {RUNBOOK}
            to: Module
            field: module
    """)
    write(tmp_path, "runbooks/app.md", "---\nmodule: src/app.py\n---\n# Runbook\n")
    write(tmp_path, "notes.md", "Call `helper` first.\n")
    app = write(tmp_path, "src/app.py", ROUTED_APP)
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'src/app.py'}) "
        "MERGE (c:Commit {repo_id: $r, name: 'c0ffee'}) MERGE (c)-[:MODIFIES]->(m)",
        {"r": repo_id},
    )

    def kept():
        return {
            "mentions": [e for e in edges(engine, repo_id, "MENTIONS") if e[5] == "helper"],
            "provider": edges(engine, repo_id, "ZZ_RUNS"),
            "implements": edges(engine, repo_id, "IMPLEMENTS"),
            "modifies": engine.run_cypher(
                "MATCH (:Commit {repo_id: $r})-[x:MODIFIES]->(m:Module {repo_id: $r}) RETURN m.name AS m",
                {"r": repo_id},
            ),
        }

    before = kept()
    assert all(before.values()), before
    write(tmp_path, "src/app.py", ROUTED_APP + "\n\n# Touched.\n")
    index_paths(engine, repo_id, tmp_path, {app}, mentions_enabled=True)
    assert kept() == before
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


# --- shared nodes, compose files and Containerfiles (G3) ------------------------

API_AND_DB = """\
    services:
      api:
        image: python:3.12
      db:
        image: postgres:16
"""
API_ONLY = """\
    services:
      api:
        image: python:3.12
"""


def node(engine, repo_id, label, name):
    """[(elementId, sources)] of the nodes with this label and name."""
    return [
        (row["id"], row["sources"]) for row in engine.run_cypher(
            f"MATCH (n:{label} {{repo_id: $r, name: $n}}) RETURN elementId(n) AS id, n.sources AS sources",
            {"r": repo_id, "n": name},
        )
    ]


def _db_is_gone(engine, repo_id):
    assert node(engine, repo_id, "Service", "db") == []
    assert node(engine, repo_id, "Container", "postgres") == []
    assert [e for e in edges(engine, repo_id, "RUNS") if e[1] == "db"] == []


def test_removed_service_is_retracted(engine, repo_id, tmp_path):
    compose = write(tmp_path, "compose.yaml", API_AND_DB)
    scan(engine, repo_id, tmp_path)
    assert len(node(engine, repo_id, "Container", "postgres")) == 1

    write(tmp_path, "compose.yaml", API_ONLY)
    index_paths(engine, repo_id, tmp_path, {compose})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    _db_is_gone(engine, repo_id)


def test_full_scan_drops_a_removed_service(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", API_AND_DB)
    scan(engine, repo_id, tmp_path)

    write(tmp_path, "compose.yaml", API_ONLY)
    scan(engine, repo_id, tmp_path)
    _db_is_gone(engine, repo_id)
    assert edges(engine, repo_id, "RUNS") == [
        ("Service", "api", "compose.yaml", "RUNS", "Container", "python", "", ("compose.yaml",)),
    ]
    incremental_equals_fresh(engine, repo_id, tmp_path)


DEPENDENT_SERVICES = """\
    services:
      web:
        image: nginx
        depends_on: [api]
        links: ["cache:redis"]
      api:
        image: python:3.12
        depends_on:
          db:
            condition: service_healthy
      db:
        image: postgres:16
        depends_on: [api]
      cache:
        image: redis
"""


def test_compose_service_dependencies_reach_the_tools(engine, repo_id, tmp_path):
    from devgraph.mcp.tools import find_dependency_cycles, get_service_dependencies, impact_analysis

    compose = write(tmp_path, "compose.yaml", DEPENDENT_SERVICES)
    scan(engine, repo_id, tmp_path)
    pinned = ("compose.yaml",)
    assert sorted(e for e in edges(engine, repo_id, "DEPENDS_ON")) == [
        ("Service", "api", "compose.yaml", "DEPENDS_ON", "Service", "db", "compose.yaml", pinned),
        ("Service", "db", "compose.yaml", "DEPENDS_ON", "Service", "api", "compose.yaml", pinned),
        ("Service", "web", "compose.yaml", "DEPENDS_ON", "Service", "api", "compose.yaml", pinned),
        ("Service", "web", "compose.yaml", "DEPENDS_ON", "Service", "cache", "compose.yaml", pinned),
    ]

    deps = get_service_dependencies(engine, repo_id, "web")["dependencies"]
    assert {(d["name"], d["type"]) for d in deps} >= {("api", "Service"), ("cache", "Service")}
    impact = impact_analysis(engine, repo_id, "db")
    assert "api" in {d["name"] for d in impact["direct_dependents"]["results"]}
    assert "web" in {d["name"] for d in impact["transitive_dependents"]["results"]}
    cycles = find_dependency_cycles(engine, repo_id, "DEPENDS_ON")
    assert cycles["count"] == 1
    incremental_equals_fresh(engine, repo_id, tmp_path)

    write(tmp_path, "compose.yaml", DEPENDENT_SERVICES.replace("        depends_on: [api]\n      cache", "      cache"))
    index_paths(engine, repo_id, tmp_path, {compose})
    assert ("db", "api") not in {(e[1], e[5]) for e in edges(engine, repo_id, "DEPENDS_ON")}
    assert find_dependency_cycles(engine, repo_id, "DEPENDS_ON")["count"] == 0
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_removed_service_unclaims_its_image(engine, repo_id, tmp_path):
    # `compose.override.yaml` is not a compose name the indexer routes, so
    # the second claimant is a compose file in another folder.
    compose = write(tmp_path, "compose.yaml", API_AND_DB)
    write(tmp_path, "ops/compose.yaml", "services:\n  db:\n    image: postgres:16\n")
    scan(engine, repo_id, tmp_path)
    assert [sources for _id, sources in node(engine, repo_id, "Container", "postgres")] == [
        ["compose.yaml", "ops/compose.yaml"]
    ]

    write(tmp_path, "compose.yaml", API_ONLY)
    index_paths(engine, repo_id, tmp_path, {compose})
    assert [sources for _id, sources in node(engine, repo_id, "Container", "postgres")] == [["ops/compose.yaml"]]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def _mentions_of(engine, repo_id, name):
    return [e for e in edges(engine, repo_id, "MENTIONS") if e[5] == name]


def test_container_mentions_survive_dockerfile_reindex(engine, repo_id, tmp_path):
    dockerfile = write(tmp_path, "Dockerfile", "FROM postgres:16\n")
    write(tmp_path, "notes.md", "Runs on `postgres`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    before = node(engine, repo_id, "Container", "postgres")
    assert len(before) == 1
    assert len(_mentions_of(engine, repo_id, "postgres")) == 1

    write(tmp_path, "Dockerfile", "FROM postgres:16\nRUN echo ready\n")
    index_paths(engine, repo_id, tmp_path, {dockerfile}, mentions_enabled=True)
    assert len(_mentions_of(engine, repo_id, "postgres")) == 1
    assert node(engine, repo_id, "Container", "postgres") == before
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


def test_container_regains_mentions_when_re_added(engine, repo_id, tmp_path):
    # The last claim going deletes the Container; its next claimant adds it
    # back, and the Markdown that mentions it links again.
    dockerfile = write(tmp_path, "Dockerfile", "FROM postgres:16\n")
    write(tmp_path, "notes.md", "Runs on `postgres`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)

    write(tmp_path, "Dockerfile", "# No stages.\n")
    index_paths(engine, repo_id, tmp_path, {dockerfile}, mentions_enabled=True)
    assert node(engine, repo_id, "Container", "postgres") == []
    write(tmp_path, "Dockerfile", "FROM postgres:16\n")
    index_paths(engine, repo_id, tmp_path, {dockerfile}, mentions_enabled=True)
    assert len(_mentions_of(engine, repo_id, "postgres")) == 1
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


REDIS_APP = """\
    import redis

    client = redis.from_url("redis://cache:6379/0")
"""


def test_datastore_mentions_survive_python_reindex(engine, repo_id, tmp_path):
    app = write(tmp_path, "app.py", REDIS_APP)
    write(tmp_path, "notes.md", "Sessions live in `Redis`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    before = node(engine, repo_id, "Cache", "Redis")
    assert len(before) == 1
    assert len(_mentions_of(engine, repo_id, "Redis")) == 1

    write(tmp_path, "app.py", REDIS_APP + "\n\n# Touched.\n")
    index_paths(engine, repo_id, tmp_path, {app}, mentions_enabled=True)
    assert len(_mentions_of(engine, repo_id, "Redis")) == 1
    assert node(engine, repo_id, "Cache", "Redis") == before
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


BUILT_API = """\
    services:
      api:
        build: ./services/api
"""


def _uses(engine, repo_id):
    return [e for e in edges(engine, repo_id, "USES") if e[0] == "Service"]


def _uses_edge(*origins):
    return ("Service", "api", "compose.yaml", "USES", "Cache", "Redis", "", tuple(origins))


def test_compose_reindex_keeps_owning_service_uses(engine, repo_id, tmp_path):
    compose = write(tmp_path, "compose.yaml", BUILT_API)
    write(tmp_path, "services/api/app.py", REDIS_APP)
    scan(engine, repo_id, tmp_path)
    assert _uses(engine, repo_id) == [_uses_edge("services/api/app.py")]

    write(tmp_path, "compose.yaml", BUILT_API + "      worker:\n        image: busybox:1\n")
    index_paths(engine, repo_id, tmp_path, {compose})
    assert _uses(engine, repo_id) == [_uses_edge("services/api/app.py")]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_shared_uses_edge_has_two_writers(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", BUILT_API)
    a = write(tmp_path, "services/api/a.py", REDIS_APP)
    b = write(tmp_path, "services/api/b.py", REDIS_APP)
    scan(engine, repo_id, tmp_path)
    assert _uses(engine, repo_id) == [_uses_edge("services/api/a.py", "services/api/b.py")]

    write(tmp_path, "services/api/a.py", "def a():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {a})
    assert _uses(engine, repo_id) == [_uses_edge("services/api/b.py")]
    incremental_equals_fresh(engine, repo_id, tmp_path)

    b.unlink()
    remove_paths(engine, repo_id, tmp_path, {b})
    assert _uses(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The mirror image: both restored, then a.py deleted first.
    write(tmp_path, "services/api/a.py", REDIS_APP)
    write(tmp_path, "services/api/b.py", REDIS_APP)
    index_paths(engine, repo_id, tmp_path, {a, b})
    assert _uses(engine, repo_id) == [_uses_edge("services/api/a.py", "services/api/b.py")]
    a.unlink()
    remove_paths(engine, repo_id, tmp_path, {a})
    assert _uses(engine, repo_id) == [_uses_edge("services/api/b.py")]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def _routed(handler):
    return f"""\
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/items")
        def {handler}():
            return 1
    """


# The route and its handler both go.
UNROUTED = "def helper():\n    return 1\n"


def _endpoint_edges(engine, repo_id):
    return sorted(
        (e[3], e[4], e[5], e[7]) for rel_type in ("IMPLEMENTS", "CALLS")
        for e in edges(engine, repo_id, rel_type) if e[0] == "Endpoint"
    )


def test_removed_route_retracts_endpoint_edges(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", BUILT_API)
    a = write(tmp_path, "services/api/a.py", _routed("items_a"))
    b = write(tmp_path, "services/api/b.py", _routed("items_b"))
    scan(engine, repo_id, tmp_path)
    implements_a = ("IMPLEMENTS", "Function", "items_a", ("services/api/a.py",))
    assert implements_a in _endpoint_edges(engine, repo_id)

    # b.py still routes GET /items, so the Endpoint and its CALLS stay with
    # b.py alone.
    write(tmp_path, "services/api/a.py", UNROUTED)
    index_paths(engine, repo_id, tmp_path, {a})
    assert implements_a not in _endpoint_edges(engine, repo_id)
    assert ("CALLS", "Service", "api", ("services/api/b.py",)) in _endpoint_edges(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)

    write(tmp_path, "services/api/b.py", UNROUTED)
    index_paths(engine, repo_id, tmp_path, {b})
    assert _endpoint_edges(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


DJANGO_ROUTE = """\
    from django.urls import path

    urlpatterns = [path("search/", search)]
"""


def _search_nodes(engine, repo_id):
    return sorted(
        (row["file"] or "", row["type"] or "", tuple(row["sources"] or ()))
        for row in engine.run_cypher(
            "MATCH (f:Function {repo_id: $r, name: 'search'}) "
            "RETURN f.file AS file, f.type AS type, f.sources AS sources",
            {"r": repo_id},
        )
    )


@pytest.mark.parametrize("first", ["a/routes.py", "b/x.py"])
def test_a_handler_stub_never_claims_a_same_named_function(engine, repo_id, tmp_path, first):
    """A route file's handler stub is its own file-less Function: it never
    MERGEs onto another file's `def search()`, whichever is written first."""
    write(tmp_path, "a/routes.py", DJANGO_ROUTE)
    write(tmp_path, "b/x.py", "def search():\n    return 1\n")
    scan(engine, repo_id, tmp_path)
    expected = graph_snapshot(engine, repo_id)
    assert _search_nodes(engine, repo_id) == [("", "view", ("a/routes.py",)), ("b/x.py", "function", ())]

    engine.delete_repository(repo_id)
    second = "b/x.py" if first == "a/routes.py" else "a/routes.py"
    for rel in (first, second):
        index_paths(engine, repo_id, tmp_path, {tmp_path / rel})
    assert graph_snapshot(engine, repo_id) == expected
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_a_handler_that_loses_its_route_decorator_drops_the_stub(engine, repo_id, tmp_path):
    routes = write(tmp_path, "api/routes.py", _routed("search"))
    scan(engine, repo_id, tmp_path)
    assert _search_nodes(engine, repo_id) == [("", "handler", ("api/routes.py",)), ("api/routes.py", "function", ())]

    write(tmp_path, "api/routes.py", "def search():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {routes})
    assert _search_nodes(engine, repo_id) == [("api/routes.py", "function", ())]
    incremental_equals_fresh(engine, repo_id, tmp_path)


FLASK_ROUTE = """\
    from flask import Flask

    app = Flask(__name__)


    @app.route("/items")
    def search():
        return 1
"""


def _implements(engine, repo_id):
    return sorted((e[1], e[5], e[6], e[7]) for e in edges(engine, repo_id, "IMPLEMENTS"))


@pytest.mark.parametrize("route", [_routed("search"), FLASK_ROUTE], ids=["fastapi", "flask"])
def test_a_route_implements_only_its_own_files_handler(engine, repo_id, tmp_path, route):
    """A FastAPI/Flask handler's `def` is in the route's file, so the Endpoint
    IMPLEMENTS that Function and the stub, never another file's same-named one,
    in a fresh scan and through every incremental path that relinks it."""
    expected = [
        ("GET /items", "search", "", ("api/routes.py",)),
        ("GET /items", "search", "api/routes.py", ("api/routes.py",)),
    ]
    write(tmp_path, "api/routes.py", route)
    write(tmp_path, "other/x.py", "def search():\n    return 2\n")
    scan(engine, repo_id, tmp_path)
    assert _implements(engine, repo_id) == expected

    # A later file adding a same-named Function re-indexes the route file
    # (its stub's name was added) and must not gain an edge either.
    later = write(tmp_path, "later/y.py", "def search():\n    return 3\n")
    index_paths(engine, repo_id, tmp_path, {later})
    assert _implements(engine, repo_id) == expected
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # A second route file newly claiming the Endpoint adds it to the batch,
    # which relinks the first file's by-name Endpoint edges.
    second = write(tmp_path, "api/more.py", route.replace("def search", "def find"))
    index_paths(engine, repo_id, tmp_path, {second})
    assert [e for e in _implements(engine, repo_id) if e[1] == "search"] == expected
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_a_by_name_call_relinks_to_an_added_handler_stub(engine, repo_id, tmp_path):
    """A route file added later brings a stub and a Function named `search`;
    another file's by-name call relinks to both, as a fresh scan links it."""
    write(tmp_path, "c.py", "def caller():\n    return api.search()\n")
    scan(engine, repo_id, tmp_path)
    routes = write(tmp_path, "api/routes.py", _routed("search"))
    index_paths(engine, repo_id, tmp_path, {routes})
    calls = sorted((e[5], e[6]) for e in edges(engine, repo_id, "CALLS") if e[1] == "caller")
    assert calls == [("search", ""), ("search", "api/routes.py")]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_the_index_upgrade_heals_cross_file_route_implements(engine, repo_id, tmp_path, monkeypatch):
    """A graph indexed before route IMPLEMENTS were pinned (format 2) has the
    Endpoint implementing every same-named Function; catch_up's automatic
    upgrade (a full_scan) retracts those and leaves the pinned edges."""
    write(tmp_path, "api/routes.py", _routed("search"))
    write(tmp_path, "other/x.py", "def search():\n    return 2\n")

    def bare(endpoint_id, handler_name, filename):
        return [Relationship("Endpoint", endpoint_id, "IMPLEMENTS", "Function", handler_name)]

    with monkeypatch.context() as old_extractor:
        old_extractor.setattr(apis_extractor, "_pinned_implements", bare)
        scan(engine, repo_id, tmp_path)
    engine.set_index_format(repo_id, 2)
    assert ("GET /items", "search", "other/x.py", ("api/routes.py",)) in _implements(engine, repo_id)
    assert dispatch.index_outdated(engine, repo_id)

    dispatch.catch_up(engine, repo_id, tmp_path, since=datetime.now(timezone.utc))
    assert _implements(engine, repo_id) == [
        ("GET /items", "search", "", ("api/routes.py",)),
        ("GET /items", "search", "api/routes.py", ("api/routes.py",)),
    ]
    assert not dispatch.index_outdated(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_a_django_route_still_implements_every_same_named_view(engine, repo_id, tmp_path):
    write(tmp_path, "a/routes.py", DJANGO_ROUTE)
    write(tmp_path, "b/views.py", "def search():\n    return 1\n")
    write(tmp_path, "c/x.py", "def search():\n    return 2\n")
    scan(engine, repo_id, tmp_path)
    assert _implements(engine, repo_id) == [
        ("* search/", "search", "", ("a/routes.py",)),
        ("* search/", "search", "b/views.py", ("a/routes.py",)),
        ("* search/", "search", "c/x.py", ("a/routes.py",)),
    ]


def test_containerfile_retracts_a_removed_stage(engine, repo_id, tmp_path):
    containerfile = write(tmp_path, "Containerfile", """\
        FROM golang:1.22 AS build
        RUN go build -o /app .

        FROM alpine:3.20
        COPY --from=build /app /app
    """)
    scan(engine, repo_id, tmp_path)

    write(tmp_path, "Containerfile", "FROM alpine:3.20\nCOPY app /app\n")
    index_paths(engine, repo_id, tmp_path, {containerfile})
    incremental_equals_fresh(engine, repo_id, tmp_path)


# --- mentions (G4) -----------------------------------------------------------

MENTIONED = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"


def _mentioned_names(engine, repo_id):
    return sorted(e[5] for e in edges(engine, repo_id, "MENTIONS"))


def test_removed_mention_is_retracted(engine, repo_id, tmp_path):
    write(tmp_path, "app.py", MENTIONED)
    notes = write(tmp_path, "notes.md", "Uses `helper` and `other`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["helper", "other"]

    write(tmp_path, "notes.md", "Uses `other`.\n")
    index_paths(engine, repo_id, tmp_path, {notes}, mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["other"]
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


def test_mentions_replace_is_document_and_repo_scoped(engine, repo_id, tmp_path):
    other = f"{repo_id}-other"
    roots = {repo_id: tmp_path / "one", other: tmp_path / "two"}
    for repo, root in roots.items():
        write(root, "app.py", MENTIONED)
        write(root, "notes.md", "Uses `helper` and `other`.\n")
        write(root, "more.md", "Uses `helper` and `other`.\n")
        scan(engine, repo, root, mentions_enabled=True)
    others_before = edges(engine, other, "MENTIONS")
    more_before = [e for e in edges(engine, repo_id, "MENTIONS") if e[1] == "more.md"]
    assert len(others_before) == 4 and len(more_before) == 2

    notes = write(roots[repo_id], "notes.md", "Uses `other`.\n")
    index_paths(engine, repo_id, roots[repo_id], {notes}, mentions_enabled=True)
    mentions = edges(engine, repo_id, "MENTIONS")
    assert sorted(e[5] for e in mentions if e[1] == "notes.md") == ["other"]
    assert [e for e in mentions if e[1] == "more.md"] == more_before
    assert edges(engine, other, "MENTIONS") == others_before
    for repo, root in roots.items():
        incremental_equals_fresh(engine, repo, root, mentions_enabled=True)


def test_full_scan_drops_a_removed_mention(engine, repo_id, tmp_path):
    write(tmp_path, "app.py", MENTIONED)
    write(tmp_path, "notes.md", "Uses `helper` and `other`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)

    write(tmp_path, "notes.md", "Uses `other`.\n")
    full_scan(engine, repo_id, tmp_path, mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["other"]
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


def test_mention_relink_stays_additive(engine, repo_id, tmp_path):
    app = write(tmp_path, "app.py", MENTIONED)
    write(tmp_path, "notes.md", "Uses `other` and `later`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["other"]

    write(tmp_path, "app.py", MENTIONED + "\n\ndef later():\n    return 3\n")
    index_paths(engine, repo_id, tmp_path, {app}, mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["later", "other"]
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


def test_mentions_replace_keeps_the_docs_note(engine, repo_id, tmp_path):
    write(tmp_path, "app.py", MENTIONED)
    adr = write(tmp_path, "docs/adr-1.md", "---\ntype: design_decision\nid: ADR-1\n---\n# Note\nSee `helper`.\n")
    scan(engine, repo_id, tmp_path, docs_path="docs", mentions_enabled=True)
    assert _mentioned_names(engine, repo_id) == ["helper"]

    write(tmp_path, "docs/adr-1.md", "---\ntype: design_decision\nid: ADR-1\n---\n# Note\nNothing.\n")
    index_paths(engine, repo_id, tmp_path, {adr}, docs_path="docs", mentions_enabled=True)
    assert node(engine, repo_id, "DesignDecision", "ADR-1")
    assert _mentioned_names(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True, docs_path="docs")


# --- relinking by-name edges (G5) ----------------------------------------------


A_IMPORTS_B = """\
    from pkg.b import helper


    def main():
        return helper()
"""


def test_import_target_deleted_and_restored(engine, repo_id, tmp_path):
    write(tmp_path, "pkg/a.py", A_IMPORTS_B)
    b = write(tmp_path, "pkg/b.py", "def helper():\n    return 1\n")
    scan(engine, repo_id, tmp_path)

    b.unlink()
    remove_paths(engine, repo_id, tmp_path, {b})
    write(tmp_path, "pkg/b.py", "def helper():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {b})

    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert ("Module", "pkg/a.py", "pkg/a.py", "IMPORTS", "Module", "pkg/b.py", "pkg/b.py", ("pkg/a.py",)) in edges(
        engine, repo_id, "IMPORTS"
    )
    assert edges(engine, repo_id, "CALLS") == [
        ("Function", "main", "pkg/a.py", "CALLS", "Function", "helper", "pkg/b.py", ("pkg/a.py",)),
    ]


def test_module_added_after_its_importer(engine, repo_id, tmp_path):
    write(tmp_path, "pkg/a.py", "import lib.b\n")
    scan(engine, repo_id, tmp_path)

    added = write(tmp_path, "lib/b.py", "X = 1\n")
    index_paths(engine, repo_id, tmp_path, {added})
    assert [e for e in edges(engine, repo_id, "IMPORTS") if e[5] == "lib/b.py"]
    incremental_equals_fresh(engine, repo_id, tmp_path)


@pytest.mark.parametrize("importer, specifier, target", [
    ("src/main.ts", "import { util } from './util.js';", "src/util.ts"),
    ("src/main.mts", "import { util } from './util.mjs';", "src/util.mts"),
    ("src/main.cjs", "const util = require('./util.cjs');", "src/util.cts"),
], ids=["js-to-ts", "mjs-to-mts", "cjs-to-cts"])
def test_explicit_extension_import_reaches_its_typescript_source(engine, repo_id, tmp_path, importer, specifier, target):
    write(tmp_path, importer, specifier + "\n")
    scan(engine, repo_id, tmp_path)
    assert not [e for e in edges(engine, repo_id, "IMPORTS") if e[5] == target]

    added = write(tmp_path, target, "export function util() {\n  return 1;\n}\n")
    index_paths(engine, repo_id, tmp_path, {added})
    assert [e for e in edges(engine, repo_id, "IMPORTS") if (e[1], e[5]) == (importer, target)]
    incremental_equals_fresh(engine, repo_id, tmp_path)


@pytest.mark.parametrize("files", [
    {
        "app/k.py": "from base.b import Base\n\n\nclass K(Base):\n    pass\n",
        "base/b.py": "class Base:\n    pass\n",
    },
    {
        "app/K.java": "package app;\n\nimport base.Base;\n\npublic class K extends Base {}\n",
        "base/Base.java": "package base;\n\npublic class Base {}\n",
    },
], ids=["python", "java"])
def test_supertype_added_in_another_directory(engine, repo_id, tmp_path, files):
    (sub_rel, sub_text), (base_rel, base_text) = files.items()
    write(tmp_path, sub_rel, sub_text)
    scan(engine, repo_id, tmp_path)

    base = write(tmp_path, base_rel, base_text)
    index_paths(engine, repo_id, tmp_path, {base})
    assert [e for e in edges(engine, repo_id, "EXTENDS") if (e[1], e[6]) == ("K", base_rel)]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_second_same_named_function_links_outside_callers(engine, repo_id, tmp_path):
    write(tmp_path, "a.py", "def main():\n    return obj.helper()\n")
    write(tmp_path, "b.py", "def helper():\n    return 1\n")
    scan(engine, repo_id, tmp_path)

    c = write(tmp_path, "c.py", "def helper():\n    return 2\n")
    index_paths(engine, repo_id, tmp_path, {c})
    assert ("Function", "main", "a.py", "CALLS", "Function", "helper", "c.py", ("a.py",)) in edges(
        engine, repo_id, "CALLS"
    )
    incremental_equals_fresh(engine, repo_id, tmp_path)


def rust_trio(root):
    write(root, "foo.rs", "pub struct Foo;\n")
    write(root, "display.rs", "pub trait Display {}\n")
    write(root, "conv.rs", "impl Display for Foo {}\n")


IMPL_EDGE = ("Class", "Foo", "foo.rs", "EXTENDS", "Class", "Display", "display.rs", ("conv.rs",))


def test_restored_type_regains_foreign_impl(engine, repo_id, tmp_path):
    rust_trio(tmp_path)
    scan(engine, repo_id, tmp_path)
    foo = tmp_path / "foo.rs"

    foo.unlink()
    remove_paths(engine, repo_id, tmp_path, {foo})
    assert edges(engine, repo_id, "EXTENDS") == []
    write(tmp_path, "foo.rs", "pub struct Foo;\n")
    index_paths(engine, repo_id, tmp_path, {foo})
    assert edges(engine, repo_id, "EXTENDS") == [IMPL_EDGE]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_removed_foreign_impl_is_retracted(engine, repo_id, tmp_path):
    rust_trio(tmp_path)
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == [IMPL_EDGE]

    conv = write(tmp_path, "conv.rs", "// No impls left.\n")
    index_paths(engine, repo_id, tmp_path, {conv})
    assert edges(engine, repo_id, "EXTENDS") == []
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The same edit, healed by a full_scan alone.
    write(tmp_path, "conv.rs", "impl Display for Foo {}\n")
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == [IMPL_EDGE]
    write(tmp_path, "conv.rs", "// No impls left.\n")
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == []
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # And with conv.rs deleted.
    write(tmp_path, "conv.rs", "impl Display for Foo {}\n")
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == [IMPL_EDGE]
    conv.unlink()
    remove_paths(engine, repo_id, tmp_path, {conv})
    assert edges(engine, repo_id, "EXTENDS") == []


def test_symbol_moved_between_files_in_one_batch(engine, repo_id, tmp_path):
    write(tmp_path, "a.py", "def main():\n    return obj.helper()\n")
    b = write(tmp_path, "b.py", "def helper():\n    return 1\n")
    write(tmp_path, "notes.md", "Uses `helper`.\n")
    scan(engine, repo_id, tmp_path, mentions_enabled=True)

    write(tmp_path, "b.py", "X = 1\n")
    c = write(tmp_path, "c.py", "def helper():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {b, c}, mentions_enabled=True)
    assert edges(engine, repo_id, "CALLS") == [
        ("Function", "main", "a.py", "CALLS", "Function", "helper", "c.py", ("a.py",)),
    ]
    assert [e[6] for e in edges(engine, repo_id, "MENTIONS") if e[5] == "helper"] == ["c.py"]
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)


def test_relink_reads_no_files(engine, repo_id, tmp_path, monkeypatch):
    write(tmp_path, "a.py", "def main():\n    return obj.helper()\n")
    write(tmp_path, "other.py", "def unrelated():\n    return 0\n")
    scan(engine, repo_id, tmp_path)

    b = write(tmp_path, "b.py", "def helper():\n    return 1\n")
    reads = []
    real_read_text, real_dispatch_read = Path.read_text, dispatch._read_text

    def read_text(self, *args, **kwargs):
        reads.append(Path(self))
        return real_read_text(self, *args, **kwargs)

    def dispatch_read(path):
        reads.append(Path(path))
        return real_dispatch_read(path)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(dispatch, "_read_text", dispatch_read)
    index_paths(engine, repo_id, tmp_path, {b})
    monkeypatch.undo()

    root = tmp_path.resolve()
    outside = [p for p in reads if p.resolve().is_relative_to(root) and p.resolve() != b.resolve()]
    assert outside == []
    assert ("Function", "main", "a.py", "CALLS", "Function", "helper", "b.py", ("a.py",)) in edges(
        engine, repo_id, "CALLS"
    )


# Hand-built pinned CALLS out of app.py's `main` (no extractor writes them
# yet): to one exact file, and to a package directory less its exact files.
def _with_pinned_calls(monkeypatch, rows):
    real = dispatch.extract_python_file

    def extract(content, rel_path, repo_id):
        result = real(content, rel_path, repo_id)
        if rel_path == "app.py":
            result.relationships += [
                GraphRelationship(
                    from_label="Function", from_name="main", rel_type="CALLS", to_label="Function",
                    to_name=name, repo_id=repo_id, properties=properties, from_file="app.py", to_file=to_file,
                    origin="app.py", exclude=exclude,
                )
                for name, to_file, properties, exclude in rows
            ]
        return result

    monkeypatch.setattr(dispatch, "extract_python_file", extract)


def _call_props(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Function {repo_id: $r, name: 'main'})-[c:CALLS]->(b) "
        "RETURN b.name AS name, b.file AS file, c.confidence AS confidence, c.caller_class AS caller_class",
        {"r": repo_id},
    )
    return sorted((row["name"], row["file"], row["confidence"], row["caller_class"]) for row in rows)


def test_a_pinned_call_relinks_when_its_file_is_restored(engine, repo_id, tmp_path, monkeypatch):
    _with_pinned_calls(monkeypatch, [("helper", "pkg/b.py", {"confidence": "resolved", "caller_class": "C"}, None)])
    write(tmp_path, "app.py", "def main():\n    return 0\n")
    b = write(tmp_path, "pkg/b.py", "def helper():\n    return 1\n")
    write(tmp_path, "other/b.py", "def helper():\n    return 2\n")
    scan(engine, repo_id, tmp_path)
    linked = [("helper", "pkg/b.py", "resolved", "C")]
    assert _call_props(engine, repo_id) == linked

    b.unlink()
    remove_paths(engine, repo_id, tmp_path, {b})
    assert _call_props(engine, repo_id) == []
    write(tmp_path, "pkg/b.py", "def helper():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {b})
    assert _call_props(engine, repo_id) == linked
    incremental_equals_fresh(engine, repo_id, tmp_path)


@pytest.mark.parametrize("one_batch", [True, False], ids=["one batch", "two batches"])
def test_a_package_call_follows_its_function_within_the_package(engine, repo_id, tmp_path, monkeypatch, one_batch):
    _with_pinned_calls(monkeypatch, [
        ("f", "pkg/__init__.py", {"confidence": "resolved"}, None),
        ("f", "pkg/", {"confidence": "package"}, ["app.py", "pkg/__init__.py"]),
    ])
    write(tmp_path, "app.py", "def main():\n    return 0\n")
    write(tmp_path, "pkg/__init__.py", "def f():\n    return 0\n")
    impl = write(tmp_path, "pkg/impl.py", "def f():\n    return 1\n")
    write(tmp_path, "outside/x.py", "def f():\n    return 2\n")
    write(tmp_path, "pkgextra/y.py", "def f():\n    return 3\n")
    scan(engine, repo_id, tmp_path)
    assert _call_props(engine, repo_id) == [
        ("f", "pkg/__init__.py", "resolved", None), ("f", "pkg/impl.py", "package", None),
    ]

    write(tmp_path, "pkg/impl.py", "X = 1\n")
    other = write(tmp_path, "pkg/sub/other.py", "def f():\n    return 1\n")
    if one_batch:
        index_paths(engine, repo_id, tmp_path, {impl, other})
    else:
        index_paths(engine, repo_id, tmp_path, {impl})
        index_paths(engine, repo_id, tmp_path, {other})
    assert _call_props(engine, repo_id) == [
        ("f", "pkg/__init__.py", "resolved", None), ("f", "pkg/sub/other.py", "package", None),
    ]
    incremental_equals_fresh(engine, repo_id, tmp_path)


def _spy(monkeypatch, cls, name):
    calls = []
    real = getattr(cls, name)

    def spy(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, spy)
    return calls


def test_full_scan_skips_relink(engine, repo_id, tmp_path, monkeypatch):
    write(tmp_path, "a.py", "def main():\n    return obj.helper()\n")
    calls = _spy(monkeypatch, GraphEngine, "find_name_refs")
    scan(engine, repo_id, tmp_path)
    assert calls == []

    b = write(tmp_path, "b.py", "def helper():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {b})
    assert len(calls) == 1


def _module_name_refs(engine, repo_id, name):
    (row,) = engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: $n}) "
        "RETURN m.name_refs AS refs, m.name_ref_targets AS targets, m.name_ref_sources AS sources",
        {"r": repo_id, "n": name},
    )
    return row["refs"], row["targets"], row["sources"]


def test_pass_two_omits_name_refs(engine, repo_id, tmp_path, monkeypatch):
    scan(engine, repo_id, tmp_path)
    a = write(tmp_path, "a.py", "def main():\n    return obj.helper()\n")
    calls = _spy(monkeypatch, GraphEngine, "upsert_nodes")
    index_paths(engine, repo_id, tmp_path, {a})
    modules = [n for (nodes,) in calls for n in nodes if n["label"] == "Module"]
    assert modules and not any("name_refs" in n["properties"] for n in modules)
    assert _module_name_refs(engine, repo_id, "a.py")[0] == ["CALLS\x1fFunction\x1fmain\x1fa.py\x1fFunction\x1fhelper\x1f\x1f\x1fname"]


def test_name_refs_written_empty(engine, repo_id, tmp_path):
    write(tmp_path, "plain.py", "X = 1\n")
    caller = write(tmp_path, "caller.py", "def main():\n    return obj.helper()\n")
    scan(engine, repo_id, tmp_path)
    assert _module_name_refs(engine, repo_id, "plain.py") == ([], [], [])
    assert _module_name_refs(engine, repo_id, "caller.py")[1] == ["helper"]

    write(tmp_path, "caller.py", "def main():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {caller})
    assert _module_name_refs(engine, repo_id, "caller.py") == ([], [], [])


@pytest.mark.parametrize("pinned", [False, True], ids=["by name", "path pins"])
def test_name_ref_relink_benchmark(engine, repo_id, monkeypatch, pinned):
    """The relink read over 5,000 Modules, each with 60 by-name edges drawn
    from 2,000 names, then the parse and the upsert of what it found. With
    `pinned`, every edge carries a file, a directory, a suffix and a prefix
    pin (docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md),
    the added names' directory pin linking them.

    Locally it is held to a wall-clock bound. A CI runner's clock is too
    noisy for that, so there every query the relink runs is PROFILEd
    instead, and the plans must seek, not scan: a scan per row (as a plan
    made from stale index statistics did, at 20 s) is millions of db hits
    against tens of thousands."""
    import logging
    import os
    import random
    import time

    from neo4j import ManagedTransaction, Session

    rng = random.Random(4)
    names = [f"fn{i}" for i in range(2000)]
    modules = []
    callers = []
    for i in range(5000):
        path = f"m{i}.py"
        targets = sorted(rng.sample(names, 60))
        modules.append({
            "label": "Module", "repo_id": repo_id, "name": path, "properties": {
                "source_file": path,
                "name_refs": [
                    f"CALLS\x1fFunction\x1fcaller\x1f{path}\x1fFunction\x1f{t}\x1f" + (
                        f"\x1f{t}.py\x1elib/.\x1e/x/{t}.py\x1elib/\x1fpin" if pinned else ""
                    )
                    for t in targets
                ],
                "name_ref_targets": targets,
                "name_ref_sources": [],
            },
        })
        callers.append({"label": "Function", "repo_id": repo_id, "name": "caller", "properties": {"file": path}})
    for chunk in range(0, len(modules), 500):
        engine.upsert_nodes(modules[chunk:chunk + 500] + callers[chunk:chunk + 500])
    added_names = rng.sample(names, 20)
    new_file = "lib/new.py" if pinned else "new.py"
    engine.upsert_nodes([
        {"label": "Function", "repo_id": repo_id, "name": n, "properties": {"file": new_file}} for n in added_names
    ])
    added = {("Function", n, new_file) for n in added_names}

    profiles = []
    if os.environ.get("CI"):
        def profiled(run):
            def run_profiled(self, query, *args, **kwargs):
                result = run(self, "PROFILE " + query, *args, **kwargs)
                records = list(result)
                profiles.append(result.consume().profile)
                return records
            return run_profiled

        monkeypatch.setattr(Session, "run", profiled(Session.run))
        monkeypatch.setattr(ManagedTransaction, "run", profiled(ManagedTransaction.run))

    started = time.perf_counter()
    dispatch._relink_name_refs(engine, repo_id, added, set())
    elapsed = time.perf_counter() - started
    monkeypatch.undo()

    logging.getLogger(__name__).warning("name_ref relink over 5,000 Modules: %.3f s", elapsed)
    print(f"name_ref relink over 5,000 Modules: {elapsed:.3f} s")
    (row,) = engine.run_cypher(
        "MATCH (:Function {repo_id: $r, name: 'caller'})-[c:CALLS]->() RETURN count(c) AS n", {"r": repo_id}
    )
    # Each caller links to exactly the added names among its module's targets.
    expected = sum(len(set(m["properties"]["name_ref_targets"]) & set(added_names)) for m in modules)
    assert expected > 0 and row["n"] == expected
    if not os.environ.get("CI"):
        # About 1.3 s alone and over 2 s with another suite sharing Neo4j; the
        # stale-statistics plan it guards against took 20 s.
        assert elapsed < 4.0
        return

    def operators(plan):
        yield plan["operatorType"], plan.get("args", {}).get("DbHits", 0)
        for child in plan.get("children", []):
            yield from operators(child)

    ops = [op for plan in profiles for op in operators(plan)]
    db_hits = sum(hits for _op, hits in ops)
    print(f"name_ref relink db hits: {db_hits}")
    assert len(profiles) == 2  # the Module read and the edge upsert
    assert not [op for op, _hits in ops if "Scan" in op], ops
    assert db_hits < 200_000, ops


# One file of each kind a save re-indexes, for the per-file cost guard below.
_SAVE_COST_FIXTURE = {
    "app/main.py": """\
        import redis
        import psycopg2
        from flask import Flask
        from app.util import helper

        app = Flask(__name__)


        @app.route("/items")
        def list_items():
            return helper()


        class Store:
            def get(self):
                return helper()
        """,
    "app/util.py": "def helper():\n    return 1\n",
    "docker-compose.yml": """\
        services:
          api:
            build: ./app
            image: python:3.12
            volumes:
              - data:/data
        volumes:
          data: {}
        """,
    "Dockerfile": "FROM python:3.12\n",
    "src/a.rs": "pub struct Foo;\n",
    "src/b.rs": "impl Clone for Foo {\n    fn clone(&self) -> Foo { Foo }\n}\n",
    "web/a.ts": "import { b } from './b';\nexport function a() { return b(); }\n",
    "web/b.ts": "export function b() { return 1; }\n",
    "go/main.go": "package main\n\nfunc main() { helper() }\n",
    "go/util.go": "package main\n\nfunc helper() {}\n",
    "p/A.java": "package p;\n\nimport q.B;\n\npublic class A { void a() { new B().b(); } }\n",
    "q/B.java": "package q;\n\npublic class B { public void b() {} }\n",
    "docs/adr-1.md": "---\ntype: design_decision\nid: ADR-1\n---\n# Use Store\n\nSee `Store`.\n",
    "README.md": "# Readme\n\nCalls `helper` and `Store`.\n",
}


def test_per_file_save_cost_ignores_other_repositories(engine, repo_id, tmp_path, monkeypatch):
    """Re-indexing one file must cost what the file costs, not what the
    database holds: every query a save runs is anchored on a label and an
    indexed property (audit C9, where three `{repo_id}` matches scanned
    every node and a full scan took twice as long on a 275k-node database).

    Another repository of 40,000 nodes, of every label a save looks up, sits
    beside a small one with a file of each kind. Locally the median save is
    held to a wall-clock bound; in CI, where the clock is too noisy, every query a
    save runs is PROFILEd instead and must neither scan a populated label
    (or every node) nor touch the other repository's nodes. A save also re-indexes no
    other file, except a Java file's direct importers; an edit that adds a
    function is profiled too, for the mention lookup it runs."""
    import os
    import statistics
    import time

    from neo4j import ManagedTransaction, Session

    for rel, text in _SAVE_COST_FIXTURE.items():
        write(tmp_path, rel, text)
    options = {"docs_path": "docs", "mentions_enabled": True}
    scan(engine, repo_id, tmp_path, **options)

    other = f"{repo_id}-other"
    engine.run_cypher(
        "UNWIND range(0, 3999) AS i CALL (i) { "
        "  CREATE (:Module {repo_id: $r, name: 'm' + i + '.py', source_file: 'm' + i + '.py'}) "
        "  CREATE (:Class {repo_id: $r, name: 'C' + i, file: 'm' + i + '.py'}) "
        "  CREATE (:Function {repo_id: $r, name: 'f' + i, file: 'm' + i + '.py'}) "
        "  CREATE (:Function {repo_id: $r, name: 'helper', file: 'm' + i + '.py'}) "
        "  CREATE (:Function {repo_id: $r, name: 'stub' + i, source: 'm' + i + '.py', sources: ['m' + i + '.py']}) "
        "  CREATE (:Service {repo_id: $r, name: 's' + i, file: 'c' + i + '.yml', source: 'c' + i + '.yml', "
        "                    build_context: '.'}) "
        "  CREATE (:Container {repo_id: $r, name: 'img' + i, source: 'D' + i, sources: ['D' + i]}) "
        "  CREATE (:Endpoint {repo_id: $r, name: 'GET /' + i, source: 'm' + i + '.py'}) "
        "  CREATE (:Cache {repo_id: $r, name: 'Redis' + i, source: 'm' + i + '.py'}) "
        "  CREATE (:Document {repo_id: $r, name: 'd' + i + '.md', source_file: 'd' + i + '.md'}) "
        "} IN TRANSACTIONS OF 1000 ROWS",
        {"r": other},
    )

    profiles: list[tuple[str, dict]] = []
    if os.environ.get("CI"):
        def profiled(run):
            def run_profiled(self, query, *args, **kwargs):
                result = run(self, "PROFILE " + query, *args, **kwargs)
                records = list(result)
                profiles.append((query, result.consume().profile))
                return records
            return run_profiled

        monkeypatch.setattr(Session, "run", profiled(Session.run))
        monkeypatch.setattr(ManagedTransaction, "run", profiled(ManagedTransaction.run))

    seen: list[str] = []
    original = dispatch._index_single_path

    def record(engine, repo_id, repo_root, resolved, rel_path, *args):
        seen.append(rel_path)
        return original(engine, repo_id, repo_root, resolved, rel_path, *args)

    monkeypatch.setattr(dispatch, "_index_single_path", record)
    elapsed = {}
    for rel in _SAVE_COST_FIXTURE:
        # Locally the fastest of three saves, to keep a busy machine's noise out.
        for _ in range(1 if os.environ.get("CI") else 3):
            seen.clear()
            started = time.perf_counter()
            index_paths(engine, repo_id, tmp_path, {tmp_path / rel}, **options)
            elapsed[rel] = min(elapsed.get(rel, float("inf")), time.perf_counter() - started)
            assert sorted(seen) == (["p/A.java", "q/B.java"] if rel == "q/B.java" else [rel])
    # An edit that adds a node also looks for Markdown files that mention it.
    added = write(tmp_path, "app/util.py", _SAVE_COST_FIXTURE["app/util.py"] + "\n\ndef added():\n    return 2\n")
    seen.clear()
    started = time.perf_counter()
    index_paths(engine, repo_id, tmp_path, {added}, **options)
    elapsed["app/util.py, adding a function"] = time.perf_counter() - started
    assert seen == ["app/util.py"]
    monkeypatch.undo()

    print("per-file save:", {rel: round(t, 3) for rel, t in elapsed.items()})
    if not os.environ.get("CI"):
        # About 0.03 s anchored (0.09 s on a busy machine); the old queries'
        # scans made it 0.2 s.
        assert statistics.median(elapsed.values()) < 0.15, elapsed
        return

    def operators(plan):
        yield plan["operatorType"], plan.get("args", {}).get("DbHits", 0)
        for child in plan.get("children", []):
            yield from operators(child)

    # A scan is allowed only where it reads nothing: the compose USES edge's
    # Volume end, a label no node carries (and no index covers).
    scans = [
        (op, hits, " ".join(query.split())[:120])
        for query, plan in profiles for op, hits in operators(plan)
        if "Scan" in op and (op.startswith("AllNodesScan") or hits > 1)
    ]
    assert not scans, scans
    worst = max((sum(hits for _op, hits in operators(plan)), query) for query, plan in profiles)
    print(f"per-file save: {len(profiles)} queries, worst {worst[0]} db hits")
    assert worst[0] < 5_000, worst


def test_full_scan_stamps_index_format(engine, repo_id, tmp_path):
    app_and_worker(tmp_path)
    scan(engine, repo_id, tmp_path)
    assert engine.index_format(repo_id) == dispatch.INDEX_FORMAT
    assert not dispatch.index_outdated(engine, repo_id)

    engine.run_cypher("MATCH (r:Repository {repo_id: $r}) REMOVE r.index_format", {"r": repo_id})
    assert engine.index_format(repo_id) is None
    assert dispatch.index_outdated(engine, repo_id)


def test_failed_full_scan_leaves_the_index_unstamped(engine, repo_id, tmp_path, monkeypatch):
    app_and_worker(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("scan interrupted")

    monkeypatch.setattr(dispatch, "index_paths", broken)
    with pytest.raises(RuntimeError, match="scan interrupted"):
        scan(engine, repo_id, tmp_path)
    assert not engine.index_format(repo_id)  # 0: unstamped while the scan ran
    assert dispatch.index_outdated(engine, repo_id)
