"""Integration tests for Python extractor with live Neo4j database."""

import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file


@pytest.fixture
def graph_engine():
    """Create a GraphEngine connected to local Neo4j for testing."""
    engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    # Verify connectivity
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")

    # Initialize schema
    engine.init_schema()
    yield engine
    engine.close()


def test_index_python_file_end_to_end(graph_engine):
    """Test indexing a Python file and verifying it in Neo4j."""
    repo_id = "_smoketest_python_extractor"

    # Create a temporary Python file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("""
from typing import List

class DataService:
    '''A service for data operations.'''

    def __init__(self, db_path: str):
        self.db_path = db_path

    def fetch_data(self) -> List[dict]:
        '''Fetch data from the database.'''
        return []

    def save_data(self, data: dict) -> bool:
        '''Save data to the database.'''
        return True

def process_batch(items: List[str]) -> None:
    '''Process a batch of items.'''
    pass
""")
        file_path = f.name

    try:
        # Index the file
        index_file(graph_engine, repo_id, file_path)

        # Verify nodes were created
        # Query for Module node
        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id}) RETURN m.name as name",
            {"repo_id": repo_id}
        )
        assert len(result) > 0, "Module node not found"
        module_name = result[0]["name"]
        assert "test_python_extractor_integration" in module_name or module_name.endswith(".py")

        # Query for Class nodes
        result = graph_engine.run_cypher(
            "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
            {"repo_id": repo_id}
        )
        class_names = [r["name"] for r in result]
        assert "DataService" in class_names

        # Query for Function nodes
        result = graph_engine.run_cypher(
            "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name as name ORDER BY f.name",
            {"repo_id": repo_id}
        )
        function_names = [r["name"] for r in result]
        assert "__init__" in function_names
        assert "fetch_data" in function_names
        assert "save_data" in function_names
        assert "process_batch" in function_names

        # Verify relationships
        # Module CONTAINS DataService
        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id})-[:CONTAINS]->(c:Class {name: 'DataService'}) RETURN COUNT(*) as count",
            {"repo_id": repo_id}
        )
        assert result[0]["count"] > 0, "Module CONTAINS DataService relationship not found"

        # DataService CONTAINS __init__
        result = graph_engine.run_cypher(
            "MATCH (c:Class {name: 'DataService', repo_id: $repo_id})-[:CONTAINS]->(f:Function {name: '__init__'}) RETURN COUNT(*) as count",
            {"repo_id": repo_id}
        )
        assert result[0]["count"] > 0, "DataService CONTAINS __init__ relationship not found"

        # Note: IMPORTS relationships to external modules (like 'typing') won't appear
        # in the graph query because the target Module node doesn't exist unless
        # those modules are also indexed. The extractor correctly identifies imports,
        # but GraphEngine.upsert_relationship requires both nodes to exist.
        # This is the expected behavior for an incremental indexer.

        print("Integration test passed: All nodes and relationships verified in Neo4j")

    finally:
        # Cleanup: delete the repo from the graph
        graph_engine.delete_repository(repo_id)

        # Verify cleanup
        result = graph_engine.run_cypher(
            "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as count",
            {"repo_id": repo_id}
        )
        assert result[0]["count"] == 0, "Cleanup failed: nodes still exist for the test repo"

        # Clean up the temporary file
        Path(file_path).unlink()


def test_index_file_creates_idempotent_nodes(graph_engine):
    """Test that re-indexing the same file doesn't create duplicates."""
    repo_id = "_smoketest_idempotence"

    # Create a temporary Python file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("class MyClass:\n    pass\n")
        file_path = f.name

    try:
        # Index the file twice
        index_file(graph_engine, repo_id, file_path)
        index_file(graph_engine, repo_id, file_path)

        # Verify there's only one MyClass node
        result = graph_engine.run_cypher(
            "MATCH (c:Class {repo_id: $repo_id, name: 'MyClass'}) RETURN COUNT(*) as count",
            {"repo_id": repo_id}
        )
        assert result[0]["count"] == 1, "Duplicate class node created on re-index"

        print("Idempotence test passed: Re-indexing doesn't create duplicates")

    finally:
        # Cleanup
        graph_engine.delete_repository(repo_id)
        Path(file_path).unlink()
