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

from devgraph.config.project_schema import resolve_effective_schema
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths, schema_pending
from devgraph.indexer.providers import docs
from devgraph.indexer.walk import indexable_paths
from tests.indexer.docs_live_helpers import assert_matches_fresh_apply

# Unique per run: these tests drop and re-create the labels' generated
# constraints, which are database-wide and shared with real repositories,
# and a concurrent run must not delete this run's repository.
_TOKEN = uuid.uuid4().hex[:8]
REPO = f"_smoketest_docs_provider_{_TOKEN}"
RUNBOOK, ADR, FILE = (f"ZzRunbook{_TOKEN}", f"ZzAdr{_TOKEN}", f"ZzFile{_TOKEN}")
# The front-matter key tests' own labels, so their keys never meet the ones above.
KADR, RFC, KRUNBOOK = (f"ZzKAdr{_TOKEN}", f"ZzRfc{_TOKEN}", f"ZzKRunbook{_TOKEN}")
_SHOWN = {RUNBOOK: "Runbook", ADR: "Adr", FILE: "File", KADR: "Adr", RFC: "Rfc", KRUNBOOK: "Runbook"}

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
        for label in (RUNBOOK, ADR, FILE, KADR, RFC, KRUNBOOK):
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

    def spy(spec, repo_id, selected, targets=None, **kwargs):
        if targets is not None:
            seen.append(sorted(targets))
        return real(spec, repo_id, selected, targets, **kwargs)

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


def test_a_type_switched_from_docs_to_filesystem_loses_its_front_matter_values(engine, repo):
    scan(engine, repo)
    assert props(engine, "runbooks/api.md")["owner"] == "team-a"
    start, end = TYPES.index("      - label: Runbook"), TYPES.index("      - label: Adr")
    runbook_files = (
        "      - label: Runbook\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: filesystem, kind: file}\n"
    )
    with_schema(repo, TYPES[:start] + runbook_files + TYPES[end:])
    scan(engine, repo)
    p = props(engine, "runbooks/api.md")
    assert p["extractor"] == "filesystem"
    assert {"owner", "severity", "on_call"} & set(p) == set()


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
    monkeypatch.setattr(dispatch, "_prune_docs", lambda *a, **k: ([], None, {}))
    for name in ("_read_docs_batch", "_sync_docs", "_relink_docs", "_sync_docs_edges"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    assert build(repo) == with_change


# --- front-matter keys -------------------------------------------------------
#
# Adr is keyed on its `id` (adr_id), Rfc on `id` wherever `kind: rfc`, and
# Runbook stays keyed on its path. Every sequence ends with the graph a fresh
# full apply of the same files would build.

KEYED_SCHEMA = f"""
    version: 1
    node_types:
      - label: {KADR}
        key: [adr_id]
        metadata: [{{name: path}}, {{name: adr_id}}, {{name: title}}]
        source: {{provider: docs, paths: ["decisions/**/*.md"], fields: {{adr_id: id}}}}
      - label: {RFC}
        key: [id]
        metadata: [{{name: path}}, {{name: id}}]
        source: {{provider: docs, paths: ["**/*.md"], where: [{{field: kind, is: rfc}}]}}
      - label: {KRUNBOOK}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: docs, paths: ["runbooks/*.md"]}}
    relationships:
      - {{type: ZZ_REPLACES, provider: docs, from: {KADR}, to: {KADR}, field: supersedes}}
      - {{type: ZZ_RUNBOOK_ADR, provider: docs, from: {KRUNBOOK}, to: {KADR}, field: adr}}
      - {{type: ZZ_ADR_RFC, provider: docs, from: {KADR}, to: {RFC}, field: rfc}}
      - {{type: ZZ_ADR_RUNBOOK, provider: docs, from: {KADR}, to: {KRUNBOOK}, field: runbook}}
"""


@pytest.fixture
def keyed(tmp_path):
    root = tmp_path / "keyed"
    root.mkdir()
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(KEYED_SCHEMA))
    md(root, "decisions/adr-011.md", "id: ADR-011\ntitle: Eleven")
    md(root, "decisions/adr-012.md", "id: ADR-012\ntitle: Twelve\nsupersedes: ADR-011")
    md(root, "decisions/adr-013.md", "id: ADR-013\nsupersedes: [ADR-011, ADR-012]")
    md(root, "runbooks/deploy.md", "adr: ADR-012")
    return root


