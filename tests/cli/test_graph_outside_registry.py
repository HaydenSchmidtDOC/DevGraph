"""Graph data the registry doesn't own (audit 10, item 4).

- A repo id reissued after `remove --keep-graph` starts empty: `add` and the
  dashboard's register delete what an earlier repository left under it.
- `prune` lists the ids it would delete, with node counts and a warning that
  another registry on the same Neo4j may own them, and deletes only with a
  confirmation at a terminal or `--yes`.
- `stats` with no repo id counts only the registered repositories.

The prune tests use a fake engine: a live prune would delete every other
session's repositories in the shared test Neo4j.
"""

import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from devgraph import config as config_module
from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_TOKEN = uuid.uuid4().hex[:8]


def _settings(db_path: Path) -> MagicMock:
    settings = MagicMock()
    settings.registry_db_path = db_path
    settings.neo4j_uri = "bolt://127.0.0.1:7687"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "devgraph-local-dev"
    settings.enable_run_cypher = False
    return settings


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    settings = _settings(tmp_path / "registry.db")
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def live_engine():
    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    ids: list[str] = []
    yield engine, ids
    for repo_id in ids:
        engine.delete_repository(repo_id)
    engine.close()


def _git_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    return repo


def _leave_old_commit(engine: GraphEngine, repo_id: str) -> None:
    """What an earlier repository registered under `repo_id` and removed with
    `--keep-graph` leaves behind."""
    engine.upsert_repository(repo_id, repo_id, "/old/place")
    engine.upsert_node("Commit", repo_id, "0ld5ecre7", {"message": "old secret project commit"})


def _commits(engine: GraphEngine, repo_id: str) -> list[str]:
    rows = engine.run_cypher("MATCH (c:Commit {repo_id: $r}) RETURN c.name AS name", {"r": repo_id})
    return [row["name"] for row in rows]


def test_add_deletes_what_an_earlier_repository_left_under_a_reissued_id(runner, settings, live_engine, tmp_path):
    engine, ids = live_engine
    repo_id = f"zz-a10c-reuse-{_TOKEN}"
    ids.append(repo_id)
    _leave_old_commit(engine, repo_id)

    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, repo_id))])

    assert result.exit_code == 0, result.stdout
    assert "earlier repository" in result.stdout
    assert _commits(engine, repo_id) == []
    rows = engine.run_cypher("MATCH (f:Function {repo_id: $r}) RETURN f.name AS name", {"r": repo_id})
    assert [row["name"] for row in rows] == ["handler"]


def test_dashboard_register_deletes_what_an_earlier_repository_left(live_engine, tmp_path):
    engine, ids = live_engine
    repo_id = f"zz-a10c-dash-reuse-{_TOKEN}"
    ids.append(repo_id)
    _leave_old_commit(engine, repo_id)
    registry = RepoRegistry(tmp_path / "registry.db")
    try:
        client = TestClient(build_app(engine, registry, EventBroadcaster()), base_url="http://127.0.0.1")
        res = client.post("/api/repos", json={"path": str(_git_repo(tmp_path, repo_id))})
        assert res.status_code == 201, res.text
        assert res.json()["repo_id"] == repo_id
    finally:
        registry.close()
    assert _commits(engine, repo_id) == []


def test_add_of_a_fresh_id_says_nothing_about_leftovers(runner, settings, live_engine, tmp_path):
    engine, ids = live_engine
    repo_id = f"zz-a10c-fresh-{_TOKEN}"
    ids.append(repo_id)
    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, repo_id))])
    assert result.exit_code == 0, result.stdout
    assert "earlier repository" not in result.stdout


def test_stats_without_a_repo_counts_only_registered_repositories(runner, settings, live_engine, tmp_path):
    engine, ids = live_engine
    registered = f"zz-a10c-stats-{_TOKEN}"
    stray = f"zz-a10c-stray-{_TOKEN}"
    ids.extend([registered, stray])
    engine.upsert_repository(stray, stray, "/elsewhere")
    for i in range(3):
        engine.upsert_node("Function", stray, f"stray_{i}")
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(_git_repo(tmp_path, registered))
    finally:
        registry.close()
    engine.upsert_repository(registered, registered, str(tmp_path / registered))
    engine.upsert_node("Module", registered, "app.py")
    engine.upsert_node("Function", registered, "handler")
    engine.upsert_relationship("Module", "app.py", "CONTAINS", "Function", "handler", registered)

    result = runner.invoke(app, ["stats", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["nodes_by_label"] == {"Repository": 1, "Module": 1, "Function": 1}
    assert data["total_relationships"] == 1


class _FakeEngine:
    """The graph holds `kept` (registered) and two ids this registry doesn't."""

    deleted: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def verify_connectivity(self):
        pass

    def graph_repo_ids(self):
        return {"zz-kept", "zz-other-install", "zz-left-behind"}

    def repo_node_counts(self, repo_ids):
        return {"zz-other-install": 1200, "zz-left-behind": 3}

    def read_applied_schema(self, repo_id):
        return None

    def delete_repository(self, repo_id):
        self.deleted.append(repo_id)

    def close(self, timeout=None):
        pass


@pytest.fixture
def fake_prune(settings, monkeypatch, tmp_path):
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(_git_repo(tmp_path, "zz-kept"))
    finally:
        registry.close()
    _FakeEngine.deleted = []
    monkeypatch.setattr(cli_main, "GraphEngine", _FakeEngine)
    return _FakeEngine.deleted


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_prune_lists_the_ids_with_node_counts_and_the_shared_neo4j_warning(runner, fake_prune):
    result = runner.invoke(app, ["prune", "--dry-run"])
    out = _flat(result.stdout)
    assert result.exit_code == 0, result.stdout
    assert "zz-other-install (1200 node(s))" in out and "zz-left-behind (3 node(s))" in out
    assert "zz-kept" not in out
    assert "another DevGraph install" in out
    assert fake_prune == []


def test_prune_without_yes_and_no_terminal_deletes_nothing(runner, fake_prune, monkeypatch):
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: False)
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 1
    assert "devgraph prune --yes" in _flat(result.stdout)
    assert fake_prune == []


def test_prune_declined_at_the_terminal_deletes_nothing(runner, fake_prune, monkeypatch):
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["prune"], input="n\n")
    assert result.exit_code == 1
    assert fake_prune == []


def test_prune_confirmed_at_the_terminal_deletes(runner, fake_prune, monkeypatch):
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["prune"], input="y\n")
    assert result.exit_code == 0, result.stdout
    assert fake_prune == ["zz-left-behind", "zz-other-install"]


def test_prune_yes_deletes_without_asking(runner, fake_prune):
    result = runner.invoke(app, ["prune", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert fake_prune == ["zz-left-behind", "zz-other-install"]
