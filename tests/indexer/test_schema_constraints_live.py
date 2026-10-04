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
from devgraph.indexer.dispatch import apply_project_schema
from devgraph.indexer.schema_constraints import release_labels, stale_generated_objects


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


def test_a_key_the_existing_nodes_violate_restores_the_old_constraint(engine, repos, label, caplog):
    repo_id, root = repos()
    declare(root, label, key="slug")
    assert apply_project_schema(engine, repo_id, root)
    engine.run_cypher(
        f"CREATE (:{label} {{repo_id: $r, slug: 'a', code: 'same'}}), (:{label} {{repo_id: $r, slug: 'b', code: 'same'}})",
        {"r": repo_id},
    )
    declare(root, label, key="code")
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.schema_constraints"):
        assert apply_project_schema(engine, repo_id, root)
    assert constraint(engine, label) == ([label], ["repo_id", "slug"])
    assert any(label in r.getMessage() for r in caplog.records)


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
