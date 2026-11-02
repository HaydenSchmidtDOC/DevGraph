"""The Cypher console re-runs a plain read that Neo4j fails with a transient error, never a write."""
from types import SimpleNamespace

import pytest
from neo4j.exceptions import TransientError

from devgraph.graph import engine as engine_mod


class _Result:
    def keys(self):
        return ["n"]

    def __iter__(self):
        return iter(())


class _Session:
    def __init__(self, calls, fail_times):
        self.calls, self.fail_times = calls, fail_times

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, params):
        self.calls.append(query)
        if len(self.calls) <= self.fail_times:
            raise TransientError("Neo.TransientError.Transaction.Outdated")
        return _Result()


def _engine(calls, fail_times):
    fake = SimpleNamespace(_driver=SimpleNamespace(session=lambda: _Session(calls, fail_times)))
    return engine_mod.GraphEngine.run_cypher_graph.__get__(fake)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)


def test_read_is_retried_after_a_transient_error():
    calls = []
    out = _engine(calls, 1)("MATCH (n) RETURN n")
    assert out["data"] == [] and len(calls) == 2


def test_write_is_not_retried():
    calls = []
    with pytest.raises(TransientError):
        _engine(calls, 1)("MATCH (n) SET n.x = 1 RETURN n")
    assert len(calls) == 1
