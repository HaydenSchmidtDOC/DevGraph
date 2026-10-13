"""An autocommit statement's execution error reaches its own retry.

`session.run` returns a lazy result: the statement executes as the result is
pulled, so a deadlock, a lock timeout or a constraint violation surfaces only
when it is consumed. Left unconsumed, the session's next `run` raised it
inside the wrong statement's retry, and the session's close swallowed it into
a warning, so the indexer carried on as if the write had happened. The fake
session here mimics the driver on both counts."""

import pytest
from neo4j.exceptions import ConstraintError, TransientError

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine

# Captured at import: without a local Neo4j, conftest's `_fail_fast_without_neo4j`
# replaces GraphEngine.verify_connectivity for each test.
_REAL_VERIFY_CONNECTIVITY = GraphEngine.verify_connectivity


class FakeResult:
    """Raises `failure`, if any, the first time it is consumed or read."""

    def __init__(self, failure: Exception | None, records: list):
        self._failure = failure
        self._records = records

    def _pull(self):
        if self._failure is not None:
            failure, self._failure = self._failure, None
            raise failure

    def consume(self):
        self._pull()
        return "summary"

    def __iter__(self):
        self._pull()
        return iter(self._records)


class FakeSession:
    def __init__(self, driver):
        self._driver = driver
        self._outstanding: FakeResult | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # As the driver's Session.close: consume an outstanding result, swallowing its error.
        if self._outstanding is not None:
            try:
                self._outstanding.consume()
            except Exception:  # noqa: BLE001 - the driver logs a warning
                pass
        return False

    def run(self, query, *_args, **_kwargs):
        if self._outstanding is not None:  # as the driver: buffer the previous result first
            self._outstanding.consume()
        self._driver.runs.append(query)
        failure = self._driver.failures.pop(0) if self._driver.failures else None
        self._outstanding = FakeResult(failure, self._driver.records)
        return self._outstanding


class FakeDriver:
    def __init__(self, failures: list, records: list | None = None):
        self.failures = failures
        self.records = records or []
        self.runs: list[str] = []

    def session(self, **_kwargs):
        return FakeSession(self)

    def verify_connectivity(self):
        pass

    def close(self):
        pass


def fake_engine(monkeypatch, failures, records=None) -> tuple[GraphEngine, FakeDriver]:
    driver = FakeDriver(failures, records)
    monkeypatch.setattr(engine_module.GraphDatabase, "driver", lambda *a, **k: driver)
    monkeypatch.setattr(GraphEngine, "verify_connectivity", _REAL_VERIFY_CONNECTIVITY)
    monkeypatch.setattr(engine_module, "_BASE_DELAY_S", 0.0)
    return GraphEngine("bolt://example.invalid:7687", "neo4j", "secret"), driver


def test_a_write_failing_as_it_executes_is_retried(monkeypatch):
    engine, driver = fake_engine(monkeypatch, [TransientError("deadlock"), None])
    engine.delete_repository("r")
    assert len(driver.runs) == 2


def test_a_write_that_keeps_failing_raises_instead_of_being_swallowed(monkeypatch):
    engine, driver = fake_engine(monkeypatch, [TransientError("lock timeout")] * 10)
    with pytest.raises(TransientError):
        engine.delete_repository("r")
    assert len(driver.runs) == engine_module._MAX_RETRIES + 1


def test_a_constraint_violation_raises_from_its_own_write(monkeypatch):
    engine, driver = fake_engine(monkeypatch, [ConstraintError("already exists")])
    with pytest.raises(ConstraintError):
        engine.write_resolver_config("r", {})
    assert len(driver.runs) == 1


def test_a_failed_statement_is_retried_not_the_one_after_it(monkeypatch):
    """clear_extracted_properties reads the keys, then removes the stale ones:
    a failure of the read is retried as the read."""
    engine, driver = fake_engine(monkeypatch, [TransientError("deadlock"), None, None], [{"key": "stale"}])
    monkeypatch.setattr(engine, "_schema_labels", lambda repo_id: [])  # the labels' own read is not under test
    monkeypatch.setattr(engine, "_user_labels_in_use", lambda: [])
    assert engine.clear_extracted_properties("r", "fs", "Doc", keep=[]) == ["stale"]
    assert len(driver.runs) == 3
    assert driver.runs[0] == driver.runs[1] != driver.runs[2]
    assert "REMOVE n.`stale`" in driver.runs[2]


def test_records_failing_as_they_are_read_are_retried(monkeypatch):
    engine, driver = fake_engine(monkeypatch, [TransientError("deadlock"), None], [{"label": "Module", "name": "m"}])
    monkeypatch.setattr(engine, "_schema_labels", lambda repo_id: ["Doc"])  # the labels' own read is not under test
    monkeypatch.setattr(engine, "_user_labels_in_use", lambda: [])
    assert engine.extracted_nodes_at("r", "fs", ["m"]) == {("Module", "m")}
    assert len(driver.runs) == 2


def _rekeyed(count: int) -> list:
    return [("Function", f"f{i}", "a.cs", ("Ns:A",), True) for i in range(count)]


def test_a_match_key_chunk_failing_as_it_executes_is_retried(monkeypatch):
    """write_match_keys runs one statement per chunk of nodes: the last
    chunk's deadlock is retried as that chunk, not lost when the session
    closes."""
    monkeypatch.setattr(engine_module, "RETRACT_NODES", 2)
    engine, driver = fake_engine(monkeypatch, [None, TransientError("deadlock"), None])
    engine.write_match_keys("r", _rekeyed(3))
    assert len(driver.runs) == 3
    assert driver.runs[1] == driver.runs[2]


def test_a_match_key_chunk_that_keeps_failing_raises(monkeypatch):
    monkeypatch.setattr(engine_module, "RETRACT_NODES", 2)
    engine, driver = fake_engine(monkeypatch, [None] + [TransientError("lock timeout")] * 10)
    with pytest.raises(TransientError):
        engine.write_match_keys("r", _rekeyed(3))
    assert len(driver.runs) == 1 + engine_module._MAX_RETRIES + 1
    engine, driver = fake_engine(monkeypatch, [ConstraintError("bad")])
    with pytest.raises(ConstraintError):
        engine.settle_match_keys("r", [("Function", "f", "a.cs")])


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


def test_a_write_violating_a_constraint_after_its_first_rows_raises(engine):
    """Live: a write whose duplicate Repository comes after the first batch of
    returned rows fails only as the result is pulled. `_run` raises it from
    the write itself; unconsumed, the session's close only logged it. (Neo4j
    reports most other failures with the RUN, so this shape is the one that
    fails late on demand.)"""
    repo_id = "_smoketest_autocommit_late_failure"
    query = (
        "UNWIND range(1, 3000) AS i "
        "CREATE (:Repository {repo_id: $repo_id + '_' + CASE WHEN i = 2999 THEN 1 ELSE i END}) RETURN i"
    )
    try:
        with engine._driver.session() as session, pytest.raises(ConstraintError):
            engine_module._run(session, query, repo_id=repo_id)
        assert engine.run_cypher(
            "MATCH (r:Repository) WHERE r.repo_id STARTS WITH $repo_id RETURN r", {"repo_id": repo_id}
        ) == []
    finally:
        engine.run_cypher(
            "MATCH (r:Repository) WHERE r.repo_id STARTS WITH $repo_id DETACH DELETE r", {"repo_id": repo_id}
        )
