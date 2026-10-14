"""Shared checks and fixtures for the live watcher scenarios (not a test module itself).

`graph_snapshot` is the plan's comparison projection: every node as
(sorted labels, name, its file, a hash of its non-volatile properties) and
every edge as (label, name, file, type, label, name, file, origins, its other properties), leaving out `Commit` and
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
import uuid
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
#: (staged by the git-history sync), timestamps and computed insights.
_VOLATILE = {"repo_id", "last_modified_by"}

#: How long the live graph must stay equal before it counts as settled.
SETTLED_S = 1.0


#: A live test's repository older than this is from a run killed before its
#: teardown (no live test runs this long), and the next run's fixture sweeps it.
STALE_LIVE_REPO_S = 3600


def live_repo_id(prefix: str) -> str:
    """`<prefix>-<epoch seconds>-<random>`: unique, and dated for `sweep_stale_live_repos`."""
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def sweep_stale_live_repos(engine, prefix: str, max_age_s: float = STALE_LIVE_REPO_S) -> list[str]:
    """Delete the `live_repo_id(prefix)` repositories (and their `_fresh`/`-fresh`
    copies) made more than `max_age_s` ago, then release the generated
    constraints of the labels they recorded. Returns the ids deleted.

    Teardown deletes a test's repository, but a killed run never reaches it,
    and the leftovers sat in the shared Neo4j for `devgraph doctor` to report.
    A run still in progress is never touched: its ids are younger.
    """
    from devgraph.indexer.schema_constraints import release_labels

    rows = engine.run_cypher(
        "MATCH (r:Repository) WHERE r.repo_id STARTS WITH $p RETURN r.repo_id AS id, r.schema_labels AS labels",
        {"p": f"{prefix}-"},
    )
    now, swept, labels = time.time(), [], set()
    for row in rows:
        stamp = row["id"][len(prefix) + 1:].split("-", 1)[0]
        if stamp.isdigit() and now - int(stamp) > max_age_s:
            engine.delete_repository(row["id"])
            swept.append(row["id"])
            labels.update(row["labels"] or [])
    if labels:
        release_labels(engine, labels)
    return swept


def _props_hash(props: dict) -> str:
    kept = {
        k: v for k, v in props.items()
        if k not in _VOLATILE and not k.endswith("_at") and not k.startswith("insight_")
    }
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
        "RETURN labels(a)[0] AS a, a.name AS an, coalesce(a.file, a.source_file, a.path, '') AS af, "
        "type(x) AS t, labels(b)[0] AS b, b.name AS bn, coalesce(b.file, b.source_file, b.path, '') AS bf, "
        "coalesce(x.origins, []) AS o, properties(x) AS p",
        {"r": repo_id},
    )
    return (
        sorted(
            (tuple(sorted(n["labels"])), n["name"] or "", n["file"], _props_hash(n["props"])) for n in nodes
        ),
        sorted(
            (
                e["a"], e["an"] or "", e["af"], e["t"], e["b"], e["bn"] or "", e["bf"], tuple(e["o"]),
                tuple(sorted((k, v) for k, v in e["p"].items() if k != "origins")),
            )
            for e in edges
        ),
    )


def fresh_snapshot(
    engine, repo_id: str, root: Path, mentions_enabled: bool = False, docs_path: str | None = None
) -> tuple[list, list]:
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
            full_scan(engine, fresh, root, docs_path=docs_path, mentions_enabled=mentions_enabled)
        return graph_snapshot(engine, fresh)
    finally:
        engine.delete_repository(fresh)


def snapshot_diff(expected, actual) -> str:
    """A unified diff of two snapshots, the fresh scan's first."""
    return "\n".join(difflib.unified_diff(
        pprint.pformat(expected, width=160).splitlines(),
        pprint.pformat(actual, width=160).splitlines(),
        "fresh full_scan", "live graph", lineterm="",
    ))


def wait_until_equal(engine, repo_id: str, expected, timeout_s: float = 30) -> None:
    """Poll the live graph every 0.2 s until it has equalled `expected` for
    `SETTLED_S` (a batch or catch-up still running can pass through the
    expected graph on its way); fail with the diff."""
    deadline = time.monotonic() + timeout_s
    equal_since: float | None = None
    while True:
        actual = graph_snapshot(engine, repo_id)
        if actual == expected:
            equal_since = equal_since or time.monotonic()
            if time.monotonic() - equal_since >= SETTLED_S:
                return
        else:
            equal_since = None
        if time.monotonic() > deadline:
            pytest.fail(
                f"live graph did not match a fresh scan within {timeout_s} s:\n" + snapshot_diff(expected, actual)
            )
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
            headless, "EventBroadcaster", lambda: SimpleNamespace(publish=events.append, bind_loop=lambda loop: None, close=lambda: None)
        )
        agent = headless.HeadlessAgent()
        agent.events = events
        agent._watcher.start()
        self._running.append(agent)
        return agent

    def stop(self, agent) -> None:
        self._running.remove(agent)
        agent.stop()
        # The agent's shutdown is bounded: a query still running then is
        # abandoned, not waited for, and lands whenever it finishes. Wait for
        # it here (the engine already refuses new sessions), so the caller's
        # cleanup deletes the repository after its last write, not before.
        agent._engine.close(timeout=60)

    def stop_all(self) -> None:
        for agent in list(self._running):
            self.stop(agent)


@pytest.fixture
def live_agent(tmp_path, monkeypatch):
    agents = LiveAgents(tmp_path / "state" / "registry.sqlite3", tmp_path / "state" / "devgraph.log", monkeypatch)
    yield agents
    agents.stop_all()


def recency_snapshot(engine, repo_id: str) -> tuple[list, list]:
    """Every Module's git recency, and every Commit -[:MODIFIES]-> Module edge."""
    modules = engine.run_cypher(
        "MATCH (m:Module {repo_id: $r}) "
        "RETURN m.name AS n, m.created_at AS c, m.last_modified_at AS l, m.last_modified_by AS b",
        {"r": repo_id},
    )
    edges = engine.run_cypher(
        "MATCH (c:Commit {repo_id: $r})-[:MODIFIES]->(m:Module {repo_id: $r}) RETURN c.name AS c, m.name AS m",
        {"r": repo_id},
    )
    return (
        sorted((m["n"], m["c"], m["l"], m["b"]) for m in modules),
        sorted((e["c"], e["m"]) for e in edges),
    )


def fresh_recency(engine, repo_id: str, root: Path, tmp_path: Path) -> tuple[list, list]:
    """`recency_snapshot` of a fresh `full_scan` plus an initial git-history
    sync of `root`, under `<repo_id>-fresh` in a registry of its own."""
    from devgraph.indexer.git_history.extractor import sync_git_history
    from devgraph.registry.store import RepoRegistry

    registry = RepoRegistry(tmp_path / f"fresh-{time.monotonic_ns()}.sqlite3")
    fresh = registry.add_repo(root, repo_id=f"{repo_id}-fresh").repo_id
    engine.delete_repository(fresh)
    try:
        provision_repository_schema(engine, root)
        engine.upsert_repository(fresh, fresh, str(root))
        full_scan(engine, fresh, root)
        sync_git_history(engine, registry, fresh)
        return recency_snapshot(engine, fresh)
    finally:
        registry.close()
        engine.delete_repository(fresh)
