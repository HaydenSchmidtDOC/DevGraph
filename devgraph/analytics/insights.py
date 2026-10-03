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

import posixpath
from collections import Counter
from dataclasses import dataclass
from typing import Any

import networkx as nx

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
