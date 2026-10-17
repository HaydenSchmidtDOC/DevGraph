"""Graph insights: communities, PageRank and betweenness for one repository.

Computed in Python over the already-indexed graph -- stock Neo4j Community has
no Graph Data Science plugin, and the only dependency this adds is networkx
(pure Python). PageRank is a small hand-written power iteration because
networkx's own implementation needs scipy.

Results are written back onto the graph (see `GraphEngine.write_insights`),
the one store both the agent/dashboard process and the separate MCP server
process already read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)

# Edges that mean "A depends on B", directed as stored: A CALLS B gives B
# importance. The cycle finder's dependency set plus IMPLEMENTS (an
# interface its implementations lean on). CALLS edges matched by name only or
# at package confidence, and IMPORTS edges at package confidence, in any
# language, are not loaded (see GraphEngine.load_insight_graph).
DEPENDENCY_RELATIONSHIPS: tuple[str, ...] = ("CALLS", "DEPENDS_ON", "EXTENDS", "IMPLEMENTS", "IMPORTS", "USES")
# Communities also follow containment, so a file's members stay together
# and cross-file dependencies are what join files into subsystems.
COMMUNITY_RELATIONSHIPS: tuple[str, ...] = DEPENDENCY_RELATIONSHIPS + ("CONTAINS",)

_PAGERANK_DAMPING = 0.85
_PAGERANK_MAX_ITER = 200
_PAGERANK_TOLERANCE = 1e-10
# Exact betweenness is O(V*E); above this many nodes it is estimated from
# this many sampled sources, which keeps the ranking and bounds the cost.
_BETWEENNESS_SAMPLE = 256
_ROOT_LABEL = "(root)"
_LABEL_MAX_CHARS = 80


@dataclass
class InsightResult:
    node_rows: list[dict[str, Any]]
    communities: list[dict[str, Any]]
    modularity: float
    node_count: int


def pagerank(node_ids: list[str], edges: list[tuple[str, str]]) -> dict[str, float]:
    """PageRank by power iteration; dangling nodes spread their rank uniformly."""
    if not node_ids:
        return {}
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    out: list[list[int]] = [[] for _ in range(n)]
    for source, target in sorted(set(edges)):
        if source != target:
            out[index[source]].append(index[target])
    rank = [1.0 / n] * n
    for _ in range(_PAGERANK_MAX_ITER):
        dangling = sum(rank[i] for i in range(n) if not out[i])
        base = (1.0 - _PAGERANK_DAMPING) / n + _PAGERANK_DAMPING * dangling / n
        new = [base] * n
        for i in range(n):
            if out[i]:
                share = _PAGERANK_DAMPING * rank[i] / len(out[i])
                for j in out[i]:
                    new[j] += share
        delta = sum(abs(new[i] - rank[i]) for i in range(n))
        rank = new
        if delta < n * _PAGERANK_TOLERANCE:
            break
    return {nid: rank[index[nid]] for nid in node_ids}


def _community_label(members: list[dict[str, Any]], ranks: dict[str, float]) -> str:
    """Most common member directory, else the highest-PageRank member's name."""
    dirs = Counter(posixpath.dirname(m["file"]) or _ROOT_LABEL for m in members if m.get("file"))
    if dirs:
        top = max(dirs.values())
        label = min(d for d, count in dirs.items() if count == top)
    else:
        best = min(members, key=lambda m: (-ranks.get(m["id"], -1.0), m.get("name") or "", m["id"]))
        label = best.get("name") or best["id"]
    return label[:_LABEL_MAX_CHARS]


