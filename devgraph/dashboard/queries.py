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

# Same label set search_component (mcp/tools.py) searches over -- these are
# the "nameable component" labels a human would plausibly type into a
# search box, as opposed to e.g. Commit/PullRequest/Requirement rows.
_SEARCHABLE_LABELS = "n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint"


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
    label_filter = f"AND n:{label}" if label else ""
    node_rows = engine.run_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) "
        f"WHERE true {label_filter} "
        "RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name "
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
    engine: GraphEngine, repo_id: str, query: str, max_results: int
) -> list[dict[str, Any]]:
    """Lightweight rows for the node browser's search box.

    Same match logic `mcp.tools.search_component` uses (name/description
    substring over the same label set), trimmed to the {id, name, label,
    file} shape the dashboard actually renders.
    """
    rows = engine.run_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) "
        f"WHERE ({_SEARCHABLE_LABELS}) "
        "AND (toLower(n.name) CONTAINS toLower($query) "
        "OR toLower(n.description) CONTAINS toLower($query)) "
        "RETURN elementId(n) AS id, n.name AS name, labels(n)[0] AS label, "
        "coalesce(n.source_file, n.file, n.source) AS file "
        "LIMIT $limit",
        {"repo_id": repo_id, "query": query, "limit": max_results},
    )
    return rows
