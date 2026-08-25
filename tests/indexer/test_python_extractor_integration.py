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

        # Note: IMPORTS relationships to external/stdlib modules (like 'typing')
        # won't appear in the graph because no Module node exists for them
        # (they aren't part of this repo). GraphEngine.upsert_relationship
        # requires both endpoint nodes to exist. This is expected for
        # absolute imports. Relative imports (from . import X) DO resolve to
        # a same-repo Module node once that file is indexed too — see
        # test_relative_import_edge_resolves_to_indexed_module below.

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


def test_relative_import_edge_resolves_to_indexed_module(graph_engine):
    """A relative import's IMPORTS edge must actually resolve to the sibling
    Module node once both files are indexed — this is the fix for a bug
    where relative-import targets (e.g. '.', '.helpers') never matched any
    real Module node name, so find_related_files' imported_modules was
    always empty regardless of what got indexed.
    """
    repo_id = "_smoketest_relative_imports"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("from . import utils\n")
        main_path = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def helper():\n    pass\n")
        utils_path = f.name

    try:
        # Target node must exist before the IMPORTS edge is upserted —
        # upsert_relationship MATCHes both endpoints rather than creating
        # them, same as every other cross-file edge in this codebase.
        graph_engine.upsert_node("Module", repo_id, "utils.py", {"type": "module"})
        index_file(graph_engine, repo_id, main_path)

        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id})-[:IMPORTS]->(u:Module {name: 'utils.py'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": repo_id},
        )
        assert result[0]["count"] == 1, "Relative-import IMPORTS edge did not resolve to the sibling Module node"
    finally:
        graph_engine.delete_repository(repo_id)
        Path(main_path).unlink()
        Path(utils_path).unlink()


def test_calls_edges_make_find_callers_work_end_to_end(graph_engine):
    """CALLS edges must actually resolve so find_callers/impact_analysis
    (previously always empty — no CALLS extraction existed at all) work
    against real indexed data, not just in extractor-unit isolation.
    """
    from devgraph.mcp.tools import find_callers, impact_analysis

    repo_id = "_smoketest_calls_edges"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(
            "def helper():\n"
            "    pass\n"
            "\n"
            "def caller_one():\n"
            "    helper()\n"
            "\n"
            "class Service:\n"
            "    def process(self):\n"
            "        self.helper_method()\n"
            "\n"
            "    def helper_method(self):\n"
            "        pass\n"
        )
        file_path = f.name

    try:
        index_file(graph_engine, repo_id, file_path)

        callers = find_callers(graph_engine, repo_id, "helper")
        caller_names = {c["name"] for c in callers}
        assert "caller_one" in caller_names

        callers_method = find_callers(graph_engine, repo_id, "helper_method")
        assert any(c["name"] == "process" for c in callers_method)

        impact = impact_analysis(graph_engine, repo_id, "helper")
        dependent_names = {d["name"] for d in impact["direct_dependents"]}
        assert "caller_one" in dependent_names
    finally:
        graph_engine.delete_repository(repo_id)
        Path(file_path).unlink()


def test_dotted_absolute_import_edge_resolves_end_to_end(graph_engine):
    """A same-repo absolute dotted import ('from services.api_gateway.clients
    import X') must actually link to the real Module node once both files
    are indexed with repo_root-relative paths — this is the fix for RAG4
    (real repo, absolute-dotted-import style exclusively) having zero
    resolvable same-repo IMPORTS edges.
    """
    repo_id = "_smoketest_dotted_absolute_import"

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / "services" / "api_gateway").mkdir(parents=True)
        (repo_root / "services" / "api_gateway" / "clients.py").write_text("class Client:\n    pass\n")
        main_path = repo_root / "services" / "api_gateway" / "main.py"
        main_path.write_text("from services.api_gateway.clients import Client\n")

        try:
            index_file(graph_engine, repo_id, repo_root / "services" / "api_gateway" / "clients.py", repo_root=repo_root)
            index_file(graph_engine, repo_id, main_path, repo_root=repo_root)

            result = graph_engine.run_cypher(
                "MATCH (m:Module {repo_id: $repo_id, name: 'services/api_gateway/main.py'})"
                "-[:IMPORTS]->(t:Module {name: 'services/api_gateway/clients.py'}) "
                "RETURN COUNT(*) as count",
                {"repo_id": repo_id},
            )
            assert result[0]["count"] == 1
        finally:
            graph_engine.delete_repository(repo_id)