def kscan(engine, root):
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def entries(engine, label=KADR):
    """{name: path} of one label's entries."""
    rows = engine.run_cypher(f"MATCH (n:{label} {{repo_id: $r}}) RETURN n.name AS n, n.path AS p", {"r": REPO})
    return {r["n"]: r["p"] for r in rows}


def element_id(engine, name, label=KADR):
    rows = engine.run_cypher(
        f"MATCH (n:{label} {{repo_id: $r, name: $n}}) RETURN elementId(n) AS id", {"r": REPO, "n": name}
    )
    return rows[0]["id"] if rows else None


def replaces(engine):
    return edges(engine, "ZZ_REPLACES")


def links(engine, rel_type):
    return [(a, b) for a, b, _f in edges(engine, rel_type)]


def doctor_lines(root):
    files = indexable_paths(root)
    return [line["detail"] for line in docs.source_report(root, resolve_effective_schema(root), files)]


def test_keyed_full_scan_links_by_id_and_a_list_makes_two_edges(engine, keyed):
    kscan(engine, keyed)
    assert entries(engine) == {
        "ADR-011": "decisions/adr-011.md", "ADR-012": "decisions/adr-012.md", "ADR-013": "decisions/adr-013.md",
    }
    assert links(engine, "ZZ_REPLACES") == [("ADR-012", "ADR-011"), ("ADR-013", "ADR-011"), ("ADR-013", "ADR-012")]
    assert links(engine, "ZZ_RUNBOOK_ADR") == [("runbooks/deploy.md", "ADR-012")]
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_file_losing_a_key_in_one_type_still_owns_it_in_another(engine, keyed):
    md(keyed, "decisions/adr-1.md", "id: ADR-1")
    kscan(engine, keyed)
    x = md(keyed, "decisions/x.md", "kind: rfc\nid: ADR-1\nsupersedes: ADR-011")
    index_paths(engine, REPO, keyed, {x})
    assert entries(engine, RFC) == {"ADR-1": "decisions/x.md"}
    assert entries(engine)["ADR-1"] == "decisions/adr-1.md"
    assert "decisions/x.md" not in entries(engine).values()
    assert ("ADR-1", "ADR-011") not in links(engine, "ZZ_REPLACES")
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_the_relink_writes_edges_from_owners_only(engine, keyed, relinks):
    (keyed / "decisions" / "adr-012.md").unlink()
    md(keyed, "decisions/adr-006.md", "id: ADR-006")
    kscan(engine, keyed)  # the runbook names ADR-012, which no file claims yet
    assert links(engine, "ZZ_RUNBOOK_ADR") == []

    # The copy reaches the disk without an event of its own; its owner comes in the batch.
    md(keyed, "decisions/adr-012 copy.md", "id: ADR-012\nsupersedes: ADR-007")
    owner = md(keyed, "decisions/adr-012.md", "id: ADR-012\nsupersedes: ADR-006")
    seven = md(keyed, "decisions/adr-007.md", "id: ADR-007")
    index_paths(engine, REPO, keyed, {owner, seven})

    assert [(KADR, "ADR-007"), (KADR, "ADR-012")] in relinks
    assert entries(engine)["ADR-012"] == "decisions/adr-012.md"
    assert links(engine, "ZZ_RUNBOOK_ADR") == [("runbooks/deploy.md", "ADR-012")]
    assert [b for a, b in links(engine, "ZZ_REPLACES") if a == "ADR-012"] == ["ADR-006"]
    assert_matches_fresh_apply(engine, REPO, keyed)


