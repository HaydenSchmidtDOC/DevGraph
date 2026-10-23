"""`devgraph doctor`: Podman is informational once Neo4j answers, and the
schema-constraint check reads only registered repositories.

Doctor failed on any machine without Podman, even with Neo4j reachable (a
Docker or native install), and printed a warning per constraint for every
repository left in the graph by an earlier test run or a removed repository,
none of which the user has registered.
"""

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry
from tests.helpers import stub


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
    return _doctor_args(tmp_path, engine_cls, podman, [])


def _doctor_args(tmp_path, engine_cls, podman, args):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path)), \
         patch.object(cli_main, "GraphEngine", engine_cls), \
         patch.object(cli_main, "resolve_podman", return_value=podman), \
         patch.object(cli_main, "_stale_schema_objects", return_value=[]), \
         patch("devgraph.indexer.schema_constraints.constraint_drift", return_value=[]):
        result = CliRunner().invoke(cli_main.app, ["doctor", *args])
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



# --- first-run failures: a refused login, a stopped machine, a stopped container ----


class _RefusedEngine(_ReachableEngine):
    def verify_connectivity(self):
        from neo4j.exceptions import AuthError

        raise AuthError("{neo4j_code: Neo.ClientError.Security.Unauthorized} {message: The client is unauthorized}")


class _DownEngine(_ReachableEngine):
    def verify_connectivity(self):
        from neo4j.exceptions import ServiceUnavailable

        raise ServiceUnavailable("Couldn't connect to 127.0.0.1:7687")


def _fake_podman(tmp_path, stdout="", stderr="", code=0):
    """A `podman` that prints what a real one would and exits with `code`."""
    (tmp_path / "podman.out").write_text(stdout, encoding="utf-8")
    (tmp_path / "podman.err").write_text(stderr, encoding="utf-8")
    script = tmp_path / "podman"
    script.write_text(
        f"#!/bin/sh\ncat '{tmp_path}/podman.out'\ncat '{tmp_path}/podman.err' >&2\nexit {code}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return script


def test_a_refused_login_names_the_settings_to_check(tmp_path):
    out, result = _doctor(tmp_path, _RefusedEngine, None)
    neo4j = out.split("Neo4j", 1)[1].split("Podman", 1)[0]
    assert "Refused the login" in neo4j and "Not reachable" not in neo4j
    assert "DEVGRAPH_NEO4J_USER" in neo4j and "DEVGRAPH_NEO4J_PASSWORD" in neo4j and ".env" in neo4j
    assert "Unauthorized" not in neo4j
    # Neo4j answered, so a missing podman is not the problem.
    assert "[X]" not in _podman_lines(out)
    assert result.exit_code == 1


def test_an_unreachable_neo4j_says_what_to_start_or_set(tmp_path):
    out, result = _doctor(tmp_path, _DownEngine, None)
    neo4j = out.split("Neo4j", 1)[1].split("Podman", 1)[0]
    assert "Not reachable" in neo4j and "DEVGRAPH_NEO4J_URI" in neo4j
    assert result.exit_code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="the fake podman is a shell script")
def test_a_stopped_podman_machine_says_to_start_it(tmp_path):
    podman = _fake_podman(
        tmp_path,
        stderr="Error: unable to connect to Podman socket: Cannot connect to Podman. "
        "Please verify your connection to the Linux system, or try `podman machine start`",
        code=125,
    )
    out, result = _doctor(tmp_path, _DownEngine, podman)
    lines = _podman_lines(out)
    assert "container not found" not in lines
    assert "[X] podman ps failed" in lines and "Podman machine is stopped" in lines
    assert "podman machine start" in lines
    assert result.exit_code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="the fake podman is a shell script")
