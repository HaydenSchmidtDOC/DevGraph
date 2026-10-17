"""A build manifest added or removed keeps the graph equal to a fresh scan
(audit 6, item 4): `tools/dist/` is source until `tools/pyproject.toml`
makes it build output, and source again once the manifest goes. Driven
through RepoSync, as the agent's watcher delivers the batch."""

import subprocess

import pytest

from devgraph.agent.sync import RepoSync
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.registry.store import RepoRegistry
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)


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
def repo_id(engine):
    sweep_stale_live_repos(engine, "zz-buildout")
    repo = live_repo_id("zz-buildout")
    yield repo
    engine.delete_repository(repo)


def files(engine, repo_id):
    rows = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS name", {"r": repo_id})
    return {row["name"] for row in rows}


def assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    assert actual == expected, snapshot_diff(expected, actual)


@pytest.mark.parametrize("manifest", ["tools/pyproject.toml", "tools/Tools.csproj"])
def test_a_manifest_added_then_removed_keeps_live_equal_to_fresh(engine, repo_id, tmp_path, manifest):
    root = tmp_path / "repo"
    (root / "tools/dist").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    (root / "tools/dist/gen.py").write_text("def generate():\n    return 1\n")
    (root / "app.py").write_text("def main():\n    return 2\n")
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    try:
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        assert "tools/dist/gen.py" in files(engine, repo_id)
        sync = RepoSync(engine, registry, lambda event: None, lambda *args: None)

        (root / manifest).write_text("<Project />\n" if manifest.endswith("proj") else "[project]\nname = 'tools'\n")
        sync.on_changes(repo_id, {root / manifest}, set())
        assert "tools/dist/gen.py" not in files(engine, repo_id)
        assert_fresh(engine, repo_id, root)

        (root / manifest).unlink()
        sync.on_changes(repo_id, set(), {root / manifest})
        assert "tools/dist/gen.py" in files(engine, repo_id)
        assert_fresh(engine, repo_id, root)
    finally:
        registry.close()
