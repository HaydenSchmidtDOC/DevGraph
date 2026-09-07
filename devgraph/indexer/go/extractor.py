"""Tree-sitter-based Go source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape and philosophy,
adapted to Go's grammar. Extracts:
  - Module (the file itself)
  - Class: Go has no classes. Struct type declarations (`type X struct {...}`)
    are mapped to the Class label as the closest structural equivalent —
    structs are Go's only user-defined aggregate type with named fields and
    methods attached via receivers, which is the same shape a Class node
    already models for Python.
  - Function: free `func` declarations, plus methods (`func (r Receiver)
    Foo()`), attributed to their receiver's struct the same way Python
    methods are attributed to their enclosing class.
  - CONTAINS relationships (Module->Class, Module->Function, Class->Function).
  - EXTENDS: Go has no inheritance, so this mostly doesn't apply. The one
    exception is struct embedding (`type Dog struct { Animal }`), Go's
    composition mechanism — this is the closest semantic analog to
    inheritance (an embedded struct's fields/methods are promoted onto the
    embedding struct, similar to how a Python subclass inherits its base's
    attributes), so embedding emits an EXTENDS edge. This is a deliberate
    stretch of EXTENDS' original meaning: embedding is composition, not
    inheritance, but DevGraph's schema has no dedicated "composes"
    relationship type and EXTENDS is the nearest existing one.
  - IMPORTS (import declarations, resolved against go.mod's module path
    when available).
  - `//` doc comments immediately preceding a declaration, Go's doc-comment
    convention (a contiguous comment block directly above the declaration,
    conventionally starting with the declared identifier's name) — stored
    the same way Python's docstrings are (`description`/`docstring_full`).

Tree-sitter was already chosen for Python specifically because it
"generalizes to non-Python languages later" (see python/extractor.py's
module docstring) — this is that generalization for Go.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_GO_LANGUAGE = Language(tsgo.language())


def _make_parser() -> Parser:
    return Parser(_GO_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _clean_comment_line(raw: str) -> str:
    """Strip a single Go comment node's `//` or `/* */` markers."""
    text = raw.strip()
    if text.startswith("//"):
        return text[2:].strip()
    if text.startswith("/*") and text.endswith("*/"):
        return text[2:-2].strip()
    return text


def _preceding_doc_comment(node: Node, source: bytes) -> str | None:
    """Collect the contiguous `comment` sibling(s) directly above `node`
    (no blank line in between), Go's doc-comment convention. Returns None if
    there's no comment immediately preceding, or a blank line separates one.
    """
    collected: list[Node] = []
    current = node.prev_named_sibling
    expected_end_row = node.start_point[0] - 1
    while current is not None and current.type == "comment" and current.end_point[0] == expected_end_row:
        collected.append(current)
        expected_end_row = current.start_point[0] - 1
        current = current.prev_named_sibling

    if not collected:
        return None

    collected.reverse()
    lines = [_clean_comment_line(_text(c, source)) for c in collected]
    return "\n".join(lines).strip() or None


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """Same convention as the Python extractor: first line up to the first
    blank line or terminating period, hard-truncated as a fallback.
    """
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _callee_simple_name(func_node: Node, source: bytes) -> str | None:
    """Resolve a call expression's `function` field to a simple callee name.

    - `foo()` -> 'foo'
    - `obj.foo()` -> 'foo' (the selector's field name — no type info, so
      this over-links same-named methods across structs/packages rather
      than under-linking, matching the Python extractor's `self.foo()`
      handling).
    - `get()()` (chained/immediately-invoked call) -> resolves to the outer
      call's own callee name by recursing on its function field.
    """
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "selector_expression":
        field = func_node.child_by_field_name("field")
        if field is not None:
            return _text(field, source)
    if func_node.type == "call_expression":
        inner = func_node.child_by_field_name("function")
        if inner is not None:
            return _callee_simple_name(inner, source)
    if func_node.type == "parenthesized_expression" and func_node.named_children:
        return _callee_simple_name(func_node.named_children[0], source)
    return None


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a function/method body for call expressions, returning each
    callee's simple name. Does not descend into a nested named function/
    method declaration (Go doesn't actually allow declaring these nested,
    but the guard is kept defensive and mirrors the Python extractor's
    scope-boundary behavior); anonymous func literals ARE descended into
    since DevGraph has no separate node to attribute their calls to.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in ("function_declaration", "method_declaration"):
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


