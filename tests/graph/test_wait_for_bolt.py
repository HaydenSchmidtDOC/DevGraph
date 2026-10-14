"""`wait_for_bolt`: first-run setup waits for a just-started Neo4j to answer.

A fresh container accepts TCP at once but answers Bolt only ~20 s later, so
setup that checked TCP and went on failed on a clean machine.
"""

import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import wait_for_bolt


class _Starting:
    """Raises ServiceUnavailable `failures` times, then answers."""

    def __init__(self, failures, error=None):
        self.failures, self.calls, self.error = failures, 0, error

    def verify_connectivity(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise ServiceUnavailable("Connection reset by peer")
        if self.error is not None:
            raise self.error


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(engine_module.time, "sleep", lambda s: None)


def test_waits_out_a_neo4j_that_is_still_starting():
    engine, waits = _Starting(failures=5), []
    assert wait_for_bolt(engine, timeout_s=60, on_wait=waits.append) is True
    assert engine.calls == 6
    assert len(waits) == 5


def test_gives_up_at_the_deadline(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(clock))
    engine = _Starting(failures=10**6)
    assert wait_for_bolt(engine, timeout_s=60) is False
    assert engine.calls == 6  # a try every 10 s until 60 s have passed


def test_a_refused_login_counts_as_answering_and_is_not_waited_out():
    engine = _Starting(failures=0, error=AuthError("unauthorized"))
    assert wait_for_bolt(engine, timeout_s=60) is True
    assert engine.calls == 1
