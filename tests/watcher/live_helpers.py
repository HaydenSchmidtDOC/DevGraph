"""Shared checks and fixtures for the live watcher scenarios (not a test module itself).

`graph_snapshot` is the plan's comparison projection: every node as
(sorted labels, name, its file, a hash of its non-volatile properties) and
every edge as (label, name, type, label, name), leaving out `Commit` and
`Repository` nodes and their edges. `fresh_snapshot` is the same projection of
a fresh `full_scan` of the same files, and `wait_until_equal` polls the live
graph until it matches.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import pprint
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from devgraph.config.settings import Settings
from devgraph.graph.engine import provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.providers import docs_cache
from tests.indexer.docs_live_helpers import _uncached

NEO4J = {"neo4j_uri": "bolt://127.0.0.1:7687", "neo4j_user": "neo4j", "neo4j_password": "devgraph-local-dev"}

#: Properties a fresh scan can't reproduce: the repository id, git recency
#: (staged by the git-history sync) and timestamps.
_VOLATILE = {"repo_id", "last_modified_by"}


def _props_hash(props: dict) -> str:
    kept = {k: v for k, v in props.items() if k not in _VOLATILE and not k.endswith("_at")}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:16]


def graph_snapshot(engine, repo_id: str) -> tuple[list, list]:
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE NOT n:Commit AND NOT n:Repository "
        "RETURN labels(n) AS labels, n.name AS name, "
        "coalesce(n.source_file, n.file, n.path, '') AS file, properties(n) AS props",
        {"r": repo_id},
    )
    edges = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "WHERE NOT a:Commit AND NOT a:Repository AND NOT b:Commit AND NOT b:Repository "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": repo_id},
    )
    return (
        sorted(
            (tuple(sorted(n["labels"])), n["name"] or "", n["file"], _props_hash(n["props"])) for n in nodes
        ),
        sorted((e["a"], e["an"] or "", e["t"], e["b"], e["bn"] or "") for e in edges),
    )


def fresh_snapshot(engine, repo_id: str, root: Path) -> tuple[list, list]:
    """The snapshot of a fresh `full_scan` of `root` under `<repo_id>_fresh`,
    read uncached, which is deleted again afterwards."""
    fresh = f"{repo_id}_fresh"
    engine.delete_repository(fresh)
    try:
        provision_repository_schema(engine, root)
        engine.upsert_repository(fresh, fresh, str(root))
        with (
            patch.object(docs_cache, "read", _uncached),
            patch.object(docs_cache, "read_fresh", _uncached),
            patch.object(docs_cache, "forget", lambda root: None),
        ):
            full_scan(engine, fresh, root)
        return graph_snapshot(engine, fresh)
    finally:
        engine.delete_repository(fresh)


def wait_until_equal(engine, repo_id: str, expected, timeout_s: float = 30) -> None:
    """Poll the live graph every 0.2 s until it equals `expected`; fail with the diff."""
    deadline = time.monotonic() + timeout_s
    while True:
        actual = graph_snapshot(engine, repo_id)
        if actual == expected:
            return
        if time.monotonic() > deadline:
            diff = difflib.unified_diff(
                pprint.pformat(expected, width=160).splitlines(),
                pprint.pformat(actual, width=160).splitlines(),
                "fresh full_scan", "live graph", lineterm="",
            )
            pytest.fail(f"live graph did not match a fresh scan within {timeout_s} s:\n" + "\n".join(diff))
        time.sleep(0.2)


class LiveAgents:
    """Starts `HeadlessAgent`s on one registry: only the watcher runs (no
    dashboard, no insights or schema-rescan schedulers), and every event the
    agent publishes lands in its `events` list."""

    def __init__(self, registry_path: Path, log_path: Path, monkeypatch) -> None:
        self.registry_path = registry_path
        self._settings = Settings(
            _env_file=None, registry_db_path=registry_path, dashboard_enabled=False, log_file=log_path, **NEO4J
        )
        self._monkeypatch = monkeypatch
        self._running: list = []

    def start(self):
        from devgraph.agent import headless

        events: list[dict] = []
        self._monkeypatch.setattr(headless, "get_settings", lambda: self._settings)
        self._monkeypatch.setattr(
            headless, "EventBroadcaster", lambda: SimpleNamespace(publish=events.append, bind_loop=lambda loop: None)
        )
        agent = headless.HeadlessAgent()
        agent.events = events
        agent._watcher.start()
        self._running.append(agent)
        return agent

    def stop(self, agent) -> None:
        self._running.remove(agent)
        agent.stop()

    def stop_all(self) -> None:
        for agent in list(self._running):
            self.stop(agent)


@pytest.fixture
def live_agent(tmp_path, monkeypatch):
    agents = LiveAgents(tmp_path / "state" / "registry.sqlite3", tmp_path / "state" / "devgraph.log", monkeypatch)
    yield agents
    agents.stop_all()
