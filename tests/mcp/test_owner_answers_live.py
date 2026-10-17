"""find_callers, impact_analysis and find_related_files say where an answer
is (a caller's owner and line, a dependent's file and owner) and take one
class's method by `owner` or as `A.run`, against a live Neo4j. Fixture code
is fictional."""

import uuid

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools

CORE = """\
def helper():
    return 0


class A:
    def run(self):
        return helper()


class B:
    def run(self):
        return helper()
"""
USE = """\
from app.core import A


def go():
    return A().run()
"""


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
def repo(engine, tmp_path):
    repo_id = f"zz-owners-{uuid.uuid4().hex[:8]}"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "core.py").write_text(CORE)
    (tmp_path / "app" / "use.py").write_text(USE)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    yield repo_id
    engine.delete_repository(repo_id)


def test_callers_name_their_owner_and_line(engine, repo):
    rows = tools.find_callers(engine, repo, "helper")["results"]
    assert sorted((r["name"], r["file"], r["owner"], r["start_line"]) for r in rows) == [
        ("run", "app/core.py", "A", 6), ("run", "app/core.py", "B", 11),
    ]


def test_one_class_s_method_by_owner_or_qualified_name(engine, repo):
    by_owner = tools.find_callers(engine, repo, "run", owner="A")["results"]
    assert [(r["name"], r["file"]) for r in by_owner] == [("go", "app/use.py")]
    assert tools.find_callers(engine, repo, "A.run")["results"] == by_owner
    # `A::run` is the same method (B's run is told apart: its callers are its own).
    assert tools.find_callers(engine, repo, "A::run")["results"] == by_owner
    impact = tools.impact_analysis(engine, repo, "A.run")
    assert impact["direct_dependents"]["results"] == [
        {"name": "go", "type": "Function", "file": "app/use.py", "owner": ""}
    ]
    related = tools.find_related_files(engine, repo, "run", owner="B")
    assert related["related_components"]["results"] == ["helper"]
    assert "ambiguous_targets" not in related
    with pytest.raises(ToolError, match="no component named 'run' with owner 'C'"):
        tools.find_callers(engine, repo, "run", owner="C")
    with pytest.raises(ToolError, match="no component named 'C.run'"):
        tools.impact_analysis(engine, repo, "C.run")


def test_impact_dependents_name_their_file_and_owner(engine, repo):
    rows = tools.impact_analysis(engine, repo, "helper")["direct_dependents"]["results"]
    assert sorted((r["name"], r["file"], r["owner"]) for r in rows) == [
        ("run", "app/core.py", "A"), ("run", "app/core.py", "B"),
    ]