@pytest.mark.parametrize("copy_first", [False, True])
def test_the_original_keeps_its_id_whichever_file_is_saved_first(engine, keyed, copy_first):
    original = keyed / "decisions" / "adr-012.md"
    text = original.read_text()
    if copy_first:
        original.unlink()
        kscan(engine, keyed)
        copy = md(keyed, "decisions/adr-012 copy.md", "id: ADR-012\nsupersedes: ADR-013")
        index_paths(engine, REPO, keyed, {copy})
        assert entries(engine)["ADR-012"] == "decisions/adr-012 copy.md"
        original.write_text(text)
        index_paths(engine, REPO, keyed, {original})
    else:
        kscan(engine, keyed)
        copy = md(keyed, "decisions/adr-012 copy.md", "id: ADR-012\nsupersedes: ADR-013")
        index_paths(engine, REPO, keyed, {copy})

    assert entries(engine)["ADR-012"] == "decisions/adr-012.md"
    assert [b for a, b in links(engine, "ZZ_REPLACES") if a == "ADR-012"] == ["ADR-011"]
    assert "Adr: decisions/adr-012 copy.md: 'id' 'ADR-012' is also used by decisions/adr-012.md, whose path " \
        "sorts first and keeps it; change the id in one of them" in [
            line.replace(KADR, "Adr") for line in doctor_lines(keyed)
        ]
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_file_sorting_first_takes_the_entry_over_in_place_and_hands_it_back_on_delete(engine, keyed):
    md(keyed, "decisions/adr-010.md", "id: ADR-010")
    md(keyed, "decisions/adr-012-v2.md", "id: ADR-012\nsupersedes: ADR-013")  # sorts after the original
    kscan(engine, keyed)
    before = element_id(engine, "ADR-012")

    first = md(keyed, "decisions/adr-000.md", "id: ADR-012\nsupersedes: ADR-010")
    index_paths(engine, REPO, keyed, {first})
    assert entries(engine)["ADR-012"] == "decisions/adr-000.md"
    assert element_id(engine, "ADR-012") == before
    assert ("ADR-013", "ADR-012") in links(engine, "ZZ_REPLACES")
    assert [b for a, b in links(engine, "ZZ_REPLACES") if a == "ADR-012"] == ["ADR-010"]
    assert links(engine, "ZZ_RUNBOOK_ADR") == [("runbooks/deploy.md", "ADR-012")]
    assert_matches_fresh_apply(engine, REPO, keyed)

    first.unlink()
    remove_paths(engine, REPO, keyed, {first})
    assert entries(engine)["ADR-012"] == "decisions/adr-012.md"
    assert element_id(engine, "ADR-012") == before
    assert ("ADR-013", "ADR-012") in links(engine, "ZZ_REPLACES")
    assert [b for a, b in links(engine, "ZZ_REPLACES") if a == "ADR-012"] == ["ADR-011"]
    assert_matches_fresh_apply(engine, REPO, keyed)


