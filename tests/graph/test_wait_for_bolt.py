"""`wait_for_bolt`: first-run setup waits for a just-started Neo4j to answer.

A fresh container accepts TCP at once but answers Bolt only ~20 s later, so
setup that checked TCP and went on failed on a clean machine.
"""

import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import wait_for_bolt


class _Starting:
    """A driver whose verify raises ServiceUnavailable `failures` times, then answers."""

    def __init__(self, failures, error=None):
        self.failures, self.calls, self.error, self.closed = failures, 0, error, False
        self.options = {}

    def verify_connectivity(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise ServiceUnavailable("Connection reset by peer")
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(engine_module.time, "sleep", lambda s: None)


@pytest.fixture
def driver(monkeypatch):
    holder = {}

    def make(driver):
        def factory(uri, **options):
            driver.options = options
            return driver

        monkeypatch.setattr(engine_module.GraphDatabase, "driver", factory)
        holder["driver"] = driver
        return driver

    return make


def _wait(**kw):
    return wait_for_bolt("bolt://127.0.0.1:7687", "neo4j", "pw", **kw)


def test_waits_out_a_neo4j_that_is_still_starting(driver):
    fake, waits = driver(_Starting(failures=5)), []
    assert _wait(timeout_s=60, on_wait=waits.append) is True
    assert fake.calls == 6 and len(waits) == 5 and fake.closed


def test_gives_up_at_the_deadline(driver, monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(clock))
    fake = driver(_Starting(failures=10**6))
    assert _wait(timeout_s=60) is False
    assert fake.calls == 6  # a try every 10 s until 60 s have passed
    assert fake.closed


def test_each_try_is_bounded(driver):
    """No inner retries, and a short connection timeout, so --wait N can't overrun by much."""
    fake = driver(_Starting(failures=0))
    _wait(timeout_s=60)
    assert fake.options["connection_timeout"] <= 10
    assert fake.options["connection_acquisition_timeout"] <= 10


def test_a_refused_login_counts_as_answering_and_is_not_waited_out(driver):
    fake = driver(_Starting(failures=0, error=AuthError("unauthorized")))
    assert _wait(timeout_s=60) is True
    assert fake.calls == 1
