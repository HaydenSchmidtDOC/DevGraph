"""The docs front-matter provider end to end through dispatch, against a live Neo4j.

Full scan, the watcher's index_paths/remove_paths, the schema rescan and the
relink of edges whose target appears in a later batch.
"""

import logging
import os
import shutil
import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths, schema_pending
from devgraph.indexer.providers import docs

REPO = "_smoketest_docs_provider"

# Unique per run: these tests drop and re-create the labels' generated
# constraints, which are database-wide and shared with real repositories.
_TOKEN = uuid.uuid4().hex[:8]
RUNBOOK, ADR, FILE = (f"ZzRunbook{_TOKEN}", f"ZzAdr{_TOKEN}", f"ZzFile{_TOKEN}")
_SHOWN = {RUNBOOK: "Runbook", ADR: "Adr", FILE: "File"}

TYPES = """
    version: 1
    node_types:
      - label: Runbook
        key: [path]
        metadata:
          - {name: path}
          - {name: owner}
          - {name: severity, type: integer}
          - {name: on_call}
        source:
          provider: docs
          paths: ["runbooks/**/*.md"]
          where: [{field: type, is: runbook}]
          fields: {on_call: on-call-team}
      - label: Adr
        key: [path]
        metadata: [{name: path}, {name: status}]
        source: {provider: docs, paths: ["adr/*.md"]}
"""
RELS = """
    relationships:
      - type: ZZ_RUNBOOK_FOR
        provider: docs
        from: Runbook
        to: Service
        field: service
      - type: ZZ_SEE_ALSO
        provider: docs
        from: Runbook
        to: Runbook
        field: see
      - type: ZZ_DECIDED_IN
        provider: docs
        from: Runbook
        to: Adr
        field: adr
"""
FILE_TYPE = """
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
"""
SCHEMA = TYPES + RELS

COMPOSE = "services:\n  api:\n    image: python:3.12\n  db:\n    image: postgres:16\n"


def _labels(text):
    return text.replace("Runbook", RUNBOOK).replace("Adr", ADR).replace("label: File", f"label: {FILE}")


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        for label in (RUNBOOK, ADR, FILE):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
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
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def md(root, rel, front=None, body="# Notes\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = body if front is None else f"---\n{textwrap.dedent(front).strip()}\n---\n{body}"
    path.write_text(text)
    return path


def with_schema(root, text=SCHEMA):
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(_labels(text)))
    return root


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "compose.yaml").write_text(COMPOSE)
    md(root, "runbooks/api.md", """
        type: runbook
        owner: team-a
        severity: 2
        on-call-team: pager-a
        service: api
    """)
    md(root, "runbooks/db.md", """
        type: runbook
        owner: team-b
        service: db
        see: runbooks/api.md
    """)
    md(root, "runbooks/draft.md", "type: draft\nservice: api")
    md(root, "adr/0001.md", "status: accepted")
    return with_schema(root)


def scan(engine, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def _shown(key):
    label, _, name = key.partition(":")
    return f"{_SHOWN.get(label, label)}:{name}"


def docs_nodes(engine):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'docs' RETURN labels(n)[0] + ':' + n.name AS k", {"r": REPO}
    )
    return sorted(_shown(r["k"]) for r in rows)


def label_nodes(engine, label):
    rows = engine.run_cypher(f"MATCH (n:{label} {{repo_id: $r}}) RETURN n.name AS n", {"r": REPO})
    return sorted(r["n"] for r in rows)


def props(engine, rel):
    rows = engine.run_cypher(
        f"MATCH (n:{RUNBOOK} {{repo_id: $r, name: $n}}) RETURN properties(n) AS p", {"r": REPO, "n": rel}
    )
    return rows[0]["p"] if rows else None


def edges(engine, rel_type="ZZ_RUNBOOK_FOR"):
    rows = engine.run_cypher(
        f"MATCH (a {{repo_id: $r}})-[:{rel_type}]->(b {{repo_id: $r}}) "
        "RETURN a.name AS a, b.name AS b, coalesce(b.file, '') AS f",
        {"r": REPO},
    )
    return sorted((r["a"], r["b"], r["f"]) for r in rows)


