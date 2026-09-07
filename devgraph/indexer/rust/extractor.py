"""Tree-sitter-based Rust source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape and philosophy
(Implementation Plan #8, row 5): parse with Tree-sitter, walk the tree, and
emit `GraphNode`/`GraphRelationship` records via the shared
`devgraph.indexer.common` dataclasses -- name-based CALLS resolution
(over-link same-named calls rather than require type resolution) and
heuristic, non-materializing-guess IMPORTS resolution (a guessed target
that doesn't correspond to a real indexed Module node just never becomes an
edge, since `upsert_relationship` only MATCH-links existing nodes).

Rust has no direct equivalent of Python's `class`/`def`, so this extractor
makes a few judgment calls, documented here since they're not obvious from
the graph schema alone:

- `struct`, `enum`, and `trait` items all map to the graph's generic
  `Class` label -- the closest structural equivalent DevGraph's schema has
  to "a named, inheritable type/interface". `properties["kind"]` records
  which one it was ('struct' | 'enum' | 'trait') for callers that care.
- `impl Trait for Type` -> an EXTENDS edge from Class(Type) to
  Class(Trait), the same direction Python's subclass-EXTENDS-base uses (the
  Type "inherits"/implements the Trait's contract). `impl Type { .. }`
  (an inherent impl, no trait) attributes its methods to Class(Type) the
  same way but emits no EXTENDS edge, since there's no trait to point at.
- Free (module-level) `fn` items map to Function, CONTAINS from Module.
  `impl`-block methods (both trait impls and inherent impls) are attributed
  to the struct/enum they're implemented for, the same way Python attributes
  methods to their enclosing class rather than to the module.
- A trait's *default* methods (a `function_item` with a body, inside the
  trait's own declaration_list) are attributed to the trait's Class node.
  Signature-only trait methods (`fn foo(&self);` -- no body, a
  `function_signature_item` node, not `function_item`) are NOT extracted as
  Function nodes, mirroring how the Python extractor only extracts actual
  definitions, never bare declarations.
- Inline `mod foo { .. }` blocks are flattened into their enclosing scope
  (v1 does not model inline-module namespacing/scoping -- a struct declared
  inside an inline `mod` is treated exactly as if it were declared at the
  enclosing level). File-based `mod foo;` declarations are handled as
  IMPORTS (see below), not flattened, since they point at another file.

IMPORTS resolution (single-crate scope only, per the Implementation Plan --
Cargo workspaces/multi-crate resolution is out of scope for v1):

- `mod foo;` / `pub mod foo;` (a file-based submodule declaration) resolves
  to a same-directory file guess: both `{dir}/foo.rs` and `{dir}/foo/mod.rs`
  are emitted as candidate IMPORTS targets, since only one of the two is
  the actual on-disk convention for any given module and the wrong guess
  simply never materializes an edge.
- `use crate::foo::bar::Baz;` (crate-relative) is resolved by converting
  `::` to `/` under an assumed crate root of `src/` -- the near-universal
  convention for a single-crate Cargo project's `src/lib.rs`/`src/main.rs`
  root (a workspace with a different layout won't resolve correctly; see
  module-level limitation note below). Because Tree-sitter gives no type
  information, whether the last path segment (`Baz`) is itself a module or
  an item defined inside a module is ambiguous, so both readings are
  emitted as candidate targets: `src/foo/bar/Baz.rs` (or `/mod.rs`) in case
  `Baz` is itself a module, and `src/foo/bar.rs` (or `/mod.rs`) in case
  `Baz` is an item inside the `bar` module -- exactly one is normally real.
- `use std::...` and any other non-`crate::`-rooted `use` (external crates,
  `self::`, `super::`) is skipped entirely, per the plan -- no same-repo
  guess is attempted for paths that aren't obviously crate-relative.

Known limitations (documented per the Implementation Plan's per-language
table):
- Cargo workspaces / multi-crate repos are out of scope; the `src/` crate
  root assumption breaks for any non-standard layout.
- `use self::...` / `use super::...` and glob `use crate::foo::*;` imports
  are not resolved to per-item edges (the wildcard case resolves only the
  module itself, not the items it re-exports).
- Inline `mod { }` scoping is flattened rather than modeled as a nested
  namespace.
- Trait supertraits (`trait A: B`) do not emit an EXTENDS edge -- only
  `impl Trait for Type` does, per the Implementation Plan's brief.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_rust as tsrust
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_RUST_LANGUAGE = Language(tsrust.language())

# Assumed crate root for `use crate::...` resolution -- see module docstring.
_CRATE_ROOT = "src"

# Node types that introduce a new definition scope; call-target walking must
# not descend into these when scanning an enclosing body, so calls get
# attributed to the correct (innermost) definition, not hoisted outward --
# mirrors the Python extractor's function_definition/class_definition stop.
_SCOPE_BOUNDARY_TYPES = (
    "function_item",
    "impl_item",
    "trait_item",
    "struct_item",
    "enum_item",
    "mod_item",
)


def _make_parser() -> Parser:
    return Parser(_RUST_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """First line up to a blank line or terminating period, matching the
    summary convention the Python extractor uses for `description`."""
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _leading_doc_comment(node: Node, source: bytes) -> str | None:
    """Collect `///` line comments (or a `/** */` block comment) immediately
    preceding `node`, Rust's *outer* doc-comment convention -- the closest
    equivalent to Python's leading-string-literal docstring.

    Deliberately excludes `//!`/`/*!` *inner* doc comments: those document
    the *enclosing* item (typically the module -- see `_extract_module_doc`)
    rather than whatever item happens to follow them, so treating them as
    this item's own doc would misattribute a module-level doc comment to
    the first struct/fn that happens to follow it in the file.
    """
    lines: list[str] = []
    sib = node.prev_sibling
    while sib is not None:
        if sib.type == "line_comment":
            text = _text(sib, source)
            if text.startswith("///") and not text.startswith("////"):
                lines.insert(0, text[3:].strip())
                sib = sib.prev_sibling
                continue
            break
        if sib.type == "block_comment":
            text = _text(sib, source)
            if text.startswith("/**") and not text.startswith("/***"):
                inner = text[3:]
                if inner.endswith("*/"):
                    inner = inner[:-2]
                lines.insert(0, inner.strip())
                sib = sib.prev_sibling
                continue
            break
        break
    return "\n".join(lines) if lines else None


def _extract_module_doc(root: Node, source: bytes) -> str | None:
    """Collect leading `//!`/`/*! */` inner doc comments at the very top of
    the file -- Rust's module-doc convention (there's no leading-string-
    literal equivalent to attach a Module docstring to, unlike Python)."""
    lines: list[str] = []
    for child in root.children:
        if child.type == "line_comment":
            text = _text(child, source)
            if text.startswith("//!"):
                lines.append(text[3:].strip())
                continue
            break
        if child.type == "block_comment":
            text = _text(child, source)
            if text.startswith("/*!"):
                inner = text[3:]
                if inner.endswith("*/"):
                    inner = inner[:-2]
                lines.append(inner.strip())
                continue
            break
        break
    return "\n".join(lines) if lines else None


def _type_name(node: Node | None, source: bytes) -> str | None:
    """Reduce a type expression node to a simple type name, unwrapping the
    generic/reference/scoped-path shapes that can wrap a struct/enum/trait
    reference (e.g. `Foo<T>`, `&Foo`, `pkg::Foo`)."""
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier"):
        return _text(node, source)
    if node.type in ("generic_type", "reference_type"):
        inner = node.child_by_field_name("type")
        return _type_name(inner, source)
    if node.type == "scoped_type_identifier":
        name = node.child_by_field_name("name")
        return _text(name, source) if name is not None else None
    return _text(node, source)


def _callee_simple_name(func_node: Node, source: bytes) -> str | None:
    """Resolve a call expression's `function` field to a simple callee name,
    same philosophy as Python's `_callee_simple_name`.

    - `foo()` -> 'foo'
    - `self.foo()` / `obj.foo()` -> 'foo' (the field name)
    - `Foo::new()` / `pkg::Foo::new()` -> 'new' (the last scoped-path
      segment -- structural, not type-resolved, so this over-links
      same-named associated functions across types, same trade-off Python
      makes for `self.foo()`).
    - `(get())()` / nested calls -> resolves to the innermost call's own
      callee name.
    """
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "field_expression":
        field = func_node.child_by_field_name("field")
        if field is not None:
            return _text(field, source)
    if func_node.type == "scoped_identifier":
        name = func_node.child_by_field_name("name")
        if name is not None:
            return _text(name, source)
    if func_node.type == "call_expression":
        inner_func = func_node.child_by_field_name("function")
        if inner_func is not None:
            return _callee_simple_name(inner_func, source)
    if func_node.type == "parenthesized_expression":
        for child in func_node.named_children:
            return _callee_simple_name(child, source)
    return None


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a body for call expressions, same philosophy as Python's
    `_extract_call_targets`: name-based, over-linking rather than
    under-linking, and not descending into a nested definition's own scope
    (see `_SCOPE_BOUNDARY_TYPES`)."""
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in _SCOPE_BOUNDARY_TYPES:
            return
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                name = _callee_simple_name(func, source)
                if name:
                    targets.append(name)
        for child in node.children:
            walk(child)

    walk(body)
    return targets


