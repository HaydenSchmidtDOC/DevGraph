"""Registration never deletes or merges into another repository's graph data.

Two DevGraph installs can share one Neo4j (a laptop and a desktop, WSL and
Windows) and register the same folder name, so graph data under the id a
registry is about to issue may be another install's live repository, or one
unregistered here with `remove --keep-graph`.

- Its Repository node names this same path: the data is this repository's
  (registered again, or the same layout on another machine), so it is adopted.
- Another path, or nodes with no Repository node: the next free suffixed id is
  issued and the old data is left exactly as it was.
- Registered while Neo4j was down (no check possible): the first rescan or
  agent index refuses rather than merging.

Every id here is a `zz-a10c-` id of this test's own; the shared test Neo4j
holds other sessions' repositories.
"""

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from devgraph import config as config_module
from devgraph.agent.sync import RepoSync
from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    settings = MagicMock()
    settings.registry_db_path = tmp_path / "registry.db"
    settings.neo4j_uri = "bolt://127.0.0.1:7687"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "devgraph-local-dev"
    settings.enable_run_cypher = False
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def graph():
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


@pytest.fixture
def name():
    return f"zz-a10c-reuse-{uuid.uuid4().hex[:8]}"


def _git_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    return repo


def _other_repository(engine: GraphEngine, repo_id: str, path: str | None) -> None:
    """Another repository's data under `repo_id`: a Repository node at
    `path` (none when None) and one of its commits."""
    if path is not None:
        engine.upsert_repository(repo_id, repo_id, path)
    engine.upsert_node("Commit", repo_id, "0ld5ecre7", {"message": "old secret project commit"})


def _names(engine: GraphEngine, label: str, repo_id: str) -> list[str]:
    rows = engine.run_cypher(f"MATCH (n:{label} {{repo_id: $r}}) RETURN n.name AS name", {"r": repo_id})
    return sorted(row["name"] for row in rows)


def _repository_path(engine: GraphEngine, repo_id: str) -> str | None:
    rows = engine.run_cypher("MATCH (r:Repository {repo_id: $r}) RETURN r.path AS path", {"r": repo_id})
    return rows[0]["path"] if rows else None


def _registered(settings) -> list[str]:
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return [r.repo_id for r in registry.list_repos()]
    finally:
        registry.close()


def test_add_adopts_data_whose_repository_is_this_same_path(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    repo = _git_repo(tmp_path, name)
    ids.append(name)
    _other_repository(engine, name, str(repo.resolve()))

    result = runner.invoke(app, ["add", str(repo)])

    assert result.exit_code == 0, result.stdout
    assert _registered(settings) == [name]
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _names(engine, "Function", name) == ["handler"]


def test_add_takes_a_free_id_beside_another_paths_data_and_leaves_it(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, "/home/other-machine/elsewhere")

    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, name))])

    assert result.exit_code == 0, result.stdout
    out = " ".join(result.stdout.split())
    assert f"so this one is {name}-2" in out and "left in place" in out
    assert _registered(settings) == [f"{name}-2"]
    assert _names(engine, "Function", f"{name}-2") == ["handler"]
    assert _names(engine, "Commit", f"{name}-2") == []
    # The other repository's data is exactly as it was.
    assert _repository_path(engine, name) == "/home/other-machine/elsewhere"
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _names(engine, "Function", name) == []


def test_add_takes_a_free_id_beside_nodes_with_no_repository_node(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, None)

    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, name))])

    assert result.exit_code == 0, result.stdout
    assert _registered(settings) == [f"{name}-2"]
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _repository_path(engine, name) is None


def test_add_skips_every_held_suffix(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    ids.extend([name, f"{name}-2", f"{name}-3"])
    _other_repository(engine, name, "/elsewhere/one")
    _other_repository(engine, f"{name}-2", "/elsewhere/two")

    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, name))])

    assert result.exit_code == 0, result.stdout
    assert _registered(settings) == [f"{name}-3"]
    assert _names(engine, "Commit", f"{name}-2") == ["0ld5ecre7"]


