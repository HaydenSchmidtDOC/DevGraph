"""Tests for the get_source MCP tool: reads a Function/Class's actual source
text off disk using the graph's last-indexed line range, plus docstring_full.
"""

import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import index_paths
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


@pytest.fixture
def repo_with_sibling(engine):
    """A registered repo `proj` next to an unregistered `proj-private`."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "proj"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        sibling = Path(tmpdir) / "proj-private"
        sibling.mkdir()
        (sibling / "x.py").write_text("SIBLING_CONTENT = 1\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as regdir:
            registry = RepoRegistry(Path(regdir) / "registry.db")
            record = registry.add_repo(repo_root, repo_id="_smoketest_get_source_sibling")
            engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
            try:
                yield engine, registry, record.repo_id
            finally:
                engine.delete_repository(record.repo_id)
                registry.close()


@pytest.mark.parametrize(
    "file_value",
    ["../proj-private/x.py", "{sibling}/x.py"],
    ids=["relative-escape", "absolute-sibling-prefix"],
)
def test_get_source_refuses_file_outside_the_repo(repo_with_sibling, file_value):
    engine, registry, repo_id = repo_with_sibling
    repo_root = registry.get(repo_id).path
    file_value = file_value.format(sibling=str(repo_root.resolve()) + "-private")
    engine.run_cypher(
        "CREATE (:Function {repo_id: $repo_id, name: 'escaper', file: $file, start_line: 1, end_line: 1})",
        {"repo_id": repo_id, "file": file_value},
    )

    result = get_source(engine, registry, repo_id, "escaper")

    assert result["source"] is None
    assert result["file"] is None


@pytest.fixture
def two_helpers(engine):
    """`helper` defined in a.py and b.py, each with a `run` that calls `target`."""
    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as regdir:
        repo_root = Path(tmpdir)
        (repo_root / ".git").mkdir()
        for stem in ("a", "b"):
            (repo_root / f"{stem}.py").write_text(
                f"def helper():\n    return '{stem}'\n\n\ndef run():\n    from t import target\n    return target()\n", encoding="utf-8"
            )
        (repo_root / "t.py").write_text("def target():\n    return 1\n", encoding="utf-8")
        registry = RepoRegistry(Path(regdir) / "registry.db")
        record = registry.add_repo(repo_root, repo_id="_smoketest_get_source_two")
        engine.delete_repository(record.repo_id)
        engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
        for name in ("t.py", "a.py", "b.py"):
            index_file(engine, record.repo_id, repo_root / name, repo_root=repo_root)
        try:
            yield engine, registry, record.repo_id, repo_root
        finally:
            engine.delete_repository(record.repo_id)
            registry.close()


def test_an_ambiguous_name_returns_candidates_not_one_at_random(two_helpers):
    engine, registry, repo_id, _root = two_helpers
    result = get_source(engine, registry, repo_id, "helper")
    assert result["status"] == "ambiguous"
    assert result["source"] is None
    assert result["count"] == 2 and result["truncated"] is False
    assert result["candidates"] == [
        {"label": "Function", "name": "helper", "file": "a.py"},
        {"label": "Function", "name": "helper", "file": "b.py"},
    ]


def test_file_picks_one_of_several_same_named_nodes(two_helpers):
    engine, registry, repo_id, _root = two_helpers
    result = get_source(engine, registry, repo_id, "helper", file="b.py")
    assert "status" not in result
    assert result["file"] == "b.py"
    assert "return 'b'" in result["source"]


def test_a_file_that_is_not_utf8_is_decoded_not_raised(two_helpers):
    engine, registry, repo_id, root = two_helpers
    (root / "b.py").write_bytes(b"def helper():\n    return 'caf\xe9'\n")  # Latin-1, as on disk
    result = get_source(engine, registry, repo_id, "helper", file="b.py")
    assert result["source"] == "def helper():\n    return 'café'"  # as the indexer decodes it
    assert "UTF-8" in result["notice"] and "cp1252" in result["notice"]


def test_get_source_honours_a_pep_263_declaration(two_helpers):
    engine, registry, repo_id, root = two_helpers
    (root / "b.py").write_bytes("# -*- coding: latin-1 -*-\ndef helper():\n    return 'café'\n".encode("latin-1"))
    index_paths(engine, repo_id, root, {root / "b.py"})  # the def moved down a line
    result = get_source(engine, registry, repo_id, "helper", file="b.py")
    assert result["source"] == "def helper():\n    return 'café'"
    assert "notice" not in result  # declared, so read as intended


def test_find_callers_keeps_same_named_callers_in_different_files_apart(two_helpers):
    from devgraph.mcp.tools import find_callers

    engine, _registry, repo_id, _root = two_helpers
    result = find_callers(engine, repo_id, "target")
    rows = [(r["name"], r["file"]) for r in result["results"]]
    assert ("run", "a.py") in rows and ("run", "b.py") in rows


def test_get_source_notes_when_a_declared_codec_did_not_decode_the_file(two_helpers):
    engine, registry, repo_id, root = two_helpers
    (root / "b.py").write_bytes("# coding: ascii\ndef helper():\n    return 'café'\n".encode())
    index_paths(engine, repo_id, root, {root / "b.py"})
    result = get_source(engine, registry, repo_id, "helper", file="b.py")
    assert result["source"] == "def helper():\n    return 'café'"
    assert "ascii" in result["notice"] and "utf-8" in result["notice"]
