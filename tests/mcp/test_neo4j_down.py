"""With Neo4j down, an MCP call fails within seconds with an error saying what to
do, instead of retrying for the driver's default 30 s and returning its raw error."""

import socket
from contextlib import contextmanager
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import AuthError, ServiceUnavailable, TransientError

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


class _Driver:
    """Fails each session's read with the next of `failures`, then answers."""

    def __init__(self, failures, slow_s=0.0):
        self.failures, self.slow_s, self.attempts = list(failures), slow_s, 0

    @contextmanager
    def session(self, **kwargs):
        yield self

    def execute_read(self, work):
        self.attempts += 1
        if self.failures:
            time.sleep(self.slow_s)
            raise self.failures.pop(0)
        return [], False


def _quick_engine(driver):
    engine = GraphEngine(CLOSED, "neo4j", "x", quick_retry_s=1.0, connection_timeout_s=2.0)
    engine._driver = driver
    return engine


def test_a_quick_blip_is_retried_once():
    driver = _Driver([TransientError("deadlock")])
    assert _quick_engine(driver).run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1) == ([], False)
    assert driver.attempts == 2
    driver = _Driver([ServiceUnavailable("dropped"), ServiceUnavailable("dropped")])
    with pytest.raises(ServiceUnavailable):
        _quick_engine(driver).run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1)
    assert driver.attempts == 2


def test_a_slow_failure_is_not_retried():
    driver = _Driver([ServiceUnavailable("timed out")], slow_s=1.1)
    with pytest.raises(ServiceUnavailable):
        _quick_engine(driver).run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1)
    assert driver.attempts == 1


def test_the_server_starts_and_serves_with_neo4j_down(tmp_path, monkeypatch, caplog):
    from devgraph.graph import engine as engine_module

    monkeypatch.setattr(engine_module, "_BASE_DELAY_S", 0.001)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=CLOSED))
    for name in ("start_tray_if_not_running", "register_tray_holder", "stop_tray_if_last_holder"):
        monkeypatch.setattr(mcp_server.lifecycle, name, lambda: None)
    served = []
    monkeypatch.setattr(mcp_server.anyio, "run", lambda fn, server: served.append(server))
    mcp_server.main()
    assert len(served) == 1
    assert f"Neo4j isn't reachable at {CLOSED}; start Neo4j" in caplog.text and "serving anyway" in caplog.text


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
