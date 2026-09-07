"""Shared graph-write dataclasses used by every per-language source extractor.

Split out of `devgraph/indexer/python/extractor.py` so the JS/TS, C#, C++,
Java, Rust, and Go extractors (Implementation Plan #8) share one definition
instead of each redeclaring the same three classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GraphNode:
    """A node to be upserted into the graph."""

    label: str
    repo_id: str
    name: str
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "repo_id": self.repo_id,
            "name": self.name,
            "properties": self.properties,
        }


@dataclass
class GraphRelationship:
    """A relationship to be upserted into the graph."""

    from_label: str
    from_name: str
    rel_type: str
    to_label: str
    to_name: str
    repo_id: str
    properties: dict | None = None

    def to_dict(self) -> dict:
        return {
            "from_label": self.from_label,
            "from_name": self.from_name,
            "rel_type": self.rel_type,
            "to_label": self.to_label,
            "to_name": self.to_name,
            "repo_id": self.repo_id,
            "properties": self.properties,
        }


@dataclass
class ExtractionResult:
    """Result of parsing a source file: nodes and relationships."""

    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }
