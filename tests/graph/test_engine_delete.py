"""Tests for GraphEngine.delete_nodes_by_source_file — the file-delete
cleanup path that was previously entirely missing (only whole-repo
delete_repository existed)."""

import pytest

from devgraph.graph.engine import GraphEngine


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


class TestDeleteNodesBySourceFile:
    def test_deletes_nodes_matching_source_file_property(self, engine):
        repo_id = "_smoketest_delete_source_file"
        engine.upsert_node("Module", repo_id, "gone.py", {"type": "module", "source_file": "gone.py"})
        engine.upsert_node("Class", repo_id, "GoneClass", {"file": "gone.py"})
        engine.upsert_node("Module", repo_id, "kept.py", {"type": "module", "source_file": "kept.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "gone.py")

            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in remaining}
            assert names == {"kept.py"}
        finally:
            engine.delete_repository(repo_id)

    def test_matches_source_property_variant(self, engine):
        """datastores/apis/containers extractors use `source` instead of `source_file`/`file`."""
        repo_id = "_smoketest_delete_source_variant"
        engine.upsert_node("Database", repo_id, "PostgreSQL", {"source": "db.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "db.py")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert remaining[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_scoped_to_repo_id(self, engine):
        repo_a = "_smoketest_delete_scope_a"
        repo_b = "_smoketest_delete_scope_b"
        engine.upsert_node("Module", repo_a, "shared_name.py", {"source_file": "shared_name.py"})
        engine.upsert_node("Module", repo_b, "shared_name.py", {"source_file": "shared_name.py"})

        try:
            engine.delete_nodes_by_source_file(repo_a, "shared_name.py")

            remaining_a = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_a}
            )
            remaining_b = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_b}
            )
            assert remaining_a[0]["c"] == 0
            assert remaining_b[0]["c"] == 1
        finally:
            engine.delete_repository(repo_a)
            engine.delete_repository(repo_b)

    def test_no_match_is_a_no_op(self, engine):
        repo_id = "_smoketest_delete_no_match"
        engine.upsert_node("Module", repo_id, "kept.py", {"source_file": "kept.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "nonexistent.py")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert remaining[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)
