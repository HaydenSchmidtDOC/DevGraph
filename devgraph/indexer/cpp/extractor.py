"""Tree-sitter-based C++ source-code extractor.

Handles both C++ source files (.cpp/.cc/.cxx) and headers (.h/.hpp) with one
extractor — headers and sources share the same grammar, and the same
class/function/include shapes, so there is no reason to split them.

Scope, per Implementation Plan #8's per-language table: **structural parity
only, not import-graph parity.** Module/Class/Function/CONTAINS/EXTENDS/CALLS
extraction follows the same name-based philosophy as the Python extractor
(see `devgraph/indexer/python/extractor.py`'s `_callee_simple_name`). IMPORTS
extraction is deliberately weak: `#include` resolution has no reliable
general algorithm without a real build system (compiler include paths, `-I`
flags, precompiled headers, vcpkg/conan layouts, etc.), so:

  - `#include "local.h"` (quoted form) gets the same "guessed same-repo
    relative path, never materializes an edge if wrong" treatment Python's
    dotted-import guessing uses (`upsert_relationship` only MATCH-links real
    existing nodes, so a wrong guess is simply inert).
  - `#include <system.h>` (angle-bracket form) is **not resolved at all** —
    skipped entirely. These are almost always system/standard-library/
    third-party headers with no repo-relative meaning, and guessing a
    same-repo path for them would be actively misleading rather than merely
    unresolved.

This means the resulting IMPORTS graph for a C++ repo will be thin compared
to Python's (quoted-include-only, same-directory-guess-only) — that is
expected and documented, not a bug. See Implementation Plan #8's Open Item 3.

C++ has no module-level docstring convention analogous to Python's leading
string-literal (no equivalent of a triple-quoted string as the first
statement of a file), so Module nodes carry no `description`/
`docstring_full` here — only Class/Function nodes do, from an immediately
preceding `///` (one or more consecutive line comments) or `/** ... */`
block comment.

C++ namespaces are flattened: a `namespace ns { ... }` body's declarations
are walked as if they were directly in the enclosing scope (Module, or the
enclosing class for a nested namespace inside a class — vanishingly rare).
The graph schema has no Namespace node type to model in Plan #8's scope, and
Python's own package/module structure already carries the equivalent
information at the file level, so this matches the "parity, not innovation"
bar rather than inventing a new node type C++ alone would use. Preprocessor
conditionals (`#ifdef`/`#ifndef`/`#if`/`#else`/`#elif`) are flattened the
same way, since `preproc_ifdef` etc. wrap their body as direct named
children in Tree-sitter's cpp grammar — this lets include-guarded headers
(the overwhelming majority of real-world .h files) parse normally instead of
losing every declaration inside the guard.

Out-of-class method definitions (`ClassName::MethodName(...) { ... }`,
common in .cpp files that implement a class declared in the matching .h)
are attributed to Class ClassName via a text split on '::' on the qualified
declarator (e.g. 'Outer::Inner::method' -> class 'Inner', method 'method';
'A::~A' -> class 'A', method '~A') — the segment immediately before the
final name, not the leftmost one, since the leftmost segment is more likely
to be an enclosing namespace than the class itself. This is a textual
heuristic, not a semantic one: it does not require (or verify) that a class
named 'Inner' was actually declared in this same file. When it wasn't (e.g.
the class lives in a header this file doesn't happen to also be extracting
in the same batch), a minimal stub Class node is still emitted so the
Function's CONTAINS edge has a real endpoint to MATCH onto — Neo4j's
`SET n += properties` merge (see GraphEngine.upsert_nodes) means this stub
never clobbers the fuller Class node the header's own extraction writes,
regardless of which file is indexed first or second.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_CPP_LANGUAGE = Language(tscpp.language())

# Declaration types Tree-sitter's cpp grammar uses purely as transparent
# wrappers around a body of further declarations — walked through, not
# treated as a scope of their own. `namespace_definition` is included here
# too (flattened, see module docstring) even though it has an actual name,
# because Plan #8's schema has no Namespace node to attribute it to.
_TRANSPARENT_CONTAINER_TYPES = (
    "preproc_ifdef",
    "preproc_if",
    "preproc_elif",
    "preproc_else",
    "linkage_specification",  # extern "C" { ... }
    "declaration_list",  # the body of the above / of a namespace
)


def _make_parser() -> Parser:
    return Parser(_CPP_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _unwrap_declarator(node: Node | None) -> Node | None:
    """Strip pointer/reference/parenthesized wrapping to reach the innermost
    declarator, e.g. `int* getPtr()`'s declarator field is a
    `pointer_declarator` wrapping the actual `function_declarator`.
    """
    while node is not None and node.type in (
        "pointer_declarator",
        "reference_declarator",
        "abstract_pointer_declarator",
        "abstract_reference_declarator",
        "parenthesized_declarator",
    ):
        inner = node.child_by_field_name("declarator")
        if inner is None:
            break
        node = inner
    return node


def _base_name_text(node: Node, source: bytes) -> str | None:
    """Render a single base-class node (`type_identifier`/`qualified_identifier`/
    `template_type`) as its bare name. A templated base (`Base<T>`) resolves
    to the template's bare name ('Base'), not the full instantiation — same
    "don't chase full type resolution" philosophy as everything else here.
    """
    if node.type in ("type_identifier", "qualified_identifier", "identifier"):
        return _text(node, source)
    if node.type == "template_type":
        name_node = node.child_by_field_name("name")
        return _text(name_node, source) if name_node is not None else _text(node, source)
    return None


def _extract_base_class_names(base_clause: Node | None, source: bytes) -> list[str]:
    """Extract base class names from a `base_class_clause` node.

    Handles multiple bases and mixed access specifiers
    (`class D : public A, private B, protected C`) — `access_specifier` and
    punctuation children are simply skipped, every `type_identifier`/
    `qualified_identifier`/`template_type` child is a base.
    """
    if base_clause is None:
        return []
    names: list[str] = []
    for child in base_clause.named_children:
        name = _base_name_text(child, source)
        if name:
            names.append(name)
        # access_specifier ('public'/'private'/'protected') and 'virtual' are
        # not base classes — skipped implicitly (_base_name_text returns None).
    return names


def _recover_macro_prefixed_class(node: Node, source: bytes) -> tuple[str, list[str]] | None:
    """Recover the class name (+ any recognizable base classes) from
    Tree-sitter-cpp's misparse of `class EXPORT_MACRO Name [: bases] { ... }`
    — the extremely common DLL-export/visibility-attribute convention (e.g.
    `class MYLIB_API Foo : public Base`, `class FOO_EXPORT Bar`). Without
    macro expansion, the grammar has no way to know `EXPORT_MACRO` isn't the
    class name, so it parses the macro token as the name and reads
    everything after it — the *real* name, the base-class list, and the
    entire member list — as a bogus top-level `function_definition` whose
    body is itself corrupted (constructors/fields come out as garbled
    `call_expression`/`declaration`/`ERROR` nodes, not reliable per-member
    data).

    This recovers just the Class node's name and any base classes found as
    direct children of that bogus `function_definition` (best-effort — a
    single base resolves reliably; a comma-separated multi-base list under
    this misparse was not verified and may be incomplete). It deliberately
    does NOT attempt to recover members from the corrupted body — walking
    that compound_statement for classes/functions/calls would fabricate
    nodes/edges that don't correspond to real code, which is worse than
    simply not extracting them. Confirmed against real code: this pattern
    affects a majority of yaml-cpp's public classes (`YAML_CPP_API` prefix
    on nearly every exported class) in this extractor's own golden-repo spot
    check — without this recovery, entire header files' worth of classes
    and EXTENDS edges would silently vanish rather than degrading gracefully.

    Returns None if `node` doesn't match this specific misparse shape (the
    ordinary case: a real function/method definition, handled by
    `_visit_function` instead).
    """
    type_field = node.child_by_field_name("type")
    if type_field is None or type_field.type not in ("class_specifier", "struct_specifier"):
        return None
    if type_field.child_by_field_name("body") is not None:
        return None  # a real class/struct with its own body — not this misparse

    declarator = node.child_by_field_name("declarator")

    # Look for the ERROR node holding the base-class-list fragment. Its
    # *shape* varies (empirically, both of these occur in real code, and
    # nothing else distinguishes them structurally):
    #   - 'Name : public' — the ERROR's first child is the class name; the
    #     'declarator' field then holds the (bare, non-namespaced) base.
    #   - ': public Base' — the ERROR's first child is ':'; 'declarator'
    #     then holds the real, un-confused class name, and the identifier
    #     inside the ERROR is a (bare) base.
    # Disambiguated by whether the ERROR node's first child is itself an
    # identifier (case 1) or not (case 2, starts with ':').
    error_node = next((c for c in node.children if c.type == "ERROR"), None)
    error_ident = None
    error_ident_is_leading = False
    if error_node is not None:
        error_ident = next((c for c in error_node.children if c.type == "identifier"), None)
        error_ident_is_leading = bool(error_node.children) and error_node.children[0].type == "identifier"

    base_nodes: list[Node] = []
    if error_ident is not None and error_ident_is_leading:
        class_name_node = error_ident
        if declarator is not None and declarator.type == "identifier":
            base_nodes.append(declarator)
    elif error_ident is not None:
        class_name_node = declarator if declarator is not None and declarator.type == "identifier" else None
        base_nodes.append(error_ident)
    else:
        # No base-class list: 'declarator' is the real, un-confused name.
        class_name_node = declarator if declarator is not None and declarator.type == "identifier" else None

    for child in node.children:
        if child.type in ("type_identifier", "qualified_identifier", "template_type"):
            base_nodes.append(child)

    if class_name_node is None:
        return None

    class_name = _text(class_name_node, source)
    base_names = [n for n in (_base_name_text(b, source) for b in base_nodes) if n]
    return class_name, base_names


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a function/method body for call expressions and return the
    callee's simple name — same philosophy as Python's `_callee_simple_name`:

    - `foo()` -> 'foo'
    - `obj.foo()` / `obj->foo()` / `this->foo()` -> 'foo' (the member name;
      no type info available, so this over-links same-named methods across
      unrelated classes rather than under-linking real calls)
    - Does not descend into a nested `function_definition` or
      `class_specifier`/`struct_specifier` — those are walked separately so
      calls are attributed to the correct enclosing scope, not hoisted out.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in ("function_definition", "class_specifier", "struct_specifier"):
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


def _callee_simple_name(func_node: Node, source: bytes) -> str | None:
    """Resolve a call expression's `function` field to a simple callee name."""
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "field_expression":
        # obj.foo / obj->foo / this->foo — the member name being accessed.
        field = func_node.child_by_field_name("field")
        if field is not None:
            return _text(field, source)
    if func_node.type == "qualified_identifier":
        # Explicit-scope call, e.g. `Base::foo()` — take the final segment.
        name_node = func_node.child_by_field_name("name")
        if name_node is not None:
            return _callee_simple_name(name_node, source)
        return _text(func_node, source).rsplit("::", 1)[-1]
    if func_node.type == "call_expression":
        # Chained/immediately-invoked call, e.g. `get_handler()()`.
        inner_func = func_node.child_by_field_name("function")
        if inner_func is not None:
            return _callee_simple_name(inner_func, source)
    return None