def compute_insights(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> InsightResult:
    """Communities over dependency+containment edges; PageRank and betweenness
    over dependency edges only. Inputs are sorted first so the same graph
    always gives the same answer, whatever order Neo4j returned it in."""
    by_id = {n["id"]: n for n in nodes}
    usable = sorted(
        (e["source"], e["target"], e["type"])
        for e in edges
        if e["type"] in COMMUNITY_RELATIONSHIPS
        and e["source"] in by_id
        and e["target"] in by_id
        and e["source"] != e["target"]
    )
    dependency = [(s, t) for s, t, kind in usable if kind in DEPENDENCY_RELATIONSHIPS]

    dep_nodes = sorted({nid for pair in dependency for nid in pair})
    ranks = pagerank(dep_nodes, dependency)
    dep_graph = nx.Graph()
    dep_graph.add_nodes_from(dep_nodes)
    dep_graph.add_edges_from(dependency)
    sample = _BETWEENNESS_SAMPLE if dep_graph.number_of_nodes() > _BETWEENNESS_SAMPLE else None
    betweenness = (
        nx.betweenness_centrality(dep_graph, k=sample, normalized=True, seed=0) if dep_nodes else {}
    )

    community_graph = nx.Graph()
    community_graph.add_edges_from((s, t) for s, t, _ in usable)
    if community_graph.number_of_edges():
        groups = nx.community.louvain_communities(community_graph, seed=0)
        modularity = float(nx.community.modularity(community_graph, groups))
    else:
        groups, modularity = [], 0.0
    ordered = sorted(
        (sorted(group) for group in groups),
        key=lambda members: (-len(members), min(by_id[m].get("name") or "" for m in members), members[0]),
    )

    community_of: dict[str, int] = {}
    communities: list[dict[str, Any]] = []
    for number, members in enumerate(ordered):
        for member in members:
            community_of[member] = number
        communities.append(
            {"community": number, "label": _community_label([by_id[m] for m in members], ranks), "size": len(members)}
        )

    scored = sorted(set(community_of) | set(ranks))
    node_rows = [
        {
            "id": nid,
            "community": community_of.get(nid),
            "pagerank": ranks.get(nid),
            "betweenness": betweenness.get(nid) if nid in ranks else None,
        }
        for nid in scored
    ]
    return InsightResult(node_rows, communities, modularity, len(scored))


# ── storage ──────────────────────────────────────────────────────────────

# The Repository node keeps the largest communities for listing; every node
# still carries its own community number.
_MAX_STORED_COMMUNITIES = 50

INSIGHT_METRICS: dict[str, str] = {"pagerank": "insight_pagerank", "betweenness": "insight_betweenness"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _repo_lock(repo_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(repo_id, threading.Lock())


def graph_fingerprint(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], community: dict[str, Any] | None = None
) -> str:
    """A digest of a loaded insight graph by node identity, not `elementId`
    (which changes when a file is re-indexed), with each node's stored
    community, or `community`'s for it when given (what a write just stored).
    Equal digests mean a recompute would store exactly what is stored now."""
    ident = {
        n["id"]: (
            sorted(n.get("labels") or []), n.get("name"), n.get("file"), n.get("owner"),
            community.get(n["id"]) if community is not None else n.get("community"),
        )
        for n in nodes
    }
    payload = json.dumps(
        [
            sorted(ident.values(), key=repr),
            sorted(((ident.get(e["source"]), e["type"], ident.get(e["target"])) for e in edges), key=repr),
        ],
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refresh_insights(
    engine: Any, repo_id: str, *, blocking: bool = True, unless_fingerprint: str | None = None
) -> dict[str, Any] | None:
    """Load, compute and store one repository's insights.

    Serialized per repository within this process (the agent's scheduler and
    the dashboard share one). With `blocking=False`, returns None instead of
    waiting when a run is already in progress.

    The summary stores the graph's `fingerprint` as written. When the loaded
    graph's fingerprint equals `unless_fingerprint`, nothing is computed or
    written and the result is `{"computed_at", "fingerprint", "unchanged": True}`.
    """
    lock = _repo_lock(repo_id)
    if not lock.acquire(blocking=blocking):
        return None
    try:
        # Stamped before loading so an index finishing mid-run is not marked covered.
        started = datetime.now(timezone.utc).isoformat()
        nodes, edges = engine.load_insight_graph(repo_id, COMMUNITY_RELATIONSHIPS)
        if unless_fingerprint is not None and graph_fingerprint(nodes, edges) == unless_fingerprint:
            return {"computed_at": started, "fingerprint": unless_fingerprint, "unchanged": True}
        result = compute_insights(nodes, edges)
        stored = result.communities[:_MAX_STORED_COMMUNITIES]
        summary = {
            "computed_at": started,
            "node_count": result.node_count,
            "community_count": len(result.communities),
            "modularity": result.modularity,
            "communities": json.dumps(stored),
            "fingerprint": graph_fingerprint(nodes, edges, {row["id"]: row["community"] for row in result.node_rows}),
        }
        engine.write_insights(repo_id, result.node_rows, summary)
        logger.info(
            "graph insights for %s: %d communities over %d nodes", repo_id, len(result.communities), result.node_count
        )
        return {**summary, "communities": stored}
    finally:
        lock.release()


def read_insights(engine: Any, repo_id: str) -> dict[str, Any] | None:
    """The stored summary with `communities` decoded, or None if never computed."""
    raw = engine.read_insights_summary(repo_id)
    if raw is None:
        return None
    try:
        communities = json.loads(raw.get("communities") or "[]")
    except ValueError:
        communities = []
    if not isinstance(communities, list):
        communities = []
    return {**raw, "communities": communities}


#: How long reading stored insights may take before Neo4j cancels it.
READ_TIMEOUT_S = 30


def top_nodes(
    engine: Any, repo_id: str, metric: str, limit: int, timeout_s: float = READ_TIMEOUT_S
) -> list[dict[str, Any]]:
    """Highest-scoring nodes for one of INSIGHT_METRICS (validated by callers).
    A read-only query bounded by `timeout_s`.

    The property name comes from the INSIGHT_METRICS allow-list, never from
    caller input, so it is the only thing interpolated into the query.
    """
    prop = INSIGHT_METRICS[metric]
    rows, _more = engine.run_read_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) WHERE n.{prop} IS NOT NULL "
        f"RETURN n.name AS name, labels(n) AS labels, n.file AS file, n.{prop} AS score, "
        f"n.insight_community AS community ORDER BY score DESC, name LIMIT $limit",
        {"repo_id": repo_id, "limit": limit},
        timeout_s=timeout_s,
        max_rows=max(1, limit),
    )
    return rows


def community_members(
    engine: Any, repo_id: str, communities: list[int], per_community: int, timeout_s: float = READ_TIMEOUT_S
) -> dict[int, list[dict[str, Any]]]:
    """Each community's top members by PageRank (containers without a score
    last). A read-only query bounded by `timeout_s`."""
    rows, _more = engine.run_read_cypher(
        "MATCH (n {repo_id: $repo_id}) WHERE n.insight_community IN $communities "
        "WITH n ORDER BY coalesce(n.insight_pagerank, -1.0) DESC, n.name "
        "WITH n.insight_community AS community, "
        "collect({name: n.name, labels: labels(n), file: n.file, pagerank: n.insight_pagerank})[..$k] AS members "
        "RETURN community, members",
        {"repo_id": repo_id, "communities": communities, "k": per_community},
        timeout_s=timeout_s,
        max_rows=max(1, len(communities)),
    )
    return {row["community"]: row["members"] for row in rows}


# ── scheduling ───────────────────────────────────────────────────────────

_SCHEDULER_INTERVAL_S = 30.0
# A repository's insights are recomputed once its indexing has been quiet for
# _SETTLE_S, or, while edits go on, once they are _MAX_LAG_S behind it. A run
# that took T is followed by none for _DURATION_FACTOR * T, so a repository
# whose refresh is slow is not recomputed continuously.
_SETTLE_S = 60.0
_MAX_LAG_S = 600.0
_DURATION_FACTOR = 5.0


def _seconds_since(now: datetime, stamp: str) -> float:
    at = datetime.fromisoformat(stamp)
    return (now - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))).total_seconds()


