"""blame_component, find_requirements_for and explain_architecture return at most
max_results rows in a {count, results, truncated} envelope, not every row."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-capped-lists-{uuid.uuid4().hex[:8]}"
MANY = 20

_SEED = """
CREATE (m:Module {repo_id: $r, name: 'README.md'})
CREATE (s:Service {repo_id: $r, name: 'api', file: 'compose.yml'})
CREATE (e:Endpoint {repo_id: $r, name: 'GET /health'})-[:CALLS]->(s)
WITH m, s
UNWIND range(1, $many) AS i
CREATE (c:Commit {repo_id: $r, name: 'sha' + i, message: 'change ' + i,
                  authored_date: '2026-01-' + right('0' + i, 2) + 'T00:00:00'})-[:MODIFIES]->(m)
CREATE (m)-[:SATISFIES]->(:Requirement {repo_id: $r, name: 'req-' + right('0' + i, 2), title: 'Req ' + i, body: 'x'})
CREATE (:Service {repo_id: $r, name: 'svc-' + right('0' + i, 2), file: 'compose.yml'})
"""


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.run_cypher(_SEED, {"r": REPO, "many": MANY})
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def test_blame_component_returns_the_newest_max_results_commits(engine):
    result = tools.blame_component(engine, REPO, "README.md")
    assert result["count"] == MANY and result["truncated"] is True
    assert [r["sha"] for r in result["results"]] == [f"sha{i}" for i in range(MANY, MANY - 15, -1)]
    assert len(tools.blame_component(engine, REPO, "README.md", max_results=MANY)["results"]) == MANY


def test_find_requirements_for_is_capped(engine):
    result = tools.find_requirements_for(engine, REPO, "README.md")
    assert result["count"] == MANY and len(result["results"]) == 15 and result["truncated"] is True
    assert result["results"][0] == {"name": "req-01", "title": "Req 1", "body": "x"}


def test_explain_architecture_caps_its_lists_and_skips_services_no_endpoint_calls(engine):
    result = tools.explain_architecture(engine, REPO, max_results=5)
    services = result["services_and_datastores"]
    assert services["count"] == MANY + 1 and len(services["results"]) == 5 and services["truncated"] is True
    assert services["results"][0] == {"service": "api", "uses": []}
    assert result["endpoints"] == {"count": 1, "results": [{"endpoints": ["GET /health"], "calls": "api"}], "truncated": False}
