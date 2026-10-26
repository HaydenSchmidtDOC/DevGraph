"""Live round trip of graph insights through Neo4j. Skips without Neo4j."""

import json
import uuid

import pytest

from devgraph.analytics.insights import (
    COMMUNITY_RELATIONSHIPS,
    community_members,
    read_insights,
    refresh_insights,
    top_nodes,
)
from devgraph.graph.engine import GraphEngine

# Unique per run, so sessions sharing the Neo4j never seed or delete each
# other's repositories; tests/conftest.py sweeps any a teardown leaves.
_TOKEN = uuid.uuid4().hex[:8]

REPO = f"_smoketest_graph_insights_{_TOKEN}"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def build_two_subsystems(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    engine.run_cypher(
        "UNWIND $nodes AS row "
        "CREATE (n:Function {repo_id: $repo, name: row.name, file: row.file})",
        {
            "repo": REPO,
            "nodes": [
                {"name": "a", "file": "auth/a.py"}, {"name": "b", "file": "auth/b.py"}, {"name": "c", "file": "auth/c.py"},
                {"name": "d", "file": "billing/d.py"}, {"name": "e", "file": "billing/e.py"}, {"name": "f", "file": "billing/f.py"},
            ],
        },
    )
    engine.run_cypher(
        "UNWIND $pairs AS pair "
        "MATCH (s:Function {repo_id: $repo, name: pair[0]}), (t:Function {repo_id: $repo, name: pair[1]}) "
        "CREATE (s)-[:CALLS]->(t)",
        {"repo": REPO, "pairs": [["a", "b"], ["b", "c"], ["a", "c"], ["d", "e"], ["e", "f"], ["d", "f"], ["c", "d"]]},
    )
    # History edges and the Repository root must not take part.
    engine.run_cypher(
        "MATCH (r:Repository {repo_id: $repo}), (a:Function {repo_id: $repo, name: 'a'}) "
        "CREATE (c:Commit {repo_id: $repo, name: 'abc123'})-[:MODIFIES]->(a), (r)-[:CONTAINS]->(a)",
        {"repo": REPO},
    )


def test_load_returns_only_relevant_edges_and_their_nodes(engine):
    build_two_subsystems(engine)
    nodes, edges = engine.load_insight_graph(REPO, COMMUNITY_RELATIONSHIPS)
    assert sorted(n["name"] for n in nodes) == ["a", "b", "c", "d", "e", "f"]
    assert len(edges) == 7 and {e["type"] for e in edges} == {"CALLS"}


def test_load_leaves_out_python_calls_matched_by_name_or_package(engine):
    build_two_subsystems(engine)
    engine.run_cypher(
        "UNWIND $pairs AS pair "
        "MATCH (s:Function {repo_id: $repo, name: pair[0]}), (t:Function {repo_id: $repo, name: pair[1]}) "
        "MATCH (s)-[r:CALLS]->(t) SET r.confidence = pair[2]",
        {"repo": REPO, "pairs": [["a", "b", "resolved"], ["b", "c", "name"], ["a", "c", "package"]]},
    )
    _nodes, edges = engine.load_insight_graph(REPO, COMMUNITY_RELATIONSHIPS)
    # The resolved edge and the four without a confidence stay.
    assert len(edges) == 5


def test_load_leaves_out_imports_at_package_confidence(engine):
    """A package-confidence IMPORTS edge (a C# class reached in any C#
    file, a Java class file found by suffix in another module) can fan one
    import out to many same-named targets, as a package CALLS edge can."""
    build_two_subsystems(engine)
    engine.run_cypher(
        "UNWIND $rows AS row "
        "MERGE (m:Module {repo_id: $repo, name: row[0]}) MERGE (t:Module {repo_id: $repo, name: row[1]}) "
        "CREATE (m)-[r:IMPORTS]->(t) SET r.confidence = row[2]",
        {"repo": REPO, "rows": [["m.py", "n.py", "resolved"], ["m.py", "o.py", "package"], ["m.py", "p.py", None]]},
    )
    _nodes, edges = engine.load_insight_graph(REPO, COMMUNITY_RELATIONSHIPS)
    assert sum(e["type"] == "IMPORTS" for e in edges) == 2


def test_refresh_writes_node_and_repository_properties(engine):
    build_two_subsystems(engine)
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 2 and summary["node_count"] == 6
    assert [c["label"] for c in summary["communities"]] == ["auth", "billing"]

    stored = read_insights(engine, REPO)
    assert stored["community_count"] == 2
    assert stored["communities"] == summary["communities"]
    assert stored["computed_at"] == summary["computed_at"]

    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $repo}) RETURN n.name AS name, n.insight_community AS c, "
        "n.insight_pagerank AS pr, n.insight_betweenness AS bt ORDER BY name",
        {"repo": REPO},
    )
    assert [r["c"] for r in rows] == [0, 0, 0, 1, 1, 1]
    assert all(isinstance(r["pr"], float) and isinstance(r["bt"], float) for r in rows)
    commit = engine.run_cypher("MATCH (c:Commit {repo_id: $repo}) RETURN c.insight_community AS c", {"repo": REPO})
    assert commit == [{"c": None}]


