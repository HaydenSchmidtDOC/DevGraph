"""The Overview panel's "Top god nodes" degree fallback, run as index.html writes
it against a live Neo4j: it ranks as the god_nodes tool does, leaving out git
history and mentions (Commit, PullRequest, Issue and Document nodes, and their
MODIFIES, RESOLVES, REFERENCES and MENTIONS edges). Fixture data is fictional."""

import re
import uuid
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

_HTML = Path(__file__).parents[2] / "devgraph" / "dashboard" / "static" / "index.html"
REPO = f"zz-dash-god-{uuid.uuid4().hex[:8]}"


def _god_query(repo_id: str) -> str:
    (template,) = re.findall(r"const godJson = await runCypher\(`(.*?)`\);", _HTML.read_text(encoding="utf-8"))
    return template.replace("${where}", f'WHERE n.repo_id = "{repo_id}" ')


@pytest.fixture
def engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.upsert_repository(REPO, REPO, "/srv/example")
    engine.upsert_node("Module", REPO, "core.py")
    engine.upsert_node("Class", REPO, "Engine")
    engine.upsert_relationship("Module", "core.py", "CONTAINS", "Class", "Engine", REPO)
    for i in reversed(range(3)):  # created out of name order: ties must still rank by name
        engine.upsert_node("Function", REPO, f"use{i}")
        engine.upsert_relationship("Function", f"use{i}", "CALLS", "Class", "Engine", REPO)
    for k in range(8):
        engine.upsert_node("Commit", REPO, f"sha{k}", {"message": "sweep", "authored_date": "2026-01-01T00:00:00"})
        engine.upsert_relationship("Commit", f"sha{k}", "MODIFIES", "Module", "core.py", REPO)
    engine.upsert_node("Document", REPO, "README.md")
    engine.upsert_relationship("Document", "README.md", "MENTIONS", "Class", "Engine", REPO)
    engine.upsert_node("Issue", REPO, "42")
    engine.upsert_node("PullRequest", REPO, "7")
    engine.upsert_relationship("Commit", "sha0", "REFERENCES", "Issue", "42", REPO)
    engine.upsert_relationship("PullRequest", "7", "RESOLVES", "Issue", "42", REPO)
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


def test_the_dashboard_degree_list_leaves_history_and_mentions_out(engine):
    rows = engine.run_cypher(_god_query(REPO), {})
    assert {row["label"] for row in rows}.isdisjoint({"Repository", "Commit", "PullRequest", "Issue", "Document"})
    degrees = {row["name"]: row["degree"] for row in rows}
    assert degrees["Engine"] == 4  # CONTAINS and three CALLS; the MENTIONS edge is left out
    assert degrees["core.py"] == 1  # CONTAINS; the eight MODIFIES edges are left out
    ranked = tools.god_nodes(engine, REPO, max_results=6)["results"]
    assert [(row["name"], row["degree"]) for row in rows] == [(r["name"], r["degree"]) for r in ranked]