def all_docs_edges(engine):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->() WHERE a.extractor = 'docs' RETURN count(x) AS n", {"r": REPO}
    )
    return rows[0]["n"]


def snapshot(engine):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) RETURN labels(n) AS labels, properties(n) AS p", {"r": REPO}
    )
    rels = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": REPO},
    )
    return (
        sorted(repr((sorted(n["labels"]), sorted((k, repr(v)) for k, v in n["p"].items()))) for n in nodes),
        sorted((r["a"], r["an"] or "", r["t"], r["b"], r["bn"] or "") for r in rels),
    )


# --- full scan ---------------------------------------------------------------


def test_full_scan_builds_docs_nodes_with_their_fields_and_edges(engine, repo):
    scan(engine, repo)
    assert docs_nodes(engine) == ["Adr:adr/0001.md", "Runbook:runbooks/api.md", "Runbook:runbooks/db.md"]
    p = props(engine, "runbooks/api.md")
    assert (p["path"], p["owner"], p["severity"], p["on_call"], p["extractor"]) == (
        "runbooks/api.md", "team-a", 2, "pager-a", "docs",
    )
    assert edges(engine) == [
        ("runbooks/api.md", "api", "compose.yaml"),
        ("runbooks/db.md", "db", "compose.yaml"),
    ]
    assert edges(engine, "ZZ_SEE_ALSO") == [("runbooks/db.md", "runbooks/api.md", "")]


def test_a_service_name_fans_out_to_every_compose_file_declaring_it(engine, repo):
    (repo / "other").mkdir()
    (repo / "other" / "docker-compose.yml").write_text("services:\n  api:\n    image: node:22\n")
    scan(engine, repo)
    assert [e for e in edges(engine) if e[0] == "runbooks/api.md"] == [
        ("runbooks/api.md", "api", "compose.yaml"),
        ("runbooks/api.md", "api", "other/docker-compose.yml"),
    ]


# --- edits through the watcher -----------------------------------------------


def test_editing_a_field_updates_the_property(engine, repo):
    scan(engine, repo)
    path = md(repo, "runbooks/api.md", "type: runbook\nowner: team-c\nservice: api")
    index_paths(engine, REPO, repo, {path})
    assert props(engine, "runbooks/api.md")["owner"] == "team-c"


def test_removing_a_field_from_the_file_removes_the_property(engine, repo):
    scan(engine, repo)
    path = md(repo, "runbooks/api.md", "type: runbook\nservice: api")
    index_paths(engine, REPO, repo, {path})
    p = props(engine, "runbooks/api.md")
    assert "owner" not in p and "severity" not in p and "on_call" not in p


def test_changing_the_named_service_moves_the_edge(engine, repo):
    scan(engine, repo)
    path = md(repo, "runbooks/api.md", "type: runbook\nservice: db")
    index_paths(engine, REPO, repo, {path})
    assert edges(engine) == [
        ("runbooks/api.md", "db", "compose.yaml"),
        ("runbooks/db.md", "db", "compose.yaml"),
    ]


def test_breaking_a_where_condition_removes_the_node_and_its_edges(engine, repo):
    scan(engine, repo)
    path = md(repo, "runbooks/api.md", "type: draft\nservice: api")
    index_paths(engine, REPO, repo, {path})
    assert "Runbook:runbooks/api.md" not in docs_nodes(engine)
    assert [e for e in edges(engine) if e[0] == "runbooks/api.md"] == []
    assert edges(engine, "ZZ_SEE_ALSO") == []  # its incoming edge went with it


def test_an_incoming_docs_edge_survives_an_edit_of_its_target(engine, repo):
    scan(engine, repo)
    path = md(repo, "runbooks/api.md", "type: runbook\nowner: team-z\nservice: api")
    index_paths(engine, REPO, repo, {path})
    assert edges(engine, "ZZ_SEE_ALSO") == [("runbooks/db.md", "runbooks/api.md", "")]


