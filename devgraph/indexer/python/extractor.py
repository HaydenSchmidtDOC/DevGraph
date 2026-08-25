"""Tree-sitter-based Python source-code extractor.

Parses a Python file using Tree-sitter's grammar-based parser and extracts:
  - Module (the file itself)
  - Classes with their base classes (inheritance)
  - Functions (at module level and within classes)
  - Imports (from/import statements)
  - Decorators (applied to classes/functions)

Tree-sitter over stdlib `ast` per the Implementation Plan: grammar-based,
incremental-parse friendly, and the only option that generalizes to
non-Python languages later.

Returns a structured result (list of dataclasses) describing nodes and
relationships that can be upserted into the graph via
GraphEngine.upsert_node/upsert_relationship. All nodes are keyed on
(repo_id, name) for idempotent incremental indexing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

logger = logging.getLogger(__name__)

_PY_LANGUAGE = Language(tspython.language())


def _make_parser() -> Parser:
    return Parser(_PY_LANGUAGE)


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


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _dotted_name(node: Node, source: bytes) -> str:
    """Render an identifier/attribute node (e.g. `module.attr`) as dotted text."""
    if node.type in ("identifier", "dotted_name"):
        return _text(node, source)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None:
            return f"{_dotted_name(obj, source)}.{_text(attr, source)}"
    return _text(node, source)


def _extract_decorator_names(decorator_nodes: list[Node], source: bytes) -> list[str]:
    """Extract decorator names from a list of `decorator` nodes."""
    names = []
    for deco in decorator_nodes:
        # A `decorator` node wraps one child: identifier, attribute, or call.
        target = deco.named_children[0] if deco.named_children else None
        if target is None:
            continue
        if target.type == "call":
            func = target.child_by_field_name("function")
            if func is not None:
                names.append(_dotted_name(func, source))
        elif target.type in ("identifier", "attribute"):
            names.append(_dotted_name(target, source))
    return names


def _extract_base_class_names(superclasses_node: Node | None, source: bytes) -> list[str]:
    """Extract base class names from an `argument_list` node under class_definition."""
    if superclasses_node is None:
        return []
    names = []
    for child in superclasses_node.named_children:
        if child.type in ("identifier", "attribute"):
            names.append(_dotted_name(child, source))
        elif child.type == "keyword_argument":
            # e.g. `class Foo(metaclass=Meta):` — not a real base class.
            continue
    return names


def _extract_imports(root: Node, source: bytes) -> list[tuple[str, list[str]]]:
    """Extract import statements from the parse tree.

    Returns a list of (module_name, [imported_names]) tuples.
    For 'from X import Y', module_name='X' and imported_names=['Y'].
    For 'import X', module_name='X' and imported_names=['X'].
    """
    imports: list[tuple[str, list[str]]] = []

    def walk(node: Node) -> None:
        if node.type == "import_statement":
            # import X [as Y] [, Z [as W]]
            for child in node.named_children:
                if child.type == "dotted_name":
                    mod_name = _text(child, source)
                    imports.append((mod_name, [mod_name]))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        mod_name = _text(name_node, source)
                        alias = _text(alias_node, source) if alias_node else mod_name
                        imports.append((mod_name, [alias]))
        elif node.type == "import_from_statement":
            # from X import Y [as Z][, ...] | from . import Y | from X import *
            module_node = node.child_by_field_name("module_name")
            module_name = _text(module_node, source) if module_node else ""
            imported_names: list[str] = []
            for child in node.named_children:
                if child.type == "dotted_name" and child is not module_node:
                    imported_names.append(_text(child, source))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        alias = _text(alias_node, source) if alias_node else _text(name_node, source)
                        imported_names.append(alias)
                elif child.type == "wildcard_import":
                    imported_names.append("*")
            if module_name and imported_names:
                imports.append((module_name, imported_names))

        for child in node.children:
            walk(child)

    walk(root)
    return imports


def extract_python_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Python file and extract nodes and relationships.

    Args:
        source_code: The Python source code as a string.
        file_path: Relative or absolute path to the file (for the Module node name).
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (it uses
        ERROR nodes rather than raising), so partial results are extracted
        and the error is logged rather than the whole file being skipped.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    # Create a Module node for the file itself.
    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties={"type": "module", "source_file": file_path},
    )
    result.nodes.append(module_node)

    # Extract imports at the module level.
    imports = _extract_imports(root, source_bytes)
    for module_name, imported_names in imports:
        for _imp_name in imported_names:
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

    def visit_block(block: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit statements in a block (module body, class body, function body)."""
        for node in block.named_children:
            if node.type == "class_definition":
                _visit_class(node, parent_name, parent_label)
            elif node.type in ("function_definition",):
                _visit_function(node, parent_name, parent_label)
            elif node.type == "decorated_definition":
                # decorated_definition wraps decorator(s) + the actual definition.
                definition = node.child_by_field_name("definition")
                decorators = [c for c in node.named_children if c.type == "decorator"]
                if definition is not None and definition.type == "class_definition":
                    _visit_class(definition, parent_name, parent_label, extra_decorators=decorators)
                elif definition is not None and definition.type == "function_definition":
                    _visit_function(definition, parent_name, parent_label, extra_decorators=decorators)

    def _visit_class(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        superclasses_node = node.child_by_field_name("superclasses")
        base_classes = _extract_base_class_names(superclasses_node, source_bytes)

        result.nodes.append(
            GraphNode(
                label="Class",
                repo_id=repo_id,
                name=class_name,
                properties={"type": "class", "decorators": decorators, "file": file_path},
            )
        )

        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=class_name,
                repo_id=repo_id,
            )
        )

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

        body = node.child_by_field_name("body")
        if body is not None:
            visit_block(body, class_name, "Class")

    def _visit_function(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        result.nodes.append(
            GraphNode(
                label="Function",
                repo_id=repo_id,
                name=func_name,
                properties={"type": "function", "decorators": decorators, "file": file_path},
            )
        )

        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Function",
                to_name=func_name,
                repo_id=repo_id,
            )
        )

        # Nested function definitions (rare, but Tree-sitter walks these fine).
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                if child.type == "function_definition":
                    _visit_function(child, func_name, "Function")
                elif child.type == "decorated_definition":
                    definition = child.child_by_field_name("definition")
                    decos = [c for c in child.named_children if c.type == "decorator"]
                    if definition is not None and definition.type == "function_definition":
                        _visit_function(definition, func_name, "Function", extra_decorators=decos)

    visit_block(root, None, "Module")

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
