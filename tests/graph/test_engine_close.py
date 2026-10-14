"""GraphEngine.close fences the driver: new sessions are refused once a close
is requested, and the close waits, up to a cap, for sessions already open.

Closing the neo4j driver under a running query breaks that query's
connection mid-read (a BufferError in the querying thread), so a close must
never run while a session is open. These use a fake driver that records
whether it was used after it closed."""

import threading
import time

import pytest
from neo4j.exceptions import DriverError, ServiceUnavailable

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import EngineClosed, GraphEngine


# Captured at import: without a local Neo4j, conftest's `_fail_fast_without_neo4j`
# replaces GraphEngine.verify_connectivity for each test, bypassing the fence.
_REAL_VERIFY_CONNECTIVITY = GraphEngine.verify_connectivity


class FakeDriver:
    """Records the order of queries and the close. A query blocks while
    `gate` is set and not yet released; `inside` is set once one is running."""

    def __init__(self):
        self.events: list[str] = []
        self.closed = False
        self.used_after_close = False
        self.gate: threading.Event | None = None
        self.inside = threading.Event()

    def work(self, what: str) -> None:
        if self.closed:
            self.used_after_close = True
        self.inside.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.closed:
            self.used_after_close = True
        self.events.append(what)

    def session(self, **_kwargs):
        return FakeSession(self)

    def verify_connectivity(self):
        self.work("verify")

    def close(self):
        self.events.append("close")
        self.closed = True


class FakeSession:
    def __init__(self, driver: FakeDriver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, *_args, **_kwargs):
        self._driver.work("run")
        return []

    def execute_write(self, fn, *_args, **_kwargs):
        self._driver.work("write")

    def execute_read(self, fn, *_args, **_kwargs):
        self._driver.work("read")
        return []


def fake_engine(monkeypatch) -> tuple[GraphEngine, FakeDriver]:
    driver = FakeDriver()
    monkeypatch.setattr(engine_module.GraphDatabase, "driver", lambda *a, **k: driver)
    monkeypatch.setattr(GraphEngine, "verify_connectivity", _REAL_VERIFY_CONNECTIVITY)
    return GraphEngine("bolt://example.invalid:7687", "neo4j", "secret"), driver


def run_in_thread(fn, *args) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def target():
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def test_close_waits_for_an_open_session_before_closing_the_driver(monkeypatch):
    engine, driver = fake_engine(monkeypatch)
    driver.gate = threading.Event()
    thread, errors = run_in_thread(engine.run_cypher, "RETURN 1")
    assert driver.inside.wait(5)
    threading.Timer(0.3, driver.gate.set).start()
    engine.close(timeout=5)
    thread.join(5)
    assert driver.events == ["run", "close"]
    assert not driver.used_after_close and errors == []


def test_verify_connectivity_counts_as_in_flight(monkeypatch):
    engine, driver = fake_engine(monkeypatch)
    driver.gate = threading.Event()
    thread, errors = run_in_thread(engine.verify_connectivity)
    assert driver.inside.wait(5)
    threading.Timer(0.3, driver.gate.set).start()
    engine.close(timeout=5)
    thread.join(5)
    assert driver.events == ["verify", "close"] and errors == []


def test_a_session_requested_after_close_is_refused_as_unavailable(monkeypatch):
    engine, driver = fake_engine(monkeypatch)
    engine.close()
    started = time.monotonic()
    with pytest.raises(EngineClosed) as raised:
        engine.read_insights_summary("r")
    with pytest.raises(EngineClosed):
        engine.verify_connectivity()  # not retried as a transient blip either
    assert time.monotonic() - started < 0.4
    # Callers already treat these as "database unavailable".
    assert isinstance(raised.value, ServiceUnavailable) and isinstance(raised.value, DriverError)
    assert not driver.used_after_close


def test_close_is_bounded_and_abandons_a_hung_query_with_one_warning(monkeypatch, caplog):
    engine, driver = fake_engine(monkeypatch)
    driver.gate = threading.Event()
    thread, errors = run_in_thread(engine.run_cypher, "RETURN 1")
    assert driver.inside.wait(5)
    started = time.monotonic()
    with caplog.at_level("WARNING", logger="devgraph.graph.engine"):
        engine.close(timeout=0.3)
    assert time.monotonic() - started < 1.0
    # The driver is left open under the hung query rather than closed under it.
    assert not driver.closed
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "still running" in warnings[0].getMessage()
    # Once the query ends, its thread finishes cleanly and a later close closes.
    driver.gate.set()
    thread.join(5)
    assert errors == [] and not driver.used_after_close
    engine.close(timeout=1)
    assert driver.closed
