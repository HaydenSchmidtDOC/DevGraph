"""A changed docs path or mentions setting takes effect (audit 10 item 2):
a full scan removes the Documents, MENTIONS edges and notes the new
settings don't make, and the agent rescans a repository whose settings
differ from the ones its last full scan used."""

import textwrap
import uuid
from unittest.mock import patch

import pytest

from devgraph.agent.schema_rescan import SchemaRescanScheduler
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, scan_options_pending
from devgraph.indexer.providers import docs_cache
from devgraph.mcp import tools
from devgraph.registry.store import RepoRegistry
from tests.indexer.docs_live_helpers import _uncached
from tests.watcher.live_helpers import graph_snapshot, snapshot_diff

_TOKEN = uuid.uuid4().hex[:8]
FILES = {
    "orders.py": "def place_order():\n    return 1\n",
    "README.md": "Call `place_order()` to order.\n",
    "docs/r.md": "---\ntype: requirement\nid: REQ-9\nlinks: [orders.py]\n---\n# Req\n",
    "notes/r2.md": "---\ntype: requirement\nid: REQ-10\nlinks: [orders.py]\n---\n# Req\n",
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


@pytest.fixture(autouse=True)
def uncached():
    with (
        patch.object(docs_cache, "read", _uncached),
        patch.object(docs_cache, "read_fresh", _uncached),
        patch.object(docs_cache, "forget", lambda root: None),
    ):
        yield


@pytest.fixture
def repo(engine, tmp_path):
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    (root / ".git").mkdir()
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    repo_id = registry.add_repo(root, repo_id=f"zz-scanopts-{_TOKEN}").repo_id
    engine.delete_repository(repo_id)
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    try:
        yield engine, registry, repo_id, root
    finally:
        engine.delete_repository(repo_id)
        engine.delete_repository(f"{repo_id}_fresh")
        registry.close()


def _assert_fresh(engine, repo_id, root, **options):
    fresh = f"{repo_id}_fresh"
    engine.delete_repository(fresh)
    try:
        engine.upsert_repository(fresh, fresh, str(root))
        full_scan(engine, fresh, root, **options)
        expected, actual = graph_snapshot(engine, fresh), graph_snapshot(engine, repo_id)
        assert actual == expected, snapshot_diff(expected, actual)
    finally:
        engine.delete_repository(fresh)


def _documents(engine, repo_id):
    return engine.run_cypher(
        "MATCH (d:Document {repo_id: $r}) OPTIONAL MATCH (d)-[m:MENTIONS]->() RETURN d.name AS d, count(m) AS m",
        {"r": repo_id},
    )


def test_a_rescan_with_mentions_off_removes_documents_and_mentions(repo):
    engine, _, repo_id, root = repo
    full_scan(engine, repo_id, root, docs_path="docs", mentions_enabled=True)
    assert _documents(engine, repo_id)
    full_scan(engine, repo_id, root, docs_path="notes", mentions_enabled=False)
    assert _documents(engine, repo_id) == []
    _assert_fresh(engine, repo_id, root, docs_path="notes", mentions_enabled=False)


def test_a_rescan_with_the_docs_path_cleared_removes_its_notes(repo):
    engine, _, repo_id, root = repo
    full_scan(engine, repo_id, root, docs_path="docs")
    full_scan(engine, repo_id, root, docs_path="")
    assert engine.list_docs_note_files(repo_id) == set()
    _assert_fresh(engine, repo_id, root, docs_path="")


def _scheduler(engine, registry):
    return SchemaRescanScheduler(engine, registry, interval_s=0.01)


def test_the_agent_applies_mentions_enabled(repo):
    engine, registry, repo_id, root = repo
    full_scan(engine, repo_id, root)
    registry.mark_indexed(repo_id)
    assert not scan_options_pending(engine, repo_id, None, False)

    registry.set_mentions_enabled(repo_id, True)
    assert scan_options_pending(engine, repo_id, None, True)
    notice = tools.find_mentions(engine, repo_id, "place_order", registry=registry)["notice"]
    assert "devgraph rescan" in notice
    assert _scheduler(engine, registry).run_once() == [repo_id]
    assert not scan_options_pending(engine, repo_id, None, True)
    assert "notice" not in tools.find_mentions(engine, repo_id, "place_order", registry=registry)
    _assert_fresh(engine, repo_id, root, mentions_enabled=True)

    registry.set_mentions_enabled(repo_id, False)
    assert _scheduler(engine, registry).run_once() == [repo_id]
    assert _documents(engine, repo_id) == []
    _assert_fresh(engine, repo_id, root, mentions_enabled=False)


def test_the_agent_applies_a_docs_path_change(repo):
    engine, registry, repo_id, root = repo
    full_scan(engine, repo_id, root, docs_path="docs")
    registry.set_docs_path(repo_id, "docs")
    registry.mark_indexed(repo_id)
    assert _scheduler(engine, registry).run_once() == []

    registry.set_docs_path(repo_id, "notes")
    result = tools.find_requirements_for(engine, repo_id, "orders.py", registry=registry)
    assert "devgraph rescan" in result["notice"]
    assert _scheduler(engine, registry).run_once() == [repo_id]
    assert engine.list_docs_note_files(repo_id) == {"notes/r2.md"}
    assert "notice" not in tools.trace_design_rationale(engine, repo_id, "orders.py", registry=registry)
    _assert_fresh(engine, repo_id, root, docs_path="notes")
