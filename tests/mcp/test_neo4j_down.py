"""With Neo4j down, an MCP call fails within seconds with an error saying what to
do, instead of retrying for the driver's default 30 s and returning its raw error."""

import logging
import socket
from contextlib import contextmanager
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired, TransientError

from devgraph.config.settings import Settings
from devgraph.graph.engine import EngineClosed, GraphEngine
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools

#: Nothing listens here (the discard port), so every connection is refused.
CLOSED = "bolt://127.0.0.1:9"
#: The most a call may take with Neo4j down, on any platform.
BOUND_S = 5.0


@pytest.fixture
def silent_uri():
    """A port that accepts connections and never answers the Bolt handshake: a
    failure that takes the whole connection timeout, like Windows' slow refusal."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(50)
    held = []

    def accept():
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    yield f"bolt://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    for conn in held:
        conn.close()


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("find_callers", {"target_name": "save"}), ("describe_node", {"name": "save"}), ("summarise_repository", {})],
)
@pytest.mark.parametrize("down", ["refused", "silent"])
def test_a_call_with_neo4j_down_fails_fast_and_says_how_to_fix_it(tmp_path, monkeypatch, tool, arguments, down, request):
    uri = CLOSED if down == "refused" else request.getfixturevalue("silent_uri")
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=uri)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-neo4j-down", tmp_path)
    engine = mcp_server.mcp_engine(settings)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool(tool, arguments)

    started = time.monotonic()
    try:
        result = anyio.run(scenario)
    finally:
        engine.close(timeout=0.1)
    elapsed = time.monotonic() - started
    assert elapsed < BOUND_S, f"{tool} took {elapsed:.1f} s with Neo4j down ({down})"
    assert result.is_error is True
    (content,) = result.content
    assert f"Neo4j isn't reachable at {uri}; start Neo4j" in content.text
    assert "devgraph doctor" in content.text


def test_a_refused_login_names_the_settings():
    error = tools.neo4j_unavailable(AuthError("unauthorized"), CLOSED)
    assert f"Neo4j at {CLOSED} refused the username or password" in str(error)
    assert "DEVGRAPH_NEO4J_PASSWORD" in str(error)


def test_other_errors_are_not_reported_as_neo4j_down():
    assert tools.neo4j_unavailable(ValueError("x"), CLOSED) is None
    assert tools.neo4j_unavailable(EngineClosed("closing"), CLOSED) is None
    assert tools.neo4j_unavailable(ServiceUnavailable("down"), CLOSED) is not None


class _Tx:
    def __init__(self, failure):
        self.failure = failure

    def run(self, query, parameters):
        raise self.failure


class _Driver:
    """Each read takes the next of `steps`: ("connect", error) fails before the
    transaction function runs (no connection), ("query", error) fails inside it,
    after `slow_s`; no steps left, the read answers."""

    def __init__(self, steps, slow_s=0.0):
        self.steps, self.slow_s, self.attempts = list(steps), slow_s, 0

    @contextmanager
    def session(self, **kwargs):
        yield self

    def execute_read(self, work):
        self.attempts += 1
        if not self.steps:
            return [], False
        stage, failure = self.steps.pop(0)
        time.sleep(self.slow_s)
        if stage == "connect":
            raise failure
        return work(_Tx(failure))


def _fail_fast_engine(driver):
    engine = GraphEngine(CLOSED, "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    engine._driver = driver
    return engine


def _read(engine):
    return engine.run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1)


@pytest.mark.parametrize(
    "failure", [TransientError("deadlock"), SessionExpired("expired"), ServiceUnavailable("connection lost")]
)
def test_a_failure_once_connected_is_retried_once_however_long_it_took(failure):
    driver = _Driver([("query", failure)], slow_s=1.2)
    assert _read(_fail_fast_engine(driver)) == ([], False)
    assert driver.attempts == 2
    driver = _Driver([("query", failure), ("query", failure)])
    with pytest.raises(type(failure)):
        _read(_fail_fast_engine(driver))
    assert driver.attempts == 2


def test_no_connection_at_all_is_not_retried():
    driver = _Driver([("connect", ServiceUnavailable("refused"))])
    with pytest.raises(ServiceUnavailable):
        _read(_fail_fast_engine(driver))
    assert driver.attempts == 1


def test_the_driver_itself_never_retries(caplog):
    engine = mcp_server.mcp_engine(Settings(neo4j_uri=CLOSED))
    caplog.set_level(logging.WARNING, logger="neo4j")
    try:
        with pytest.raises(ServiceUnavailable):
            _read(engine)
    finally:
        engine.close(timeout=0.1)
    assert not [r for r in caplog.records if "will be retried" in r.getMessage()]


class _SchemaEngine:
    """Unreachable until `up`; counts init_schema runs."""

    uri = CLOSED

    def __init__(self):
        self.up, self.inits = False, 0

    def probe_connectivity(self):
        if not self.up:
            raise ServiceUnavailable("refused")

    def init_schema(self, wait=True):
        assert wait is False
        self.inits += 1

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        if not self.up:
            raise ServiceUnavailable("refused")
        return [{"repo_name": "zz-schema", "service_count": 0}], False


def test_a_server_started_with_neo4j_down_provisions_the_schema_once_it_is_up(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    record = _Repo("zz-schema", tmp_path)
    engine = _SchemaEngine()
    server = mcp_server.build_server(
        engine, _Registry([record]), session_repo=record, session_source="env", provision_schema=True
    )

    def call():
        async def scenario():
            async with Client(server, mode="auto") as client:
                return await client.call_tool("summarise_repository", {})

        return anyio.run(scenario)

    down = call()
    assert down.is_error is True and "isn't reachable" in down.content[0].text
    assert engine.inits == 0
    engine.up = True
    assert call().is_error is False and call().is_error is False
    assert engine.inits == 1


@pytest.mark.parametrize("down", ["refused", "silent"])
def test_the_server_starts_and_serves_with_neo4j_down(tmp_path, monkeypatch, caplog, down, request):
    uri = CLOSED if down == "refused" else request.getfixturevalue("silent_uri")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=uri))
    for name in ("start_tray_if_not_running", "register_tray_holder", "stop_tray_if_last_holder"):
        monkeypatch.setattr(mcp_server.lifecycle, name, lambda: None)
    served = []
    monkeypatch.setattr(mcp_server.anyio, "run", lambda fn, server: served.append(time.monotonic()))
    started = time.monotonic()
    mcp_server.main()
    assert len(served) == 1
    assert served[0] - started < BOUND_S, "one connectivity probe, not retried"
    assert f"Neo4j isn't reachable at {uri}; start Neo4j" in caplog.text and "serving anyway" in caplog.text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("describe_node", {"name": "save"}), ("find_callers", {"target_name": "save"}), ("summarise_repository", {})],
)
def test_a_refused_login_is_the_friendly_error_in_every_tool(tmp_path, monkeypatch, tool, arguments):
    live = "bolt://127.0.0.1:7687"
    probe = GraphEngine(live, "neo4j", "devgraph-local-dev")
    try:
        probe.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    finally:
        probe.close()
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=live, neo4j_user="zz-no-such-user")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-wrong-login", tmp_path)
    engine = mcp_server.mcp_engine(settings)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool(tool, arguments)

    try:
        result = anyio.run(scenario)
    finally:
        engine.close()
    (content,) = result.content
    assert result.is_error is True
    assert f"Neo4j at {live} refused the username or password" in content.text
    assert "Neo.ClientError" not in content.text
