"""Applied-schema state and removed-type cleanup on a live Neo4j."""

import pytest

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_applied_schema"
OTHER = "_smoketest_applied_schema_other"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    yield test_engine
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    test_engine.close()


def test_no_state_until_recorded(engine):
    assert engine.read_applied_schema(REPO) is None
    engine.upsert_repository(REPO, REPO, "/tmp/demo")
    assert engine.read_applied_schema(REPO) is None


def test_record_and_read_back(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/demo")
    engine.record_applied_schema(REPO, "sha256:abc", ["Widget"], ["LINKS"], ["Widget:sku"])
    assert engine.read_applied_schema(REPO) == {
        "hash": "sha256:abc", "labels": ["Widget"], "relationship_types": ["LINKS"], "keys": ["Widget:sku"]
    }
    assert {"repo_id": REPO, "labels": ["Widget"], "keys": ["Widget:sku"]} in engine.read_all_applied_schemas()
    engine.record_applied_schema(REPO, "absent", [], [])
    assert engine.read_applied_schema(REPO) == {"hash": "absent", "labels": [], "relationship_types": [], "keys": []}


def test_delete_label_nodes_is_scoped_to_the_repo(engine):
    engine.run_cypher("CREATE (:ZzWidget {repo_id: $a, name: 'w1'}), (:ZzWidget {repo_id: $b, name: 'w2'})", {"a": REPO, "b": OTHER})
    assert engine.delete_label_nodes(REPO, "ZzWidget") == 1
    remaining = engine.run_cypher("MATCH (n:ZzWidget) RETURN n.repo_id AS r", {})
    assert [r["r"] for r in remaining] == [OTHER]


def test_delete_relationship_type_keeps_the_nodes(engine):
    engine.run_cypher(
        "CREATE (a:ZzWidget {repo_id: $r, name: 'a'})-[:ZZ_LINKS]->(b:ZzWidget {repo_id: $r, name: 'b'})",
        {"r": REPO},
    )
    assert engine.delete_relationship_type(REPO, "ZZ_LINKS") == 1
    assert engine.run_cypher("MATCH (n:ZzWidget {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 2}]
