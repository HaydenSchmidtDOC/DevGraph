"""Generated constraints/indexes follow the schema across every repository sharing the database.

Every test uses a random `Zz...` label and its own repo ids, so it never
touches another test's (or a real repository's) constraints.
"""

import logging
import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.graph.schema import constraint_statements
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import apply_project_schema
from devgraph.indexer.schema_constraints import constraint_drift, release_labels, stale_generated_objects


def _token():
    return uuid.uuid4().hex[:10]


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
def label(engine):
    name = f"Zz{_token()}"
    yield name
    for statement in (
        f"DROP CONSTRAINT {name.lower()}_repo_key IF EXISTS",
        f"DROP INDEX {name.lower()}_repo_name IF EXISTS",
    ):
        engine.run_cypher(statement)


@pytest.fixture
def repos(engine, tmp_path):
    """Factory for scratch repositories; every one is deleted from the graph afterwards."""
    made = []

    def make():
        repo_id = f"_smoketest_constraints_{_token()}"
        root = tmp_path / repo_id
        root.mkdir()
        (root / "notes.txt").write_text("hello\n")
        engine.upsert_repository(repo_id, repo_id, str(root))
        made.append(repo_id)
        return repo_id, root

    yield make
    for repo_id in made:
        engine.delete_repository(repo_id)


def declare(root, label, key="slug", filesystem=False):
    if filesystem:
        body = f"""
            version: 1
            node_types:
              - label: {label}
                key: [path]
                metadata: [{{name: path}}]
                source: {{provider: filesystem, kind: file}}
        """
    else:
        body = f"""
            version: 1
            node_types:
              - label: {label}
                key: [{key}]
                metadata: [{{name: {key}}}]
        """
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(body))


def undeclare(root):
    (root / "devgraph.schema.yaml").unlink()


def constraint(engine, label):
    rows = engine.run_cypher(
        "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties WHERE name = $n RETURN labelsOrTypes, properties",
        {"n": f"{label.lower()}_repo_key"},
    )
    return (rows[0]["labelsOrTypes"], rows[0]["properties"]) if rows else None


def index_exists(engine, label):
    rows = engine.run_cypher("SHOW INDEXES YIELD name WHERE name = $n RETURN name", {"n": f"{label.lower()}_repo_name"})
    return bool(rows)


def test_removing_a_label_from_its_only_repo_drops_its_constraint_and_index(engine, repos, label):
    repo_id, root = repos()
    declare(root, label, filesystem=True)
    assert apply_project_schema(engine, repo_id, root)
    assert constraint(engine, label) == ([label], ["repo_id", "path"])
    assert index_exists(engine, label)

    undeclare(root)
    assert apply_project_schema(engine, repo_id, root)
    assert constraint(engine, label) is None
    assert not index_exists(engine, label)


def test_a_shared_label_is_kept_until_the_last_repo_removes_it(engine, repos, label):
    a_id, a_root = repos()
    b_id, b_root = repos()
    for repo_id, root in ((a_id, a_root), (b_id, b_root)):
        declare(root, label)
        assert apply_project_schema(engine, repo_id, root)

    undeclare(a_root)
    assert apply_project_schema(engine, a_id, a_root)
    assert constraint(engine, label) == ([label], ["repo_id", "slug"])

    undeclare(b_root)
    assert apply_project_schema(engine, b_id, b_root)
    assert constraint(engine, label) is None


def test_a_label_with_remaining_nodes_is_kept(engine, repos, label):
    a_id, a_root = repos()
    declare(a_root, label)
    assert apply_project_schema(engine, a_id, a_root)
    other_id, _ = repos()  # a repository with nodes of the label but no recorded schema
    engine.run_cypher(f"CREATE (:{label} {{repo_id: $r, slug: 'x', name: 'x'}})", {"r": other_id})

    undeclare(a_root)
    assert apply_project_schema(engine, a_id, a_root)
    assert constraint(engine, label) is not None