@pytest.fixture
def dashboard(graph, tmp_path):
    engine, _ = graph
    registry = RepoRegistry(tmp_path / "dash-registry.db")
    yield TestClient(build_app(engine, registry, EventBroadcaster()), base_url="http://127.0.0.1")
    registry.close()


def test_dashboard_register_takes_a_free_id_beside_another_paths_data(dashboard, graph, tmp_path, name):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, "/home/other-machine/elsewhere")

    res = dashboard.post("/api/repos", json={"path": str(_git_repo(tmp_path, name))})

    assert res.status_code == 201, res.text
    assert res.json()["repo_id"] == f"{name}-2"
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _repository_path(engine, name) == "/home/other-machine/elsewhere"


def test_dashboard_register_with_an_explicit_id_skips_it_too(dashboard, graph, tmp_path, name):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, None)

    res = dashboard.post("/api/repos", json={"path": str(_git_repo(tmp_path, "checkout")), "repo_id": name})

    assert res.status_code == 201, res.text
    assert res.json()["repo_id"] == f"{name}-2"
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]


def test_dashboard_register_adopts_data_whose_repository_is_this_same_path(dashboard, graph, tmp_path, name):
    engine, ids = graph
    repo = _git_repo(tmp_path, name)
    ids.append(name)
    _other_repository(engine, name, str(repo.resolve()))

    res = dashboard.post("/api/repos", json={"path": str(repo)})

    assert res.status_code == 201, res.text
    assert res.json()["repo_id"] == name
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]


def _registered_while_neo4j_was_down(settings, repo: Path) -> str:
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return registry.add_repo(repo).repo_id
    finally:
        registry.close()


def test_rescan_of_a_repo_registered_while_neo4j_was_down_refuses_another_paths_data(
    runner, settings, graph, tmp_path, name
):
    engine, ids = graph
    ids.append(name)
    _other_repository(engine, name, "/home/other-machine/elsewhere")
    assert _registered_while_neo4j_was_down(settings, _git_repo(tmp_path, name)) == name

    result = runner.invoke(app, ["rescan", name])

    assert result.exit_code == 1
    out = " ".join(result.stdout.split())
    assert "already holds data under" in out and f"devgraph remove {name} --keep-graph" in out
    assert _names(engine, "Function", name) == []
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _repository_path(engine, name) == "/home/other-machine/elsewhere"


def test_rescan_of_a_repo_registered_while_neo4j_was_down_runs_on_an_empty_id(
    runner, settings, graph, tmp_path, name
):
    engine, ids = graph
    ids.append(name)
    _registered_while_neo4j_was_down(settings, _git_repo(tmp_path, name))

    result = runner.invoke(app, ["rescan", name])

    assert result.exit_code == 0, result.stdout
    assert _names(engine, "Function", name) == ["handler"]


def test_the_agent_writes_nothing_for_a_never_indexed_repo_over_another_paths_data(settings, graph, tmp_path, name):
    engine, ids = graph
    ids.append(name)
    _other_repository(engine, name, None)
    repo = _git_repo(tmp_path, name)
    _registered_while_neo4j_was_down(settings, repo)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        sync = RepoSync(engine, registry, publish=lambda event: None, request_catch_up=lambda *a: None)
        sync.on_changes(name, {repo / "app.py"}, set())
        assert sync.on_catch_up(name, datetime(1970, 1, 1, tzinfo=timezone.utc)) is False
        assert registry.get(name).last_indexed is None
    finally:
        registry.close()
    assert _names(engine, "Function", name) == []
    assert _repository_path(engine, name) is None


