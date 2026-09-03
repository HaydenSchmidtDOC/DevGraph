"""Mentions extractor for Phase 2: Document nodes with MENTIONS edges.

Scans every .md/.markdown file in a repo (not limited to a docs path) and
detects syntactically-plausible references to existing entities. Creates a
Document node and MENTIONS relationships for each entity referenced in
code-like contexts (inline code, fenced blocks, call syntax, declaration syntax).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DECLARATION_KEYWORDS = (
    "class",
    "struct",
    "interface",
    "def",
    "function",
    "void",
    "int",
    "string",
    "bool",
    "const",
    "let",
    "var",
)


@dataclass
class DocumentNode:
    """A Document node representing a Markdown file."""

    label: str = "Document"
    name: str = ""
    repo_id: str = ""
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """A relationship linking entities in the graph."""

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str


@dataclass
class ExtractionResult:
    """Result of mentions extraction."""

    documents: list[DocumentNode] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)


class MentionsExtractor:
    """Extracts Document nodes and MENTIONS relationships from Markdown."""

    def __init__(self, repo_id: str, ambiguous_mode: str = "all") -> None:
        self.repo_id = repo_id
        self.ambiguous_mode = ambiguous_mode

    def extract_from_source(
        self, content: str, filename: str, known_entities: list[tuple[str, str]]
    ) -> ExtractionResult:
        """Extract a Document node and MENTIONS relationships from Markdown content.

        Args:
            content: The Markdown file content.
            filename: The repo-relative file path (used as node name).
            known_entities: List of (name, label) tuples of entities to match.
            Returns: ExtractionResult with Document node and MENTIONS relationships.
        """
        # Validate ambiguous_mode and fall back to "all" if unrecognized
        if self.ambiguous_mode not in ("all", "skip"):
            logger.warning(
                f"unrecognized ambiguous_mode '{self.ambiguous_mode}', falling back to 'all'"
            )
            self.ambiguous_mode = "all"

        result = ExtractionResult()

        # Parse the title from the first H1 heading or use filename stem
        title = _first_heading(content) or Path(filename).stem

        # Create Document node
        doc_node = DocumentNode(
            label="Document",
            name=filename,
            repo_id=self.repo_id,
            properties={
                "source_file": filename,
                "title": title,
            },
        )
        result.documents.append(doc_node)

        # Parse code contexts: inline code spans and fenced blocks
        code_regions = _parse_code_regions(content)

        # Build a map of entity names to their labels for collision detection
        entity_map: dict[str, list[str]] = {}
        for name, label in known_entities:
            if name not in entity_map:
                entity_map[name] = []
            entity_map[name].append(label)

        # Track which entities we've already linked (to avoid duplicates)
        linked_entities = set()

        # Check each known entity name
        for name, label in known_entities:
            if name in linked_entities:
                continue

            # Check for matches in the content
            matches = _find_matches(content, name, code_regions)

            if not matches:
                continue

            # Handle collisions
            all_labels_for_name = entity_map[name]
            if len(all_labels_for_name) > 1 and self.ambiguous_mode == "skip":
                # Skip ambiguous names in skip mode
                linked_entities.add(name)
                continue

            # Create MENTIONS relationship(s)
            if self.ambiguous_mode == "all":
                # Link to all entities with this name (across all labels)
                for target_label in all_labels_for_name:
                    result.relationships.append(
                        Relationship(
                            source_label="Document",
                            source_name=filename,
                            relationship_type="MENTIONS",
                            target_label=target_label,
                            target_name=name,
                        )
                    )
            else:
                # Link to the single entity with this label
                result.relationships.append(
                    Relationship(
                        source_label="Document",
                        source_name=filename,
                        relationship_type="MENTIONS",
                        target_label=label,
                        target_name=name,
                    )
                )

            linked_entities.add(name)

        return result


def _first_heading(content: str) -> str | None:
    """Extract the first H1 heading from Markdown content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            return stripped.lstrip("#").strip()
    return None


def _parse_code_regions(content: str) -> list[tuple[int, int]]:
    """Parse code regions (inline code and fenced blocks) and return character offset ranges.

    Returns: List of (start, end) character offset tuples for code regions.
    """
    regions: list[tuple[int, int]] = []
    lines = content.split("\n")
    char_offset = 0
    in_fence = False
    fence_char = None

    for line in lines:
        line_start = char_offset

        # Check for fence markers (``` or ~~~)
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            fence_marker = "```" if "```" in line else "~~~"
            if not in_fence:
                in_fence = True
                fence_char = fence_marker
                # The entire fenced block line is code
                regions.append((line_start, char_offset + len(line)))
            elif fence_marker == fence_char:
                in_fence = False
                fence_char = None
                # The fence closing line is code
                regions.append((line_start, char_offset + len(line)))
            else:
                # Different fence marker inside a fence, treat as content
                if in_fence:
                    regions.append((line_start, char_offset + len(line)))
        elif in_fence:
            # Inside a fenced block, the entire line is code
            regions.append((line_start, char_offset + len(line)))
        else:
            # Not in a fence, look for inline code spans
            inline_regions = _parse_inline_code(line)
            for start, end in inline_regions:
                regions.append((line_start + start, line_start + end))

        char_offset += len(line) + 1  # +1 for newline

    return regions


