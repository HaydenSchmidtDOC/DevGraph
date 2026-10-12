"""The name-lookup MCP tools start from a label, so Neo4j plans an index seek (or
one label's scan), never a scan of every node or every edge in the database."""

import uuid

import pytest
from neo4j import GraphDatabase

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

URI, AUTH = "bolt://127.0.0.1:7687", ("neo4j", "devgraph-local-dev")
REPO = f"zz-tool-plans-{uuid.uuid4().hex[:8]}"
WHOLE_DATABASE = ("AllNodesScan", "RelationshipTypeScan")


class _Recording:
    """The real engine, keeping every read query it is given."""

    def __init__(self, engine):
        self.engine = engine
        self.reads = []

    def run_read_cypher(self, query, parameters, **kwargs):
        self.reads.append((query, parameters))
        return self.engine.run_read_cypher(query, parameters, **kwargs)


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(URI, *AUTH)
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.run_cypher(
        "CREATE (:Function {repo_id: $r, name: 'save', file: 'a.py'})<-[:CALLS]-"
        "(:Function {repo_id: $r, name: 'caller', file: 'b.py', last_modified_at: '2026-01-01'})"
        "-[:SATISFIES]->(:Requirement {repo_id: $r, name: 'req-1'})",
        {"r": REPO},
    )
    test_engine.run_cypher(
        "MATCH (q:Requirement {repo_id: $r}) CREATE (:File {repo_id: $r, name: 'docs/spec.md'})-[:SATISFIES]->(q)",
        {"r": REPO},
    )
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def _operators(plan):
    yield plan["operatorType"]
    for child in plan.get("children", []):
        yield from _operators(child)


CALLS = {
    "find_callers": lambda e: tools.find_callers(e, REPO, "save"),
    "impact_analysis": lambda e: tools.impact_analysis(e, REPO, "save"),
    "find_related_files": lambda e: tools.find_related_files(e, REPO, "save"),
    "trace_design_rationale": lambda e: tools.trace_design_rationale(e, REPO, "caller"),
    "find_requirements_for": lambda e: tools.find_requirements_for(e, REPO, "caller"),
    "list_recent_changes": lambda e: tools.list_recent_changes(e, REPO, 5),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_no_query_scans_the_whole_database(engine, name):
    recording = _Recording(engine)
    CALLS[name](recording)
    assert recording.reads
    with GraphDatabase.driver(URI, auth=AUTH) as driver, driver.session() as session:
        for query, parameters in recording.reads:
            plan = session.run("EXPLAIN " + query, parameters).consume().plan
            scans = [op for op in _operators(plan) if any(kind in op for kind in WHOLE_DATABASE)]
            assert scans == [], f"{name} plans {scans} for:\n{query}"


def test_a_declared_label_is_still_looked_up(engine):
    """A provider's node (here a File) is found through the repository's declared labels."""
    result = tools.find_requirements_for(engine, REPO, "docs/spec.md", extra_labels=("File",))
    assert [r["name"] for r in result["results"]] == ["req-1"]
