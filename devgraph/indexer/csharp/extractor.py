"""Tree-sitter-based C# source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape (Implementation Plan
#8, row 2): parses a C# file and extracts:
  - Module (the file itself)
  - Class nodes for `class`/`record`/`struct` declarations, with base
    classes AND implemented interfaces both emitted as EXTENDS (the C#
    grammar's `base_list` doesn't distinguish a base class from an
    implemented interface, so — like Python's CALLS name-based over-linking
    — this over-links rather than under-links; a guessed EXTENDS target that
    turns out to be an interface simply never gets more than a name/no
    inheritance-depth semantics attached to it).
  - Function nodes for methods AND constructors (C# doesn't have free
    functions the way Python does; "Function" here means any method-shaped
    member — a constructor's own `name` field is literally the class name,
    so no special-casing is needed to key it sensibly).
  - CONTAINS relationships (Module->Class, Class->Function).
  - XML doc comments (`/// <summary>...</summary>`) immediately preceding a
    class/method/constructor, extracted the same way Python's docstring
    extraction feeds `description`/`docstring_full`.
  - CALLS edges, name-based exactly like Python's `_callee_simple_name`.
  - IMPORTS edges from `using` directives, via a namespace-to-folder
    convention guess (weaker than Python's dotted-import guess — see
    `_using_target` below for why).

Known scope cuts (documented, not silently dropped):
  - `interface` declarations are not emitted as Class nodes (brief allows
    skipping when "implementing interfaces" isn't easy to do precisely;
    tree-sitter-c-sharp's `base_list` doesn't distinguish interfaces from
    base classes at all, so doing it "properly" would need semantic type
    info this extractor doesn't have). Interface names still appear as
    EXTENDS targets from an implementing class's base_list — they just never
    resolve to a real Class node, which is the same non-materializing-guess
    behavior IMPORTS targets get.
  - Local functions (`local_function_statement`) and lambda bodies are not
    extracted as their own Function nodes and are not treated as CALLS
    scope boundaries — calls inside a lambda are attributed to the
    enclosing method (matching Python's own lambda behavior, since a Python
    lambda isn't a `function_definition` node either); calls inside a local
    function ARE excluded from the enclosing method's CALLS (the walk stops
    descending at `local_function_statement`, same treatment as a nested
    `def`), they're simply not attributed anywhere.
  - Top-level statements (C# 9+ `Program.cs` style, code with no enclosing
    class) are not modeled — there's no enclosing Function/Class to attach
    CALLS to, and this is a rare style in mid-size/library code, which is
    what the golden-repo parity bar targets.
  - Cross-project (`.csproj` reference) resolution is out of scope per the
    Implementation Plan's table for this language.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_CS_LANGUAGE = Language(tscsharp.language())

# Node types whose bodies represent a class-shaped declaration.
_CLASS_LIKE = ("class_declaration", "struct_declaration", "record_declaration")
# Node types whose bodies represent a method-shaped declaration (Function).
_METHOD_LIKE = ("method_declaration", "constructor_declaration")
# Node types that open a new lexical scope a CALLS walk must not descend
# into (mirrors Python's stop-at-nested-def/class behavior).
_NESTED_SCOPE_TYPES = _CLASS_LIKE + _METHOD_LIKE + ("local_function_statement", "interface_declaration")


def _make_parser() -> Parser:
    return Parser(_CS_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _dotted_name(node: Node, source: bytes) -> str:
    """Render an identifier/qualified_name node as dotted text, e.g. `A.B.C`."""
    if node.type == "identifier":
        return _text(node, source)
    if node.type == "qualified_name":
        qualifier = node.child_by_field_name("qualifier")
        name = node.child_by_field_name("name")
        if qualifier is not None and name is not None:
            return f"{_dotted_name(qualifier, source)}.{_text(name, source)}"
    return _text(node, source)


def _base_name(node: Node, source: bytes) -> str:
    """Render one `base_list` entry as a simple/dotted name.

    `generic_name` (e.g. `IEnumerable<string>`) is reduced to its bare
    identifier (`IEnumerable`) — type arguments carry no useful graph
    identity here, same spirit as Python ignoring generic subscripts.
    """
    if node.type == "generic_name":
        ident = node.child_by_field_name("name") or (node.named_children[0] if node.named_children else None)
        if ident is not None:
            return _text(ident, source)
        return _text(node, source)
    if node.type in ("identifier", "qualified_name"):
        return _dotted_name(node, source)
    return _text(node, source)


def _extract_attribute_names(node: Node, source: bytes) -> list[str]:
    """Extract `[Attribute(...)]` names attached to a declaration — C#'s
    nearest equivalent to Python decorators. `attribute_list` nodes are
    children of the declaration node itself, preceding its keyword token.
    """
    names: list[str] = []
    for child in node.children:
        if child.type != "attribute_list":
            continue
        for attr in child.named_children:
            if attr.type != "attribute":
                continue
            name_node = attr.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node, source))
    return names


def _extract_base_list_names(class_like_node: Node, source: bytes) -> list[str]:
    """`base_list` is an unnamed-field child of class/struct/record
    declarations (tree-sitter-c-sharp gives it no field name), so it's found
    by type rather than `child_by_field_name`.
    """
    base_list = next((c for c in class_like_node.named_children if c.type == "base_list"), None)
    if base_list is None:
        return []
    return [_base_name(c, source) for c in base_list.named_children]


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a method/constructor body for invocation expressions and return
    the callee's simple name — the identifier a Function node is keyed on.

    - `Foo()` -> 'Foo'
    - `this.Foo()` / `obj.Foo()` -> 'Foo' (the member name) — no type info
      is available, so (like Python's extractor) this intentionally
      over-links same-named methods across classes rather than requiring
      full type resolution.
    - Chained/immediately-invoked calls (`GetHandler()()`) resolve to the
      outer call's own callee name by recursing into its `function` field.
    - Does not descend into nested class/method/local-function scopes —
      those are walked separately by the caller so calls are attributed to
      the correct enclosing scope, not hoisted to the outer method.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in _NESTED_SCOPE_TYPES:
            return  # nested scope — attributed separately by the caller
        if node.type == "invocation_expression":
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
    """Resolve an invocation expression's `function` field to a simple callee name."""
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "generic_name":
        # `Foo<T>()` — a direct generic method call. The name identifier is
        # the first named child (grammar gives it no field name); strip the
        # `<T>` so 'Foo<T>()' resolves to the same Function node as a
        # non-generic 'Foo()' would (no type-arg-aware overload resolution,
        # same name-based philosophy as everything else here).
        ident = func_node.child_by_field_name("name") or (
            func_node.named_children[0] if func_node.named_children else None
        )
        return _text(ident, source) if ident is not None else None
    if func_node.type == "member_access_expression":
        name = func_node.child_by_field_name("name")
        if name is not None:
            # `t.Annotation<JSchemaAnnotation>()` — the name field itself can
            # be a generic_name; recurse so the type args get stripped here too.
            if name.type == "generic_name":
                return _callee_simple_name(name, source)
            return _text(name, source)
    if func_node.type == "invocation_expression":
        # Chained/immediately-invoked call, e.g. `GetHandler()()` — resolve
        # to the outer call's own callee by recursing on its function field.
        inner_func = func_node.child_by_field_name("function")
        if inner_func is not None:
            return _callee_simple_name(inner_func, source)
    return None


