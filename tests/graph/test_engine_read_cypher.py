"""run_read_cypher: read access mode, timeout, row cap. Live Neo4j."""

import pytest
from neo4j.exceptions import Neo4jError

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_read_cypher"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def seed(engine, n):
    engine.run_cypher("UNWIND range(1, $n) AS i CREATE (:ZzItem {repo_id: $r, name: 'i' + i, i: i})", {"n": n, "r": REPO})


def test_returns_rows_under_the_cap(engine):
    seed(engine, 3)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i ORDER BY i", {"repo_id": REPO}, timeout_s=10, max_rows=5
    )
    assert rows == [{"i": 1}, {"i": 2}, {"i": 3}] and truncated is False


def test_caps_rows_and_flags_truncation(engine):
    seed(engine, 10)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i ORDER BY i", {"repo_id": REPO}, timeout_s=10, max_rows=4
    )
    assert [r["i"] for r in rows] == [1, 2, 3, 4] and truncated is True


def test_exactly_the_cap_is_not_truncated(engine):
    seed(engine, 4)
    rows, truncated = engine.run_read_cypher(
        "MATCH (n:ZzItem {repo_id: $repo_id}) RETURN n.i AS i", {"repo_id": REPO}, timeout_s=10, max_rows=4
    )
    assert len(rows) == 4 and truncated is False


def test_writes_are_refused_in_read_mode(engine):
    with pytest.raises(Neo4jError):
        engine.run_read_cypher("CREATE (:ZzItem {repo_id: $repo_id, name: 'x'})", {"repo_id": REPO}, timeout_s=10, max_rows=5)
    assert engine.run_cypher("MATCH (n:ZzItem {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 0}]


def test_the_timeout_is_enforced(engine):
    slow = "UNWIND range(1, 100000000) AS i WITH i WHERE i % 7 = 3 RETURN count(i) AS c"
    with pytest.raises(Neo4jError):
        engine.run_read_cypher(slow + " // $repo_id", {"repo_id": REPO}, timeout_s=1, max_rows=1)


def test_temporal_and_spatial_values_survive_the_tool_function_as_json(engine):
    import json

    from devgraph.config.project_tools import CypherTool
    from devgraph.mcp.tool_plane import make_tool_function

    query = (
        "MATCH (n:ZzItem {repo_id: $repo_id}) "
        "RETURN date('2026-01-02') AS d, datetime('2026-01-02T03:04:05Z') AS dt, "
        "time('03:04:05Z') AS t, duration('P1D') AS du, point({x: 1, y: 2}) AS p, "
        "point({x: 1, y: 2, z: 3}) AS p3, [date('2026-01-02')] AS nested"
    )
    seed(engine, 1)
    rows, _ = engine.run_read_cypher(query, {"repo_id": REPO}, timeout_s=10, max_rows=5)
    assert rows  # the driver hands back neo4j.time / neo4j.spatial objects here
    tool = CypherTool(name="temporal", description="d", cypher=query)
    result = make_tool_function(tool, engine, REPO)()
    json.dumps(result)  # must not raise
    row = result["results"][0]
    assert row["d"] == "2026-01-02" and row["dt"].startswith("2026-01-02T03:04:05")
    assert row["du"] == "P1D" and row["nested"] == ["2026-01-02"]
    assert row["p"]["x"] == 1.0 and row["p"]["y"] == 2.0 and "srid" in row["p"] and "z" not in row["p"]
    assert row["p3"]["z"] == 3.0
