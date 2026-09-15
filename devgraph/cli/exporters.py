"""Graph data serializers for `devgraph export`.

Each format serializer takes the same raw node/edge lists (from
`dashboard/queries.py`'s `graph_slice` or a direct Cypher query) and
returns a string in the requested format.
"""

from __future__ import annotations

import json
from typing import Any


def export_json(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Export as a JSON object with 'nodes' and 'edges' arrays.

    Each node: {id, label, name, file, ...}
    Each edge: {source, target, rel_type}
    """
    return json.dumps({"nodes": nodes, "edges": edges}, indent=2, default=str)


def export_cypher(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Export as Cypher CREATE statements.

    Generates CREATE (n:Label {prop: val}) for each node and
    MATCH (a), (b) CREATE (a)-[:TYPE]->(b) for each edge.
    """
    lines: list[str] = []
    for n in nodes:
        label = n.get("label", "Node")
        props = {k: v for k, v in n.items() if k not in ("id", "label") and v is not None}
        props_str = _format_props(props)
        lines.append(f"CREATE (:{label} {{id: {_quote(n.get('id', ''))}{props_str}}});")

    for e in edges:
        src = _quote(e.get("source", ""))
        tgt = _quote(e.get("target", ""))
        rel = e.get("rel_type", "RELATED_TO")
        lines.append(
            f"MATCH (a {{id: {src}}}), (b {{id: {tgt}}}) "
            f"CREATE (a)-[:{rel}]->(b);"
        )

    return "\n".join(lines)


def export_dot(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Export as Graphviz DOT format.

    Each node becomes a labeled vertex; each edge becomes a directed edge.
    """
    lines: list[str] = ["digraph DevGraph {"]
    for n in nodes:
        nid = _dot_id(n.get("id", ""))
        label = n.get("name") or n.get("label", "")
        lines.append(f"  {nid} [label={_quote(label)}];")

    for e in edges:
        src = _dot_id(e.get("source", ""))
        tgt = _dot_id(e.get("target", ""))
        rel = e.get("rel_type", "")
        lines.append(f"  {src} -> {tgt} [label={_quote(rel)}];")

    lines.append("}")
    return "\n".join(lines)


def _quote(s: str | int | float | bool) -> str:
    """Quote a string value for Cypher/DOT output."""
    if isinstance(s, str):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(s)


def _dot_id(s: str) -> str:
    """Sanitize a string for use as a DOT node ID."""
    if not s:
        return '""'
    sanitized = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in s)
    return sanitized if sanitized else '""'


def _format_props(props: dict[str, Any]) -> str:
    """Format a props dict as a Cypher property map suffix."""
    if not props:
        return ""
    items = []
    for k, v in props.items():
        if isinstance(v, str):
            items.append(f"{k}: {_quote(v)}")
        elif isinstance(v, bool):
            items.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            items.append(f"{k}: null")
        else:
            items.append(f"{k}: {v}")
    return ", " + ", ".join(items)