def _clean_doc_comment_lines(comment_nodes: list[Node], source: bytes) -> str:
    """Strip the leading `///` (and one following space) off each XML doc
    comment line and join them back into one block of text.
    """
    lines = []
    for node in comment_nodes:
        raw = _text(node, source)
        stripped = raw[3:] if raw.startswith("///") else raw
        if stripped.startswith(" "):
            stripped = stripped[1:]
        lines.append(stripped)
    return "\n".join(lines).strip()


def _summary_from_xml_doc(full_text: str) -> str | None:
    """Pull the text inside a `<summary>...</summary>` tag out of a joined
    XML-doc-comment block, collapsing internal newlines/indentation to a
    single line. Returns None if there's no `<summary>` tag, so the caller
    can fall back to first-line-of-text behavior.
    """
    lower = full_text.lower()
    start_tag = lower.find("<summary>")
    if start_tag == -1:
        return None
    end_tag = lower.find("</summary>", start_tag)
    if end_tag == -1:
        return None
    inner = full_text[start_tag + len("<summary>") : end_tag]
    collapsed = " ".join(line.strip() for line in inner.splitlines() if line.strip())
    return collapsed or None


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """Compute a short summary line the same way Python's extractor does:
    prefer the `<summary>` tag's text; otherwise fall back to the first
    non-blank line of the raw doc-comment block. Hard-truncated for
    anything that doesn't produce a short line either way.
    """
    summary = _summary_from_xml_doc(full_text)
    if summary is None:
        first_para = full_text.split("\n\n", 1)[0].strip()
        summary = first_para.split("\n", 1)[0].strip()

    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


