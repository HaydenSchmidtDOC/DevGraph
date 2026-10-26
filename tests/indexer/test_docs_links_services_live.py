"""A docs note's `links:` names a Module path or a Service name, as the docs
extractor documents (audit 10 item 8b), live and incrementally."""

import uuid
from unittest.mock import patch

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.indexer.docs.extractor import DocsExtractor
from devgraph.indexer.providers import docs_cache
from devgraph.mcp import tools
from tests.indexer.docs_live_helpers import _uncached
from tests.watcher.live_helpers import graph_snapshot, snapshot_diff

NOTE = "---\ntype: requirement\nid: REQ-7\nlinks: [orders.py, orders-api]\n---\n# Orders are durable\n"
COMPOSE = "services:\n  orders-api:\n    image: python:3.12\n"


def test_a_link_names_a_module_or_a_service():
    rels = DocsExtractor("r").extract_from_source(NOTE, "docs/req.md").relationships
    assert {(r.source_label, r.source_name, r.relationship_type) for r in rels} == {
        ("Module", "orders.py", "SATISFIES"), ("Module", "orders-api", "SATISFIES"),
        ("Service", "orders.py", "SATISFIES"), ("Service", "orders-api", "SATISFIES"),
    }


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
def repo(engine, tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "orders.py").write_text("def place_order():\n    return 1\n")
    (root / "docs" / "req.md").write_text(NOTE)
    repo_id = f"zz-notelinks-{uuid.uuid4().hex[:8]}"
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    with (
        patch.object(docs_cache, "read", _uncached),
        patch.object(docs_cache, "read_fresh", _uncached),
        patch.object(docs_cache, "forget", lambda r: None),
    ):
        try:
            yield engine, repo_id, root
        finally:
            engine.delete_repository(repo_id)
            engine.delete_repository(f"{repo_id}_fresh")


def _satisfied_by(engine, repo_id):
    return sorted(
        (row["l"], row["n"]) for row in engine.run_cypher(
            "MATCH (n {repo_id: $r})-[:SATISFIES]->(:Requirement {name: 'REQ-7'}) RETURN labels(n)[0] AS l, n.name AS n",
            {"r": repo_id},
        )
    )


def _assert_fresh(engine, repo_id, root):
    fresh = f"{repo_id}_fresh"
    engine.delete_repository(fresh)
    engine.upsert_repository(fresh, fresh, str(root))
    full_scan(engine, fresh, root, docs_path="docs")
    expected, actual = graph_snapshot(engine, fresh), graph_snapshot(engine, repo_id)
    engine.delete_repository(fresh)
    assert actual == expected, snapshot_diff(expected, actual)


def test_a_full_scan_links_the_named_service(repo):
    engine, repo_id, root = repo
    (root / "docker-compose.yml").write_text(COMPOSE)
    full_scan(engine, repo_id, root, docs_path="docs")
    assert _satisfied_by(engine, repo_id) == [("Module", "orders.py"), ("Service", "orders-api")]
    found = tools.find_requirements_for(engine, repo_id, "orders-api")["results"]
    assert [r["name"] for r in found] == ["REQ-7"]


def test_a_service_added_later_is_linked_and_its_removal_unlinks(repo):
    engine, repo_id, root = repo
    full_scan(engine, repo_id, root, docs_path="docs")
    assert _satisfied_by(engine, repo_id) == [("Module", "orders.py")]

    compose = root / "docker-compose.yml"
    compose.write_text(COMPOSE)
    index_paths(engine, repo_id, root, {compose}, docs_path="docs")
    assert _satisfied_by(engine, repo_id) == [("Module", "orders.py"), ("Service", "orders-api")]
    _assert_fresh(engine, repo_id, root)

    compose.write_text(COMPOSE + "    environment: [A=1]\n")
    index_paths(engine, repo_id, root, {compose}, docs_path="docs")
    _assert_fresh(engine, repo_id, root)

    compose.unlink()
    remove_paths(engine, repo_id, root, {compose}, docs_path="docs")
    assert _satisfied_by(engine, repo_id) == [("Module", "orders.py")]
    _assert_fresh(engine, repo_id, root)
