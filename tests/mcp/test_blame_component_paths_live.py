"""blame_component against a live Neo4j takes a file path as find_related_prs and
issue_history_for do (audit 10 item 8c): `./src/a.py` and `src\\a.py` name the
stored `src/a.py`. Fixture data is fictional."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp.tools import blame_component

REPO = f"zz-blame-paths-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def seeded():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    engine.upsert_repository(REPO, REPO, "/srv/example")
    engine.upsert_node("Module", REPO, "src/a.py")
    engine.upsert_node("Commit", REPO, "sha1", {"message": "Add a", "author": "Zed", "authored_date": "2026-01-01T00:00:00"})
    engine.upsert_relationship("Commit", "sha1", "MODIFIES", "Module", "src/a.py", REPO)
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


@pytest.mark.parametrize("path", ["src/a.py", "./src/a.py", "src\\a.py"])
def test_blame_component_normalises_the_path(seeded, path):
    result = blame_component(seeded, REPO, path)
    assert [row["sha"] for row in result["results"]] == ["sha1"]