def _use_segments(node: Node, source: bytes) -> list[str] | None:
    """Render a `use` path node (`crate`/`identifier`/`scoped_identifier`/
    `use_as_clause`) as its dotted-path segments, e.g. `crate::foo::Bar` ->
    ['crate', 'foo', 'Bar']."""
    if node.type in ("identifier", "crate"):
        return [_text(node, source)]
    if node.type == "scoped_identifier":
        path_node = node.child_by_field_name("path")
        name_node = node.child_by_field_name("name")
        if path_node is None or name_node is None:
            return None
        base = _use_segments(path_node, source)
        if base is None:
            return None
        return base + [_text(name_node, source)]
    if node.type == "use_as_clause":
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return None
        return _use_segments(path_node, source)
    return None


def _emit_use_targets(segments: list[str], imported_name: str) -> list[tuple[str, list[str]]]:
    """Turn `['crate', 'foo', 'bar', 'Baz']` into candidate same-repo file
    guesses under the assumed crate root (see module docstring): both
    readings of the last segment (itself a module, or an item inside the
    second-to-last-segment's module) are emitted, since Tree-sitter can't
    tell which without type information."""
    if not segments or segments[0] != "crate":
        return []
    rest = segments[1:]
    if not rest:
        return []

    out: list[tuple[str, list[str]]] = []
    full_path = "/".join([_CRATE_ROOT, *rest])
    out.append((f"{full_path}.rs", [imported_name]))
    out.append((f"{full_path}/mod.rs", [imported_name]))
    if len(rest) > 1:
        parent_path = "/".join([_CRATE_ROOT, *rest[:-1]])
        out.append((f"{parent_path}.rs", [imported_name]))
        out.append((f"{parent_path}/mod.rs", [imported_name]))
    return out