def test_a_key_change_replaces_the_constraint(engine, repos, label):
    repo_id, root = repos()
    declare(root, label, key="slug")
    assert apply_project_schema(engine, repo_id, root)
    declare(root, label, key="code")
    assert apply_project_schema(engine, repo_id, root)
    assert constraint(engine, label) == ([label], ["repo_id", "code"])


def test_a_key_change_waits_until_every_repo_agrees(engine, repos, label, caplog):
    a_id, a_root = repos()
    b_id, b_root = repos()
    for repo_id, root in ((a_id, a_root), (b_id, b_root)):
        declare(root, label, key="slug")
        assert apply_project_schema(engine, repo_id, root)

    declare(a_root, label, key="code")
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.schema_constraints"):
        assert apply_project_schema(engine, a_id, a_root)
    assert constraint(engine, label) == ([label], ["repo_id", "slug"])
    assert any(label in r.getMessage() and b_id in r.getMessage() for r in caplog.records)

    declare(b_root, label, key="code")
    assert apply_project_schema(engine, b_id, b_root)
    assert constraint(engine, label) == ([label], ["repo_id", "code"])


def test_a_key_change_blocked_by_duplicate_nodes_never_drops_the_constraint(engine, repos, label, caplog, monkeypatch):
    repo_id, root = repos()
    declare(root, label, key="slug")
    assert apply_project_schema(engine, repo_id, root)
    engine.run_cypher(
        f"CREATE (:{label} {{repo_id: $r, slug: 'a', code: 'same'}}), (:{label} {{repo_id: $r, slug: 'b', code: 'same'}})",
        {"r": repo_id},
    )
    statements = []
    run = engine.run_schema_statement
    monkeypatch.setattr(engine, "run_schema_statement", lambda stmt: (statements.append(stmt), run(stmt))[1])

    declare(root, label, key="code")
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.schema_constraints"):
        assert apply_project_schema(engine, repo_id, root)
        assert apply_project_schema(engine, repo_id, root)
    assert constraint(engine, label) == ([label], ["repo_id", "slug"])
    assert not [s for s in statements if s.startswith("DROP")]
    assert any(label in r.getMessage() and "duplicate" in r.getMessage() for r in caplog.records)
    assert {"repo_id": repo_id, "label": label, "status": "blocked", "key": ("code",)} in constraint_drift(engine)


def test_a_release_racing_another_repos_apply_is_reprovisioned(engine, repos, label, monkeypatch):
    """A drops X after B provisioned it but before B recorded it; B's apply must still end with X constrained."""
    a_id, a_root = repos()
    b_id, b_root = repos()
    declare(a_root, label)
    assert apply_project_schema(engine, a_id, a_root)
    declare(b_root, label)
    undeclare(a_root)

    reconcile = dispatch.filesystem.reconcile
    raced = []

    def interleave(*args, **kwargs):  # runs between B's provisioning and B's record
        if not raced:
            raced.append(True)
            assert apply_project_schema(engine, a_id, a_root)
            assert constraint(engine, label) is None  # A saw nobody else recording X
        return reconcile(*args, **kwargs)

    monkeypatch.setattr(dispatch.filesystem, "reconcile", interleave)
    assert apply_project_schema(engine, b_id, b_root)
    assert raced
    assert constraint(engine, label) == ([label], ["repo_id", "slug"])


def test_drift_reports_an_applied_label_without_its_constraint(engine, repos, label):
    repo_id, root = repos()
    declare(root, label)
    assert apply_project_schema(engine, repo_id, root)
    assert not [d for d in constraint_drift(engine) if d["label"] == label]
    engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key")
    assert {"repo_id": repo_id, "label": label, "status": "missing", "key": ("slug",)} in constraint_drift(engine)


def builtin_constraints(engine):
    names = {s.split()[2] for s in constraint_statements() if s.startswith("CREATE")}
    rows = engine.run_cypher("SHOW CONSTRAINTS YIELD name RETURN name")
    return names & {r["name"] for r in rows}


