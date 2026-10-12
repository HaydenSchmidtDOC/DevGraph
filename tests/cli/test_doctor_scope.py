"""`devgraph doctor`: Podman is informational once Neo4j answers, and the
schema-constraint check reads only registered repositories.

Doctor failed on any machine without Podman, even with Neo4j reachable (a
Docker or native install), and printed a warning per constraint for every
repository left in the graph by an earlier test run or a removed repository,
none of which the user has registered.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry


def _settings(tmp_path):
    return SimpleNamespace(
        registry_db_path=tmp_path / "registry.sqlite3",
        neo4j_uri="bolt://127.0.0.1:7687", neo4j_user="neo4j", neo4j_password="devgraph-local-dev",
        health_check_interval_s=30,
    )


class _ReachableEngine:
    def __init__(self, *args, **kwargs):
        pass

    def verify_connectivity(self):
        pass

    def init_schema(self):
        pass

    def close(self):
        pass


class _UnreachableEngine(_ReachableEngine):
    def verify_connectivity(self):
        raise OSError("connection refused")


def _doctor(tmp_path, engine_cls, podman):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path)), \
         patch.object(cli_main, "GraphEngine", engine_cls), \
         patch.object(cli_main, "resolve_podman", return_value=podman), \
         patch.object(cli_main, "_stale_schema_objects", return_value=[]), \
         patch("devgraph.indexer.schema_constraints.constraint_drift", return_value=[]):
        result = CliRunner().invoke(cli_main.app, ["doctor"])
    return " ".join(result.stdout.split()), result


def _podman_lines(out):
    return out.split("Podman", 1)[1].split("Registry", 1)[0]


def test_missing_podman_is_informational_when_neo4j_is_reachable(tmp_path):
    out, result = _doctor(tmp_path, _ReachableEngine, None)
    podman = _podman_lines(out)
    assert "[X]" not in podman and "not needed" in podman, podman
    assert result.exit_code == 0, out


def test_missing_podman_fails_when_neo4j_is_unreachable(tmp_path):
    out, result = _doctor(tmp_path, _UnreachableEngine, None)
    assert "[X] podman not found" in _podman_lines(out)
    assert result.exit_code == 1


# --- live: only registered repositories count -------------------------------------


@pytest.fixture
def engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    yield engine
    engine.close()


def test_constraint_check_ignores_unregistered_repositories(engine, tmp_path):
    tag = uuid.uuid4().hex[:8]
    label = f"ZzDoctor{tag}"
    registered, orphan = f"zz-doctor-reg-{tag}", f"zz-doctor-orphan-{tag}"
    root = tmp_path / "registered"
    (root / ".git").mkdir(parents=True)
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    registry.add_repo(root, repo_id=registered)
    registry.close()
    try:
        # Both record the label as applied, and neither has its constraint: a
        # `missing` finding for each, but only the registered one is the user's.
        for repo in (registered, orphan):
            engine.upsert_repository(repo, repo, f"/tmp/{repo}")
            engine.record_applied_schema(repo, "sha256:x", [label], [], [f"{label}:slug"])
        with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path)), \
             patch.object(cli_main, "resolve_podman", return_value=None):
            result = CliRunner().invoke(cli_main.app, ["doctor"])
        out = " ".join(result.stdout.split())
    finally:
        for repo in (registered, orphan):
            engine.delete_repository(repo)
    constraints = out.split("Schema constraints", 1)[1]
    assert f"devgraph rescan {registered} --now" in constraints
    assert orphan not in constraints