def _resolve_use(arg: Node, source: bytes) -> list[tuple[str, list[str]]]:
    """Resolve a `use_declaration`'s `argument` field to candidate IMPORTS
    targets. Only `crate::`-rooted paths are handled -- external crates,
    `self::`, and `super::` are skipped entirely, per the plan."""
    if arg.type in ("scoped_identifier", "identifier", "use_as_clause"):
        segs = _use_segments(arg, source)
        if segs:
            return _emit_use_targets(segs, segs[-1])
        return []

    if arg.type == "scoped_use_list":
        path_node = arg.child_by_field_name("path")
        list_node = arg.child_by_field_name("list")
        base_segs = _use_segments(path_node, source) if path_node is not None else None
        if base_segs is None or list_node is None:
            return []
        results: list[tuple[str, list[str]]] = []
        for item in list_node.named_children:
            if item.type == "identifier":
                name = _text(item, source)
                results.extend(_emit_use_targets(base_segs + [name], name))
            elif item.type == "use_as_clause":
                inner_path = item.child_by_field_name("path")
                if inner_path is not None:
                    inner_segs = _use_segments(inner_path, source)
                    if inner_segs:
                        results.extend(_emit_use_targets(base_segs + inner_segs, inner_segs[-1]))
            elif item.type == "self":
                # `use crate::foo::{self, bar}` -- `self` imports the `foo`
                # module itself, i.e. the base path unchanged.
                if base_segs:
                    results.extend(_emit_use_targets(base_segs, base_segs[-1]))
        return results

    if arg.type == "use_wildcard":
        # `use crate::foo::*;` -- resolves the module itself (foo.rs /
        # foo/mod.rs), not the individual items it re-exports.
        inner = arg.named_children[0] if arg.named_children else None
        if inner is not None:
            segs = _use_segments(inner, source)
            if segs:
                return _emit_use_targets(segs, "*")
        return []

    return []


def _extract_imports(root: Node, source: bytes, current_dir: str) -> list[tuple[str, list[str]]]:
    """Extract `mod`/`use` statements from the parse tree.

    Args:
        current_dir: The importing file's directory, relative to the repo
            root, using forward slashes ('' for repo-root files). Used only
            for `mod foo;` resolution (directory-relative); `use crate::...`
            resolution is always relative to the assumed crate root (see
            module docstring), not to the importing file's own location.

    Returns:
        A list of (import_target, [imported_names]) tuples -- see module
        docstring for the `mod`/`use` resolution rules.
    """
    imports: list[tuple[str, list[str]]] = []

    def walk(node: Node) -> None:
        if node.type == "mod_item" and node.child_by_field_name("body") is None:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                mod_name = _text(name_node, source)
                base = f"{current_dir}/{mod_name}" if current_dir else mod_name
                imports.append((f"{base}.rs", [mod_name]))
                imports.append((f"{base}/mod.rs", [mod_name]))
        elif node.type == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg is not None:
                imports.extend(_resolve_use(arg, source))
        else:
            for child in node.children:
                walk(child)

    walk(root)
    return imports