def _receiver_type_name(method_node: Node, source: bytes) -> str | None:
    """Extract the struct name a method's receiver is attached to.

    `func (d *Dog) Speak()` and `func (d Dog) Speak()` both resolve to
    'Dog' — pointer vs. value receiver makes no difference to which struct
    the method is attributed to.
    """
    receiver = method_node.child_by_field_name("receiver")
    if receiver is None:
        return None
    for param in receiver.named_children:
        if param.type != "parameter_declaration":
            continue
        type_node = param.child_by_field_name("type")
        if type_node is None:
            continue
        if type_node.type == "pointer_type" and type_node.named_children:
            return _text(type_node.named_children[0], source)
        if type_node.type == "type_identifier":
            return _text(type_node, source)
        if type_node.type == "generic_type":
            # Receiver on a generic struct, e.g. `func (d *Dog[T]) Speak()`.
            # Best-effort: take the first type_identifier descendant.
            for child in type_node.named_children:
                if child.type == "type_identifier":
                    return _text(child, source)
    return None


def _embedded_field_type_name(field_decl: Node, source: bytes) -> str | None:
    """For a `field_declaration` with no `name` field (Go's embedded-field
    syntax), return the embedded type's simple name — 'Animal' for
    `Animal`/`*Animal`, 'Other' for `pkg.Other`/`*pkg.Other`.
    """
    for child in field_decl.named_children:
        if child.type == "type_identifier":
            return _text(child, source)
        if child.type == "qualified_type":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                return _text(name_node, source)
    return None


def _extract_imports(root: Node, source: bytes) -> list[str]:
    """Extract each import spec's raw import path string (e.g.
    'fmt', 'path/to/pkg', 'github.com/someone/pkg')."""
    paths: list[str] = []

    def walk(node: Node) -> None:
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                # interpreted_string_literal includes the surrounding quotes.
                raw = _text(path_node, source)
                paths.append(raw.strip('"'))
        for child in node.children:
            walk(child)

    walk(root)
    return paths


def _resolve_import_targets(import_path: str, module_path: str | None) -> list[str]:
    """Guess the Module node name(s) an import path should resolve to.

    Always includes the bare import path itself (kept for compatibility/
    introspection, same as the Python extractor keeps the bare dotted
    name). When `module_path` (go.mod's `module` directive) is known:
      - an intra-module import ('mymod/pkg/sub' under module 'mymod') gets
        an additional same-repo file-path guess: the import path's
        directory, reinterpreted as '<dir>/<last-segment>.go' — Go's
        convention that an import path mirrors the package's directory
        structure relative to the module root, guessing the package's
        primary file is named after its own directory (true for many small
        packages, not guaranteed for larger multi-file ones).
      - a non-intra-module import (e.g. 'github.com/someone/pkg') gets NO
        extra guess — it's genuinely external, so there's nothing to guess
        at, same as Python's third-party-import behavior.
    When `module_path` is unknown (no go.mod found), every import gets the
    same last-path-segment same-repo guess, since we can't tell intra-repo
    imports from external ones without the module directive.

    Either way, a wrong guess simply never materializes an edge —
    upsert_relationship only MATCHes real existing nodes on both ends.
    """
    candidates = [import_path]

    if module_path is not None:
        if import_path == module_path or import_path.startswith(module_path + "/"):
            rel = import_path[len(module_path) :].lstrip("/")
            if rel:
                last = rel.rsplit("/", 1)[-1]
                candidates.append(f"{rel}/{last}.go")
        # else: external (outside this repo's module) — no guess.
    else:
        last = import_path.rsplit("/", 1)[-1]
        candidates.append(f"{last}/{last}.go")

    return candidates


