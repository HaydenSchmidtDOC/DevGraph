"""get_source against a live Neo4j: the stored line range is tree-sitter's rows,
which break on `\\n` only, so the file is split the same way. A form feed,
`\\v`, `\\x1c`-`\\x1e`, `\\x85`, U+2028 or U+2029 is not a line break, and
CRLF files still return their lines without the `\\r`."""

import tempfile
import uuid
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import index_paths
from devgraph.mcp.tools import get_source
from devgraph.registry.store import RepoRegistry

# Unique per run, so sessions sharing the Neo4j never seed or delete each
# other's repositories; tests/conftest.py sweeps any a teardown leaves.
_TOKEN = uuid.uuid4().hex[:8]

_FILES = {
    "a.py": "def first():\n    return 1\n\x0c\ndef second():\n    s = 'x y\x85z\x0b\x1c'\n    return 2\n\ndef third():\n    return 3\n",
    "b.js": "function one() {\n  const s = 'a b';\n  return 1;\n}\nfunction two() {\n  return 2;\n}\n",
    "c.py": "def alpha():\r\n    return 1\r\n\r\ndef beta():\r\n    return 2\r\n",
}


@pytest.fixture
def indexed():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as regdir:
        root = Path(tmpdir)
        (root / ".git").mkdir()
        for rel, text in _FILES.items():
            (root / rel).write_bytes(text.encode("utf-8"))
        registry = RepoRegistry(Path(regdir) / "registry.db")
        record = registry.add_repo(root, repo_id=f"_smoketest_get_source_breaks_{_TOKEN}")
        engine.delete_repository(record.repo_id)
        engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
        index_paths(engine, record.repo_id, root, {root / rel for rel in _FILES})
        try:
            yield engine, registry, record.repo_id
        finally:
            engine.delete_repository(record.repo_id)
            registry.close()
            engine.close()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("second", "def second():\n    s = 'x y\x85z\x0b\x1c'\n    return 2"),
        ("third", "def third():\n    return 3"),
        ("two", "function two() {\n  return 2;\n}"),
        ("beta", "def beta():\n    return 2"),
    ],
)
def test_get_source_returns_exactly_the_definition(indexed, name, expected):
    engine, registry, repo_id = indexed
    assert get_source(engine, registry, repo_id, name)["source"] == expected
