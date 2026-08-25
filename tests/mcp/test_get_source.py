"""Tests for the get_source MCP tool: reads a Function/Class's actual source
text off disk using the graph's last-indexed line range, plus docstring_full.
"""

import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file
from devgraph.mcp.tools import get_source
from devgraph.registry.store import RepoRegistry


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


@pytest.fixture
def repo_with_indexed_file(engine):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / ".git").mkdir()
        source_file = repo_root / "greeter.py"
        source_file.write_text(
            'def greet(name: str) -> str:\n'
            '    """Say hello to someone."""\n'
            '    return f"hello {name}"\n'
            '\n\n'
            'class Greeter:\n'
            '    """Greets people."""\n'
            '\n'
            '    def run(self):\n'
            '        return greet("world")\n',
            encoding="utf-8",
        )

        with tempfile.TemporaryDirectory() as regdir:
            registry = RepoRegistry(Path(regdir) / "registry.db")
            record = registry.add_repo(repo_root, repo_id="_smoketest_get_source")
            engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
            index_file(engine, record.repo_id, source_file, repo_root=repo_root)

            yield engine, registry, record.repo_id

            engine.delete_repository(record.repo_id)
            registry.close()


def test_get_source_returns_function_text(repo_with_indexed_file):
    engine, registry, repo_id = repo_with_indexed_file
    result = get_source(engine, registry, repo_id, "greet")
    assert result["name"] == "greet"
    assert result["label"] == "Function"
    assert "return f\"hello {name}\"" in result["source"]
    assert result["docstring_full"] == "Say hello to someone."


def test_get_source_returns_class_text(repo_with_indexed_file):
    engine, registry, repo_id = repo_with_indexed_file
    result = get_source(engine, registry, repo_id, "Greeter")
    assert result["label"] == "Class"
    assert "def run(self):" in result["source"]
    assert result["docstring_full"] == "Greets people."


def test_get_source_unknown_component_returns_empty(repo_with_indexed_file):
    engine, registry, repo_id = repo_with_indexed_file
    result = get_source(engine, registry, repo_id, "does_not_exist")
    assert result["source"] is None
    assert result["file"] is None