def test_builtin_constraints_are_never_touched(engine, repos):
    engine.init_schema()
    before = builtin_constraints(engine)
    repo_id, root = repos()
    engine.record_applied_schema(repo_id, "sha256:old", ["Module", "Repository", "Class"], [])
    assert apply_project_schema(engine, repo_id, root)
    assert release_labels(engine, ["Module", "Repository", "Class", "Function"]) == []
    assert builtin_constraints(engine) == before


def test_a_stale_generated_constraint_is_reported_and_released(engine, label):
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.slug) IS UNIQUE")
    stale = [obj for obj in stale_generated_objects(engine, set()) if obj.label == label]
    assert [(obj.kind, obj.name) for obj in stale] == [("constraint", f"{label.lower()}_repo_key")]
    # Declared by a registered repository: not stale.
    assert not [obj for obj in stale_generated_objects(engine, {label.casefold()}) if obj.label == label]


def record_keyed_on(engine, repos, label, *keys):
    """One scratch repository per key, each recording `label` keyed on it; returns their ids."""
    ids = []
    for key in keys:
        repo_id, _root = repos()
        engine.record_applied_schema(repo_id, "sha256:x", [label], [], [f"{label}:{key}"])
        ids.append(repo_id)
    return ids


def test_drift_reports_a_conflict_when_another_repo_records_the_constraints_key(engine, repos, label):
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.path) IS UNIQUE")
    a_id, b_id = record_keyed_on(engine, repos, label, "adr_id", "path")
    assert [d for d in constraint_drift(engine) if d["label"] == label] == [{
        "repo_id": a_id, "label": label, "status": "conflict", "key": ("adr_id",),
        "constraint_key": ("path",), "declared_by": [b_id],
    }]


def test_drift_reports_blocked_over_conflict_when_duplicates_exist(engine, repos, label):
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.path) IS UNIQUE")
    a_id, _b_id = record_keyed_on(engine, repos, label, "adr_id", "path")
    engine.run_cypher(
        f"CREATE (:{label} {{repo_id: $r, path: 'a.md', adr_id: 'same'}}), "
        f"(:{label} {{repo_id: $r, path: 'b.md', adr_id: 'same'}})",
        {"r": a_id},
    )
    assert [d for d in constraint_drift(engine) if d["label"] == label] == [
        {"repo_id": a_id, "label": label, "status": "blocked", "key": ("adr_id",)}
    ]


def test_drift_reports_nothing_when_every_repo_agrees_with_the_constraint(engine, repos, label):
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.path) IS UNIQUE")
    record_keyed_on(engine, repos, label, "path", "path")
    assert not [d for d in constraint_drift(engine) if d["label"] == label]


def test_drift_reports_a_conflict_when_another_repo_records_a_third_key(engine, repos, label):
    # Neither repository records the stale constraint's key, but they disagree,
    # so realign_keys leaves it alone: doctor must not stay silent.
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.path) IS UNIQUE")
    a_id, b_id = record_keyed_on(engine, repos, label, "adr_id", "rfc_id")
    assert [d for d in constraint_drift(engine) if d["label"] == label] == sorted([
        {"repo_id": a_id, "label": label, "status": "conflict", "key": ("adr_id",),
         "constraint_key": ("path",), "declared_by": [b_id]},
        {"repo_id": b_id, "label": label, "status": "conflict", "key": ("rfc_id",),
         "constraint_key": ("path",), "declared_by": [a_id]},
    ], key=lambda d: d["repo_id"])


def test_drift_reports_a_conflict_when_another_repo_records_an_unknown_key(engine, repos, label):
    engine.run_cypher(f"CREATE CONSTRAINT {label.lower()}_repo_key FOR (n:{label}) REQUIRE (n.repo_id, n.path) IS UNIQUE")
    (a_id,) = record_keyed_on(engine, repos, label, "adr_id")
    b_id, _root = repos()
    engine.record_applied_schema(b_id, "sha256:x", [label], [])  # recorded before keys were
    assert [d for d in constraint_drift(engine) if d["label"] == label] == [{
        "repo_id": a_id, "label": label, "status": "conflict", "key": ("adr_id",),
        "constraint_key": ("path",), "declared_by": [b_id],
    }]