@pytest.fixture
def id_check_fails_once(monkeypatch):
    """Registration's id check can't reach Neo4j (a timeout, an outage that
    ends at once); every later check works."""
    real = GraphEngine.repository_owner
    calls = []

    def flaky(self, repo_id):
        calls.append(repo_id)
        if len(calls) == 1:
            raise TimeoutError("Neo4j did not answer in time")
        return real(self, repo_id)

    def unreachable(self, *args, **kwargs):
        raise TimeoutError("Neo4j did not answer in time")

    monkeypatch.setattr(GraphEngine, "repository_owner", flaky)
    # A whole-database id read, if registration still made one, fails too.
    monkeypatch.setattr(GraphEngine, "graph_repo_paths", unreachable, raising=False)
    return calls


def test_add_refuses_to_scan_into_another_paths_data_when_the_id_check_failed(
    runner, settings, graph, tmp_path, name, id_check_fails_once
):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, "/home/other-machine/elsewhere")

    result = runner.invoke(app, ["add", str(_git_repo(tmp_path, name))])

    assert result.exit_code == 1
    assert "already holds data under" in " ".join(result.stdout.split())
    assert _registered(settings) == [name]
    assert _names(engine, "Function", name) == []
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]
    assert _repository_path(engine, name) == "/home/other-machine/elsewhere"


def test_dashboard_register_refuses_to_scan_into_another_paths_data_when_the_id_check_failed(
    dashboard, graph, tmp_path, name, id_check_fails_once
):
    engine, ids = graph
    ids.extend([name, f"{name}-2"])
    _other_repository(engine, name, None)

    res = dashboard.post("/api/repos", json={"path": str(_git_repo(tmp_path, name))})

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["repo_id"] == name and body["indexed"] is False
    assert "already holds data under" in body["warning"]
    assert _names(engine, "Function", name) == []
    assert _repository_path(engine, name) is None


def test_index_history_refuses_a_never_indexed_repo_over_another_paths_data(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    ids.append(name)
    _other_repository(engine, name, "/home/other-machine/elsewhere")
    repo = _git_repo(tmp_path, name)
    subprocess.run(["git", "-c", "user.name=Example Dev", "-c", "user.email=dev@example.com",
                    "commit", "-q", "--allow-empty", "-m", "mine"], cwd=repo, check=True)
    _registered_while_neo4j_was_down(settings, repo)

    result = runner.invoke(app, ["index-history", name])

    assert result.exit_code == 1
    assert "already holds data under" in " ".join(result.stdout.split())
    assert _names(engine, "Commit", name) == ["0ld5ecre7"]


def test_annotate_note_refuses_a_never_indexed_repo_over_another_paths_data(runner, settings, graph, tmp_path, name):
    engine, ids = graph
    ids.append(name)
    _other_repository(engine, name, "/home/other-machine/elsewhere")
    repo = _git_repo(tmp_path, name)
    (repo / "req.md").write_text("---\ntype: requirement\nid: REQ-1\ntitle: Mine\n---\nBody\n", encoding="utf-8")
    _registered_while_neo4j_was_down(settings, repo)

    result = runner.invoke(app, ["annotate", name, "--note", "req.md"])

    assert result.exit_code == 1
    assert "already holds data under" in " ".join(result.stdout.split())
    rows = engine.run_cypher("MATCH (n {repo_id: $r}) RETURN DISTINCT labels(n)[0] AS label", {"r": name})
    assert sorted(row["label"] for row in rows) == ["Commit", "Repository"]


def test_the_id_check_asks_only_about_each_candidate(tmp_path):
    """Registration asks about the ids it would issue, one at a time, instead
    of reading every repo id in the database."""
    asked = []

    def is_taken(repo_id):
        asked.append(repo_id)
        return repo_id == "zz-a10c-lazy"

    repo = tmp_path / "zz-a10c-lazy"
    (repo / ".git").mkdir(parents=True)
    registry = RepoRegistry(tmp_path / "lazy.db")
    try:
        assert registry.add_repo(repo, is_taken=is_taken).repo_id == "zz-a10c-lazy-2"
    finally:
        registry.close()
    assert asked == ["zz-a10c-lazy", "zz-a10c-lazy-2"]