class InsightsScheduler:
    """Keeps every active repository's insights no older than its last index,
    within the timing above.

    One rule covers every way a repository gets indexed -- watcher batches in
    this agent, CLI rescans, dashboard registrations -- because they all
    stamp `last_indexed` in the registry. The first pass runs as soon as the
    thread starts, which also backfills repositories never computed.

    A save that changed nothing insights read (a comment, a docstring, a
    function body whose calls are the same) is found by the graph's
    fingerprint: the graph is loaded, but nothing is computed or written and
    no `insights_refreshed` is published.
    """

    def __init__(
        self,
        engine: Any,
        registry: Any,
        on_refreshed: Callable[[str], None] | None = None,
        *,
        interval_s: float = _SCHEDULER_INTERVAL_S,
        settle_s: float = _SETTLE_S,
        max_lag_s: float = _MAX_LAG_S,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._on_refreshed = on_refreshed
        self._interval_s = interval_s
        self._settle_s = settle_s
        self._max_lag_s = max_lag_s
        self._clock = clock
        self._monotonic = monotonic
        # When a run found the graph unchanged: covers `last_indexed` like `computed_at`.
        self._checked_at: dict[str, str] = {}
        self._not_before: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def _due(self, repo: Any) -> tuple[bool, str | None]:
        """Whether it is stale (insights older than its last index) and its
        edits have settled or the insights lag too far behind them; and the
        stored fingerprint."""
        if not repo.last_indexed or self._monotonic() < self._not_before.get(repo.repo_id, float("-inf")):
            return False, None
        summary = self._engine.read_insights_summary(repo.repo_id) or {}
        stamps = [s for s in (summary.get("computed_at"), self._checked_at.get(repo.repo_id)) if s]
        covered = max(stamps) if stamps else None
        # Both are UTC ISO-8601 strings from datetime.isoformat(), so they
        # order correctly as strings.
        if covered is not None and covered >= repo.last_indexed:
            return False, None
        now = self._clock()
        due = (
            _seconds_since(now, repo.last_indexed) >= self._settle_s
            or covered is None
            or _seconds_since(now, covered) >= self._max_lag_s
        )
        return due, summary.get("fingerprint")

    def run_once(self) -> list[str]:
        refreshed: list[str] = []
        for repo in self._registry.list_repos(active_only=True):
            if self._stop.is_set():
                break
            try:
                due, fingerprint = self._due(repo)
                if not due:
                    continue
                started = self._monotonic()
                result = refresh_insights(self._engine, repo.repo_id, blocking=False, unless_fingerprint=fingerprint)
                if result is None:
                    continue  # another caller is computing it right now
                finished = self._monotonic()
                self._not_before[repo.repo_id] = finished + _DURATION_FACTOR * (finished - started)
                if result.get("unchanged"):
                    self._checked_at[repo.repo_id] = result["computed_at"]
                    continue
            except Exception:
                # Interrupted by shutdown (the engine refuses new sessions
                # once it closes): not a failure worth a warning.
                logger.log(
                    logging.DEBUG if self._stop.is_set() else logging.WARNING,
                    "graph insights refresh failed for %s", repo.repo_id, exc_info=True,
                )
                continue
            refreshed.append(repo.repo_id)
            if self._on_refreshed is not None:
                try:
                    self._on_refreshed(repo.repo_id)
                except Exception:
                    logger.debug("insights_refreshed callback failed for %s", repo.repo_id, exc_info=True)
        return refreshed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-insights", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # A refresh blocked on a slow database can outlast this; the
            # thread is a daemon, so shutdown is never held hostage.
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("graph insights pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