@pytest.mark.parametrize("remove_first", [True, False])
def test_a_rename_keeping_the_id_moves_the_entry_and_keeps_its_incoming_edges(engine, keyed, relinks, remove_first):
    kscan(engine, keyed)
    before = element_id(engine, "ADR-012")
    old = keyed / "decisions" / "adr-012.md"
    new = keyed / "decisions" / "accepted" / "adr-012.md"
    new.parent.mkdir()
    old.rename(new)
    relinks.clear()
    if remove_first:
        remove_paths(engine, REPO, keyed, {old})
        index_paths(engine, REPO, keyed, {new})
    else:
        index_paths(engine, REPO, keyed, {new})
        remove_paths(engine, REPO, keyed, {old})

    assert entries(engine)["ADR-012"] == "decisions/accepted/adr-012.md"
    assert element_id(engine, "ADR-012") == before
    assert ("ADR-013", "ADR-012") in links(engine, "ZZ_REPLACES")
    assert links(engine, "ZZ_RUNBOOK_ADR") == [("runbooks/deploy.md", "ADR-012")]
    assert not [t for t in relinks if (KADR, "ADR-012") in t]
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_re_id_hands_the_old_id_to_its_claimant_and_relinks_the_new_one(engine, keyed, relinks, reads):
    md(keyed, "decisions/adr-012 copy.md", "id: ADR-012\nsupersedes: ADR-013")
    md(keyed, "runbooks/later.md", "adr: ADR-099")
    kscan(engine, keyed)
    before = element_id(engine, "ADR-012")

    path = md(keyed, "decisions/adr-012.md", "id: ADR-099\nsupersedes: ADR-011")
    reads.clear()
    index_paths(engine, REPO, keyed, {path})
    assert len(reads) == len(set(reads))  # the relink reused the batch's walk and reads

    assert entries(engine)["ADR-012"] == "decisions/adr-012 copy.md"
    assert entries(engine)["ADR-099"] == "decisions/adr-012.md"
    assert element_id(engine, "ADR-012") == before
    assert ("ADR-013", "ADR-012") in links(engine, "ZZ_REPLACES")
    assert [b for a, b in links(engine, "ZZ_REPLACES") if a == "ADR-012"] == ["ADR-013"]
    assert ("runbooks/later.md", "ADR-099") in links(engine, "ZZ_RUNBOOK_ADR")
    assert [(KADR, "ADR-099")] in relinks
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_missing_or_invalid_id_leaves_the_file_out_and_writes_the_rest(engine, keyed):
    kscan(engine, keyed)
    missing = md(keyed, "decisions/adr-020.md", "title: No id")
    spaced = md(keyed, "decisions/adr-021.md", "id: ' ADR-021'")
    listed = md(keyed, "decisions/adr-022.md", "id: [ADR-022]")
    good = md(keyed, "decisions/adr-023.md", "id: ADR-023\nsupersedes: ADR-013")
    index_paths(engine, REPO, keyed, {missing, spaced, listed, good})
    found = entries(engine)
    assert found["ADR-023"] == "decisions/adr-023.md"
    assert not {"decisions/adr-020.md", "decisions/adr-021.md", "decisions/adr-022.md"} & set(found.values())
    assert ("ADR-023", "ADR-013") in links(engine, "ZZ_REPLACES")
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_stale_entry_at_an_owner_pulled_into_the_batch_moves_to_its_new_owner(engine, keyed):
    # o.md owns ADR-050 and p.md loses it; o.md then changes to ADR-060 without an
    # event yet, and q.md (ADR-040) changes to ADR-060 and fires. o.md joins the
    # batch as ADR-060's owner, so the ADR-050 still at o.md must move to p.md.
    md(keyed, "decisions/o.md", "id: ADR-050\nsupersedes: ADR-011")
    md(keyed, "decisions/p.md", "id: ADR-050\nsupersedes: ADR-013")
    md(keyed, "decisions/q.md", "id: ADR-040")
    md(keyed, "runbooks/z.md", "adr: ADR-050")
    kscan(engine, keyed)
    before = element_id(engine, "ADR-050")

    md(keyed, "decisions/o.md", "id: ADR-060\nsupersedes: ADR-011")
    q = md(keyed, "decisions/q.md", "id: ADR-060")
    index_paths(engine, REPO, keyed, {q})

    found = entries(engine)
    assert (found["ADR-060"], found["ADR-050"]) == ("decisions/o.md", "decisions/p.md")
    assert "ADR-040" not in found
    assert element_id(engine, "ADR-050") == before
    assert ("runbooks/z.md", "ADR-050") in links(engine, "ZZ_RUNBOOK_ADR")
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_stale_entries_are_chased_through_every_owner_pulled_in(engine, keyed, path_lists):
    # Two rounds: o.md's stale ADR-050 pulls in p.md, whose stale ADR-070 pulls in s.md.
    md(keyed, "decisions/o.md", "id: ADR-050")
    md(keyed, "decisions/p.md", "id: ADR-070")
    md(keyed, "decisions/s.md", "id: ADR-070\nsupersedes: ADR-011")
    md(keyed, "decisions/q.md", "id: ADR-040")
    kscan(engine, keyed)

    md(keyed, "decisions/o.md", "id: ADR-060")
    md(keyed, "decisions/p.md", "id: ADR-050")
    q = md(keyed, "decisions/q.md", "id: ADR-060")
    path_lists.clear()
    index_paths(engine, REPO, keyed, {q})

    found = entries(engine)
    assert (found["ADR-060"], found["ADR-050"], found["ADR-070"]) == (
        "decisions/o.md", "decisions/p.md", "decisions/s.md",
    )
    # |batch| + |K_final|: q.md, plus ADR-040, ADR-060, ADR-050 and ADR-070.
    assert max(len(paths) for _name, paths in path_lists) <= 1 + 4
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_full_scan_works_out_the_id_owners_once(engine, keyed, monkeypatch):
    calls = []
    real = docs.keyed_claims
    monkeypatch.setattr(docs, "keyed_claims", lambda *args: calls.append(1) or real(*args))
    kscan(engine, keyed)
    assert links(engine, "ZZ_RUNBOOK_ADR") == [("runbooks/deploy.md", "ADR-012")]
    assert len(calls) == 1


