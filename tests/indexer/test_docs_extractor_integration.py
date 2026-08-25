"""Integration tests for the docs extractor with live Neo4j database."""

import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.docs.extractor import index_file


@pytest.fixture
def graph_engine():
    """Create a GraphEngine connected to local Neo4j for testing."""
    engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")

    engine.init_schema()
    yield engine
    engine.close()


def test_index_requirement_links_to_existing_module(graph_engine):
    """A Requirement note linking to a Module upserts the SATISFIES edge."""
    repo_id = "_smoketest_docs_extractor"

    graph_engine.upsert_node("Module", repo_id, "AuthService", {})

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(
            "---\n"
            "type: requirement\n"
            "id: req-auth-001\n"
            "links: [AuthService]\n"
            "---\n"
            "# Users must authenticate\n"
        )
        file_path = f.name

    try:
        index_file(graph_engine, repo_id, file_path)

        result = graph_engine.run_cypher(
            "MATCH (r:Requirement {repo_id: $repo_id}) RETURN r.name as name",
            {"repo_id": repo_id},
        )
        assert len(result) == 1
        assert result[0]["name"] == "req-auth-001"

        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id, name: 'AuthService'})"
            "-[:SATISFIES]->(r:Requirement {name: 'req-auth-001'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": repo_id},
        )
        assert result[0]["count"] == 1
    finally:
        graph_engine.delete_repository(repo_id)
        Path(file_path).unlink()


def test_index_file_idempotent(graph_engine):
    """Re-indexing the same note doesn't create duplicate nodes."""
    repo_id = "_smoketest_docs_idempotence"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write("---\ntype: architecture_note\nid: note-x\n---\n# X\n")
        file_path = f.name

    try:
        index_file(graph_engine, repo_id, file_path)
        index_file(graph_engine, repo_id, file_path)

        result = graph_engine.run_cypher(
            "MATCH (n:ArchitectureNote {repo_id: $repo_id, name: 'note-x'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": repo_id},
        )
        assert result[0]["count"] == 1
    finally:
        graph_engine.delete_repository(repo_id)
        Path(file_path).unlink()
