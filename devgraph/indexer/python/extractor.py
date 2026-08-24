"""AST-based Python source-code extractor.

Parses a Python file using ast module and extracts:
  - Module (the file itself)
  - Classes with their base classes (inheritance)
  - Functions (at module level and within classes)
  - Imports (from/import statements)
  - Decorators (applied to classes/functions)

Returns a structured result (list of dicts) describing nodes and relationships
that can be upserted into the graph via GraphEngine.upsert_node/upsert_relationship.
All nodes are keyed on (repo_id, name) for idempotent incremental indexing.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


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

    def to_dict(self) -> dict:
        return {
            "from_label": self.from_label,
            "from_name": self.from_name,
            "rel_type": self.rel_type,
            "to_label": self.to_label,
            "to_name": self.to_name,
            "repo_id": self.repo_id,
        }


@dataclass
class ExtractionResult:
    """Result of parsing a Python file: nodes and relationships."""

    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }


def _extract_decorator_names(decorators: list[ast.expr]) -> list[str]:
    """Extract decorator names from a list of ast.expr decorator nodes."""
    names = []
    for deco in decorators:
        if isinstance(deco, ast.Name):
            names.append(deco.id)
        elif isinstance(deco, ast.Attribute):
            # For @module.decorator, get the full attribute path
            names.append(ast.unparse(deco))
        elif isinstance(deco, ast.Call):
            # For @decorator(...), extract the function being called
            func = deco.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(ast.unparse(func))
    return names


def _extract_base_class_names(bases: list[ast.expr]) -> list[str]:
    """Extract base class names from a list of ast.expr base classes."""
    names = []
    for base in bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(ast.unparse(base))
    return names


def _extract_imports(tree: ast.AST) -> list[tuple[str, list[str]]]:
    """Extract import statements from the AST.

    Returns a list of (module_name, [imported_names]) tuples.
    For 'from X import Y', module_name='X' and imported_names=['Y'].
    For 'import X', module_name='X' and imported_names=['X'].
    """
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import X [as Y], import X.Y [as Z]
            for alias in node.names:
                imports.append((alias.name, [alias.asname or alias.name]))
        elif isinstance(node, ast.ImportFrom):
            # from X import Y [, Z]
            module_name = node.module or ""
            imported_names = []
            for alias in node.names:
                if alias.name == "*":
                    imported_names.append("*")
                else:
                    imported_names.append(alias.asname or alias.name)
            if module_name and imported_names:
                imports.append((module_name, imported_names))

    return imports


def extract_python_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Python file and extract nodes and relationships.

    Args:
        source_code: The Python source code as a string.
        file_path: Relative or absolute path to the file (for the Module node name).
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships.

    Raises:
        ValueError: If parsing fails due to syntax errors (caught and logged; doesn't crash).
    """
    result = ExtractionResult()

    try:
        # Parse the source code using the ast module.
        tree = ast.parse(source_code)
    except SyntaxError as e:
        logger.error(f"Failed to parse {file_path}: {e}")
        return result

    # Create a Module node for the file itself.
    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties={"type": "module", "source_file": file_path},
    )
    result.nodes.append(module_node)

    # Extract imports at the module level.
    imports = _extract_imports(tree)
    for module_name, imported_names in imports:
        # Create a relationship IMPORTS from Module to each imported module.
        for imp_name in imported_names:
            result.relationships.append(
                GraphRelationship(
                    from_label="Module",
                    from_name=file_path,
                    rel_type="IMPORTS",
                    to_label="Module",
                    to_name=module_name,
                    repo_id=repo_id,
                )
            )

    # Walk the AST to find classes and functions.
    def visit_node(node: ast.AST, parent_name: str | None = None):
        """Recursively visit AST nodes, extracting classes and functions."""

        if isinstance(node, ast.ClassDef):
            class_name = node.name
            decorators = _extract_decorator_names(node.decorator_list)
            base_classes = _extract_base_class_names(node.bases)

            # Create Class node.
            class_node = GraphNode(
                label="Class",
                repo_id=repo_id,
                name=class_name,
                properties={
                    "type": "class",
                    "decorators": decorators,
                    "file": file_path,
                },
            )
            result.nodes.append(class_node)

            # Create CONTAINS relationship from Module (or parent class) to this class.
            if parent_name:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Class",
                        from_name=parent_name,
                        rel_type="CONTAINS",
                        to_label="Class",
                        to_name=class_name,
                        repo_id=repo_id,
                    )
                )
            else:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Module",
                        from_name=file_path,
                        rel_type="CONTAINS",
                        to_label="Class",
                        to_name=class_name,
                        repo_id=repo_id,
                    )
                )

            # Create EXTENDS relationships for base classes.
            for base_class in base_classes:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Class",
                        from_name=class_name,
                        rel_type="EXTENDS",
                        to_label="Class",
                        to_name=base_class,
                        repo_id=repo_id,
                    )
                )

            # Visit nested classes and functions within this class.
            for child in node.body:
                visit_node(child, parent_name=class_name)

        elif isinstance(node, ast.FunctionDef):
            func_name = node.name
            decorators = _extract_decorator_names(node.decorator_list)

            # Create Function node.
            func_node = GraphNode(
                label="Function",
                repo_id=repo_id,
                name=func_name,
                properties={
                    "type": "function",
                    "decorators": decorators,
                    "file": file_path,
                },
            )
            result.nodes.append(func_node)

            # Create CONTAINS relationship from Module (or parent class) to this function.
            if parent_name:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Class",
                        from_name=parent_name,
                        rel_type="CONTAINS",
                        to_label="Function",
                        to_name=func_name,
                        repo_id=repo_id,
                    )
                )
            else:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Module",
                        from_name=file_path,
                        rel_type="CONTAINS",
                        to_label="Function",
                        to_name=func_name,
                        repo_id=repo_id,
                    )
                )

            # Visit nested functions within this function (rare, but possible).
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    visit_node(child, parent_name=func_name)

        else:
            # For other node types, check if they have a body attribute
            # and recursively visit children
            if hasattr(node, "body") and isinstance(node.body, list):
                for child in node.body:
                    visit_node(child, parent_name=parent_name)

    # Visit all top-level nodes
    for node in tree.body:
        visit_node(node)

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
) -> None:
    """Extract Python file and upsert results into the graph.

    This is a thin wrapper that calls extract_python_file() and then
    upserts each node and relationship via the GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Python file to index.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    # Use relative path as the module name for cleaner graph representation.
    module_name = file_path.name

    result = extract_python_file(source_code, module_name, repo_id)

    # Upsert all nodes.
    for node in result.nodes:
        engine.upsert_node(node.label, node.repo_id, node.name, node.properties)

    # Upsert all relationships.
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.from_label,
            rel.from_name,
            rel.rel_type,
            rel.to_label,
            rel.to_name,
            rel.repo_id,
        )
