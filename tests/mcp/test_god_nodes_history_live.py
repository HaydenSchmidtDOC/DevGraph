"""god_nodes against a live Neo4j once git history is indexed (audit 10 item 7):
it ranks the code and architecture, so syncing the history (Commit nodes and
their MODIFIES edges) changes neither which nodes it lists nor their degrees,
and no Commit is ever listed. Fixture code is fictional."""

import subprocess
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.git_history.extractor import sync_git_history
from devgraph.mcp import tools
from devgraph.registry.store import RepoRegistry

_CORE = "class Engine:\n    def run(self):\n        return 1\n"
_USER = "from core import Engine\ndef use{i}():\n    return Engine().run()\n"


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=Test Author", "-c", "user.email=test@example.com", *args],
        cwd=root, check=True, capture_output=True,
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
def history_repo(engine, tmp_path):
    """Thirteen files: a sweep commit touching twelve, then six edits to one."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "core.py").write_text(_CORE)
    users = {f"m{i}.py": _USER.format(i=i) for i in range(12)}
    for rel, text in users.items():
        (root / rel).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    for rel, text in users.items():
        (root / rel).write_text(text + "\n")
    _git(root, "commit", "-qam", "reformat everything")
    for k in range(6):
        (root / "m0.py").write_text(users["m0.py"] + "\n" * (k + 2))
        _git(root, "commit", "-qam", f"tweak {k}")
    registry = RepoRegistry(tmp_path / "registry.db")
    repo_id = registry.add_repo(root, repo_id=f"zz-god-history-{uuid.uuid4().hex[:8]}").repo_id
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    try:
        yield registry, repo_id
    finally:
        engine.delete_repository(repo_id)
        registry.close()


def test_god_nodes_ranking_is_unchanged_by_git_history(engine, history_repo):
    registry, repo_id = history_repo
    before = tools.god_nodes(engine, repo_id, max_results=50)
    assert sync_git_history(engine, registry, repo_id)
    assert engine.run_cypher("MATCH (c:Commit {repo_id: $r}) RETURN count(c) AS n", {"r": repo_id})[0]["n"] == 8
    after = tools.god_nodes(engine, repo_id, max_results=50)
    assert after == before
    assert not any("Commit" in row["labels"] for row in after["results"])
