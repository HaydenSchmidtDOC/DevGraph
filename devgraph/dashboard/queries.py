"""Dashboard-shaped read queries against `GraphEngine`.

Kept separate from `graph/engine.py` (general-purpose upsert/delete API) and
from `mcp/tools.py` (MCP tool response shapes) since these are read queries
built specifically for the dashboard's UI: a stat grid, a Cytoscape.js
node/edge payload, and a lightweight search-box row shape. Every query is
hard-scoped to `repo_id`, same discipline as the rest of the codebase (see
Design Brief Principle 3).
"""

from __future__ import annotations

from typing import Any

from devgraph.graph.engine import GraphEngine

def count_nodes(engine: GraphEngine, repo_id: str) -> int:
    """Cheap total node count for a repo, used by the `/api/repos` list."""
    results = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id}) RETURN count(n) AS count", {"repo_id": repo_id}
    )
    return results[0]["count"] if results else 0


def summary_counts(engine: GraphEngine, repo_id: str) -> dict[str, Any]:
    """Node counts grouped by label and relationship counts grouped by type.

    Unlike `mcp.tools.summarise_repository` (a fixed list of named counts
    for the MCP tool's response contract), this groups over whatever labels
    /types are actually present so the dashboard's stat grid doesn't need
    updating every time a new label is introduced.
    """
    node_rows = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id}) "
        "UNWIND labels(n) AS label "
        "RETURN label, count(*) AS count "
        "ORDER BY label",
        {"repo_id": repo_id},
    )
    rel_rows = engine.run_cypher(
        "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
        "RETURN type(r) AS type, count(*) AS count "
        "ORDER BY type",
        {"repo_id": repo_id},
    )
    return {
        "nodes_by_label": {row["label"]: row["count"] for row in node_rows},
        "relationships_by_type": {row["type"]: row["count"] for row in rel_rows},
    }


def node_counts_by_label(engine: GraphEngine, repo_ids: list[str]) -> dict[str, int]:
    """Node counts grouped by label across `repo_ids`; scans nodes only."""
    rows = engine.run_cypher(
        "MATCH (n) WHERE n.repo_id IN $ids "
        "UNWIND labels(n) AS label "
        "RETURN label, count(*) AS count",
        {"ids": repo_ids},
    )
    return {row["label"]: row["count"] for row in rows}


def graph_slice(
    engine: GraphEngine, repo_id: str, label: str | None, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Nodes + edges for the graph canvas, capped at `limit` nodes.

    Edges are fetched only between nodes already in the capped node set
    (rather than independently capped) so the canvas never shows a dangling
    edge to a node it wasn't given. Caller (routes.py) is responsible for
    validating `label` against the known label set before it reaches here --
    Cypher can't parameterize a label, so an unvalidated value must never be
    interpolated into the query string.
    """
    label_filter = f"AND n:`{label}`" if label else ""
    node_rows = engine.run_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) "
        f"WHERE true {label_filter} "
        "RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, n.file AS file "
        "LIMIT $limit",
        {"repo_id": repo_id, "limit": limit},
    )
    ids = [row["id"] for row in node_rows]
    if not ids:
        return node_rows, []

    edge_rows = engine.run_cypher(
        "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
        "WHERE elementId(a) IN $ids AND elementId(b) IN $ids "
        "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS rel_type "
        "LIMIT $limit",
        {"repo_id": repo_id, "ids": ids, "limit": limit},
    )
    return node_rows, edge_rows


def search_components(
    engine: GraphEngine,
    repo_id: str,
    query: str,
    max_results: int,
    project_labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Lightweight rows for the node browser's search box, best first.

    Ranked the way `mcp.tools.search_component` ranks (`ranked_search`: exact
    name, then name prefix, then substring, then description), over the same
    label set, for the whole query as one case-insensitive term, trimmed to
    the {id, name, label, file} shape the dashboard actually renders.
    `project_labels` are a repo's applied schema labels, searched in addition
    to the built-in set; the caller must take them from the applied schema
    (never from user input) since a label cannot be parameterized.
    """
    from devgraph.mcp.tools import _SEARCH_LABELS, _name_variants, ranked_search

    terms = [query.lower()]
    labels = _SEARCH_LABELS + tuple(label for label in project_labels or [] if label not in _SEARCH_LABELS)
    rows, _count, _lower_bound = ranked_search(
        engine, labels, terms, _name_variants(query, terms), max(1, max_results), repo_id=repo_id
    )
    return [{"id": r["id"], "name": r["name"], "label": r["labels"][0], "file": r["file"]} for r in rows]


def total_counts(engine: GraphEngine) -> dict[str, Any]:
    """Aggregate node and relationship counts across all repos.

    Returns the same shape as summary_counts but without a repo_id filter.
    """
    node_rows = engine.run_cypher(
        "MATCH (n) UNWIND labels(n) AS label "
        "RETURN label, count(*) AS count ORDER BY label"
    )
    rel_rows = engine.run_cypher(
        "MATCH (a)-[r]->(b) "
        "RETURN type(r) AS type, count(*) AS count ORDER BY type"
    )
    return {
        "nodes_by_label": {row["label"]: row["count"] for row in node_rows},
        "relationships_by_type": {row["type"]: row["count"] for row in rel_rows},
    }