def test_a_long_chain_of_stale_entries_is_chased_with_a_fixed_number_of_lookups(engine, keyed, monkeypatch):
    # c00 owns T, c01 owns c00's old id, ... : each owner pulled in holds a stale
    # entry whose new owner is the next file. The chase must not query per link.
    length = 40
    for n in range(length):
        md(keyed, f"decisions/c{n:02}.md", f"id: K-{n}")
    md(keyed, "decisions/q.md", "id: Q")
    kscan(engine, keyed)

    md(keyed, "decisions/c00.md", "id: T")
    for n in range(1, length):
        md(keyed, f"decisions/c{n:02}.md", f"id: K-{n - 1}")
    q = md(keyed, "decisions/q.md", "id: T")
    calls = []
    for name in ("extracted_nodes_at", "extracted_entries", "existing_node_names", "list_file_nodes"):
        real = getattr(engine, name)
        monkeypatch.setattr(engine, name, lambda *args, _real=real, _name=name: calls.append(_name) or _real(*args))
    index_paths(engine, REPO, keyed, {q})

    assert len(calls) <= 4, calls  # the batch's snapshot and fetch, the pulled-in owners, the relink's fetch
    found = entries(engine)
    assert found["T"] == "decisions/c00.md" and found["K-0"] == "decisions/c01.md"
    assert f"K-{length - 1}" not in found
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_an_owner_pulled_in_does_not_count_its_other_entries_as_added(engine, keyed, relinks):
    # runbooks/r.md is a (path-keyed) Runbook and owns Rfc R-1. A loser of R-1
    # pulls it into the batch; its Runbook entry was already there, so nothing
    # links to it anew.
    md(keyed, "runbooks/r.md", "kind: rfc\nid: R-1")
    md(keyed, "decisions/x.md", "id: ADR-051\nrunbook: runbooks/r.md")
    kscan(engine, keyed)
    assert links(engine, "ZZ_ADR_RUNBOOK") == [("ADR-051", "runbooks/r.md")]

    copy = md(keyed, "x/r.md", "kind: rfc\nid: R-1")
    relinks.clear()
    index_paths(engine, REPO, keyed, {copy})

    assert not [t for t in relinks if (KRUNBOOK, "runbooks/r.md") in t]
    assert entries(engine, RFC) == {"R-1": "runbooks/r.md"}
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_takeover_moves_only_the_deleted_entries_and_leaves_the_rest_to_the_owners_event(engine, keyed):
    # o.md takes ADR-080 over from the deleted g.md while its own event (which
    # also makes it Rfc ADR-080) is still pending. The takeover must not write
    # that Rfc entry: o.md's event would then see it as already there and never
    # relink x.md's edge into it.
    g = md(keyed, "decisions/g.md", "id: ADR-080")
    md(keyed, "decisions/x.md", "id: ADR-081\nrfc: ADR-080")
    kscan(engine, keyed)

    o = md(keyed, "decisions/o.md", "id: ADR-080\nkind: rfc")
    g.unlink()
    remove_paths(engine, REPO, keyed, {g})
    assert entries(engine, RFC) == {}
    index_paths(engine, REPO, keyed, {o})

    assert entries(engine)["ADR-080"] == "decisions/o.md"
    assert links(engine, "ZZ_ADR_RFC") == [("ADR-081", "ADR-080")]
    assert_matches_fresh_apply(engine, REPO, keyed)