def test_a_new_runbook_links_to_a_service_and_runbook_in_the_same_batch(engine, repo):
    scan(engine, repo)
    (repo / "svc").mkdir()
    compose = repo / "svc" / "compose.yml"
    compose.write_text("services:\n  worker:\n    image: python:3.12\n")
    a = md(repo, "runbooks/new/a.md", "type: runbook\nservice: worker\nsee: runbooks/new/b.md")
    b = md(repo, "runbooks/new/b.md", "type: runbook")
    index_paths(engine, REPO, repo, {compose, a, b})
    assert ("runbooks/new/a.md", "worker", "svc/compose.yml") in edges(engine)
    assert ("runbooks/new/a.md", "runbooks/new/b.md", "") in edges(engine, "ZZ_SEE_ALSO")


# --- relink ------------------------------------------------------------------


@pytest.fixture
def relinks(monkeypatch):
    """Records the `targets` of every targeted build_edges call: the relink."""
    seen = []
    real = docs.build_edges

    def spy(spec, repo_id, selected, targets=None):
        if targets is not None:
            seen.append(sorted(targets))
        return real(spec, repo_id, selected, targets)

    monkeypatch.setattr(docs, "build_edges", spy)
    return seen


def test_a_compose_file_added_in_a_later_batch_gets_the_edge(engine, repo, relinks):
    compose = repo / "compose.yaml"
    compose.unlink()
    scan(engine, repo)
    assert edges(engine) == []

    compose.write_text(COMPOSE)
    index_paths(engine, REPO, repo, {compose})
    assert edges(engine) == [
        ("runbooks/api.md", "api", "compose.yaml"),
        ("runbooks/db.md", "db", "compose.yaml"),
    ]
    assert relinks


def test_a_deleted_then_recreated_compose_file_gets_its_edges_back(engine, repo):
    scan(engine, repo)
    compose = repo / "compose.yaml"
    compose.unlink()
    remove_paths(engine, REPO, repo, {compose})
    assert edges(engine) == []

    compose.write_text(COMPOSE)
    index_paths(engine, REPO, repo, {compose})
    assert edges(engine) == [
        ("runbooks/api.md", "api", "compose.yaml"),
        ("runbooks/db.md", "db", "compose.yaml"),
    ]


def test_reindexing_an_unchanged_compose_file_triggers_no_relink(engine, repo, relinks):
    scan(engine, repo)
    before = edges(engine)
    index_paths(engine, REPO, repo, {repo / "compose.yaml"})
    assert relinks == []
    assert edges(engine) == before


def test_reindexing_an_unchanged_runbook_triggers_no_relink(engine, repo, relinks):
    scan(engine, repo)
    before = edges(engine, "ZZ_SEE_ALSO")
    index_paths(engine, REPO, repo, {repo / "runbooks" / "api.md"})
    assert relinks == []  # its node was already in the graph, so nothing was added
    assert edges(engine, "ZZ_SEE_ALSO") == before


def test_a_docs_target_added_in_a_later_batch_gets_the_edge(engine, repo, relinks):
    md(repo, "runbooks/api.md", "type: runbook\nservice: api\nadr: ./adr/0002.md")
    scan(engine, repo)
    assert edges(engine, "ZZ_DECIDED_IN") == []

    adr = md(repo, "adr/0002.md", "status: proposed")
    index_paths(engine, REPO, repo, {adr})
    assert edges(engine, "ZZ_DECIDED_IN") == [("runbooks/api.md", "adr/0002.md", "")]
    assert [(ADR, "adr/0002.md")] in relinks


# --- bad values and failures -------------------------------------------------