def _parse_inline_code(line: str) -> list[tuple[int, int]]:
    """Parse inline code spans (backtick-delimited) in a single line.

    Returns: List of (start, end) character offset tuples within the line.
    """
    regions = []
    i = 0
    while i < len(line):
        if line[i] == "`":
            # Find the closing backtick
            start = i
            i += 1
            while i < len(line) and line[i] != "`":
                if line[i] == "\\":
                    i += 2  # Skip escaped character
                else:
                    i += 1
            if i < len(line):
                regions.append((start, i + 1))
                i += 1
            else:
                # Unclosed backtick, treat rest of line as code
                regions.append((start, len(line)))
                break
        else:
            i += 1
    return regions


def _find_matches(content: str, name: str, code_regions: list[tuple[int, int]]) -> bool:
    """Check if a name appears in a code-like context in the content.

    Returns: True if the name is found in a code span, call syntax, or declaration syntax.
    """
    # Check code spans
    if _find_in_code_regions(content, name, code_regions):
        return True

    # Check call syntax: Name\s*\(
    call_pattern = r"\b" + re.escape(name) + r"\s*\("
    if re.search(call_pattern, content):
        return True

    # Check declaration syntax: keyword\s+Name\b (same line only, not across newlines)
    declaration_pattern = r"\b(" + "|".join(re.escape(kw) for kw in _DECLARATION_KEYWORDS) + r")\s+" + re.escape(name) + r"\b"
    for line in content.splitlines():
        if re.search(declaration_pattern, line):
            return True

    return False


def _find_in_code_regions(content: str, name: str, code_regions: list[tuple[int, int]]) -> bool:
    """Check if a name appears within any code region using word boundaries.

    Returns: True if the name is found in a code region with proper word boundaries.
    """
    # Build a pattern with word boundaries
    pattern = r"\b" + re.escape(name) + r"\b"

    for start, end in code_regions:
        region_text = content[start:end]
        if re.search(pattern, region_text):
            return True

    return False


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
    ambiguous_mode: str = "all",
) -> None:
    """Extract a mentions file and upsert results into the graph.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Markdown file to index.
        repo_root: The repository's root directory. When given, the Document node
            is keyed by file_path's path relative to repo_root (forward slashes),
            which prevents same-named files in different directories from colliding
            into one Document node. When omitted, falls back to the bare filename
            for backwards compatibility, but loses the collision-prevention benefit.
        ambiguous_mode: How to handle ambiguous names: "all" (link all) or "skip" (skip).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")

    # Compute the document name (repo-relative path or bare filename)
    if repo_root is not None:
        try:
            rel = file_path.resolve().relative_to(Path(repo_root).resolve())
            doc_name = rel.as_posix()
        except ValueError:
            # file_path wasn't actually under repo_root, fall back to bare filename
            doc_name = file_path.name
    else:
        doc_name = file_path.name

    # Query all entity names/labels in this repo
    query = """
    MATCH (n {repo_id: $repo_id})
    WHERE n.name IS NOT NULL
    RETURN DISTINCT n.name as name, labels(n)[0] as label
    """
    results = engine.run_cypher(query, {"repo_id": repo_id})
    known_entities = [(row["name"], row["label"]) for row in results]

    # Extract mentions
    extractor = MentionsExtractor(repo_id, ambiguous_mode=ambiguous_mode)
    result = extractor.extract_from_source(content, doc_name, known_entities)

    # Upsert Document node
    engine.upsert_nodes(
        [
            {
                "label": doc.label,
                "repo_id": doc.repo_id,
                "name": doc.name,
                "properties": doc.properties,
            }
            for doc in result.documents
        ]
    )

    # Upsert relationships
    engine.upsert_relationships(
        [
            {
                "from_label": rel.source_label,
                "from_name": rel.source_name,
                "rel_type": rel.relationship_type,
                "to_label": rel.target_label,
                "to_name": rel.target_name,
                "repo_id": repo_id,
                "properties": {},
            }
            for rel in result.relationships
        ]
    )