def extract_rust_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Rust file and extract nodes and relationships.

    Args:
        source_code: The Rust source code as a string.
        file_path: The Module node's identity -- the file's path relative to
            the repo root, using forward slashes (e.g. 'src/lib.rs').
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (ERROR
        nodes rather than raising), so partial results are extracted and the
        error is logged rather than the whole file being skipped.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    module_properties: dict = {"type": "module", "source_file": file_path}
    module_doc = _extract_module_doc(root, source_bytes)
    if module_doc:
        module_properties["description"] = _docstring_summary(module_doc)
        module_properties["docstring_full"] = module_doc
    module_node = GraphNode(label="Module", repo_id=repo_id, name=file_path, properties=module_properties)
    result.nodes.append(module_node)

    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    for target, imported_names in _extract_imports(root, source_bytes, current_dir):
        for _imp_name in imported_names:
            result.relationships.append(
                GraphRelationship(
                    from_label="Module",
                    from_name=file_path,
                    rel_type="IMPORTS",
                    to_label="Module",
                    to_name=target,
                    repo_id=repo_id,
                )
            )

    def _emit_call(caller_name: str, caller_label: str, target_name: str, caller_class: str | None = None) -> None:
        properties = {"caller_class": caller_class} if caller_class else None
        result.relationships.append(
            GraphRelationship(
                from_label=caller_label,
                from_name=caller_name,
                rel_type="CALLS",
                to_label="Function",
                to_name=target_name,
                repo_id=repo_id,
                properties=properties,
            )
        )

    def visit_block(block: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit statements in a block (source_file, impl/trait body, fn
        body). Iterates raw `.children` (not `.named_children`) so doc
        comments stay positionally adjacent for `_leading_doc_comment`'s
        `prev_sibling` walk."""
        for node in block.children:
            if node.type == "struct_item":
                _visit_type_item(node, "struct", parent_name, parent_label)
            elif node.type == "enum_item":
                _visit_type_item(node, "enum", parent_name, parent_label)
            elif node.type == "trait_item":
                _visit_trait(node, parent_name, parent_label)
            elif node.type == "impl_item":
                _visit_impl(node, parent_name, parent_label)
            elif node.type == "function_item":
                _visit_function(node, parent_name, parent_label)
            elif node.type == "mod_item":
                body_node = node.child_by_field_name("body")
                if body_node is not None:
                    # Inline `mod foo { .. }` -- flattened into the same
                    # scope (see module docstring's known limitations).
                    visit_block(body_node, parent_name, parent_label)
            elif node.type in (
                "line_comment",
                "block_comment",
                "use_declaration",
                "visibility_modifier",
                "attribute_item",
                "inner_attribute_item",
                "{",
                "}",
            ):
                continue
            else:
                # Any other statement (const/static items, etc.) at this
                # scope -- attribute its call expressions to the enclosing
                # scope, same as Python's module/class-body-level handling.
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(parent_name if parent_name else file_path, parent_label, target, caller_class)

    def _visit_type_item(node: Node, kind: str, parent_name: str | None, parent_label: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        type_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "class",
            "kind": kind,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Class", repo_id=repo_id, name=type_name, properties=properties))
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=type_name,
                repo_id=repo_id,
            )
        )

    def _visit_trait(node: Node, parent_name: str | None, parent_label: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        trait_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "class",
            "kind": "trait",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Class", repo_id=repo_id, name=trait_name, properties=properties))
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=trait_name,
                repo_id=repo_id,
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            # Default methods only (function_item has a body);
            # signature-only methods (function_signature_item) are skipped.
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, trait_name, "Class")

    def _visit_impl(node: Node, parent_name: str | None, parent_label: str) -> None:
        type_field = node.child_by_field_name("type")
        target_name = _type_name(type_field, source_bytes)
        if target_name is None:
            return

        trait_field = node.child_by_field_name("trait")
        if trait_field is not None:
            trait_name = _type_name(trait_field, source_bytes)
            if trait_name:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Class",
                        from_name=target_name,
                        rel_type="EXTENDS",
                        to_label="Class",
                        to_name=trait_name,
                        repo_id=repo_id,
                    )
                )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, target_name, "Class")

    def _visit_function(node: Node, parent_name: str | None, parent_label: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "function",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Function", repo_id=repo_id, name=func_name, properties=properties))
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

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            caller_class = parent_name if parent_label == "Class" else None
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, caller_class)

            # Nested fn items (rare, but Tree-sitter walks these fine).
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, func_name, "Function")

    visit_block(root, None, "Module")

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a Rust file and upsert results into the graph.

    Thin wrapper calling extract_rust_file() and upserting each node and
    relationship via the GraphEngine, matching the Python extractor's
    index_file() wrapper.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Rust file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes) -- what makes `mod`/`use` resolution and same-named
            files in different directories work correctly. When omitted,
            falls back to the bare filename.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    if repo_root is not None:
        try:
            rel = file_path.resolve().relative_to(Path(repo_root).resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name
    else:
        module_name = file_path.name

    result = extract_rust_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
