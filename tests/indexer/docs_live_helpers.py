"""Shared checks and fixtures for the docs-provider tests (not a test module itself)."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from devgraph.graph.schema import RELATIONSHIP_TYPES
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.providers import docs, docs_cache


def _uncached(root, rel, path):
    return docs.read_front_matter(path)


def docs_graph(engine, repo_id):
    """The docs part of one repository's graph: every docs node's (label, name, path)
    and every docs edge, with the target's file for file-scoped targets."""
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'docs' "
        "RETURN labels(n)[0] AS label, n.name AS name, n.path AS path",
        {"r": repo_id},
    )
    edges = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "WHERE a.extractor = 'docs' AND NOT type(x) IN $builtin "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn, "
        "coalesce(b.file, '') AS bf",
        {"r": repo_id, "builtin": list(RELATIONSHIP_TYPES)},
    )
    return (
        sorted((n["label"], n["name"], n["path"]) for n in nodes),
        sorted((e["a"], e["an"], e["t"], e["b"], e["bn"], e["bf"]) for e in edges),
    )


def assert_matches_fresh_apply(engine, repo_id, repo_root):
    """The docs graph a sequence of events left equals a fresh full apply of the same files.

    The fresh apply runs under a second repo_id over the same root, which is
    deleted again afterwards. It reads every file uncached, and leaves the
    cache of the root under test as it was (its `forget` is a no-op).
    """
    after_events = docs_graph(engine, repo_id)
    fresh = f"{repo_id}_fresh"
    engine.delete_repository(fresh)
    try:
        engine.upsert_repository(fresh, fresh, str(repo_root))
        with (
            patch.object(docs_cache, "read", _uncached),
            patch.object(docs_cache, "read_fresh", _uncached),
            patch.object(docs_cache, "forget", lambda root: None),
        ):
            full_scan(engine, fresh, repo_root)
        assert after_events == docs_graph(engine, fresh)
    finally:
        engine.delete_repository(fresh)


def _forget_every_root():
    with docs_cache._lock:
        roots = {root for root, _ in docs_cache._store}
    for root in roots:
        docs_cache.forget(Path(root))


@pytest.fixture
def cache_on(monkeypatch):
    """Every file looks at least 10 s old, so the docs read cache stores successful parses."""
    monkeypatch.setattr(docs_cache, "_clock", lambda: time.time_ns() + 10_000_000_000)
    yield
    _forget_every_root()


@pytest.fixture
def cache_off(monkeypatch):
    """Every docs read goes straight to `docs.read_front_matter`."""
    monkeypatch.setattr(docs_cache, "read", _uncached)
    monkeypatch.setattr(docs_cache, "read_fresh", _uncached)


def wait_ctime_advance(path, before_ns=None):
    """Touch a probe beside `path` until its ctime passes `before_ns` (default: `path`'s
    own ctime), so the next write to `path` gets a later ctime."""
    if before_ns is None:
        before_ns = os.stat(path).st_ctime_ns
    probe = Path(path).parent / ".ctime-probe"
    try:
        while True:
            probe.write_bytes(b"x")
            if os.stat(probe).st_ctime_ns > before_ns:
                return
    finally:
        probe.unlink(missing_ok=True)