def test_bad_values_never_stop_the_batch(engine, repo, caplog):
    scan(engine, repo)
    good = md(repo, "runbooks/good.md", "type: runbook\nowner: team-g\nservice: api")
    huge = md(repo, "runbooks/huge.md", "type: runbook\nseverity: 0x" + "f" * 40 + "\nservice: 0x" + "f" * 40)
    items = ", ".join(f"s{i}" for i in range(200))
    many = md(repo, "runbooks/many.md", f"type: runbook\nowner: [{items}]\nservice: [{items}, api]")
    later = md(repo, "runbooks/zz-good.md", "type: runbook\nowner: team-h\nservice: db")

    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
        index_paths(engine, REPO, repo, {good, huge, many, later})

    assert props(engine, "runbooks/good.md")["owner"] == "team-g"
    assert props(engine, "runbooks/zz-good.md")["owner"] == "team-h"
    assert "severity" not in props(engine, "runbooks/huge.md")
    assert "owner" not in props(engine, "runbooks/many.md")
    linked = edges(engine)
    assert ("runbooks/good.md", "api", "compose.yaml") in linked
    assert ("runbooks/zz-good.md", "db", "compose.yaml") in linked
    assert not [e for e in linked if e[0] in ("runbooks/huge.md", "runbooks/many.md")]  # api is item 201
    assert "runbooks/huge.md" in caplog.text


def test_a_failing_docs_pass_is_logged_and_the_batch_carries_on(engine, repo, monkeypatch, caplog):
    scan(engine, repo)

    def boom(*args, **kwargs):
        raise RuntimeError("edge pass exploded")

    monkeypatch.setattr(docs, "build_edges", boom)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
    path = md(repo, "runbooks/api.md", "type: runbook\nowner: team-q\nservice: api")
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
        index_paths(engine, REPO, repo, {path, repo / "pkg" / "mod.py"})
    assert props(engine, "runbooks/api.md")["owner"] == "team-q"
    assert "pkg/mod.py" in label_nodes(engine, "Module")
    assert "edge pass exploded" in caplog.text


# --- deletes -----------------------------------------------------------------


def test_deleting_a_file_removes_its_node(engine, repo):
    scan(engine, repo)
    (repo / "runbooks" / "api.md").unlink()
    remove_paths(engine, REPO, repo, {repo / "runbooks" / "api.md"})
    assert docs_nodes(engine) == ["Adr:adr/0001.md", "Runbook:runbooks/db.md"]
    assert edges(engine, "ZZ_SEE_ALSO") == []


def test_deleting_a_directory_removes_every_node_below_it(engine, repo):
    scan(engine, repo)
    shutil.rmtree(repo / "runbooks")
    remove_paths(engine, REPO, repo, {repo / "runbooks"})
    assert docs_nodes(engine) == ["Adr:adr/0001.md"]
    assert edges(engine) == []


# --- schema changes ----------------------------------------------------------


def test_a_new_fields_mapping_is_applied_by_the_rescan(engine, repo):
    scan(engine, repo)
    with_schema(repo, SCHEMA.replace("{on_call: on-call-team}", "{on_call: owner}"))
    scan(engine, repo)
    assert props(engine, "runbooks/api.md")["on_call"] == "team-a"


def test_a_removed_metadata_declaration_clears_the_property(engine, repo):
    scan(engine, repo)
    assert props(engine, "runbooks/api.md")["severity"] == 2
    with_schema(repo, SCHEMA.replace("          - {name: severity, type: integer}\n", ""))
    scan(engine, repo)
    p = props(engine, "runbooks/api.md")
    assert "severity" not in p
    assert (p["path"], p["owner"]) == ("runbooks/api.md", "team-a")


def test_removed_types_and_relationships_are_gone_after_the_rescan(engine, repo):
    scan(engine, repo)
    assert edges(engine, "ZZ_SEE_ALSO")
    text = SCHEMA.replace(
        "      - type: ZZ_SEE_ALSO\n        provider: docs\n        from: Runbook\n        to: Runbook\n        field: see\n",
        "",
    ).replace("ZZ_RUNBOOK_FOR", "ZZ_SERVES")
    with_schema(repo, text)
    scan(engine, repo)
    assert edges(engine, "ZZ_SEE_ALSO") == []
    assert edges(engine, "ZZ_RUNBOOK_FOR") == []
    assert ("runbooks/api.md", "api", "compose.yaml") in edges(engine, "ZZ_SERVES")

    adr_type = "      - label: Adr\n        key: [path]\n        metadata: [{name: path}, {name: status}]\n" \
        "        source: {provider: docs, paths: [\"adr/*.md\"]}\n"
    with_schema(repo, text.replace(adr_type, "").replace(
        "      - type: ZZ_DECIDED_IN\n        provider: docs\n        from: Runbook\n        to: Adr\n        field: adr\n",
        "",
    ))
    scan(engine, repo)
    assert label_nodes(engine, ADR) == []
    assert "Runbook:runbooks/api.md" in docs_nodes(engine)