def test_an_exited_container_is_reported_stopped_with_the_start_command(tmp_path):
    podman = _fake_podman(tmp_path, stdout="devgraph-neo4j\texited\n")
    out, result = _doctor(tmp_path, _DownEngine, podman)
    lines = _podman_lines(out)
    assert "[OK]" not in lines
    assert "[X] devgraph-neo4j is exited" in lines and "podman start devgraph-neo4j" in lines
    assert result.exit_code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="the fake podman is a shell script")
def test_a_running_container_is_ok(tmp_path):
    podman = _fake_podman(tmp_path, stdout="devgraph-neo4j\trunning\n")
    out, result = _doctor(tmp_path, _ReachableEngine, podman)
    assert "[OK] devgraph-neo4j running" in _podman_lines(out)


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
    # The orphan is named further down, by the orphaned-graph-data check
    # (which offers `devgraph prune`) -- never as a constraint to rescan.
    constraints = out.split("Schema constraints", 1)[1].split("Orphaned graph data", 1)[0]
    assert f"devgraph rescan {registered} --now" in constraints
    assert orphan not in constraints
    assert orphan in out.split("Orphaned graph data", 1)[1]


def test_doctor_wait_waits_for_neo4j_before_checking(tmp_path, monkeypatch):
    from neo4j.exceptions import ServiceUnavailable

    from devgraph.graph import engine as engine_module

    monkeypatch.setattr(engine_module.time, "sleep", lambda s: None)
    tries = []

    class StartingDriver:
        def verify_connectivity(self):
            tries.append(1)
            if len(tries) <= 3:
                raise ServiceUnavailable("Connection reset by peer")

        def close(self):
            pass

    monkeypatch.setattr(engine_module.GraphDatabase, "driver", lambda *a, **k: StartingDriver())
    out, result = _doctor_args(tmp_path, _ReachableEngine, None, ["--wait", "90"])
    assert "Waiting for Neo4j" in out and "Neo4j answered after" in out
    assert "[OK] Reachable" in out
    assert result.exit_code == 0, out


def test_doctor_wait_says_how_long_it_waited_when_neo4j_never_answers(tmp_path, monkeypatch):
    from devgraph.graph import engine as engine_module

    stub(monkeypatch, engine_module, "wait_for_bolt", lambda *a, **k: False)
    out, result = _doctor_args(tmp_path, _DownEngine, None, ["--wait", "1"])
    assert "did not answer after waiting" in out
    assert result.exit_code == 1


def test_doctor_without_wait_checks_once(tmp_path):
    tries = []

    class Down(_DownEngine):
        def verify_connectivity(self):
            tries.append(1)
            super().verify_connectivity()

    out, result = _doctor(tmp_path, Down, None)
    assert "Waiting for Neo4j" not in out and len(tries) == 1


@pytest.mark.parametrize("code", [
    "Neo.ClientError.Security.AuthenticationRateLimit", "Neo.ClientError.Security.CredentialsExpired",
])
def test_any_security_refusal_is_a_login_problem(tmp_path, code):
    from neo4j.exceptions import Neo4jError

    class Refused(_ReachableEngine):
        def verify_connectivity(self):
            raise Neo4jError._hydrate_neo4j(code=code, message="The client has provided incorrect credentials too many times")

    out, result = _doctor(tmp_path, Refused, None)
    neo4j = out.split("Neo4j", 1)[1].split("Podman", 1)[0]
    assert "Refused the login" in neo4j and "DEVGRAPH_NEO4J_PASSWORD" in neo4j
    assert "[X]" not in _podman_lines(out)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake podman is a shell script")
def test_a_podman_failure_that_does_not_name_the_machine_gets_no_machine_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main.sys, "platform", "darwin")
    podman = _fake_podman(tmp_path, stderr="Error: connection refused", code=125)
    out, _ = _doctor(tmp_path, _DownEngine, podman)
    lines = _podman_lines(out)
    assert "The Podman machine is stopped" not in lines
    assert "If the Podman machine is stopped" in lines and "podman machine start" in lines