def test_top_nodes_and_community_members(engine):
    build_two_subsystems(engine)
    refresh_insights(engine, REPO)
    bridges = top_nodes(engine, REPO, "betweenness", 2)
    assert {b["name"] for b in bridges} == {"c", "d"}
    assert set(bridges[0]) == {"name", "labels", "file", "score", "community"}
    members = community_members(engine, REPO, [0], 2)
    assert len(members[0]) == 2
    assert members[0][0]["pagerank"] >= members[0][1]["pagerank"]


def test_a_rerun_clears_properties_of_nodes_that_lost_their_edges(engine):
    build_two_subsystems(engine)
    refresh_insights(engine, REPO)
    engine.run_cypher("MATCH (:Function {repo_id: $repo})-[r:CALLS]->() DELETE r", {"repo": REPO})
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 0 and summary["node_count"] == 0
    leftover = engine.run_cypher(
        "MATCH (n {repo_id: $repo}) WHERE n.insight_community IS NOT NULL OR n.insight_pagerank IS NOT NULL "
        "OR n.insight_betweenness IS NOT NULL RETURN count(n) AS n",
        {"repo": REPO},
    )
    assert leftover == [{"n": 0}]


def test_read_insights_is_none_before_the_first_run(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    assert read_insights(engine, REPO) is None


def test_communities_json_is_capped_at_fifty(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    pairs = [[f"p{i}", f"q{i}"] for i in range(60)]
    engine.run_cypher(
        "UNWIND $pairs AS pair "
        "CREATE (:Function {repo_id: $repo, name: pair[0], file: pair[0] + '/x.py'})"
        "-[:CALLS]->(:Function {repo_id: $repo, name: pair[1], file: pair[0] + '/y.py'})",
        {"repo": REPO, "pairs": pairs},
    )
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 60
    raw = engine.read_insights_summary(REPO)
    assert len(json.loads(raw["communities"])) == 50


def test_a_refresh_of_a_deleted_repository_does_not_recreate_it(engine):
    """A refresh that lands after `devgraph remove` deleted the graph only
    updates nodes that still exist: it never brings back a Repository node."""
    import uuid

    repo = f"_smoketest_insights_removed_{uuid.uuid4().hex[:8]}"
    try:
        refresh_insights(engine, repo)
        assert engine.run_cypher("MATCH (n {repo_id: $r}) RETURN count(n) AS c", {"r": repo}) == [{"c": 0}]
    finally:
        engine.delete_repository(repo)


def test_an_unchanged_graph_is_checked_without_recomputing(engine):
    """The fingerprint a refresh returns is the graph as it stored it, so the
    next check of the same graph is `unchanged` (nothing written), a
    re-created node that lost its insight properties is not, and neither is an
    edge change."""
    build_two_subsystems(engine)
    first = refresh_insights(engine, REPO)
    assert read_insights(engine, REPO)["fingerprint"] == first["fingerprint"]
    before = read_insights(engine, REPO)["computed_at"]
    check = refresh_insights(engine, REPO, unless_fingerprint=first["fingerprint"])
    assert check["unchanged"] is True and check["fingerprint"] == first["fingerprint"]
    assert read_insights(engine, REPO)["computed_at"] == before

    engine.run_cypher(
        "MATCH (n:Function {repo_id: $repo, name: 'b'}) "
        "REMOVE n.insight_community, n.insight_pagerank, n.insight_betweenness",
        {"repo": REPO},
    )
    second = refresh_insights(engine, REPO, unless_fingerprint=first["fingerprint"])
    assert "unchanged" not in second and second["fingerprint"] == first["fingerprint"]

    engine.run_cypher(
        "MATCH (s:Function {repo_id: $repo, name: 'f'}), (t:Function {repo_id: $repo, name: 'a'}) "
        "CREATE (s)-[:CALLS]->(t)",
        {"repo": REPO},
    )
    third = refresh_insights(engine, REPO, unless_fingerprint=first["fingerprint"])
    assert "unchanged" not in third and third["fingerprint"] != first["fingerprint"]