@pytest.mark.parametrize("gives_up", ["delete", "re-id"])
def test_a_relink_writes_no_edge_onto_an_entry_its_pending_owner_has_not_moved_yet(engine, keyed, gives_up):
    # g.md claims ADR-K3 on disk (outranking sub/c.md) but its event is pending,
    # so the entry is still at sub/c.md. A relink must not hang g.md's links on
    # it: if g.md gives the id up before its event, nothing would remove them.
    md(keyed, "decisions/sub/c.md", "id: ADR-K3")
    g = keyed / "decisions" / "g.md"
    if gives_up == "re-id":
        md(keyed, "decisions/g.md", "id: ADR-K5")
    kscan(engine, keyed)

    md(keyed, "decisions/g.md", "id: ADR-K3\nrfc: ADR-K1")
    o = md(keyed, "decisions/o.md", "id: ADR-K1\nkind: rfc")
    index_paths(engine, REPO, keyed, {o})
    assert links(engine, "ZZ_ADR_RFC") == []

    if gives_up == "delete":
        g.unlink()
        remove_paths(engine, REPO, keyed, {g})
    else:
        md(keyed, "decisions/g.md", "id: ADR-K5")
        index_paths(engine, REPO, keyed, {g})
    assert entries(engine)["ADR-K3"] == "decisions/sub/c.md"
    assert links(engine, "ZZ_ADR_RFC") == []
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_relink_links_an_entry_from_its_file_while_a_pending_claimant_outranks_it(engine, keyed):
    # g.md holds ADR-K1 and names RFC K3, which doesn't exist yet. b.md claims
    # ADR-K1 too (and outranks g.md) but its event is pending when K3 appears,
    # then b.md is deleted before it: the entry stays at g.md, linked from g.md.
    g = md(keyed, "decisions/g.md", "id: ADR-K1\nrfc: ADR-K3")
    kscan(engine, keyed)

    b = md(keyed, "decisions/b.md", "id: ADR-K1")
    r = md(keyed, "x/r.md", "id: ADR-K3\nkind: rfc")
    index_paths(engine, REPO, keyed, {r})
    b.unlink()
    remove_paths(engine, REPO, keyed, {b})

    assert entries(engine)["ADR-K1"] == "decisions/g.md" and g.exists()
    assert links(engine, "ZZ_ADR_RFC") == [("ADR-K1", "ADR-K3")]
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_takeover_rebuilds_the_links_of_the_owners_other_entries(engine, keyed):
    # o.md is already the Adr R-9 (with a link) when it takes Rfc R-9 over from
    # a/g.md: the takeover clears o.md's outgoing links, so it rebuilds those too.
    g = md(keyed, "a/g.md", "kind: rfc\nid: R-9")
    md(keyed, "decisions/o.md", "kind: rfc\nid: R-9\nsupersedes: ADR-011")
    kscan(engine, keyed)
    assert entries(engine, RFC) == {"R-9": "a/g.md"}

    g.unlink()
    remove_paths(engine, REPO, keyed, {g})
    assert entries(engine, RFC) == {"R-9": "decisions/o.md"}
    assert ("R-9", "ADR-011") in links(engine, "ZZ_REPLACES")
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_a_symlink_into_an_ignored_directory_is_left_out_by_the_scan_and_by_a_save(engine, keyed):
    md(keyed, "build/hidden.md", "kind: rfc\nid: RFC-H")
    (keyed / "decisions" / "h.md").symlink_to(keyed / "build" / "hidden.md")
    kscan(engine, keyed)
    assert entries(engine, RFC) == {}
    assert not [line for line in doctor_lines(keyed) if "build/hidden.md" in line]

    index_paths(engine, REPO, keyed, {keyed / "decisions" / "h.md"})
    assert entries(engine, RFC) == {}
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_deleting_an_owner_without_a_claimant_removes_its_entry_and_edges(engine, keyed):
    kscan(engine, keyed)
    path = keyed / "decisions" / "adr-012.md"
    path.unlink()
    remove_paths(engine, REPO, keyed, {path})
    assert "ADR-012" not in entries(engine)
    assert links(engine, "ZZ_REPLACES") == [("ADR-013", "ADR-011")]
    assert links(engine, "ZZ_RUNBOOK_ADR") == []
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_deleting_the_folder_removes_every_entry(engine, keyed):
    md(keyed, "decisions/x.md", "kind: rfc\nid: RFC-1")
    md(keyed, "decisions/adr-012 copy.md", "id: ADR-012")
    kscan(engine, keyed)
    shutil.rmtree(keyed / "decisions")
    remove_paths(engine, REPO, keyed, {keyed / "decisions"})
    assert entries(engine) == {} and entries(engine, RFC) == {}
    assert links(engine, "ZZ_REPLACES") == [] and links(engine, "ZZ_RUNBOOK_ADR") == []
    assert_matches_fresh_apply(engine, REPO, keyed)