def test_an_edge_type_whose_field_changed_is_rebuilt_from_the_new_field(engine, repo):
    scan(engine, repo)
    with_schema(repo, SCHEMA.replace("field: service", "field: owner"))
    scan(engine, repo)
    assert edges(engine) == []  # no Service is named team-a/team-b


def test_an_invalid_schema_leaves_docs_nodes_alone_and_writes_no_edges(engine, repo):
    scan(engine, repo)
    before = docs_nodes(engine)
    engine.run_cypher("MATCH ({repo_id: $r})-[x:ZZ_RUNBOOK_FOR]->() DELETE x", {"r": REPO})
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    md(repo, "runbooks/api.md", "type: draft")
    full_scan(engine, REPO, repo)
    assert docs_nodes(engine) == before
    assert edges(engine) == []
    assert props(engine, "runbooks/api.md")["owner"] == "team-a"


def test_a_pending_schema_pauses_docs_writes(engine, repo):
    scan(engine, repo)
    with_schema(repo, SCHEMA.replace("runbooks/**/*.md", "**/*.md"))
    assert schema_pending(engine, REPO, repo)
    path = md(repo, "runbooks/api.md", "type: runbook\nowner: team-p")
    index_paths(engine, REPO, repo, {path})
    remove_paths(engine, REPO, repo, {repo / "runbooks" / "db.md"})
    assert props(engine, "runbooks/api.md")["owner"] == "team-a"
    assert "Runbook:runbooks/db.md" in docs_nodes(engine)


# --- a filesystem File and a docs Runbook on one path (spec §3.6) ------------


def shared_schema(with_file=True, with_runbook=True):
    text = TYPES
    if not with_runbook:
        start = text.index("      - label: Runbook")
        text = text[:start] + text[text.index("      - label: Adr"):]
    if with_file:
        text += FILE_TYPE
    if with_runbook:
        text += RELS
    return text


@pytest.fixture
def shared(repo):
    return with_schema(repo, shared_schema())


def test_a_docs_unmatch_leaves_the_file_node(engine, shared):
    scan(engine, shared)
    path = md(shared, "runbooks/api.md", "type: draft")
    index_paths(engine, REPO, shared, {path})
    assert "runbooks/api.md" not in label_nodes(engine, RUNBOOK)
    assert "runbooks/api.md" in label_nodes(engine, FILE)


def test_deleting_a_shared_file_removes_both_nodes_and_leaves_no_edges(engine, shared):
    scan(engine, shared)
    (shared / "runbooks" / "api.md").unlink()
    remove_paths(engine, REPO, shared, {shared / "runbooks" / "api.md"})
    assert "runbooks/api.md" not in label_nodes(engine, RUNBOOK)
    assert "runbooks/api.md" not in label_nodes(engine, FILE)
    assert "runbooks/db.md" in label_nodes(engine, RUNBOOK) and "runbooks/db.md" in label_nodes(engine, FILE)
    assert [e for e in edges(engine) if e[0] == "runbooks/api.md"] == []
    assert edges(engine, "ZZ_SEE_ALSO") == []


def test_removing_the_runbook_type_leaves_the_file_node(engine, shared):
    scan(engine, shared)
    with_schema(shared, shared_schema(with_runbook=False))
    scan(engine, shared)
    assert label_nodes(engine, RUNBOOK) == []
    assert "runbooks/api.md" in label_nodes(engine, FILE)


