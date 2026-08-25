"""Docs extractor for Phase 2: Requirement / DesignDecision / ArchitectureNote.

Parses Markdown files under a repo's configured docs path. Each file is a
single note, identified by YAML front-matter:

    ---
    type: requirement | design_decision | architecture_note
    id: unique-slug
    links: [ModuleOrServiceName, ...]   # optional, what this note documents
    supersedes: other-note-id           # optional, design_decision only
    ---
    # Title
    Body text...

Files without a `type` front-matter field are skipped (not every Markdown
file under a docs/ folder is necessarily a DevGraph note). `id` defaults to
the filename stem if omitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_TYPE_TO_LABEL = {
    "requirement": "Requirement",
    "design_decision": "DesignDecision",
    "architecture_note": "ArchitectureNote",
}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass
class DocNode:
    """A Requirement / DesignDecision / ArchitectureNote node."""

    label: str
    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """A relationship linking a doc node to code or to another doc node."""

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str


@dataclass
class ExtractionResult:
    """Result of docs extraction."""

    docs: list[DocNode] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)


class DocsExtractor:
    """Extracts Requirement/DesignDecision/ArchitectureNote notes from Markdown."""

    def __init__(self, repo_id: str) -> None:
        self.repo_id = repo_id

    def extract_from_source(self, content: str, filename: str = "unknown.md") -> ExtractionResult:
        """Extract a single doc node (and its links) from one Markdown file's content."""
        result = ExtractionResult()

        match = _FRONTMATTER_RE.match(content)
        if not match:
            return result

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return result
        if not isinstance(meta, dict):
            return result

        doc_type = meta.get("type")
        label = _TYPE_TO_LABEL.get(doc_type)
        if label is None:
            return result

        note_id = str(meta.get("id") or Path(filename).stem)
        body = match.group(2).strip()

        properties = {
            "source_file": filename,
            "title": _first_heading(body) or note_id,
            "body": body,
        }

        result.docs.append(DocNode(label=label, name=note_id, repo_id=self.repo_id, properties=properties))

        for target in _as_list(meta.get("links")):
            result.relationships.append(
                Relationship(
                    source_label="Module",
                    source_name=str(target),
                    relationship_type=_LINK_REL[label],
                    target_label=label,
                    target_name=note_id,
                )
            )

        supersedes = meta.get("supersedes")
        if supersedes and label == "DesignDecision":
            result.relationships.append(
                Relationship(
                    source_label="DesignDecision",
                    source_name=note_id,
                    relationship_type="SUPERSEDES",
                    target_label="DesignDecision",
                    target_name=str(supersedes),
                )
            )

        decided_by = meta.get("decided_by")
        if decided_by and label == "DesignDecision":
            result.relationships.append(
                Relationship(
                    source_label="DesignDecision",
                    source_name=note_id,
                    relationship_type="DECIDED_BY",
                    target_label="ArchitectureNote",
                    target_name=str(decided_by),
                )
            )

        return result


# Requirement notes are SATISFIED_BY a module (Module -[:SATISFIES]-> Requirement);
# ArchitectureNote/DesignDecision notes DOCUMENT a module (Module -[:DOCUMENTED_BY]-> note).
_LINK_REL = {
    "Requirement": "SATISFIES",
    "DesignDecision": "DOCUMENTED_BY",
    "ArchitectureNote": "DOCUMENTED_BY",
}


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return None


def index_file(engine, repo_id: str, file_path: str | Path) -> None:
    """Extract a docs Markdown file and upsert results into the graph.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Markdown file to index.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")
    result = DocsExtractor(repo_id).extract_from_source(content, file_path.name)

    for doc in result.docs:
        engine.upsert_node(doc.label, doc.repo_id, doc.name, doc.properties)

    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label,
            rel.source_name,
            rel.relationship_type,
            rel.target_label,
            rel.target_name,
            repo_id,
        )