def _collect_doc_comment(container_children: list[Node], index: int) -> list[Node]:
    """Given a container's list of named children and the index of a
    class/method-like declaration in it, return the contiguous run of `///`
    comment nodes immediately preceding it (empty list if none / not
    contiguous / not XML-doc-style).
    """
    comments: list[Node] = []
    row = container_children[index].start_point[0]
    i = index - 1
    while i >= 0:
        node = container_children[i]
        if node.type != "comment":
            break
        text = node.text.decode("utf-8", errors="replace") if node.text else ""
        if not text.startswith("///"):
            break
        if node.end_point[0] + 1 != row:
            break  # not immediately adjacent (blank line or other code between)
        comments.append(node)
        row = node.start_point[0]
        i -= 1
    comments.reverse()
    return comments


def _using_target(dotted: str) -> str | None:
    """Reinterpret a `using`d namespace as a same-repo file-path guess.

    'MyApp.Services.Foo' -> 'MyApp/Services/Foo.cs'.

    This is a WEAKER heuristic than Python's dotted-import guess: a Python
    absolute import's dotted path is (by convention) the actual package/
    module file path, but a C# namespace has no required relationship to
    folder layout at all — `namespace MyApp.Services` can live in any
    file/folder a developer chooses, and the last namespace segment being a
    same-named .cs file is a common but unenforced convention (e.g. a
    `Foo.cs` file with `namespace MyApp.Services` doesn't have to sit under
    `MyApp/Services/`). Emitting this guess is still safe: like every other
    guessed target in this codebase, `upsert_relationship` only MATCHes an
    edge into existence when both endpoints already exist as real nodes, so
    a wrong guess just never produces an edge rather than a false one.
    Returns None for a single-segment namespace ('System') — nothing more
    specific to guess.
    """
    if "." not in dotted:
        return None
    return dotted.replace(".", "/") + ".cs"