def test_an_invalid_schema_leaves_both_nodes(engine, shared):
    scan(engine, shared)
    (shared / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    full_scan(engine, REPO, shared)
    assert "runbooks/api.md" in label_nodes(engine, RUNBOOK)
    assert "runbooks/api.md" in label_nodes(engine, FILE)


def test_removing_the_file_type_leaves_the_runbook_and_its_edges(engine, shared):
    scan(engine, shared)
    before = edges(engine)
    with_schema(shared, shared_schema(with_file=False))
    scan(engine, shared)
    assert label_nodes(engine, FILE) == []
    assert "runbooks/api.md" in label_nodes(engine, RUNBOOK)
    assert edges(engine) == before


ABOUT = f"""
      - type: ZZ_ABOUT
        provider: docs
        from: Runbook
        to: {FILE}
        field: about
"""


def test_a_filesystem_target_created_or_recreated_later_gets_the_edge(engine, repo, relinks):
    with_schema(repo, shared_schema() + ABOUT)
    md(repo, "runbooks/api.md", "type: runbook\nservice: api\nabout: notes/later.txt")
    scan(engine, repo)
    assert edges(engine, "ZZ_ABOUT") == []

    later = repo / "notes" / "later.txt"
    later.parent.mkdir()
    later.write_text("hello\n")
    index_paths(engine, REPO, repo, {later})
    assert edges(engine, "ZZ_ABOUT") == [("runbooks/api.md", "notes/later.txt", "")]

    later.unlink()
    remove_paths(engine, REPO, repo, {later})
    assert edges(engine, "ZZ_ABOUT") == []
    later.write_text("again\n")
    index_paths(engine, REPO, repo, {later})
    assert edges(engine, "ZZ_ABOUT") == [("runbooks/api.md", "notes/later.txt", "")]

    relinks.clear()
    later.write_text("edited\n")
    index_paths(engine, REPO, repo, {later})
    assert relinks == []  # the File node already existed


def test_a_full_scan_reads_each_docs_file_once(engine, repo, monkeypatch):
    read = []
    real = docs.read_front_matter
    monkeypatch.setattr(docs, "read_front_matter", lambda path: read.append(path) or real(path))
    scan(engine, repo)
    assert edges(engine)  # the edges were still written
    assert sorted(read) == sorted(set(read))
    assert len(read) == 4  # runbooks/api.md, db.md, draft.md and adr/0001.md


# --- scope -------------------------------------------------------------------


def test_ignored_directories_and_outside_symlinks_are_never_read(engine, repo, tmp_path, monkeypatch):
    with_schema(repo, SCHEMA.replace("runbooks/**/*.md", "**/*.md"))
    md(repo, "node_modules/dep/runbook.md", "type: runbook")
    outside = md(tmp_path, "outside/secret.md", "type: runbook")
    os.symlink(outside, repo / "runbooks" / "link.md")
    read = []
    real = docs.read_front_matter
    monkeypatch.setattr(docs, "read_front_matter", lambda path: read.append(path) or real(path))

    scan(engine, repo)
    index_paths(engine, REPO, repo, {repo / "node_modules" / "dep" / "runbook.md", repo / "runbooks" / "link.md"})

    assert read
    assert not [p for p in read if "node_modules" in str(p) or "secret" in str(p) or "link" in str(p)]
    assert not [n for n in docs_nodes(engine) if "node_modules" in n or "link" in n]


# --- no docs sources ---------------------------------------------------------


def test_without_docs_sources_the_graph_is_unchanged(engine, repo, monkeypatch):
    (repo / "devgraph.schema.yaml").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def run():\n    return 1\n")

    def build(root):
        scan(engine, root)
        compose = root / "compose.yaml"
        compose.unlink()
        remove_paths(engine, REPO, root, {compose})
        compose.write_text(COMPOSE)
        index_paths(engine, REPO, root, {compose, root / "runbooks" / "api.md"})
        return snapshot(engine)

    with_change = build(repo)
    engine.delete_repository(REPO)
    for name in ("_apply_docs", "_read_docs_batch", "_sync_docs", "_relink_docs", "_sync_docs_edges"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    assert build(repo) == with_change
