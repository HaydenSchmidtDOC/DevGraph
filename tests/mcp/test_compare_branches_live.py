"""compare_branches against a live Neo4j: the C8 callers query runs and finds the indexed callers."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp.tools import compare_branches
from devgraph.registry.store import RepoRegistry
from tests.indexer.git_compare_helpers import two_branch_repo

# Unique per run, so a concurrent run never deletes this run's repository.
REPO = f"_smoketest_compare_branches_{uuid.uuid4().hex[:8]}"
NEO4J = {"uri": "bolt://127.0.0.1:7687", "user": "neo4j", "password": "devgraph-local-dev"}

LIB = "def edit():\n    return 1\n\ndef gone():\n    return 1\n\ndef quiet():\n    return 1\n"
USES = "from lib import edit, gone\n\ndef use_edit():\n    return edit()\n\ndef use_gone():\n    return gone()\n"


@pytest.fixture
def engine():
    test_engine = GraphEngine(**NEO4J)
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        test_engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    try:
        yield test_engine
    finally:
        try:
            test_engine.delete_repository(REPO)
        finally:
            test_engine.close()


def test_callers_of_changed_and_removed_symbols(tmp_path, engine):
    repo = two_branch_repo(
        tmp_path,
        {"lib.py": LIB, "uses.py": USES},
        {"lib.py": "def edit():\n    return 2\n\ndef quiet():\n    return 2\n\ndef fresh():\n    return 1\n"},
    )
    registry = RepoRegistry(tmp_path / "r.db")
    registry.add_repo(repo, REPO)
    engine.upsert_repository(REPO, "lib", str(repo))
    full_scan(engine, REPO, repo)  # the working tree is `main`

    result = compare_branches(engine, registry, REPO, "main", "feature")

    (lib,) = result["files"]["results"]
    assert sorted(s["name"] for s in lib["symbols"]["changed"]) == ["edit", "quiet"]
    assert [s["name"] for s in lib["symbols"]["removed"]] == ["gone"]
    callers = result["impacted_callers"]
    assert callers["truncated"] is False
    assert callers["results"] == [
        {"caller": "use_edit", "caller_type": "Function", "caller_file": "uses.py", "calls": "edit", "calls_file": "lib.py"},
        {"caller": "use_gone", "caller_type": "Function", "caller_file": "uses.py", "calls": "gone", "calls_file": "lib.py"},
    ]
    assert result["notices"] == ["impacted_callers come from the last index of the working tree, not from either ref"]