def _extract_using_targets(root: Node, source: bytes) -> list[str]:
    """Walk the whole tree for `using_directive` nodes (old-style C# allows
    `using` inside a namespace body, not just at file scope) and return the
    list of IMPORTS target names — both the bare dotted namespace (kept for
    compatibility, same as Python) and the folder-guess file path when the
    namespace is multi-segment.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type == "using_directive":
            # Find the qualified_name/identifier child that names the
            # namespace — skip the alias's own `name` field (for
            # `using Alias = X.Y;`) and the `static`/`global` keyword leaves.
            alias_name = node.child_by_field_name("name")
            alias_span = (alias_name.start_byte, alias_name.end_byte) if alias_name else None
            for child in node.named_children:
                if child.type not in ("identifier", "qualified_name"):
                    continue
                if alias_span is not None and (child.start_byte, child.end_byte) == alias_span:
                    continue
                dotted = _dotted_name(child, source)
                targets.append(dotted)
                file_guess = _using_target(dotted)
                if file_guess:
                    targets.append(file_guess)
            return  # using_directive has no nested using_directives
        for c in node.children:
            walk(c)

    walk(root)
    return targets


def extract_csharp_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a C# file and extract nodes and relationships.

    Args:
        source_code: The C# source code as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, forward-slashed (e.g. 'MyApp/Services/Foo.cs').
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (ERROR
        nodes rather than a raised exception), so partial results are
        extracted and the error is logged rather than the whole file being
        skipped.
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

    for target in _extract_using_targets(root, source_bytes):
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

    def visit_block(container: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit a container's named children: `compilation_unit` (file
        scope), a namespace's `declaration_list`, or a class/struct/record's
        `declaration_list`.
        """
        children = container.named_children
        for idx, node in enumerate(children):
            if node.type == "comment":
                continue
            if node.type in ("namespace_declaration",):
                # Block-form namespace — transparent container: its members
                # belong to the same parent (Module, or an enclosing class
                # for the rare nested-namespace-in-class case, which C#
                # doesn't actually allow, but this stays correct either way).
                body = node.child_by_field_name("body")
                if body is not None:
                    visit_block(body, parent_name, parent_label)
            elif node.type == "file_scoped_namespace_declaration":
                # No body field — everything after it in this same container
                # is already a sibling that visit_block will reach normally.
                continue
            elif node.type in _CLASS_LIKE:
                _visit_class(node, children, idx, parent_name, parent_label)
            elif node.type in _METHOD_LIKE:
                _visit_method(node, children, idx, parent_name, parent_label)
            elif node.type == "interface_declaration":
                continue  # scope cut — see module docstring
            else:
                # Other statement (field/property/top-level code) — attribute
                # any invocation expressions in it to the enclosing scope.
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(parent_name if parent_name else file_path, parent_label, target, caller_class)

    def _visit_class(
        node: Node, container_children: list[Node], index: int, parent_name: str | None, parent_label: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        decorators = _extract_attribute_names(node, source_bytes)
        base_names = _extract_base_list_names(node, source_bytes)

        class_properties: dict = {
            "type": "class",
            "decorators": decorators,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc_comments = _collect_doc_comment(container_children, index)
        if doc_comments:
            full_text = _clean_doc_comment_lines(doc_comments, source_bytes)
            if full_text:
                class_properties["description"] = _docstring_summary(full_text)
                class_properties["docstring_full"] = full_text

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
        for base_name in base_names:
            result.relationships.append(
                GraphRelationship(
                    from_label="Class",
                    from_name=class_name,
                    rel_type="EXTENDS",
                    to_label="Class",
                    to_name=base_name,
                    repo_id=repo_id,
                )
            )

        body = node.child_by_field_name("body")
        if body is not None:
            visit_block(body, class_name, "Class")

    def _visit_method(
        node: Node, container_children: list[Node], index: int, parent_name: str | None, parent_label: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        decorators = _extract_attribute_names(node, source_bytes)

        func_properties: dict = {
            "type": "function",
            "decorators": decorators,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc_comments = _collect_doc_comment(container_children, index)
        if doc_comments:
            full_text = _clean_doc_comment_lines(doc_comments, source_bytes)
            if full_text:
                func_properties["description"] = _docstring_summary(full_text)
                func_properties["docstring_full"] = full_text

        result.nodes.append(
            GraphNode(label="Function", repo_id=repo_id, name=func_name, properties=func_properties)
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

        body = node.child_by_field_name("body")  # block or arrow_expression_clause; None for abstract/interface
        if body is not None:
            caller_class = parent_name if parent_label == "Class" else None
            for target in _extract_call_targets(body, source_bytes):
                _emit_call(func_name, "Function", target, caller_class)

    visit_block(root, None, "Module")

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a C# file and upsert results into the graph.

    Thin wrapper matching `python/extractor.py`'s `index_file` signature and
    behavior: calls `extract_csharp_file()` then upserts each node and
    relationship via the GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the C# file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes) — same rationale as the Python extractor: prevents
            same-named files in different directories from colliding into
            one Module node. Falls back to the bare filename when omitted.

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

    result = extract_csharp_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
