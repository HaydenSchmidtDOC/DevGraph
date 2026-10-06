"""The DEVGRAPH_REQUIRE_NEO4J hook turns Neo4j-related skips into failures."""

from types import SimpleNamespace

from tests.conftest import _fail_neo4j_skip


def _skip(reason):
    return SimpleNamespace(skipped=True, outcome="skipped", longrepr=("f.py", 1, f"Skipped: {reason}"))


def test_neo4j_skip_becomes_a_failure_when_required():
    report = _skip("Neo4j not available: auth error")
    _fail_neo4j_skip(report, True)
    assert report.outcome == "failed" and "auth error" in report.longrepr


def test_neo4j_skip_is_kept_when_not_required():
    report = _skip("Neo4j not available")
    _fail_neo4j_skip(report, False)
    assert report.outcome == "skipped"


def test_unrelated_skip_is_kept_when_required():
    report = _skip("POSIX only")
    _fail_neo4j_skip(report, True)
    assert report.outcome == "skipped"