def extract_go_file(
    source_code: str,
    file_path: str,
    repo_id: str,
    module_path: str | None = None,
) -> ExtractionResult:
    """Parse a Go file and extract nodes and relationships.

    Args:
        source_code: The Go source code as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, using forward slashes (e.g. 'pkg/service/main.go').
        repo_id: Repository ID for scoping nodes.
        module_path: The repo's go.mod `module` directive value, if known
            (e.g. 'github.com/someone/myrepo'). Used to tell intra-module
            imports apart from external ones when guessing IMPORTS targets
            (see `_resolve_import_targets`). Optional — pass None when no
            go.mod was found or this file isn't part of a module.

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

    # Module node for the file itself. Its docstring is the doc comment
    # immediately preceding the `package` clause, if any (Go's package-doc
    # convention, usually found in one file per package, often doc.go).
    module_properties: dict = {"type": "module", "source_file": file_path}
    package_clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    if package_clause is not None:
        module_docstring = _preceding_doc_comment(package_clause, source_bytes)
        if module_docstring:
            module_properties["description"] = _docstring_summary(module_docstring)
            module_properties["docstring_full"] = module_docstring

    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties=module_properties,
    )
    result.nodes.append(module_node)

    for import_path in _extract_imports(root, source_bytes):
        for target in _resolve_import_targets(import_path, module_path):
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

    def _emit_contains(parent_label: str, parent_name: str, child_label: str, child_name: str) -> None:
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name,
                rel_type="CONTAINS",
                to_label=child_label,
                to_name=child_name,
                repo_id=repo_id,
            )
        )

    def _emit_call(caller_name: str, caller_label: str, target_name: str, caller_class: str | None) -> None:
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

    def _visit_struct(type_spec: Node) -> None:
        name_node = type_spec.child_by_field_name("name")
        struct_node = type_spec.child_by_field_name("type")
        if name_node is None or struct_node is None or struct_node.type != "struct_type":
            return
        struct_name = _text(name_node, source_bytes)

        struct_properties: dict = {
            "type": "class",
            "decorators": [],
            "file": file_path,
            "start_line": type_spec.start_point[0] + 1,
            "end_line": type_spec.end_point[0] + 1,
        }
        # The doc comment belongs to the enclosing type_declaration (which
        # may hold several grouped type_specs in a `type (...)` block), not
        # the type_spec itself — mirror that when looking for it, but fall
        # back to the type_spec's own preceding sibling for the ungrouped
        # `type X struct {...}` case (type_declaration IS the direct parent
        # there too, so both paths converge to the same node in practice).
        doc_source = type_spec.parent if type_spec.parent is not None and type_spec.parent.type == "type_declaration" else type_spec
        docstring = _preceding_doc_comment(doc_source, source_bytes)
        if docstring:
            struct_properties["description"] = _docstring_summary(docstring)
            struct_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(label="Class", repo_id=repo_id, name=struct_name, properties=struct_properties)
        )
        _emit_contains("Module", file_path, "Class", struct_name)

        field_list = struct_node.child_by_field_name("body")
        if field_list is None:
            # Some grammar versions expose the field list as the struct's
            # sole unnamed-field child rather than a "body" field; fall back
            # to scanning named children directly.
            field_list = next((c for c in struct_node.named_children if c.type == "field_declaration_list"), None)
        if field_list is not None:
            for field_decl in field_list.named_children:
                if field_decl.type != "field_declaration":
                    continue
                if field_decl.child_by_field_name("name") is not None:
                    continue  # a regular named field, not an embed
                embedded_name = _embedded_field_type_name(field_decl, source_bytes)
                if embedded_name:
                    result.relationships.append(
                        GraphRelationship(
                            from_label="Class",
                            from_name=struct_name,
                            rel_type="EXTENDS",
                            to_label="Class",
                            to_name=embedded_name,
                            repo_id=repo_id,
                        )
                    )

    def _visit_function(node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)
        body_node = node.child_by_field_name("body")

        func_properties: dict = {
            "type": "function",
            "decorators": [],
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        docstring = _preceding_doc_comment(node, source_bytes)
        if docstring:
            func_properties["description"] = _docstring_summary(docstring)
            func_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(label="Function", repo_id=repo_id, name=func_name, properties=func_properties)
        )
        _emit_contains("Module", file_path, "Function", func_name)

        if body_node is not None:
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, caller_class=None)

    def _visit_method(node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        method_name = _text(name_node, source_bytes)
        body_node = node.child_by_field_name("body")
        receiver_type = _receiver_type_name(node, source_bytes)

        method_properties: dict = {
            "type": "function",
            "decorators": [],
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        docstring = _preceding_doc_comment(node, source_bytes)
        if docstring:
            method_properties["description"] = _docstring_summary(docstring)
            method_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(label="Function", repo_id=repo_id, name=method_name, properties=method_properties)
        )
        if receiver_type:
            _emit_contains("Class", receiver_type, "Function", method_name)
        else:
            _emit_contains("Module", file_path, "Function", method_name)

        if body_node is not None:
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(method_name, "Function", target, caller_class=receiver_type)

    for node in root.named_children:
        if node.type == "type_declaration":
            for type_spec in node.named_children:
                if type_spec.type == "type_spec":
                    _visit_struct(type_spec)
        elif node.type == "function_declaration":
            _visit_function(node)
        elif node.type == "method_declaration":
            _visit_method(node)

    return result


def _find_module_path(repo_root: Path) -> str | None:
    """Read go.mod's `module` directive from the repo root, if present."""
    go_mod = repo_root / "go.mod"
    if not go_mod.exists():
        return None
    try:
        for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("module "):
                return line[len("module ") :].strip()
    except OSError:
        return None
    return None


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a Go file and upsert results into the graph.

    Thin wrapper that calls extract_go_file() and upserts each node and
    relationship via the GraphEngine, matching python/extractor.py's
    index_file() signature/behavior.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Go file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root, and
            go.mod (if present at repo_root) is read to resolve intra-module
            imports. When omitted, falls back to the bare filename and no
            go.mod-based import resolution.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    module_path = None
    if repo_root is not None:
        repo_root = Path(repo_root)
        try:
            rel = file_path.resolve().relative_to(repo_root.resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name
        module_path = _find_module_path(repo_root)
    else:
        module_name = file_path.name

    result = extract_go_file(source_code, module_name, repo_id, module_path)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