# --- bounds ------------------------------------------------------------------


@pytest.fixture
def path_lists(engine, monkeypatch):
    """Every repo-relative path list the docs engine calls receive."""
    seen = []
    # Where each call takes its list: (repo_id, extractor, paths, ...), (repo_id, files), (repo_id, label, names).
    positions = {
        "delete_extracted_edges": 2, "prune_extracted_at": 2, "delete_extracted_nodes": 2,
        "extracted_nodes_at": 2, "list_file_nodes": 1, "existing_node_names": 2,
    }
    for name, position in positions.items():
        real = getattr(engine, name)

        def spy(*args, _real=real, _name=name, _position=position):
            if args[_position] is not None:
                seen.append((_name, list(args[_position])))
            return _real(*args)

        monkeypatch.setattr(engine, name, spy)
    return seen


@pytest.fixture
def reads(monkeypatch):
    read = []
    real = docs.read_front_matter
    monkeypatch.setattr(docs, "read_front_matter", lambda path: read.append(path) or real(path))
    return read


def test_fifty_duplicate_claimants_keep_every_path_list_small(engine, keyed, path_lists, reads):
    for n in range(50):
        md(keyed, f"decisions/adr-012 copy {n}.md", "id: ADR-012\nsupersedes: ADR-013")
    kscan(engine, keyed)

    for batch in ([keyed / "decisions" / "adr-012 copy 7.md"], [keyed / "decisions" / "adr-012.md"]):
        path_lists.clear()
        reads.clear()
        index_paths(engine, REPO, keyed, set(batch))
        assert path_lists
        assert max(len(paths) for _name, paths in path_lists) <= len(batch) + 1  # |batch| + |K|
        assert len(reads) == len(set(reads))

    path_lists.clear()
    reads.clear()
    gone = keyed / "decisions" / "adr-012.md"
    gone.unlink()
    remove_paths(engine, REPO, keyed, {gone})
    assert max(len(paths) for _name, paths in path_lists) <= 2
    assert len(reads) == len(set(reads))
    assert entries(engine)["ADR-012"] == "decisions/adr-012 copy 0.md"
    assert_matches_fresh_apply(engine, REPO, keyed)


def test_without_keyed_types_a_save_reads_only_the_batch(engine, repo, reads):
    scan(engine, repo)
    reads.clear()
    path = md(repo, "runbooks/api.md", "type: runbook\nowner: team-c\nservice: api")
    index_paths(engine, REPO, repo, {path})
    assert reads == [path]
    assert_matches_fresh_apply(engine, REPO, repo)