def _extract_doc_comment(node: Node, source: bytes) -> str | None:
    """Extract a class/function's doc comment: one or more consecutive `///`
    line comments, or a single `/** ... */` block comment, immediately
    preceding `node` as its previous named sibling. A plain `//`/`/* */`
    comment (no doc-comment marker) does not count.
    """
    prev = node.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    text = _text(prev, source)
    if text.startswith("///"):
        lines = [text[3:].strip()]
        cur = prev.prev_named_sibling
        while cur is not None and cur.type == "comment":
            cur_text = _text(cur, source)
            if not cur_text.startswith("///"):
                break
            lines.append(cur_text[3:].strip())
            cur = cur.prev_named_sibling
        lines.reverse()
        return "\n".join(lines).strip()
    if text.startswith("/**"):
        return _clean_block_comment(text)
    return None


def _clean_block_comment(raw: str) -> str:
    """Strip `/** ... */` delimiters and each line's leading `*` (the common
    Doxygen/Javadoc-style convention: ` * line text`).
    """
    body = raw.strip()
    if body.startswith("/**"):
        body = body[3:]
    elif body.startswith("/*"):
        body = body[2:]
    if body.endswith("*/"):
        body = body[:-2]

    lines = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        lines.append(line)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _doc_summary(full_text: str, max_chars: int = 120) -> str:
    """First line/sentence of a doc comment, truncated — mirrors the
    Python extractor's `_docstring_summary`.
    """
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _extract_includes(root: Node, source: bytes, current_dir: str) -> list[str]:
    """Extract `#include` targets as same-repo Module-name guesses.

    Only quoted includes (`#include "local.h"`) produce a guess — resolved
    relative to the including file's own directory (`current_dir`), then
    normalized (so `#include "../inc/foo.h"` from `src/x.cpp` resolves to
    `inc/foo.h`, not `src/../inc/foo.h`). Angle-bracket includes
    (`#include <system.h>`) are skipped entirely — see module docstring.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type == "preproc_include":
            for child in node.named_children:
                if child.type == "string_literal":
                    content = next(
                        (c for c in child.named_children if c.type == "string_content"), None
                    )
                    header = _text(content, source) if content is not None else None
                    if header:
                        joined = f"{current_dir}/{header}" if current_dir else header
                        targets.append(posixpath.normpath(joined))
                # system_lib_string (angle-bracket form) intentionally skipped.
        for child in node.children:
            walk(child)

    walk(root)
    return targets


def extract_cpp_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a C++ source or header file and extract nodes and relationships.

    Args:
        source_code: The C++ source as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, using forward slashes (e.g. 'src/widget.cpp').
            `index_file()` computes this automatically for real repo files.
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships.
        Tree-sitter returns a best-effort tree (ERROR nodes, not an
        exception) on a syntax error, so partial results are still
        extracted and the error is logged rather than the file being
        skipped outright.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties={"type": "module", "source_file": file_path},
    )
    result.nodes.append(module_node)

    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    for target in _extract_includes(root, source_bytes, current_dir):
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

    classes_emitted: set[str] = set()

    def _ensure_class_stub(class_name: str) -> None:
        if class_name in classes_emitted:
            return
        classes_emitted.add(class_name)
        result.nodes.append(
            GraphNode(
                label="Class",
                repo_id=repo_id,
                name=class_name,
                properties={"type": "class"},
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

    def visit_block(block: Node, parent_name: str | None, parent_label: str) -> None:
        for node in block.named_children:
            if node.type in ("class_specifier", "struct_specifier"):
                _visit_class(node, parent_name, parent_label)
            elif node.type == "function_definition":
                recovered = _recover_macro_prefixed_class(node, source_bytes)
                if recovered is not None:
                    class_name, base_names = recovered
                    _visit_recovered_macro_class(node, class_name, base_names, parent_name, parent_label)
                else:
                    _visit_function(node, parent_name, parent_label)
            elif node.type == "namespace_definition":
                body = node.child_by_field_name("body")
                if body is not None:
                    visit_block(body, parent_name, parent_label)
            elif node.type in _TRANSPARENT_CONTAINER_TYPES:
                visit_block(node, parent_name, parent_label)
            elif node.type in ("comment", "preproc_include", "preproc_def", "preproc_call"):
                continue
            else:
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(parent_name if parent_name else file_path, parent_label, target, caller_class)

    def _visit_class(node: Node, parent_name: str | None, parent_label: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        base_clause = node.child_by_field_name("base_class_clause") or next(
            (c for c in node.named_children if c.type == "base_class_clause"), None
        )
        base_classes = _extract_base_class_names(base_clause, source_bytes)

        class_properties: dict = {
            "type": "class",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _extract_doc_comment(node, source_bytes)
        if doc:
            class_properties["description"] = _doc_summary(doc)
            class_properties["docstring_full"] = doc

        classes_emitted.add(class_name)
        result.nodes.append(
            GraphNode(label="Class", repo_id=repo_id, name=class_name, properties=class_properties)
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

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            visit_block(body_node, class_name, "Class")

    def _visit_recovered_macro_class(
        node: Node, class_name: str, base_names: list[str], parent_name: str | None, parent_label: str
    ) -> None:
        """Emit a Class node (+ EXTENDS) recovered by
        `_recover_macro_prefixed_class` — no member walk, see that
        function's docstring for why.
        """
        class_properties: dict = {
            "type": "class",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _extract_doc_comment(node, source_bytes)
        if doc:
            class_properties["description"] = _doc_summary(doc)
            class_properties["docstring_full"] = doc

        classes_emitted.add(class_name)
        result.nodes.append(
            GraphNode(label="Class", repo_id=repo_id, name=class_name, properties=class_properties)
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
        for base_class in base_names:
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

    def _visit_function(node: Node, parent_name: str | None, parent_label: str) -> None:
        declarator = _unwrap_declarator(node.child_by_field_name("declarator"))
        if declarator is None or declarator.type != "function_declarator":
            return  # unsupported declarator shape (e.g. function-pointer variable) — skip
        inner = declarator.child_by_field_name("declarator")
        if inner is None:
            return

        class_name: str | None = None
        if inner.type == "qualified_identifier":
            # Out-of-class definition, e.g. 'ClassName::method' or
            # 'Outer::Inner::method' — see module docstring for the
            # class-attribution heuristic (last segment before the name).
            qualified_text = _text(inner, source_bytes)
            parts = qualified_text.split("::")
            func_name = parts[-1]
            if len(parts) >= 2:
                class_name = parts[-2]
        elif inner.type in ("identifier", "field_identifier", "destructor_name", "operator_name"):
            func_name = _text(inner, source_bytes)
            class_name = parent_name if parent_label == "Class" else None
        else:
            return  # e.g. array_declarator or other shape not modeled here

        if not func_name:
            return

        if class_name:
            _ensure_class_stub(class_name)
            contains_label, contains_name = "Class", class_name
        else:
            contains_label, contains_name = parent_label, (parent_name if parent_name else file_path)

        func_properties: dict = {
            "type": "function",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _extract_doc_comment(node, source_bytes)
        if doc:
            func_properties["description"] = _doc_summary(doc)
            func_properties["docstring_full"] = doc

        result.nodes.append(
            GraphNode(label="Function", repo_id=repo_id, name=func_name, properties=func_properties)
        )

        result.relationships.append(
            GraphRelationship(
                from_label=contains_label,
                from_name=contains_name,
                rel_type="CONTAINS",
                to_label="Function",
                to_name=func_name,
                repo_id=repo_id,
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, class_name)

    visit_block(root, None, "Module")

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a C++ file and upsert results into the graph.

    Thin wrapper mirroring `python/extractor.py`'s `index_file`: calls
    `extract_cpp_file()` then upserts each node/relationship via the
    GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the .cpp/.cc/.cxx/.h/.hpp file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes) — needed for '#include "local.h"' same-directory
            resolution and to avoid same-named files in different
            directories colliding into one Module node. When omitted, falls
            back to the bare filename.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8", errors="replace")

    if repo_root is not None:
        try:
            rel = file_path.resolve().relative_to(Path(repo_root).resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name
    else:
        module_name = file_path.name

    result = extract_cpp_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
