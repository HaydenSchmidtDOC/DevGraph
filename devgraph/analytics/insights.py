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

import json
import logging
import posixpath
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)

# Edges that mean "A depends on B", directed as stored: A CALLS B gives B
# importance. The cycle finder's dependency set plus IMPLEMENTS (an
# interface its implementations lean on).
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


def refresh_insights(engine: Any, repo_id: str, *, blocking: bool = True) -> dict[str, Any] | None:
    """Load, compute and store one repository's insights.

    Serialized per repository within this process (the agent's scheduler and
    the dashboard share one). With `blocking=False`, returns None instead of
    waiting when a run is already in progress.
    """
    lock = _repo_lock(repo_id)
    if not lock.acquire(blocking=blocking):
        return None
    try:
        # Stamped before loading so an index finishing mid-run is not marked covered.
        started = datetime.now(timezone.utc).isoformat()
        nodes, edges = engine.load_insight_graph(repo_id, COMMUNITY_RELATIONSHIPS)
        result = compute_insights(nodes, edges)
        stored = result.communities[:_MAX_STORED_COMMUNITIES]
        summary = {
            "computed_at": started,
            "node_count": result.node_count,
            "community_count": len(result.communities),
            "modularity": result.modularity,
            "communities": json.dumps(stored),
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


def top_nodes(engine: Any, repo_id: str, metric: str, limit: int) -> list[dict[str, Any]]:
    """Highest-scoring nodes for one of INSIGHT_METRICS (validated by callers).

    The property name comes from the INSIGHT_METRICS allow-list, never from
    caller input, so it is the only thing interpolated into the query.
    """
    prop = INSIGHT_METRICS[metric]
    return engine.run_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) WHERE n.{prop} IS NOT NULL "
        f"RETURN n.name AS name, labels(n) AS labels, n.file AS file, n.{prop} AS score, "
        f"n.insight_community AS community ORDER BY score DESC, name LIMIT $limit",
        {"repo_id": repo_id, "limit": limit},
    )


def community_members(
    engine: Any, repo_id: str, communities: list[int], per_community: int
) -> dict[int, list[dict[str, Any]]]:
    """Each community's top members by PageRank (containers without a score last)."""
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id}) WHERE n.insight_community IN $communities "
        "WITH n ORDER BY coalesce(n.insight_pagerank, -1.0) DESC, n.name "
        "WITH n.insight_community AS community, "
        "collect({name: n.name, labels: labels(n), file: n.file, pagerank: n.insight_pagerank})[..$k] AS members "
        "RETURN community, members",
        {"repo_id": repo_id, "communities": communities, "k": per_community},
    )
    return {row["community"]: row["members"] for row in rows}


# ── scheduling ───────────────────────────────────────────────────────────

_SCHEDULER_INTERVAL_S = 30.0


class InsightsScheduler:
    """Keeps every active repository's insights no older than its last index.

    One rule covers every way a repository gets indexed -- watcher batches in
    this agent, CLI rescans, dashboard registrations -- because they all
    stamp `last_indexed` in the registry. The first pass runs as soon as the
    thread starts, which also backfills repositories never computed.
    """

    def __init__(
        self,
        engine: Any,
        registry: Any,
        on_refreshed: Callable[[str], None] | None = None,
        *,
        interval_s: float = _SCHEDULER_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._on_refreshed = on_refreshed
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def is_stale(self, repo: Any) -> bool:
        if not repo.last_indexed:
            return False
        summary = self._engine.read_insights_summary(repo.repo_id)
        # Both are UTC ISO-8601 strings from datetime.isoformat(), so they
        # order correctly as strings.
        return summary is None or (summary.get("computed_at") or "") < repo.last_indexed

    def run_once(self) -> list[str]:
        refreshed: list[str] = []
        for repo in self._registry.list_repos(active_only=True):
            if self._stop.is_set():
                break
            try:
                if not self.is_stale(repo):
                    continue
                if refresh_insights(self._engine, repo.repo_id, blocking=False) is None:
                    continue  # another caller is computing it right now
            except Exception:
                logger.warning("graph insights refresh failed for %s", repo.repo_id, exc_info=True)
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

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # A refresh blocked on a slow database can outlast this; the
            # thread is a daemon, so shutdown is never held hostage.
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("graph insights pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
